"""
Workflow Service.

Handles workflow rule evaluation and action execution.
"""

import builtins
import ipaddress
import logging
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.models.email_profile import EmailModule
from app.models.finance.automation import (
    ActionType,
    ExecutionStatus,
    TriggerEvent,
    WorkflowEntityType,
    WorkflowExecution,
    WorkflowRule,
)

# The SUBMODULE is named directly rather than reached through
# `app.services.finance.automation`, whose `__init__` imports this module: a
# `from <package> import <submodule>` there resolves an attribute on a
# half-initialised package.
from app.services.finance.automation.webhook_policy import (
    effective_policy,
    organization_in_scope,
)

logger = logging.getLogger(__name__)

_HEADER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")
_ALLOWED_WEBHOOK_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def _email_module_for_entity(entity_type: str | None) -> EmailModule:
    if not entity_type:
        return EmailModule.FINANCE
    entity = entity_type.upper()
    if entity in {
        "EXPENSE",
        "CASH_ADVANCE",
    }:
        return EmailModule.EXPENSE
    if entity in {
        "EMPLOYEE",
        "LEAVE_REQUEST",
        "DISCIPLINARY_CASE",
        "PERFORMANCE_APPRAISAL",
        "LOAN",
        "RECRUITMENT",
        "PAYROLL_RUN",
        "PAYROLL_ENTRY",
        "SALARY_SLIP",
    }:
        return EmailModule.PEOPLE_PAYROLL
    if entity in {
        "MATERIAL_REQUEST",
        "FLEET_VEHICLE",
        "FLEET_RESERVATION",
        "FLEET_MAINTENANCE",
        "FLEET_INCIDENT",
    }:
        return EmailModule.INVENTORY_FLEET
    # Default: finance workflows
    return EmailModule.FINANCE


# `_db_setting`, `_coerce_csv`, `_coerce_bool`, `_allowed_webhook_hosts`,
# `_allowed_webhook_domains`, `_allow_insecure_webhooks`,
# `_allow_localhost_webhooks` and `_webhook_timeout` all used to live here.
# They are gone rather than merely unused.
#
# Each of them read one of the four SSRF ceiling keys — or the unbounded
# timeout — directly, through `automation_settings.get_by_key` at whatever
# scope its caller happened to be in. Now that those keys are PLATFORM-owned,
# that read resolves the platform row ALONE: an organization's own, NARROWER
# row no longer applies to it. Leaving these readers beside a policy object
# that composes both layers would therefore not merely duplicate the answer,
# it would give the WRONG one — the ceiling where the conjunction is meant to
# be — and would do it silently, in the direction of MORE reachable hosts.
#
# There is exactly one reader of the ceiling keys left in the application,
# `webhook_policy.read_platform_webhook_ceiling`, and exactly one composition,
# `webhook_policy.effective_policy`. The bounded timeout is
# `effective_policy(db, org).timeout_seconds`.


def _is_private_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


# `webhook_allowlist_configured` and `has_active_webhook_actions` used to live
# here, and both are gone rather than merely unused.
#
# `webhook_allowlist_configured` answered "is the allowlist configured?" from
# whatever scope its caller happened to be in. Its one caller was the startup
# check, which now asks the question at PLATFORM scope explicitly:
# `webhook_policy.read_platform_webhook_ceiling(db).is_configured`.
#
# `has_active_webhook_actions` ran `select(func.count()).select_from(
# WorkflowRule)` — a shape `app.db.org_listener._add_org_filter` resolves no
# entity from, so it counted across every organization no matter what session
# it was handed, on a table with no RLS policy to catch it. Leaving it here
# unused would leave that shape available to the next caller. Its replacement,
# `webhook_policy.any_tenant_has_an_active_webhook_rule`, enumerates the
# catalogue definer and asks each organization in its own session.


def _validate_webhook_target(
    url: str,
    db: Session | None = None,
    *,
    allow_localhost: bool | None = None,
    organization_id: UUID | None = None,
) -> tuple[bool, str | None]:
    """The ONE host/scheme/loopback gate for both outbound webhook channels.

    Every decision below comes from a single
    :func:`~app.services.finance.automation.webhook_policy.effective_policy`
    object — the CONJUNCTION of the platform ceiling and this organization's
    narrowing — resolved once per call. It is deliberately not four separate
    settings reads: four reads can disagree with each other mid-call, and each
    of them was previously a place where the ceiling could be consulted without
    the narrowing.

    ``allow_localhost`` is a caller's NARROWING, and is now ``AND``-ed with the
    policy rather than replacing it. Under the retired code
    ``allow_localhost=True`` overrode the settings outright — an in-process
    argument that could widen the SSRF policy of a deployment that had turned
    loopback off. ``None`` still means "no opinion, use the policy".
    """
    if not url or not isinstance(url, str):
        return False, "Webhook URL is required"

    parsed = urlsplit(url)
    if parsed.scheme not in {"https", "http"}:
        return False, "Webhook URL must use http or https"
    if parsed.username or parsed.password:
        return False, "Webhook URL must not include credentials"
    if not parsed.hostname:
        return False, "Webhook URL must include a host"

    host = parsed.hostname
    policy = effective_policy(db, organization_in_scope(db, organization_id))

    if not policy.permits_host(host):
        return False, "Webhook host is not in the allowlist"

    allow_localhost_webhooks = (
        policy.allow_localhost
        if allow_localhost is None
        else (allow_localhost and policy.allow_localhost)
    )
    require_https = parsed.scheme == "http" and not policy.allow_insecure
    loopback_host = False

    try:
        ip_value = ipaddress.ip_address(host)
        if ip_value.is_loopback:
            loopback_host = True
        if _is_private_address(ip_value):
            if not (allow_localhost_webhooks and ip_value.is_loopback):
                return False, "Webhook target is not allowed"
    except ValueError:
        try:
            addr_info = socket.getaddrinfo(host, None)
        except socket.gaierror:
            return False, "Webhook host could not be resolved"
        for _, _, _, _, sockaddr in addr_info:
            ip_str = sockaddr[0]
            ip_value = ipaddress.ip_address(ip_str)
            if ip_value.is_loopback:
                loopback_host = True
            if _is_private_address(ip_value):
                if not (allow_localhost_webhooks and ip_value.is_loopback):
                    return False, "Webhook target is not allowed"

    if require_https and not (allow_localhost_webhooks and loopback_host):
        return False, "Webhook URL must use https"

    return True, None


