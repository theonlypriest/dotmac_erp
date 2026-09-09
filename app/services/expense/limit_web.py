"""Expense limit web service helpers."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date, datetime, timezone
from functools import partial

try:
    from datetime import UTC  # type: ignore
except ImportError:  # pragma: no cover
    UTC = timezone.utc

from decimal import Decimal
from uuid import UUID

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, joinedload

from app.models.expense import (
    ExpenseClaim,
    ExpenseClaimStatus,
    LimitActionType,
    LimitPeriodType,
    LimitScopeType,
)
from app.models.expense.expense_claim_action import (
    ExpenseClaimAction,
    ExpenseClaimActionStatus,
    ExpenseClaimActionType,
)
from app.models.expense.limit_rule import (
    ExpenseApproverLimit,
)
from app.services.common import PaginationParams, coerce_uuid
from app.services.common_filters import build_active_filters
from app.services.expense import ExpenseLimitService
from app.services.web_forms import safe_form_text
from app.templates import templates
from app.web.deps import WebAuthContext, base_context

logger = logging.getLogger(__name__)


_safe_form_text = partial(safe_form_text, strip=True)


class ExpenseLimitWebService:
    """Service layer for expense limit web routes."""

    def limits_index_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
    ) -> HTMLResponse:
        """Show expense limit categories."""
        context = base_context(request, auth, "Spending Limits", "limits", db=db)
        return templates.TemplateResponse(request, "expense/limits/index.html", context)

    @staticmethod
    def _get_approver_scope_id(form, scope_type: str) -> str:
        """Resolve approver scope target from employee typeahead or scoped select."""
        if scope_type == "EMPLOYEE":
            if hasattr(form, "getlist"):
                values = [
                    _safe_form_text(value)
                    for value in form.getlist("scope_id")
                    if _safe_form_text(value)
                ]
                if values:
                    return values[0]
            return _safe_form_text(form.get("scope_id"))

        scope_option_id = _safe_form_text(form.get("scope_option_id"))
        if scope_option_id:
            return scope_option_id

        if hasattr(form, "getlist"):
            values = [
                _safe_form_text(value)
                for value in form.getlist("scope_id")
                if _safe_form_text(value)
            ]
            if values:
                return values[-1]
        return _safe_form_text(form.get("scope_id"))

    def limit_rules_list_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        scope_type: str | None = None,
        is_active: str | None = None,
        search: str | None = None,
        page: int = 1,
    ) -> HTMLResponse:
        """List expense limit rules."""
        org_id = coerce_uuid(auth.organization_id)
        service = ExpenseLimitService(db)

        # Parse filters
        scope_enum = None
        if scope_type:
            try:
                scope_enum = LimitScopeType(scope_type.upper())
            except ValueError:
                pass

        active_filter = None
        if is_active == "true":
            active_filter = True
        elif is_active == "false":
            active_filter = False

        # Paginate
        per_page = 25

        result = service.list_rules(
            org_id,
            scope_type=scope_enum,
            is_active=active_filter,
            search=search,
            pagination=PaginationParams.from_page(page, per_page),
        )

        # Calculate pagination
        total_pages = result.total_pages

        active_filters = build_active_filters(
            params={
                "scope_type": scope_type,
                "is_active": is_active,
                "search": search,
            },
            labels={"search": "Search"},
        )
        context = base_context(request, auth, "Expense Limits", "limits")
        context.update(
            {
                "rules": result.items,
                "total": result.total,
                "page": page,
                "total_pages": total_pages,
                "scope_types": [s.value for s in LimitScopeType],
                "period_types": [p.value for p in LimitPeriodType],
                "action_types": [a.value for a in LimitActionType],
                "filters": {
                    "scope_type": scope_type,
                    "is_active": is_active,
                    "search": search,
                },
                "active_filters": active_filters,
            }
        )
        return templates.TemplateResponse(request, "expense/limits/list.html", context)

    def new_limit_rule_form_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
    ) -> HTMLResponse:
        """New expense limit rule form."""
        org_id = coerce_uuid(auth.organization_id)

        # Get scope options (grades, departments, designations)
        scope_options = self._get_scope_options(db, org_id)

        context = base_context(request, auth, "New Expense Limit Rule", "limits")
        context.update(
            {
                "rule": None,
                "scope_types": [s.value for s in LimitScopeType],
                "period_types": [p.value for p in LimitPeriodType],
                "action_types": [a.value for a in LimitActionType],
                "scope_options": scope_options,
                "errors": {},
            }
        )
        return templates.TemplateResponse(
            request, "expense/limits/rule_form.html", context
        )

    async def create_limit_rule_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
    ) -> HTMLResponse | RedirectResponse:
        """Create new expense limit rule."""
        org_id = coerce_uuid(auth.organization_id)
        service = ExpenseLimitService(db)

        form = getattr(request.state, "csrf_form", None)
        if form is None:
            form = await request.form()

        rule_code = _safe_form_text(form.get("rule_code"))
        rule_name = _safe_form_text(form.get("rule_name"))
        description = _safe_form_text(form.get("description"))
        scope_type = _safe_form_text(form.get("scope_type"))
        scope_id = self._get_approver_scope_id(form, scope_type)
        period_type = _safe_form_text(form.get("period_type"))
        limit_amount = _safe_form_text(form.get("limit_amount"))
        action_type = _safe_form_text(form.get("action_type"))
        priority = _safe_form_text(form.get("priority"), "100")
        effective_from = _safe_form_text(form.get("effective_from"))
        effective_to = _safe_form_text(form.get("effective_to"))
        is_active = _safe_form_text(form.get("is_active")) in {"1", "true", "on", "yes"}

        errors = {}
        if not rule_code:
            errors["rule_code"] = "Required"
        if not rule_name:
            errors["rule_name"] = "Required"
        if not scope_type:
            errors["scope_type"] = "Required"
        if not period_type:
            errors["period_type"] = "Required"
        if not limit_amount:
            errors["limit_amount"] = "Required"
        if not action_type:
            errors["action_type"] = "Required"
        if not effective_from:
            errors["effective_from"] = "Required"

        limit_amount_value = None
        if limit_amount:
            try:
                limit_amount_value = Decimal(limit_amount)
            except Exception:
                errors["limit_amount"] = "Invalid amount"

        priority_value = 100
        if priority:
            try:
                priority_value = int(priority)
            except Exception:
                errors["priority"] = "Invalid number"

        effective_from_date = None
        if effective_from:
            try:
                effective_from_date = date.fromisoformat(effective_from)
            except Exception:
                errors["effective_from"] = "Invalid date"

        effective_to_date = None
        if effective_to:
            try:
                effective_to_date = date.fromisoformat(effective_to)
            except Exception:
                errors["effective_to"] = "Invalid date"

        if limit_amount_value is None:
            errors["limit_amount"] = errors.get("limit_amount") or "Required"
        if effective_from_date is None:
            errors["effective_from"] = errors.get("effective_from") or "Required"

        scope_options = self._get_scope_options(db, org_id)

        if errors:
            context = base_context(request, auth, "New Expense Limit Rule", "limits")
            context.update(
                {
                    "rule": {
                        "rule_code": rule_code,
                        "rule_name": rule_name,
                        "description": description,
                        "scope_type": scope_type,
                        "scope_id": scope_id,
                        "period_type": period_type,
                        "limit_amount": limit_amount,
                        "action_type": action_type,
                        "priority": priority,
                        "effective_from": effective_from,
                        "effective_to": effective_to,
                        "is_active": is_active,
                    },
                    "scope_types": [s.value for s in LimitScopeType],
                    "period_types": [p.value for p in LimitPeriodType],
                    "action_types": [a.value for a in LimitActionType],
                    "scope_options": scope_options,
                    "errors": errors,
                }
            )
            return templates.TemplateResponse(
                request, "expense/limits/rule_form.html", context
            )

        try:
            if limit_amount_value is None or effective_from_date is None:
                raise ValueError("Missing required form values")
            service.create_rule(
                org_id,
                rule_code=rule_code,
                rule_name=rule_name,
                description=description or None,
                scope_type=LimitScopeType(scope_type.upper()),
                scope_id=coerce_uuid(scope_id) if scope_id else None,
                period_type=LimitPeriodType(period_type.upper()),
                limit_amount=limit_amount_value,
                action_type=LimitActionType(action_type.upper()),
                priority=priority_value,
                effective_from=effective_from_date,
                effective_to=effective_to_date,
                is_active=is_active,
            )
            db.commit()
            return RedirectResponse(
                url="/expense/limits/rules?success=Record+saved+successfully",
                status_code=303,
            )
        except Exception as e:
            db.rollback()
            errors["_form"] = str(e)
            context = base_context(request, auth, "New Expense Limit Rule", "limits")
            context.update(
                {
                    "rule": {
                        "rule_code": rule_code,
                        "rule_name": rule_name,
                        "description": description,
                        "scope_type": scope_type,
                        "scope_id": scope_id,
                        "period_type": period_type,
                        "limit_amount": limit_amount,
                        "action_type": action_type,
                        "priority": priority,
                        "effective_from": effective_from,
                        "effective_to": effective_to,
                        "is_active": is_active,
                    },
                    "scope_types": [s.value for s in LimitScopeType],
                    "period_types": [p.value for p in LimitPeriodType],
                    "action_types": [a.value for a in LimitActionType],
                    "scope_options": scope_options,
                    "errors": errors,
                }
            )
            return templates.TemplateResponse(
                request, "expense/limits/rule_form.html", context
            )

    def edit_limit_rule_form_response(
        self,
        request: Request,
        rule_id: UUID,
        auth: WebAuthContext,
        db: Session,
    ) -> HTMLResponse | RedirectResponse:
        """Edit expense limit rule form."""
        org_id = coerce_uuid(auth.organization_id)
        service = ExpenseLimitService(db)

        try:
            rule = service.get_rule(org_id, rule_id)
        except Exception:
            return RedirectResponse(url="/expense/limits/rules", status_code=303)

        scope_options = self._get_scope_options(db, org_id)

        context = base_context(request, auth, f"Edit Rule: {rule.rule_code}", "limits")
        context.update(
            {
                "rule": rule,
                "scope_types": [s.value for s in LimitScopeType],
                "period_types": [p.value for p in LimitPeriodType],
                "action_types": [a.value for a in LimitActionType],
                "scope_options": scope_options,
                "errors": {},
            }
        )
        return templates.TemplateResponse(
            request, "expense/limits/rule_form.html", context
        )

    async def update_limit_rule_response(
        self,
        request: Request,
        rule_id: UUID,
        auth: WebAuthContext,
        db: Session,
    ) -> HTMLResponse | RedirectResponse:
        """Update expense limit rule."""
        org_id = coerce_uuid(auth.organization_id)
        service = ExpenseLimitService(db)

        form = getattr(request.state, "csrf_form", None)
        if form is None:
            form = await request.form()

        rule_name = _safe_form_text(form.get("rule_name"))
        description = _safe_form_text(form.get("description"))
        limit_amount = _safe_form_text(form.get("limit_amount"))
        action_type = _safe_form_text(form.get("action_type"))
        priority = _safe_form_text(form.get("priority"), "100")
        effective_to = _safe_form_text(form.get("effective_to"))
        is_active = _safe_form_text(form.get("is_active")) in {"1", "true", "on", "yes"}

        errors = {}
        update_data: dict[str, object] = {}

        if rule_name:
            update_data["rule_name"] = rule_name
        if description:
            update_data["description"] = description
        if limit_amount:
            try:
                update_data["limit_amount"] = Decimal(limit_amount)
            except Exception:
                errors["limit_amount"] = "Invalid amount"
        if action_type:
            try:
                update_data["action_type"] = LimitActionType(action_type.upper())
            except Exception:
                errors["action_type"] = "Invalid action type"
        if priority:
            try:
                update_data["priority"] = int(priority)
            except Exception:
                errors["priority"] = "Invalid number"
        if effective_to:
            try:
                update_data["effective_to"] = date.fromisoformat(effective_to)
            except Exception:
                errors["effective_to"] = "Invalid date"

        update_data["is_active"] = is_active

        scope_options = self._get_scope_options(db, org_id)

        if errors:
            rule = service.get_rule(org_id, rule_id)
            context = base_context(
                request, auth, f"Edit Rule: {rule.rule_code}", "limits"
            )
            context.update(
                {
                    "rule": rule,
                    "scope_types": [s.value for s in LimitScopeType],
                    "period_types": [p.value for p in LimitPeriodType],
                    "action_types": [a.value for a in LimitActionType],
                    "scope_options": scope_options,
                    "errors": errors,
                }
            )
            return templates.TemplateResponse(
                request, "expense/limits/rule_form.html", context
            )

        try:
            service.update_rule(org_id, rule_id, **update_data)
            db.commit()
            return RedirectResponse(
                url="/expense/limits/rules?success=Record+saved+successfully",
                status_code=303,
            )
        except Exception as e:
            db.rollback()
            rule = service.get_rule(org_id, rule_id)
            errors["_form"] = str(e)
            context = base_context(
                request, auth, f"Edit Rule: {rule.rule_code}", "limits"
            )
            context.update(
                {
                    "rule": rule,
                    "scope_types": [s.value for s in LimitScopeType],
                    "period_types": [p.value for p in LimitPeriodType],
                    "action_types": [a.value for a in LimitActionType],
                    "scope_options": scope_options,
                    "errors": errors,
                }
            )
            return templates.TemplateResponse(
                request, "expense/limits/rule_form.html", context
            )

    def delete_limit_rule_response(
        self,
        rule_id: UUID,
        auth: WebAuthContext,
        db: Session,
    ) -> RedirectResponse:
        """Delete expense limit rule."""
        org_id = coerce_uuid(auth.organization_id)
        service = ExpenseLimitService(db)

        try:
            service.delete_rule(org_id, rule_id)
            db.commit()
        except Exception:
            db.rollback()

        return RedirectResponse(
            url="/expense/limits/rules?success=Record+deleted+successfully",
            status_code=303,
        )

    def approver_limits_list_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        scope_type: str | None = None,
        is_active: str | None = None,
        page: int = 1,
    ) -> HTMLResponse:
        """List expense approver limits."""
        org_id = coerce_uuid(auth.organization_id)
        service = ExpenseLimitService(db)

        # Parse filters
        active_filter = None
        if is_active == "true":
            active_filter = True
        elif is_active == "false":
            active_filter = False

        # Paginate
        per_page = 25

        result = service.list_approver_limits(
            org_id,
            scope_type=scope_type,
            is_active=active_filter,
            pagination=PaginationParams.from_page(page, per_page),
        )

        total_pages = result.total_pages

        context = base_context(request, auth, "Approver Limits", "limits")
        scope_labels = self._build_approver_scope_labels(db, org_id, result.items)
        context.update(
            {
                "approver_limits": result.items,
                "approver_scope_labels": scope_labels,
                "total": result.total,
                "page": page,
                "total_pages": total_pages,
                "scope_types": ["EMPLOYEE", "GRADE", "DESIGNATION", "ROLE"],
                "filters": {
                    "scope_type": scope_type,
                    "is_active": is_active,
                },
            }
        )
        return templates.TemplateResponse(
            request, "expense/limits/approvers.html", context
        )

    def new_approver_limit_form_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
    ) -> HTMLResponse:
        """New expense approver limit form."""
        org_id = coerce_uuid(auth.organization_id)
        scope_options = self._get_scope_options(db, org_id)

        context = base_context(request, auth, "New Approver Limit", "limits")
        context.update(
            {
                "approver_limit": None,
                "scope_types": ["EMPLOYEE", "GRADE", "DESIGNATION", "ROLE"],
                "scope_options": scope_options,
                "errors": {},
            }
        )
        return templates.TemplateResponse(
            request, "expense/limits/approver_form.html", context
        )

    def approver_limit_detail_response(
        self,
        request: Request,
        approver_limit_id: UUID,
        auth: WebAuthContext,
        db: Session,
    ) -> HTMLResponse:
        """View expense approver limit details."""
        org_id = coerce_uuid(auth.organization_id)
        service = ExpenseLimitService(db)
        limit = service.get_approver_limit(org_id, approver_limit_id)

        scope_labels = self._build_approver_scope_labels(db, org_id, [limit])
        scope_label = scope_labels.get(str(limit.approver_limit_id))

        scoped_employees, is_truncated = self._list_scope_approvers(
            db=db,
            org_id=org_id,
            limit=limit,
            max_items=100,
        )

        # Budget usage carries forward until a reviewer/admin manually resets it.
        current_week_usage = self._build_current_week_usage(db, org_id, limit, service)

        context = base_context(request, auth, "Approver Limit Details", "limits")
        context.update(
            {
                "approver_limit": limit,
                "scope_label": scope_label,
                "scoped_approvers": scoped_employees,
                "scoped_approvers_truncated": is_truncated,
                "current_week_usage": current_week_usage,
            }
        )
        return templates.TemplateResponse(
            request, "expense/limits/approver_detail.html", context
        )

    def edit_approver_limit_form_response(
        self,
        request: Request,
        approver_limit_id: UUID,
        auth: WebAuthContext,
        db: Session,
    ) -> HTMLResponse:
        """Edit expense approver limit form."""
        from app.models.people.hr.designation import Designation
        from app.models.people.hr.employee import Employee
        from app.models.people.hr.employee_grade import EmployeeGrade
        from app.models.rbac import Role

        org_id = coerce_uuid(auth.organization_id)
        service = ExpenseLimitService(db)
        limit = service.get_approver_limit(org_id, approver_limit_id)
        scope_options = self._get_scope_options(db, org_id)

        scope_label = None
        if limit.scope_type == "EMPLOYEE" and limit.scope_id:
            employee = db.get(Employee, limit.scope_id)
            if employee and employee.person:
                scope_label = employee.person.name or ""
                if employee.employee_code:
                    scope_label = (
                        f"{scope_label} ({employee.employee_code})"
                        if scope_label
                        else employee.employee_code
                    )
        elif limit.scope_type == "GRADE" and limit.scope_id:
            grade = db.get(EmployeeGrade, limit.scope_id)
            scope_label = grade.grade_name if grade else None
        elif limit.scope_type == "DESIGNATION" and limit.scope_id:
            designation = db.get(Designation, limit.scope_id)
            scope_label = designation.designation_name if designation else None
        elif limit.scope_type == "ROLE" and limit.scope_id:
            role = db.get(Role, limit.scope_id)
            scope_label = role.name if role else None

        context = base_context(request, auth, "Edit Approver Limit", "limits")
        context.update(
            {
                "approver_limit": limit,
                "scope_types": ["EMPLOYEE", "GRADE", "DESIGNATION", "ROLE"],
                "scope_options": scope_options,
                "scope_label": scope_label,
                "errors": {},
            }
        )
        return templates.TemplateResponse(
            request, "expense/limits/approver_form.html", context
        )

    async def create_approver_limit_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
    ) -> HTMLResponse | RedirectResponse:
        """Create new expense approver limit."""
        org_id = coerce_uuid(auth.organization_id)
        service = ExpenseLimitService(db)

        form = getattr(request.state, "csrf_form", None)
        if form is None:
            form = await request.form()

        scope_type = _safe_form_text(form.get("scope_type"))
        scope_id = self._get_approver_scope_id(form, scope_type)
        max_approval_amount = _safe_form_text(form.get("max_approval_amount"))
        weekly_approval_budget = _safe_form_text(form.get("weekly_approval_budget"))
        can_approve_own = _safe_form_text(form.get("can_approve_own_expenses")) in {
            "1",
            "true",
            "on",
            "yes",
        }
        is_active = _safe_form_text(form.get("is_active")) in {"1", "true", "on", "yes"}

        errors: dict[str, str] = {}
        if not scope_type:
            errors["scope_type"] = "Required"
        if not max_approval_amount:
            errors["max_approval_amount"] = "Required"

        max_amount_value: Decimal | None = None
        if max_approval_amount:
            try:
                max_amount_value = Decimal(max_approval_amount)
            except (ValueError, ArithmeticError):
                errors["max_approval_amount"] = "Invalid amount"
        if max_amount_value is None:
            errors["max_approval_amount"] = (
                errors.get("max_approval_amount") or "Required"
            )

        weekly_budget_value: Decimal | None = None
        if weekly_approval_budget:
            try:
                weekly_budget_value = Decimal(weekly_approval_budget)
            except (ValueError, ArithmeticError):
                errors["weekly_approval_budget"] = "Invalid amount"

        scope_options = self._get_scope_options(db, org_id)

        form_data = {
            "scope_type": scope_type,
            "scope_id": scope_id,
            "max_approval_amount": max_approval_amount,
            "weekly_approval_budget": weekly_approval_budget,
            "can_approve_own_expenses": can_approve_own,
            "is_active": is_active,
        }

        if errors:
            context = base_context(request, auth, "New Approver Limit", "limits")
            context.update(
                {
                    "approver_limit": form_data,
                    "scope_types": ["EMPLOYEE", "GRADE", "DESIGNATION", "ROLE"],
                    "scope_options": scope_options,
                    "errors": errors,
                }
            )
            return templates.TemplateResponse(
                request, "expense/limits/approver_form.html", context
            )

        try:
            if max_amount_value is None:
                raise ValueError("Missing approval amount")
            service.create_approver_limit(
                org_id,
                scope_type=scope_type,
                scope_id=coerce_uuid(scope_id) if scope_id else None,
                max_approval_amount=max_amount_value,
                weekly_approval_budget=weekly_budget_value,
                can_approve_own_expenses=can_approve_own,
                is_active=is_active,
            )
            db.commit()
            return RedirectResponse(
                url="/expense/limits/approvers?success=Record+saved+successfully",
                status_code=303,
            )
        except Exception as e:
            db.rollback()
            errors["_form"] = str(e)
            context = base_context(request, auth, "New Approver Limit", "limits")
            context.update(
                {
                    "approver_limit": form_data,
                    "scope_types": ["EMPLOYEE", "GRADE", "DESIGNATION", "ROLE"],
                    "scope_options": scope_options,
                    "errors": errors,
                }
            )
            return templates.TemplateResponse(
                request, "expense/limits/approver_form.html", context
            )

    async def update_approver_limit_response(
        self,
        request: Request,
        approver_limit_id: UUID,
        auth: WebAuthContext,
        db: Session,
    ) -> HTMLResponse | RedirectResponse:
        """Update expense approver limit."""
        org_id = coerce_uuid(auth.organization_id)
        service = ExpenseLimitService(db)

        form = getattr(request.state, "csrf_form", None)
        if form is None:
            form = await request.form()

        scope_type = _safe_form_text(form.get("scope_type"))
        scope_id = _safe_form_text(form.get("scope_id"))
        max_approval_amount = _safe_form_text(form.get("max_approval_amount"))
        weekly_approval_budget = _safe_form_text(form.get("weekly_approval_budget"))
        can_approve_own = _safe_form_text(form.get("can_approve_own_expenses")) in {
            "1",
            "true",
            "on",
            "yes",
        }
        is_active = _safe_form_text(form.get("is_active")) in {"1", "true", "on", "yes"}

        errors: dict[str, str] = {}
        if not scope_type:
            errors["scope_type"] = "Required"
        if not max_approval_amount:
            errors["max_approval_amount"] = "Required"

        max_amount_value: Decimal | None = None
        if max_approval_amount:
            try:
                max_amount_value = Decimal(max_approval_amount)
            except (ValueError, ArithmeticError):
                errors["max_approval_amount"] = "Invalid amount"
        if max_amount_value is None:
            errors["max_approval_amount"] = (
                errors.get("max_approval_amount") or "Required"
            )

        weekly_budget_value: Decimal | None = None
        if weekly_approval_budget:
            try:
                weekly_budget_value = Decimal(weekly_approval_budget)
            except (ValueError, ArithmeticError):
                errors["weekly_approval_budget"] = "Invalid amount"

        scope_options = self._get_scope_options(db, org_id)

        form_data = {
            "approver_limit_id": approver_limit_id,
            "scope_type": scope_type,
            "scope_id": scope_id,
            "max_approval_amount": max_approval_amount,
            "weekly_approval_budget": weekly_approval_budget,
            "can_approve_own_expenses": can_approve_own,
            "is_active": is_active,
        }

        if errors:
            context = base_context(request, auth, "Edit Approver Limit", "limits")
            context.update(
                {
                    "approver_limit": form_data,
                    "scope_types": ["EMPLOYEE", "GRADE", "DESIGNATION", "ROLE"],
                    "scope_options": scope_options,
                    "scope_label": None,
                    "errors": errors,
                }
            )
            return templates.TemplateResponse(
                request, "expense/limits/approver_form.html", context
            )

        try:
            if max_amount_value is None:
                raise ValueError("Missing approval amount")
            service.update_approver_limit(
                org_id,
                approver_limit_id,
                scope_type=scope_type,
                scope_id=coerce_uuid(scope_id) if scope_id else None,
                max_approval_amount=max_amount_value,
                weekly_approval_budget=weekly_budget_value,
                can_approve_own_expenses=can_approve_own,
                is_active=is_active,
            )
            db.commit()
            return RedirectResponse(
                url="/expense/limits/approvers?success=Record+saved+successfully",
                status_code=303,
            )
        except Exception as e:
            db.rollback()
            errors["_form"] = str(e)
            context = base_context(request, auth, "Edit Approver Limit", "limits")
            context.update(
                {
                    "approver_limit": form_data,
                    "scope_types": ["EMPLOYEE", "GRADE", "DESIGNATION", "ROLE"],
                    "scope_options": scope_options,
                    "scope_label": None,
                    "errors": errors,
                }
            )
            return templates.TemplateResponse(
                request, "expense/limits/approver_form.html", context
            )

    def delete_approver_limit_response(
        self,
        approver_limit_id: UUID,
        auth: WebAuthContext,
        db: Session,
    ) -> RedirectResponse:
        """Delete expense approver limit."""
        org_id = coerce_uuid(auth.organization_id)
        service = ExpenseLimitService(db)

        try:
            service.delete_approver_limit(org_id, approver_limit_id)
            db.commit()
        except Exception:
            db.rollback()

        return RedirectResponse(
            url="/expense/limits/approvers?success=Record+deleted+successfully",
            status_code=303,
        )

    def usage_dashboard_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        employee_id: str | None = None,
    ) -> HTMLResponse:
        """Employee expense usage dashboard."""
        org_id = coerce_uuid(auth.organization_id)
        service = ExpenseLimitService(db)

        # Get employee list for selection
        from app.models.people.hr.employee import Employee, EmployeeStatus
        from app.models.person import Person

        employees = db.scalars(
            select(Employee)
            .join(Person, Person.id == Employee.person_id)
            .where(
                Employee.organization_id == org_id,
                Employee.status == EmployeeStatus.ACTIVE,
            )
            .order_by(Person.first_name, Person.last_name)
            .limit(200)
        ).all()

        usage_summary = None
        if employee_id:
            try:
                emp_uuid = coerce_uuid(employee_id)
                usage_summary = service.get_employee_usage_summary(org_id, emp_uuid)
            except Exception:
                logger.exception("Ignored exception")

        context = base_context(request, auth, "Expense Usage", "limits")
        context.update(
            {
                "employees": employees,
                "selected_employee_id": employee_id,
                "usage_summary": usage_summary,
            }
        )
        return templates.TemplateResponse(request, "expense/limits/usage.html", context)

    def evaluations_list_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        result: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        page: int = 1,
    ) -> HTMLResponse:
        """List expense limit evaluations (audit trail)."""
        from app.models.expense import LimitResultType

        org_id = coerce_uuid(auth.organization_id)
        service = ExpenseLimitService(db)

        # Parse filters
        result_enum = None
        if result:
            try:
                result_enum = LimitResultType(result.upper())
            except ValueError:
                pass

        from_date_parsed = None
        if from_date:
            try:
                from_date_parsed = date.fromisoformat(from_date)
            except Exception:
                logger.exception("Ignored exception")

        to_date_parsed = None
        if to_date:
            try:
                to_date_parsed = date.fromisoformat(to_date)
            except Exception:
                logger.exception("Ignored exception")

        # Paginate
        per_page = 25

        evaluations = service.list_evaluations(
            org_id,
            result=result_enum,
            from_date=from_date_parsed,
            to_date=to_date_parsed,
            pagination=PaginationParams.from_page(page, per_page),
        )

        total_pages = evaluations.total_pages

        context = base_context(request, auth, "Limit Evaluations", "limits")
        active_filters = build_active_filters(
            params={"result": result, "from_date": from_date, "to_date": to_date},
            labels={"from_date": "From", "to_date": "To"},
        )
        context.update(
            {
                "evaluations": evaluations.items,
                "total": evaluations.total,
                "page": page,
                "total_pages": total_pages,
                "result_types": [r.value for r in LimitResultType],
                "filters": {
                    "result": result,
                    "from_date": from_date,
                    "to_date": to_date,
                },
                "active_filters": active_filters,
            }
        )
        return templates.TemplateResponse(
            request, "expense/limits/evaluations.html", context
        )

    def reviewer_approver_list_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        q: str | None = None,
    ) -> HTMLResponse:
        """List approvers by actual approval activity for reviewer workflow."""
        from app.models.people.hr.employee import Employee, EmployeeStatus
        from app.models.person import Person

        org_id = coerce_uuid(auth.organization_id)
        search = (q or "").strip()
        service = ExpenseLimitService(db)

        activity_rows = db.execute(
            select(
                ExpenseClaim.approver_id.label("approver_id"),
                func.count(
                    case(
                        (
                            ExpenseClaimAction.action_type
                            == ExpenseClaimActionType.APPROVE,
                            1,
                        ),
                        else_=None,
                    )
                ).label("approved_count"),
                func.count(
                    case(
                        (
                            ExpenseClaimAction.action_type
                            == ExpenseClaimActionType.REJECT,
                            1,
                        ),
                        else_=None,
                    )
                ).label("rejected_count"),
                func.max(ExpenseClaimAction.created_at).label("last_action_at"),
            )
            .join(
                ExpenseClaimAction, ExpenseClaimAction.claim_id == ExpenseClaim.claim_id
            )
            .where(
                ExpenseClaim.organization_id == org_id,
                ExpenseClaim.approver_id.isnot(None),
                ExpenseClaimAction.action_type.in_(
                    [ExpenseClaimActionType.APPROVE, ExpenseClaimActionType.REJECT]
                ),
                ExpenseClaimAction.status == ExpenseClaimActionStatus.COMPLETED,
            )
            .group_by(ExpenseClaim.approver_id)
            .order_by(func.max(ExpenseClaimAction.created_at).desc())
        ).all()

        approver_ids = [row.approver_id for row in activity_rows if row.approver_id]
        if not approver_ids:
            context = base_context(request, auth, "Expense Reviewer", "limits-review")
            context.update(
                {
                    "approvers": [],
                    "filters": {
                        "q": search,
                    },
                }
            )
            return templates.TemplateResponse(
                request, "expense/limits/reviewer_approvers.html", context
            )

        paid_amount_rows = db.execute(
            select(
                ExpenseClaim.approver_id.label("approver_id"),
                func.coalesce(
                    func.sum(ExpenseClaim.net_payable_amount),
                    Decimal("0"),
                ).label("paid_amount"),
            )
            .where(
                ExpenseClaim.organization_id == org_id,
                ExpenseClaim.approver_id.in_(approver_ids),
                ExpenseClaim.status == ExpenseClaimStatus.PAID,
                ExpenseClaim.paid_on.isnot(None),
            )
            .group_by(ExpenseClaim.approver_id)
        ).all()
        paid_amount_map = {
            row.approver_id: row.paid_amount or Decimal("0") for row in paid_amount_rows
        }

        employees = list(
            db.scalars(
                select(Employee)
                .join(Person, Person.id == Employee.person_id)
                .where(
                    Employee.organization_id == org_id,
                    Employee.employee_id.in_(approver_ids),
                    Employee.status == EmployeeStatus.ACTIVE,
                )
                .order_by(Person.first_name, Person.last_name)
            ).all()
        )
        employee_map = {employee.employee_id: employee for employee in employees}

        approver_rows: list[dict[str, object]] = []
        for row in activity_rows:
            employee = employee_map.get(row.approver_id)
            if employee is None:
                continue

            display_name = employee.person.name if employee.person else "Unknown"
            if (
                search
                and search.lower() not in display_name.lower()
                and (
                    not employee.employee_code
                    or search.lower() not in employee.employee_code.lower()
                )
            ):
                continue

            budget_info = service._get_approver_weekly_budget(org_id, employee)
            weekly_budget = budget_info[0] if budget_info else None
            limit_id = budget_info[1] if budget_info else None
            max_approval_amount = Decimal("0")
            from app.config import settings as _settings

            currency_code = _settings.default_functional_currency_code
            if limit_id:
                max_approval_amount = db.scalar(
                    select(ExpenseApproverLimit.max_approval_amount).where(
                        ExpenseApproverLimit.organization_id == org_id,
                        ExpenseApproverLimit.approver_limit_id == limit_id,
                    )
                ) or Decimal("0")
                currency_code = (
                    db.scalar(
                        select(ExpenseApproverLimit.currency_code).where(
                            ExpenseApproverLimit.organization_id == org_id,
                            ExpenseApproverLimit.approver_limit_id == limit_id,
                        )
                    )
                    or _settings.default_functional_currency_code
                )
            latest_reset = (
                service.get_latest_weekly_reset(
                    org_id,
                    approver_id=employee.employee_id,
                    approver_limit_id=limit_id,
                    from_datetime=None,
                )
                if limit_id
                else None
            )

            approver_rows.append(
                {
                    "employee_id": str(employee.employee_id),
                    "employee_code": employee.employee_code or "-",
                    "name": display_name,
                    "limit_id": str(limit_id) if limit_id else None,
                    "weekly_budget": weekly_budget,
                    "max_approval_amount": max_approval_amount,
                    "currency_code": currency_code,
                    "last_reset_at": latest_reset.reset_at if latest_reset else None,
                    "approved_count": int(row.approved_count or 0),
                    "rejected_count": int(row.rejected_count or 0),
                    "paid_amount": paid_amount_map.get(row.approver_id, Decimal("0")),
                    "last_action_at": row.last_action_at,
                }
            )

        context = base_context(request, auth, "Expense Reviewer", "limits-review")
        context.update(
            {
                "approvers": approver_rows,
                "filters": {
                    "q": search,
                },
            }
        )
        return templates.TemplateResponse(
            request, "expense/limits/reviewer_approvers.html", context
        )

    def reviewer_approver_detail_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        approver_id: UUID,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> HTMLResponse | RedirectResponse:
        """Show claims approved/rejected by one approver for reviewer decision."""
        from app.models.people.hr.employee import Employee
        from app.models.person import Person

        org_id = coerce_uuid(auth.organization_id)
        service = ExpenseLimitService(db)
        employee = db.scalar(
            select(Employee)
            .join(Person, Person.id == Employee.person_id)
            .where(
                Employee.organization_id == org_id,
                Employee.employee_id == approver_id,
            )
        )
        if employee is None:
            return RedirectResponse(
                url="/expense/limits/reviewer/approvers", status_code=303
            )

        parsed_from: date | None = None
        parsed_to: date | None = None
        if from_date:
            try:
                parsed_from = date.fromisoformat(from_date)
            except ValueError:
                parsed_from = None
        if to_date:
            try:
                parsed_to = date.fromisoformat(to_date)
            except ValueError:
                parsed_to = None

        week_start = service._start_of_week_utc(datetime.now(UTC)).date()
        if parsed_from is None:
            parsed_from = week_start
        if parsed_to is None:
            parsed_to = date.today()

        # Include both approvals and rejections executed by this approver.
        actions = list(
            db.execute(
                select(ExpenseClaimAction, ExpenseClaim)
                .join(
                    ExpenseClaim,
                    ExpenseClaim.claim_id == ExpenseClaimAction.claim_id,
                )
                .where(
                    ExpenseClaim.organization_id == org_id,
                    ExpenseClaim.approver_id == approver_id,
                    ExpenseClaimAction.action_type.in_(
                        [ExpenseClaimActionType.APPROVE, ExpenseClaimActionType.REJECT]
                    ),
                    ExpenseClaimAction.status == ExpenseClaimActionStatus.COMPLETED,
                    func.date(ExpenseClaimAction.created_at) >= parsed_from,
                    func.date(ExpenseClaimAction.created_at) <= parsed_to,
                )
                .order_by(ExpenseClaimAction.created_at.desc())
            ).all()
        )

        budget_info = service._get_approver_weekly_budget(org_id, employee)
        limit_id = budget_info[1] if budget_info else None
        budget_amount = budget_info[0] if budget_info else None
        latest_reset = (
            service.get_latest_weekly_reset(
                org_id,
                approver_id=approver_id,
                approver_limit_id=limit_id,
                from_datetime=None,
            )
            if limit_id
            else None
        )

        context = base_context(
            request,
            auth,
            f"Reviewer - {employee.person.name if employee.person else employee.employee_code}",
            "limits-review",
        )
        context.update(
            {
                "approver": employee,
                "actions": actions,
                "filters": {
                    "from_date": parsed_from.isoformat(),
                    "to_date": parsed_to.isoformat(),
                },
                "weekly_budget": budget_amount,
                "limit_id": str(limit_id) if limit_id else None,
                "last_reset": latest_reset,
            }
        )
        return templates.TemplateResponse(
            request, "expense/limits/reviewer_approver_detail.html", context
        )

    def reviewer_reset_approver_budget_response(
        self,
        auth: WebAuthContext,
        db: Session,
        approver_id: UUID,
        form_data: dict[str, object],
    ) -> RedirectResponse:
        """Create manual budget reset for one approver."""
        org_id = coerce_uuid(auth.organization_id)
        service = ExpenseLimitService(db)

        reason = _safe_form_text(form_data.get("reset_reason"))
        reviewed_from_raw = _safe_form_text(form_data.get("reviewed_from"))
        reviewed_to_raw = _safe_form_text(form_data.get("reviewed_to"))

        try:
            reviewed_from = (
                date.fromisoformat(reviewed_from_raw) if reviewed_from_raw else None
            )
            reviewed_to = (
                date.fromisoformat(reviewed_to_raw) if reviewed_to_raw else None
            )
        except ValueError:
            return RedirectResponse(
                url=f"/expense/limits/reviewer/approvers/{approver_id}?error=Invalid+review+date+filter",
                status_code=303,
            )

        if not reason:
            return RedirectResponse(
                url=f"/expense/limits/reviewer/approvers/{approver_id}?error=Reset+reason+is+required",
                status_code=303,
            )

        service.create_weekly_budget_reset(
            org_id,
            approver_id=approver_id,
            reviewed_by_id=coerce_uuid(auth.person_id),
            reset_reason=reason,
            reviewed_from=reviewed_from,
            reviewed_to=reviewed_to,
        )
        db.flush()
        return RedirectResponse(
            url=f"/expense/limits/reviewer/approvers/{approver_id}?success=Weekly+budget+usage+reset",
            status_code=303,
        )

    @staticmethod
    def _build_current_week_usage(
        db: Session,
        org_id: UUID,
        limit: ExpenseApproverLimit,
        service: ExpenseLimitService,
    ) -> dict[str, object] | None:
        """Build manual-reset usage stats for the approver limit detail page.

        Returns None when no weekly budget is configured (unlimited).
        """

        base_budget = limit.weekly_approval_budget
        if base_budget is None:
            return None
        if limit.scope_type != "EMPLOYEE" or not limit.scope_id:
            return {
                "usage_label": "Employee-level breakdown unavailable for this scope",
                "base_budget": base_budget,
                "used_amount": Decimal("0"),
                "remaining_budget": base_budget,
                "last_reset_at": None,
            }

        now = datetime.now(UTC)

        window_start, latest_reset = service._get_weekly_budget_window(
            org_id,
            limit.scope_id,
            limit.approver_limit_id,
            as_of=now,
        )
        usage_query = select(
            func.coalesce(func.sum(ExpenseClaim.net_payable_amount), Decimal("0"))
        ).where(
            ExpenseClaim.organization_id == org_id,
            ExpenseClaim.status == ExpenseClaimStatus.PAID,
            ExpenseClaim.paid_on.isnot(None),
            ExpenseClaim.paid_on >= window_start.date(),
            ExpenseClaim.paid_on <= now.date(),
            ExpenseClaim.approver_id == limit.scope_id,
        )

        used_amount = db.scalar(usage_query) or Decimal("0")
        remaining_budget = base_budget - used_amount

        return {
            "usage_label": (
                f"Since manual reset on {latest_reset.reset_at.date().isoformat()}"
                if latest_reset
                else f"This week starting {window_start.date().isoformat()}"
            ),
            "base_budget": base_budget,
            "used_amount": used_amount,
            "remaining_budget": remaining_budget,
            "last_reset_at": latest_reset.reset_at if latest_reset else None,
        }

    @staticmethod
    def _list_scope_approvers(
        db: Session,
        org_id: UUID,
        limit: ExpenseApproverLimit,
        max_items: int = 100,
    ) -> tuple[list[dict[str, str]], bool]:
        """List approvers that match the configured scope target."""
        from app.models.people.hr.employee import Employee, EmployeeStatus
        from app.models.person import Person
        from app.models.rbac import PersonRole

        stmt = (
            select(Employee)
            .join(Person, Person.id == Employee.person_id)
            .options(
                joinedload(Employee.person),
                joinedload(Employee.grade),
                joinedload(Employee.designation),
            )
            .where(Employee.organization_id == org_id)
        )

        scope_type = limit.scope_type
        scope_id = limit.scope_id

        if scope_type == "EMPLOYEE":
            if scope_id:
                stmt = stmt.where(Employee.employee_id == scope_id)
            else:
                stmt = stmt.where(Employee.status == EmployeeStatus.ACTIVE)
        elif scope_type == "GRADE":
            stmt = stmt.where(Employee.status == EmployeeStatus.ACTIVE)
            if scope_id:
                stmt = stmt.where(Employee.grade_id == scope_id)
        elif scope_type == "DESIGNATION":
            stmt = stmt.where(Employee.status == EmployeeStatus.ACTIVE)
            if scope_id:
                stmt = stmt.where(Employee.designation_id == scope_id)
        elif scope_type == "ROLE":
            stmt = stmt.join(PersonRole, PersonRole.person_id == Employee.person_id)
            stmt = stmt.where(Employee.status == EmployeeStatus.ACTIVE)
            if scope_id:
                stmt = stmt.where(PersonRole.role_id == scope_id)
        else:
            return [], False

        stmt = stmt.order_by(Person.first_name, Person.last_name).limit(max_items + 1)
        employees = list(db.scalars(stmt).unique().all())
        is_truncated = len(employees) > max_items
        if is_truncated:
            employees = employees[:max_items]

        rows: list[dict[str, str]] = []
        for employee in employees:
            rows.append(
                {
                    "employee_id": str(employee.employee_id),
                    "name": employee.person.name if employee.person else "Unknown",
                    "employee_code": employee.employee_code or "-",
                    "designation": (
                        employee.designation.designation_name
                        if employee.designation
                        else "-"
                    ),
                    "grade": employee.grade.grade_name if employee.grade else "-",
                    "status": employee.status.value,
                }
            )

        return rows, is_truncated

    @staticmethod
    def _build_approver_scope_labels(
        db: Session, org_id: UUID, limits: Sequence[ExpenseApproverLimit]
    ) -> dict[str, str]:
        """Resolve human-readable labels for approver limit scope targets."""
        from app.models.people.hr.designation import Designation
        from app.models.people.hr.employee import Employee
        from app.models.people.hr.employee_grade import EmployeeGrade
        from app.models.person import Person
        from app.models.rbac import Role

        labels: dict[str, str] = {}
        employee_ids: set[UUID] = set()
        grade_ids: set[UUID] = set()
        designation_ids: set[UUID] = set()
        role_ids: set[UUID] = set()

        for limit in limits:
            scope_id = getattr(limit, "scope_id", None)
            if not scope_id:
                continue

            scope_type = getattr(limit, "scope_type", "")
            if scope_type == "EMPLOYEE":
                employee_ids.add(scope_id)
            elif scope_type == "GRADE":
                grade_ids.add(scope_id)
            elif scope_type == "DESIGNATION":
                designation_ids.add(scope_id)
            elif scope_type == "ROLE":
                role_ids.add(scope_id)

        employee_map: dict[UUID, str] = {}
        if employee_ids:
            employees = list(
                db.scalars(
                    select(Employee)
                    .join(Person, Person.id == Employee.person_id)
                    .where(
                        Employee.organization_id == org_id,
                        Employee.employee_id.in_(employee_ids),
                    )
                ).all()
            )
            for employee in employees:
                name = employee.person.name if employee.person else ""
                employee_label = name
                if employee.employee_code:
                    employee_label = (
                        f"{name} ({employee.employee_code})"
                        if name
                        else employee.employee_code
                    )
                employee_map[employee.employee_id] = employee_label or str(
                    employee.employee_id
                )

        grade_map: dict[UUID, str] = {}
        if grade_ids:
            grades = list(
                db.scalars(
                    select(EmployeeGrade).where(
                        EmployeeGrade.organization_id == org_id,
                        EmployeeGrade.grade_id.in_(grade_ids),
                    )
                ).all()
            )
            for grade in grades:
                grade_map[grade.grade_id] = grade.grade_name

        designation_map: dict[UUID, str] = {}
        if designation_ids:
            designations = list(
                db.scalars(
                    select(Designation).where(
                        Designation.organization_id == org_id,
                        Designation.designation_id.in_(designation_ids),
                    )
                ).all()
            )
            for designation in designations:
                designation_map[designation.designation_id] = (
                    designation.designation_name
                )

        role_map: dict[UUID, str] = {}
        if role_ids:
            roles = list(db.scalars(select(Role).where(Role.id.in_(role_ids))).all())
            for role in roles:
                role_map[role.id] = role.name

        for limit in limits:
            scope_id = limit.scope_id
            if not scope_id:
                continue

            scope_type = limit.scope_type
            label: str | None = None
            if scope_type == "EMPLOYEE":
                label = employee_map.get(scope_id)
            elif scope_type == "GRADE":
                label = grade_map.get(scope_id)
            elif scope_type == "DESIGNATION":
                label = designation_map.get(scope_id)
            elif scope_type == "ROLE":
                label = role_map.get(scope_id)

            labels[str(limit.approver_limit_id)] = label or str(scope_id)

        return labels

    @staticmethod
    def _get_scope_options(db: Session, org_id: UUID) -> dict:
        """Get scope options for dropdowns (grades, departments, designations, employees)."""
        from app.models.people.hr.department import Department
        from app.models.people.hr.designation import Designation
        from app.models.people.hr.employee import Employee, EmployeeStatus
        from app.models.people.hr.employee_grade import EmployeeGrade
        from app.models.person import Person
        from app.models.rbac import Role

        grades = db.scalars(
            select(EmployeeGrade)
            .where(
                EmployeeGrade.organization_id == org_id, EmployeeGrade.is_active == True
            )
            .order_by(EmployeeGrade.rank.desc())
        ).all()

        departments = db.scalars(
            select(Department)
            .where(Department.organization_id == org_id, Department.is_active == True)
            .order_by(Department.department_name)
        ).all()

        designations = db.scalars(
            select(Designation)
            .where(Designation.organization_id == org_id, Designation.is_active == True)
            .order_by(Designation.designation_name)
        ).all()

        employees = db.scalars(
            select(Employee)
            .join(Person, Person.id == Employee.person_id)
            .where(
                Employee.organization_id == org_id,
                Employee.status == EmployeeStatus.ACTIVE,
            )
            .order_by(Person.first_name, Person.last_name)
            .limit(100)
        ).all()

        roles = db.scalars(
            select(Role).where(Role.is_active == True).order_by(Role.name)
        ).all()

        return {
            "grades": grades,
            "departments": departments,
            "designations": designations,
            "employees": employees,
            "roles": roles,
        }

    @staticmethod
    def employee_typeahead(
        db: Session,
        organization_id: str,
        query: str,
        limit: int = 8,
    ) -> dict:
        """Search active employees for approver limit typeahead fields."""
        from sqlalchemy import select as sa_select
        from sqlalchemy.orm import joinedload as jl

        from app.models.people.hr.employee import Employee, EmployeeStatus
        from app.models.person import Person
        from app.services.common import coerce_uuid

        org_id = coerce_uuid(organization_id)
        search_term = f"%{query.strip()}%"
        stmt = (
            sa_select(Employee)
            .join(Person, Person.id == Employee.person_id)
            .options(jl(Employee.person))
            .where(
                Employee.organization_id == org_id,
                Employee.status == EmployeeStatus.ACTIVE,
            )
            .where(
                (Person.first_name.ilike(search_term))
                | (Person.last_name.ilike(search_term))
                | (Person.email.ilike(search_term))
                | (Employee.employee_code.ilike(search_term))
            )
            .order_by(Person.first_name.asc(), Person.last_name.asc())
            .limit(limit)
        )
        employees = list(db.scalars(stmt).unique().all())
        items = []
        for employee in employees:
            name = employee.person.name if employee.person else ""
            label = name
            if employee.employee_code:
                label = (
                    f"{name} ({employee.employee_code})"
                    if name
                    else employee.employee_code
                )
            items.append(
                {
                    "ref": str(employee.employee_id),
                    "label": label,
                    "name": name,
                    "employee_code": employee.employee_code or "",
                }
            )
        return {"items": items}


expense_limit_web_service = ExpenseLimitWebService()
