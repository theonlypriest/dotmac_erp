"""
Audit Event Dispatcher — fire-and-forget data-change auditing.

Mirrors the ``fire_workflow_event()`` pattern: a single function that
service-layer methods call after a data change to record *what* changed,
*who* changed it, and *why*.

Usage::

    from app.services.audit_dispatcher import fire_audit_event
    from app.models.finance.audit.audit_log import AuditAction

    fire_audit_event(
        db=self.db,
        organization_id=org_id,
        table_schema="ar",
        table_name="invoice",
        record_id=str(invoice.invoice_id),
        action=AuditAction.INSERT,
        new_values={"customer_id": str(invoice.customer_id), "total": str(invoice.total)},
    )

The function never raises — audit failures are logged but do not break
the primary business operation.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import inspect as inspect_db
from sqlalchemy.orm import Session

from app.models.finance.audit.audit_log import AuditAction

logger = logging.getLogger(__name__)


def fire_audit_event(
    db: Session,
    organization_id: UUID,
    table_schema: str,
    table_name: str,
    record_id: str | UUID,
    action: AuditAction,
    *,
    old_values: dict[str, Any] | None = None,
    new_values: dict[str, Any] | None = None,
    user_id: UUID | None = None,
    reason: str | None = None,
) -> None:
    """Record a data change in the immutable audit log.

    Automatically reads ``request_id_var``, ``actor_id_var``,
    ``ip_address_var``, and ``user_agent_var`` from the current
    request context so callers don't need to pass them explicitly.

    This function is **fire-and-forget**: it catches all exceptions
    internally and logs them at WARNING level.  Callers do NOT need
    to wrap it in ``try/except``.

    Args:
        db: Active database session (same session as calling service).
        organization_id: Tenant scope.
        table_schema: Schema name (e.g. ``"ar"``, ``"gl"``, ``"hr"``).
        table_name: Logical table/entity name (e.g. ``"invoice"``).
        record_id: Primary key of the affected record.
        action: ``AuditAction.INSERT``, ``UPDATE``, or ``DELETE``.
        old_values: Field values *before* the change (UPDATE/DELETE).
        new_values: Field values *after* the change (INSERT/UPDATE).
        user_id: Override actor — falls back to ``actor_id_var``.
        reason: Optional business reason for the change.
    """
    try:
        bind = db.get_bind()
        if getattr(bind.dialect, "name", "") == "sqlite":
            inspector = inspect_db(db.connection())
            if not inspector.has_table("audit_log"):
                return

        from app.observability import (
            actor_id_var,
            ip_address_var,
            request_id_var,
            user_agent_var,
        )
        from app.services.common import coerce_uuid
        from app.services.finance.platform.audit_log import AuditLogService
        from app.rls import set_current_organization_sync

        # Resolve actor from explicit arg or ContextVar
        resolved_user_id: UUID | None = user_id
        if resolved_user_id is None:
            actor_str = actor_id_var.get()
            if actor_str:
                try:
                    resolved_user_id = coerce_uuid(actor_str)
                except (ValueError, AttributeError):
                    pass

        correlation_id = request_id_var.get() or None
        ip_address = ip_address_var.get() or None
        user_agent = user_agent_var.get() or None

        with db.begin_nested():
            set_current_organization_sync(db, organization_id)
            AuditLogService.log_change(
                db=db,
                organization_id=organization_id,
                table_schema=table_schema,
                table_name=table_name,
                record_id=str(record_id),
                action=action,
                old_values=old_values,
                new_values=new_values,
                user_id=resolved_user_id,
                ip_address=ip_address,
                user_agent=user_agent,
                correlation_id=correlation_id,
                reason=reason,
            )
    except Exception:
        if getattr(db, "is_active", True) is False:
            db.rollback()
        logger.warning(
            "Audit event failed: %s.%s %s %s",
            table_schema,
            table_name,
            action.value if hasattr(action, "value") else action,
            record_id,
            exc_info=True,
        )