@dataclass
class WorkflowRuleInput:
    """Input for creating a workflow rule."""

    rule_name: str
    entity_type: WorkflowEntityType
    trigger_event: TriggerEvent
    action_type: ActionType
    trigger_conditions: dict[str, Any]
    action_config: dict[str, Any]
    description: str | None = None
    priority: int = 100
    stop_on_match: bool = False
    execute_async: bool = True
    cooldown_seconds: int | None = None
    schedule_config: dict[str, Any] | None = None


@dataclass
class TriggerContext:
    """Context for evaluating workflow triggers."""

    entity_type: str
    entity_id: UUID
    event: TriggerEvent
    organization_id: UUID | None = None
    old_values: dict[str, Any] | None = None
    new_values: dict[str, Any] | None = None
    changed_fields: list[str] | None = None
    user_id: UUID | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize for Celery task arguments."""
        return {
            "entity_type": self.entity_type,
            "entity_id": str(self.entity_id),
            "event": self.event.value,
            "organization_id": str(self.organization_id)
            if self.organization_id
            else None,
            "old_values": self.old_values,
            "new_values": self.new_values,
            "changed_fields": self.changed_fields,
            "user_id": str(self.user_id) if self.user_id else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TriggerContext":
        """Deserialize from Celery task arguments."""
        return cls(
            entity_type=data["entity_type"],
            entity_id=UUID(data["entity_id"]),
            event=TriggerEvent(data["event"]),
            organization_id=UUID(data["organization_id"])
            if data.get("organization_id")
            else None,
            old_values=data.get("old_values"),
            new_values=data.get("new_values"),
            changed_fields=data.get("changed_fields"),
            user_id=UUID(data["user_id"]) if data.get("user_id") else None,
        )


@dataclass
class ActionResult:
    """Result of executing a workflow action."""

    success: bool
    result: dict[str, Any] | None = None
    error_message: str | None = None


class WorkflowService:
    """Service for managing and executing workflow rules."""

    # Production hardening caps
    MAX_RULES_PER_EVENT = 10
    MAX_EXECUTIONS_PER_MINUTE = 50

    def create_rule(
        self,
        db: Session,
        organization_id: UUID,
        input_data: WorkflowRuleInput,
        created_by: UUID,
    ) -> WorkflowRule:
        """Create a new workflow rule."""
        # Check for duplicate name
        existing = db.execute(
            select(WorkflowRule).where(
                and_(
                    WorkflowRule.organization_id == organization_id,
                    WorkflowRule.rule_name == input_data.rule_name,
                )
            )
        ).scalar_one_or_none()

        if existing:
            raise HTTPException(
                status_code=400,
                detail=f"Rule with name '{input_data.rule_name}' already exists",
            )

        rule = WorkflowRule(
            organization_id=organization_id,
            rule_name=input_data.rule_name,
            description=input_data.description,
            entity_type=input_data.entity_type,
            trigger_event=input_data.trigger_event,
            trigger_conditions=input_data.trigger_conditions,
            action_type=input_data.action_type,
            action_config=input_data.action_config,
            priority=input_data.priority,
            stop_on_match=input_data.stop_on_match,
            execute_async=input_data.execute_async,
            cooldown_seconds=input_data.cooldown_seconds,
            schedule_config=input_data.schedule_config,
            created_by=created_by,
        )

        db.add(rule)
        db.flush()
        return rule

    def get(
        self,
        db: Session,
        rule_id: UUID,
        organization_id: UUID | None = None,
    ) -> WorkflowRule | None:
        """Get a rule by ID."""
        rule = db.get(WorkflowRule, rule_id)
        if not rule:
            return None
        if organization_id is not None and rule.organization_id != organization_id:
            return None
        return rule

    def list(
        self,
        db: Session,
        organization_id: UUID,
        entity_type: WorkflowEntityType | None = None,
        trigger_event: TriggerEvent | None = None,
        is_active: bool | None = True,
        limit: int = 100,
        offset: int = 0,
    ) -> list[WorkflowRule]:
        """List workflow rules."""
        query = select(WorkflowRule).where(
            WorkflowRule.organization_id == organization_id
        )

        if entity_type:
            query = query.where(WorkflowRule.entity_type == entity_type)
        if trigger_event:
            query = query.where(WorkflowRule.trigger_event == trigger_event)
        if is_active is not None:
            query = query.where(WorkflowRule.is_active == is_active)

        query = query.order_by(WorkflowRule.priority, WorkflowRule.created_at.desc())
        query = query.offset(offset).limit(limit)

        return list(db.execute(query).scalars().all())

    def get_matching_rules(
        self,
        db: Session,
        organization_id: UUID,
        context: TriggerContext,
    ) -> builtins.list[WorkflowRule]:
        """Get all rules that match a trigger context."""
        # Convert string entity type to enum
        try:
            entity_type = WorkflowEntityType(context.entity_type)
        except ValueError:
            return []

        rules = self.list(
            db,
            organization_id,
            entity_type=entity_type,
            trigger_event=context.event,
            is_active=True,
        )

        matching = []
        for rule in rules:
            if self._evaluate_conditions(rule.trigger_conditions, context):
                matching.append(rule)

        return matching

    def _evaluate_conditions(
        self,
        conditions: dict[str, Any],
        context: TriggerContext,
    ) -> bool:
        """Evaluate if conditions are met for a trigger context.

        Supports both flat conditions (original format) and compound
        conditions with nested AND/OR groups.
        """
        if not conditions:
            return True

        # Compound condition node: {"operator": "AND"/"OR", "groups": [...]}
        if "operator" in conditions and "groups" in conditions:
            return self._evaluate_condition_node(conditions, context)

        return self._evaluate_flat_conditions(conditions, context)

    def _evaluate_condition_node(
        self,
        node: dict[str, Any],
        context: TriggerContext,
    ) -> bool:
        """Recursively evaluate AND/OR condition groups.

        Format::

            {
                "operator": "AND",  # or "OR"
                "groups": [
                    {"operator": "OR", "conditions": [...]},
                    {"field": "status", "operator": "equals", "value": "OVERDUE"}
                ]
            }

        Leaf nodes that have "conditions" key contain a list of flat
        condition dicts. Leaf nodes that have "field" key are individual
        comparisons.
        """
        op = node.get("operator", "AND").upper()
        groups = node.get("groups", [])
        conditions_list = node.get("conditions", [])

        # If this node has inline conditions (leaf group)
        if conditions_list:
            results = []
            for cond in conditions_list:
                if "field" in cond:
                    values = context.new_values or {}
                    field_value = values.get(cond["field"])
                    cond_op = cond.get("operator", "equals")
                    expected = cond.get("value")
                    results.append(self._compare_values(field_value, cond_op, expected))
                elif "operator" in cond and "groups" in cond:
                    results.append(self._evaluate_condition_node(cond, context))
            if op == "OR":
                return any(results) if results else True
            return all(results) if results else True

        # If this node has sub-groups
        if groups:
            results = []
            for group in groups:
                if isinstance(group, dict):
                    if "operator" in group and (
                        "groups" in group or "conditions" in group
                    ):
                        results.append(self._evaluate_condition_node(group, context))
                    elif "field" in group:
                        values = context.new_values or {}
                        field_value = values.get(group["field"])
                        cond_op = group.get("operator", "equals")
                        expected = group.get("value")
                        results.append(
                            self._compare_values(field_value, cond_op, expected)
                        )
                    else:
                        # Treat as flat conditions dict
                        results.append(self._evaluate_flat_conditions(group, context))
            if op == "OR":
                return any(results) if results else True
            return all(results) if results else True

        return True

    def _evaluate_flat_conditions(
        self,
        conditions: dict[str, Any],
        context: TriggerContext,
    ) -> bool:
        """Evaluate flat (non-compound) conditions — original format."""
        values = context.new_values or {}

        # Field comparisons
        field_conditions = conditions.get("fields", {})
        for field, condition in field_conditions.items():
            field_value = values.get(field)

            if isinstance(condition, dict):
                operator = condition.get("operator", "equals")
                expected = condition.get("value")

                if not self._compare_values(field_value, operator, expected):
                    return False
            else:
                # Simple equality check
                if field_value != condition:
                    return False

        # Status transition check
        status_from = conditions.get("status_from")
        status_to = conditions.get("status_to")
        if status_from or status_to:
            old_status = (
                context.old_values.get("status") if context.old_values else None
            )
            new_status = values.get("status")

            if status_from and old_status != status_from:
                return False
            if status_to and new_status != status_to:
                return False

        # Changed fields check
        required_changes = conditions.get("changed_fields", [])
        if required_changes and context.changed_fields:
            if not any(f in context.changed_fields for f in required_changes):
                return False

        # Amount threshold check
        amount_threshold = conditions.get("amount_threshold")
        if amount_threshold:
            amount_field = amount_threshold.get("field", "total_amount")
            threshold_value = Decimal(str(amount_threshold.get("value", 0)))
            operator = amount_threshold.get("operator", "greater_than")

            amount_value = values.get(amount_field)
            if amount_value is not None:
                amount_value = Decimal(str(amount_value))
                if not self._compare_values(amount_value, operator, threshold_value):
                    return False

        return True

    def _compare_values(self, value: Any, operator: str, expected: Any) -> bool:
        """Compare values using the specified operator."""
        if value is None:
            return operator == "is_null" or (operator == "equals" and expected is None)

        if operator == "equals":
            return bool(value == expected)
        elif operator == "not_equals":
            return bool(value != expected)
        elif operator == "greater_than":
            return bool(value > expected)
        elif operator == "greater_than_or_equal":
            return bool(value >= expected)
        elif operator == "less_than":
            return bool(value < expected)
        elif operator == "less_than_or_equal":
            return bool(value <= expected)
        elif operator == "contains":
            return str(expected).lower() in str(value).lower()
        elif operator == "starts_with":
            return str(value).lower().startswith(str(expected).lower())
        elif operator == "ends_with":
            return str(value).lower().endswith(str(expected).lower())
        elif operator == "matches":
            return bool(re.match(str(expected), str(value), re.IGNORECASE))
        elif operator == "in":
            return bool(value in expected)
        elif operator == "not_in":
            return bool(value not in expected)
        elif operator == "is_null":
            return bool(value is None)
        elif operator == "is_not_null":
            return bool(value is not None)

        return False

    def execute_action(
        self,
        db: Session,
        rule: WorkflowRule,
        context: TriggerContext,
        chain_depth: int = 0,
    ) -> WorkflowExecution:
        """Execute a workflow rule action."""
        execution = WorkflowExecution(
            rule_id=rule.rule_id,
            entity_type=context.entity_type,
            entity_id=context.entity_id,
            trigger_event=context.event.value,
            trigger_data={
                "old_values": context.old_values,
                "new_values": context.new_values,
                "changed_fields": context.changed_fields,
            },
            triggered_by=context.user_id,
            status=ExecutionStatus.RUNNING,
            started_at=datetime.utcnow(),
        )
        db.add(execution)
        db.flush()

        try:
            result = self._run_action(db, rule, context, chain_depth=chain_depth)

            execution.status = (
                ExecutionStatus.SUCCESS if result.success else ExecutionStatus.FAILED
            )
            execution.result = result.result
            execution.error_message = result.error_message
            execution.completed_at = datetime.utcnow()

            if execution.started_at:
                duration = (
                    execution.completed_at - execution.started_at
                ).total_seconds() * 1000
                execution.duration_ms = int(duration)

            # Update rule statistics
            rule.execution_count += 1
            if result.success:
                rule.success_count += 1
            else:
                rule.failure_count += 1
            rule.last_executed_at = datetime.utcnow()

        except Exception as e:
            logger.exception(
                "Error executing workflow action for rule %s", rule.rule_id
            )
            execution.status = ExecutionStatus.FAILED
            execution.error_message = str(e)
            execution.completed_at = datetime.utcnow()
            rule.execution_count += 1
            rule.failure_count += 1
            rule.last_executed_at = datetime.utcnow()

        db.flush()
        return execution

    def _run_action(
        self,
        db: Session,
        rule: WorkflowRule,
        context: TriggerContext,
        chain_depth: int = 0,
    ) -> ActionResult:
        """Run the action for a workflow rule."""
        config = rule.action_config

        if rule.action_type == ActionType.SEND_EMAIL:
            return self._action_send_email(db, config, context)
        elif rule.action_type == ActionType.SEND_NOTIFICATION:
            return self._action_send_notification(db, config, context)
        elif rule.action_type == ActionType.VALIDATE:
            return self._action_validate(db, config, context)
        elif rule.action_type == ActionType.UPDATE_FIELD:
            return self._action_update_field(db, config, context)
        elif rule.action_type == ActionType.CREATE_TASK:
            return self._action_create_task(db, config, context)
        elif rule.action_type == ActionType.WEBHOOK:
            return self._action_webhook(db, config, context)
        elif rule.action_type == ActionType.BLOCK:
            return self._action_block(db, config, context)
        elif rule.action_type == ActionType.TRIGGER_RULE:
            return self._action_trigger_rule(
                db,
                config,
                context,
                _depth=chain_depth,
            )
        else:
            return ActionResult(
                success=False,
                error_message=f"Unknown action type: {rule.action_type}",
            )

    def _action_send_email(
        self,
        db: Session,
        config: dict[str, Any],
        context: TriggerContext,
    ) -> ActionResult:
        """Send an email action with Jinja2 template rendering."""
        from app.services.email import send_email
        from app.services.finance.automation.template_renderer import render_template

        try:
            recipients = config.get("recipients", [])
            subject_tpl = config.get("subject", "Workflow Notification")
            body_html_tpl = config.get("body_html", "")
            body_text_tpl = config.get("body_text")

            subject = render_template(
                subject_tpl,
                entity_type=context.entity_type,
                entity_id=context.entity_id,
                old_values=context.old_values,
                new_values=context.new_values,
                user_id=context.user_id,
            )
            body_html = render_template(
                body_html_tpl,
                entity_type=context.entity_type,
                entity_id=context.entity_id,
                old_values=context.old_values,
                new_values=context.new_values,
                user_id=context.user_id,
            )
            body_text = (
                render_template(
                    body_text_tpl,
                    entity_type=context.entity_type,
                    entity_id=context.entity_id,
                    old_values=context.old_values,
                    new_values=context.new_values,
                    user_id=context.user_id,
                )
                if body_text_tpl
                else None
            )

            sent_to: list[str] = []
            module = _email_module_for_entity(context.entity_type)
            for recipient in recipients:
                if send_email(
                    db,
                    recipient,
                    subject,
                    body_html,
                    body_text,
                    module=module,
                    organization_id=context.organization_id,
                ):
                    sent_to.append(recipient)

            return ActionResult(
                success=len(sent_to) > 0,
                result={"sent_to": sent_to},
                error_message="No emails sent" if not sent_to else None,
            )

        except Exception as e:
            return ActionResult(
                success=False,
                error_message=str(e),
            )

    def _action_send_notification(
        self,
        db: Session,
        config: dict[str, Any],
        context: TriggerContext,
    ) -> ActionResult:
        """Send in-app notification action."""
        from app.models.notification import (
            EntityType,
            NotificationChannel,
            NotificationType,
        )
        from app.services.finance.automation.template_renderer import render_template
        from app.services.notification import NotificationService

        try:
            recipient_ids = config.get("recipient_ids", [])
            if not recipient_ids:
                return ActionResult(
                    success=False,
                    error_message="No recipient_ids specified in action config",
                )

            title_template = config.get("title", "Workflow Notification")
            message_template = config.get("message", "")
            action_url = config.get("action_url")
            channel_str = config.get("channel", "IN_APP")

            title = render_template(
                title_template,
                entity_type=context.entity_type,
                entity_id=context.entity_id,
                old_values=context.old_values,
                new_values=context.new_values,
                user_id=context.user_id,
            )
            message = render_template(
                message_template,
                entity_type=context.entity_type,
                entity_id=context.entity_id,
                old_values=context.old_values,
                new_values=context.new_values,
                user_id=context.user_id,
            )

            try:
                channel = NotificationChannel(channel_str)
            except ValueError:
                channel = NotificationChannel.IN_APP

            notification_service = NotificationService()
            sent_to: list[str] = []

            org_id = context.organization_id
            if not org_id:
                return ActionResult(
                    success=False,
                    error_message="organization_id missing from trigger context",
                )

            for rid in recipient_ids:
                try:
                    recipient_uuid = UUID(str(rid))
                    notification_service.create(
                        db,
                        organization_id=org_id,
                        recipient_id=recipient_uuid,
                        entity_type=EntityType.SYSTEM,
                        entity_id=context.entity_id,
                        notification_type=NotificationType.ALERT,
                        title=title,
                        message=message,
                        channel=channel,
                        action_url=action_url,
                        actor_id=context.user_id,
                    )
                    sent_to.append(str(recipient_uuid))
                except Exception as e:
                    logger.warning("Failed to send notification to %s: %s", rid, e)

            return ActionResult(
                success=len(sent_to) > 0,
                result={"sent_to": sent_to},
                error_message="No notifications sent" if not sent_to else None,
            )

        except Exception as e:
            return ActionResult(success=False, error_message=str(e))

    def _action_validate(
        self,
        db: Session,
        config: dict[str, Any],
        context: TriggerContext,
    ) -> ActionResult:
        """Validate action - check conditions and potentially block."""
        validation_rules = config.get("rules", [])
        values = context.new_values or {}

        errors = []
        for rule in validation_rules:
            field = rule.get("field")
            condition = rule.get("condition")
            message = rule.get("message", f"Validation failed for {field}")

            field_value = values.get(field)
            operator = condition.get("operator", "equals")
            expected = condition.get("value")

            if not self._compare_values(field_value, operator, expected):
                errors.append(message)

        if errors:
            return ActionResult(
                success=False,
                result={"validation_errors": errors},
                error_message="; ".join(errors),
            )

        return ActionResult(
            success=True,
            result={"validated": True},
        )

    def _action_update_field(
        self,
        db: Session,
        config: dict[str, Any],
        context: TriggerContext,
    ) -> ActionResult:
        """Update field action — loads the entity and sets a field value."""
        field_name = config.get("field")
        value = config.get("value")

        if not field_name:
            return ActionResult(
                success=False,
                error_message="No 'field' specified in action config",
            )

        try:
            from app.services.finance.automation.entity_registry import (
                field_authority_owner,
                resolve_entity,
            )

            entity = resolve_entity(
                db,
                entity_type=context.entity_type,
                entity_id=context.entity_id,
            )
            if entity is None:
                return ActionResult(
                    success=False,
                    error_message=f"Entity {context.entity_type}:{context.entity_id} not found",
                )

            if not hasattr(entity, field_name):
                return ActionResult(
                    success=False,
                    error_message=f"Entity has no field '{field_name}'",
                )

            owner = field_authority_owner(context.entity_type, field_name)
            if owner is not None:
                return ActionResult(
                    success=False,
                    error_message=(
                        f"'{field_name}' on {context.entity_type} is owned by "
                        f"{owner}; a workflow rule may not write it"
                    ),
                )

            setattr(entity, field_name, value)
            db.flush()

            return ActionResult(
                success=True,
                result={"updated_field": field_name, "new_value": value},
            )
        except ImportError:
            # entity_registry not yet available — graceful degradation
            return ActionResult(
                success=True,
                result={
                    "updated_field": field_name,
                    "new_value": value,
                    "note": "entity_registry not available, field not persisted",
                },
            )
        except Exception as e:
            return ActionResult(success=False, error_message=str(e))

    def _action_create_task(
        self,
        db: Session,
        config: dict[str, Any],
        context: TriggerContext,
    ) -> ActionResult:
        """Create a project management task from workflow trigger."""
        from app.services.finance.automation.template_renderer import render_template

        try:
            from app.models.pm.task import Task, TaskPriority, TaskStatus

            title_template = config.get("title", "Workflow Task")
            description_template = config.get("description", "")
            project_id = config.get("project_id")
            assignee_id = config.get("assignee_id")
            priority = config.get("priority", "MEDIUM")

            if not project_id:
                return ActionResult(
                    success=False,
                    error_message="project_id is required in action config",
                )

            title = render_template(
                title_template,
                entity_type=context.entity_type,
                entity_id=context.entity_id,
                old_values=context.old_values,
                new_values=context.new_values,
                user_id=context.user_id,
            )
            description = render_template(
                description_template,
                entity_type=context.entity_type,
                entity_id=context.entity_id,
                old_values=context.old_values,
                new_values=context.new_values,
                user_id=context.user_id,
            )

            org_id = context.organization_id
            if not org_id:
                return ActionResult(
                    success=False,
                    error_message="organization_id missing from trigger context",
                )

            try:
                task_priority = TaskPriority(priority)
            except ValueError:
                task_priority = TaskPriority.MEDIUM

            # Generate a unique task code from the entity
            import uuid as _uuid_mod

            task_code = f"WF-{_uuid_mod.uuid4().hex[:8].upper()}"

            task = Task(
                organization_id=org_id,
                project_id=UUID(project_id),
                task_code=task_code,
                task_name=title,
                description=description,
                status=TaskStatus.OPEN,
                priority=task_priority,
                assigned_to_id=UUID(assignee_id) if assignee_id else None,
                created_by_id=context.user_id,
            )
            db.add(task)
            db.flush()

            return ActionResult(
                success=True,
                result={
                    "task_created": True,
                    "task_id": str(task.task_id),
                    "task_code": task_code,
                    "task_name": title,
                },
            )
        except ImportError:
            return ActionResult(
                success=False,
                error_message="PM Task model not available",
            )
        except Exception as e:
            return ActionResult(success=False, error_message=str(e))

    def _action_webhook(
        self,
        db: Session,
        config: dict[str, Any],
        context: TriggerContext,
    ) -> ActionResult:
        """Webhook action."""
        import httpx

        url_value = config.get("url")
        url = "" if url_value is None else str(url_value)
        # `context.organization_id` is passed EXPLICITLY rather than left to the
        # session's ambient value. Channel 1 knows which organization it is
        # acting for, and a stated argument cannot be lost by a caller that
        # forgot to prime the session.
        is_valid, error_message = _validate_webhook_target(
            url, db, organization_id=context.organization_id
        )
        if not is_valid:
            return ActionResult(success=False, error_message=error_message)

        method = str(config.get("method", "POST")).upper()
        if method not in _ALLOWED_WEBHOOK_METHODS:
            return ActionResult(
                success=False,
                error_message=f"Webhook method '{method}' is not allowed",
            )

        raw_headers = config.get("headers", {})
        headers: dict[str, str] = {}
        if isinstance(raw_headers, dict):
            for key, value in raw_headers.items():
                key_str = str(key)
                if not _HEADER_NAME_PATTERN.match(key_str):
                    continue
                if key_str.lower() == "host":
                    continue
                value_str = str(value)
                if "\n" in value_str or "\r" in value_str:
                    continue
                headers[key_str] = value_str

        try:
            payload = {
                "entity_type": context.entity_type,
                "entity_id": str(context.entity_id),
                "event": context.event.value,
                "data": context.new_values,
            }

            # Timeout channel 1 of 2. The value an organization may set
            # (`webhook_timeout_seconds`) is bounded HERE, at the point the
            # request is made, by the platform ceiling
            # `webhook_max_timeout_seconds`. Clamping only where the setting is
            # read would leave the second channel — a service hook's
            # `handler_config["timeout_seconds"]`, clamped in
            # `app/services/hooks/registry.py` — free to exceed the maximum, and
            # a ceiling one channel honours is not a ceiling.
            #
            # Only the timeout is taken from the policy module in this change;
            # the allowlist reads above are cut over separately.
            policy = effective_policy(db, context.organization_id)
            timeout = httpx.Timeout(policy.timeout_seconds, connect=5.0)
            with httpx.Client(timeout=timeout, follow_redirects=False) as client:
                response = client.request(
                    method=method,
                    url=url,
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()

            return ActionResult(
                success=True,
                result={
                    "status_code": response.status_code,
                    "content_length": response.headers.get("content-length"),
                },
            )

        except Exception as e:
            return ActionResult(
                success=False,
                error_message=str(e),
            )

    def _action_block(
        self,
        db: Session,
        config: dict[str, Any],
        context: TriggerContext,
    ) -> ActionResult:
        """Block action - prevents the operation."""
        message = config.get("message", "Operation blocked by workflow rule")
        return ActionResult(
            success=False,
            result={"blocked": True},
            error_message=message,
        )

    # Maximum depth for rule chaining to prevent infinite recursion
    _MAX_CHAIN_DEPTH = 5

    def _action_trigger_rule(
        self,
        db: Session,
        config: dict[str, Any],
        context: TriggerContext,
        _depth: int = 0,
    ) -> ActionResult:
        """Trigger another workflow rule (rule chaining).

        Config:
            rule_id: UUID string of the rule to trigger.
        """
        if _depth >= self._MAX_CHAIN_DEPTH:
            return ActionResult(
                success=False,
                error_message=f"Rule chain depth limit ({self._MAX_CHAIN_DEPTH}) exceeded",
            )

        target_rule_id = config.get("rule_id")
        if not target_rule_id:
            return ActionResult(
                success=False,
                error_message="No rule_id specified in TRIGGER_RULE action config",
            )

        try:
            target_rule = db.get(WorkflowRule, UUID(target_rule_id))
            if not target_rule:
                return ActionResult(
                    success=False,
                    error_message=f"Target rule {target_rule_id} not found",
                )

            if not target_rule.is_active:
                return ActionResult(
                    success=False,
                    error_message=f"Target rule {target_rule_id} is not active",
                )

            if context.event != target_rule.trigger_event:
                return ActionResult(
                    success=False,
                    error_message=(
                        "Target rule trigger_event does not match context event"
                    ),
                )

            if not self._evaluate_conditions(target_rule.trigger_conditions, context):
                return ActionResult(
                    success=False,
                    error_message="Target rule conditions do not match trigger context",
                )

            if self._check_entity_rate_limit(db, context.entity_id):
                return ActionResult(
                    success=False,
                    error_message="Entity rate limit exceeded for chained rule",
                )

            if self._is_throttled(db, target_rule, context.entity_id):
                return ActionResult(
                    success=False,
                    error_message="Target rule throttled for entity",
                )

            # Execute the target rule, incrementing depth
            execution = self.execute_action(
                db,
                target_rule,
                context,
                chain_depth=_depth + 1,
            )

            return ActionResult(
                success=execution.status == ExecutionStatus.SUCCESS,
                result={
                    "chained_rule_id": target_rule_id,
                    "chained_execution_id": str(execution.execution_id),
                    "chain_depth": _depth + 1,
                },
                error_message=execution.error_message,
            )
        except Exception as e:
            return ActionResult(success=False, error_message=str(e))

    def _is_throttled(
        self,
        db: Session,
        rule: WorkflowRule,
        entity_id: UUID,
    ) -> bool:
        """Check if a rule execution should be throttled for a given entity.

        Returns True if the same rule has already fired for this entity
        within the cooldown window.
        """
        if not rule.cooldown_seconds:
            return False

        cutoff = datetime.utcnow() - timedelta(seconds=rule.cooldown_seconds)
        count = db.scalar(
            select(func.count(WorkflowExecution.execution_id)).where(
                WorkflowExecution.rule_id == rule.rule_id,
                WorkflowExecution.entity_id == entity_id,
                WorkflowExecution.triggered_at >= cutoff,
                WorkflowExecution.status.in_(
                    [
                        ExecutionStatus.SUCCESS,
                        ExecutionStatus.RUNNING,
                    ]
                ),
            )
        )
        return bool(count and count > 0)

    def _check_entity_rate_limit(
        self,
        db: Session,
        entity_id: UUID,
    ) -> bool:
        """Check if per-entity execution rate limit has been exceeded.

        Returns True if rate limit is exceeded (should skip).
        """
        cutoff = datetime.utcnow() - timedelta(minutes=1)
        count = db.scalar(
            select(func.count(WorkflowExecution.execution_id)).where(
                WorkflowExecution.entity_id == entity_id,
                WorkflowExecution.triggered_at >= cutoff,
            )
        )
        if count and count >= self.MAX_EXECUTIONS_PER_MINUTE:
            logger.warning(
                "Entity %s exceeded rate limit (%d executions/min)",
                entity_id,
                self.MAX_EXECUTIONS_PER_MINUTE,
            )
            return True
        return False

    def trigger_event(
        self,
        db: Session,
        organization_id: UUID,
        context: TriggerContext,
    ) -> builtins.list[WorkflowExecution]:
        """Trigger workflow evaluation for an event."""
        # Ensure context carries org_id for downstream action handlers
        if context.organization_id is None:
            context.organization_id = organization_id

        matching_rules = self.get_matching_rules(db, organization_id, context)
        executions: list[WorkflowExecution] = []

        # Cap number of rules evaluated per event
        if len(matching_rules) > self.MAX_RULES_PER_EVENT:
            logger.warning(
                "Capping matched rules from %d to %d for entity %s event %s",
                len(matching_rules),
                self.MAX_RULES_PER_EVENT,
                context.entity_id,
                context.event.value,
            )
            matching_rules = matching_rules[: self.MAX_RULES_PER_EVENT]

        # Per-entity rate limit check
        if self._check_entity_rate_limit(db, context.entity_id):
            return executions

        for rule in matching_rules:
            # Throttle check
            if self._is_throttled(db, rule, context.entity_id):
                logger.info(
                    "Rule %s throttled for entity %s (cooldown %ss)",
                    rule.rule_id,
                    context.entity_id,
                    rule.cooldown_seconds,
                )
                continue

            logger.info(
                "Executing rule %s (%s) for %s:%s event=%s",
                rule.rule_id,
                rule.rule_name,
                context.entity_type,
                context.entity_id,
                context.event.value,
            )

            # Async dispatch via Celery if configured
            if rule.execute_async:
                try:
                    from app.tasks.automation import execute_workflow_action

                    execute_workflow_action.delay(str(rule.rule_id), context.to_dict())
                    logger.info(
                        "Dispatched async execution for rule %s entity %s",
                        rule.rule_id,
                        context.entity_id,
                    )
                except Exception:
                    logger.exception(
                        "Failed to dispatch async task for rule %s, "
                        "falling back to sync execution",
                        rule.rule_id,
                    )
                    execution = self.execute_action(db, rule, context)
                    executions.append(execution)
            else:
                execution = self.execute_action(db, rule, context)
                executions.append(execution)

            if rule.stop_on_match:
                break

        return executions

    def update_rule(
        self,
        db: Session,
        rule_id: UUID,
        updates: dict[str, Any],
        updated_by: UUID,
    ) -> WorkflowRule:
        """Update a workflow rule, snapshotting the previous state."""
        rule = db.get(WorkflowRule, rule_id)
        if not rule:
            raise HTTPException(status_code=404, detail="Rule not found")

        # Snapshot current state before applying changes
        self._create_version_snapshot(db, rule, updated_by)

        for key, value in updates.items():
            if hasattr(rule, key):
                setattr(rule, key, value)

        rule.updated_by = updated_by
        db.flush()
        return rule

    def _create_version_snapshot(
        self,
        db: Session,
        rule: WorkflowRule,
        changed_by: UUID | None = None,
    ) -> None:
        """Create a version snapshot of the current rule state."""
        from app.models.finance.automation.workflow_rule_version import (
            WorkflowRuleVersion,
        )

        # Count existing versions for this rule
        version_count = (
            db.scalar(
                select(func.count(WorkflowRuleVersion.version_id)).where(
                    WorkflowRuleVersion.rule_id == rule.rule_id
                )
            )
            or 0
        )

        version = WorkflowRuleVersion(
            rule_id=rule.rule_id,
            version_number=version_count + 1,
            rule_name=rule.rule_name,
            description=rule.description,
            entity_type=rule.entity_type.value,
            trigger_event=rule.trigger_event.value,
            trigger_conditions=rule.trigger_conditions,
            action_type=rule.action_type.value,
            action_config=rule.action_config,
            priority=rule.priority,
            cooldown_seconds=rule.cooldown_seconds,
            schedule_config=rule.schedule_config,
            changed_by=changed_by,
        )
        db.add(version)
        db.flush()

    def delete(self, db: Session, rule_id: UUID) -> bool:
        """Delete a workflow rule."""
        rule = db.get(WorkflowRule, rule_id)
        if not rule:
            return False

        db.delete(rule)
        db.flush()
        return True

    def dry_run(
        self,
        db: Session,
        rule_id: UUID,
        sample_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Test a rule against sample data without executing the action.

        Args:
            db: Database session.
            rule_id: The rule to test.
            sample_data: Dict with keys: entity_type, entity_id, event,
                old_values, new_values.

        Returns:
            Dict describing whether conditions match and what would happen.
        """
        rule = db.get(WorkflowRule, rule_id)
        if not rule:
            raise HTTPException(status_code=404, detail="Rule not found")

        # Build a synthetic context
        try:
            event = TriggerEvent(sample_data.get("event", rule.trigger_event.value))
        except ValueError:
            event = rule.trigger_event

        context = TriggerContext(
            entity_type=sample_data.get("entity_type", rule.entity_type.value),
            entity_id=UUID(sample_data["entity_id"])
            if sample_data.get("entity_id")
            else UUID(int=0),
            event=event,
            organization_id=rule.organization_id,
            old_values=sample_data.get("old_values", {}),
            new_values=sample_data.get("new_values", {}),
            changed_fields=sample_data.get("changed_fields"),
        )

        conditions_match = self._evaluate_conditions(rule.trigger_conditions, context)

        throttled = False
        if conditions_match and context.entity_id != UUID(int=0):
            throttled = self._is_throttled(db, rule, context.entity_id)

        matched_details: list[str] = []
        if conditions_match:
            conds = rule.trigger_conditions
            if conds.get("status_to"):
                matched_details.append(f"status == {conds['status_to']}")
            if conds.get("amount_threshold"):
                t = conds["amount_threshold"]
                matched_details.append(
                    f"{t.get('field', 'amount')} {t.get('operator', '>')} {t.get('value')}"
                )
            for field, cond in conds.get("fields", {}).items():
                if isinstance(cond, dict):
                    matched_details.append(
                        f"{field} {cond.get('operator', '==')} {cond.get('value')}"
                    )
                else:
                    matched_details.append(f"{field} == {cond}")

        return {
            "conditions_match": conditions_match,
            "action_type": rule.action_type.value,
            "would_fire": conditions_match and not throttled,
            "throttled": throttled,
            "matched_conditions": matched_details,
        }

    def get_rule_versions(
        self,
        db: Session,
        rule_id: UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> builtins.list[Any]:
        """Get version history for a rule."""
        from app.models.finance.automation.workflow_rule_version import (
            WorkflowRuleVersion,
        )

        stmt = (
            select(WorkflowRuleVersion)
            .where(WorkflowRuleVersion.rule_id == rule_id)
            .order_by(WorkflowRuleVersion.version_number.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(db.scalars(stmt).all())

    def get_executions(
        self,
        db: Session,
        rule_id: UUID | None = None,
        entity_type: str | None = None,
        entity_id: UUID | None = None,
        status: ExecutionStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> builtins.list[WorkflowExecution]:
        """Get workflow executions with filters."""
        query = select(WorkflowExecution)

        if rule_id:
            query = query.where(WorkflowExecution.rule_id == rule_id)
        if entity_type:
            query = query.where(WorkflowExecution.entity_type == entity_type)
        if entity_id:
            query = query.where(WorkflowExecution.entity_id == entity_id)
        if status:
            query = query.where(WorkflowExecution.status == status)

        query = query.order_by(WorkflowExecution.triggered_at.desc())
        query = query.offset(offset).limit(limit)

        return list(db.execute(query).scalars().all())


# Singleton instance
workflow_service = WorkflowService()
