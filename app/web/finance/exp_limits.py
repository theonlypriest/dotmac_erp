"""
Expense Limits Web Routes.

HTML template routes for expense limit management:
- Limit rules (spending caps)
- Approver limits (approval authority)
- Usage dashboard
- Evaluation audit trail
"""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.services.expense.limit_web import expense_limit_web_service
from app.web.deps import (
    WebAuthContext,
    get_db_for_org,
    require_expense_access,
    require_web_permission,
)

logger = logging.getLogger(__name__)

_require_policies_manage = require_web_permission("expense:policies:manage")
_require_limits_review = require_web_permission("expense:limits:review")

router = APIRouter(tags=["expense-limits-web"])


# =============================================================================
# Limit Rules
# =============================================================================


@router.get("/limits/rules", response_class=HTMLResponse)
def limit_rules_list(
    request: Request,
    scope_type: str | None = None,
    is_active: str | None = None,
    search: str | None = None,
    page: int = Query(1, ge=1),
    auth: WebAuthContext = Depends(require_expense_access),
    db: Session = Depends(get_db_for_org),
):
    """List expense limit rules."""
    return expense_limit_web_service.limit_rules_list_response(
        request=request,
        auth=auth,
        db=db,
        scope_type=scope_type,
        is_active=is_active,
        search=search,
        page=page,
    )


@router.get("/limits", response_class=HTMLResponse)
def limits_index(
    request: Request,
    auth: WebAuthContext = Depends(require_expense_access),
    db: Session = Depends(get_db_for_org),
):
    """Show expense limit destinations."""
    return expense_limit_web_service.limits_index_response(
        request=request,
        auth=auth,
        db=db,
    )


@router.get("/limits/rules/new", response_class=HTMLResponse)
def new_limit_rule(
    request: Request,
    auth: WebAuthContext = Depends(require_expense_access),
    db: Session = Depends(get_db_for_org),
):
    """New expense limit rule form."""
    return expense_limit_web_service.new_limit_rule_form_response(
        request=request,
        auth=auth,
        db=db,
    )


@router.post("/limits/rules/new", response_class=HTMLResponse)
async def create_limit_rule(
    request: Request,
    auth: WebAuthContext = Depends(_require_policies_manage),
    db: Session = Depends(get_db_for_org),
):
    """Create new expense limit rule."""
    return await expense_limit_web_service.create_limit_rule_response(
        request=request,
        auth=auth,
        db=db,
    )


@router.get("/limits/rules/{rule_id}/edit", response_class=HTMLResponse)
def edit_limit_rule(
    request: Request,
    rule_id: UUID,
    auth: WebAuthContext = Depends(require_expense_access),
    db: Session = Depends(get_db_for_org),
):
    """Edit expense limit rule form."""
    return expense_limit_web_service.edit_limit_rule_form_response(
        request=request,
        rule_id=rule_id,
        auth=auth,
        db=db,
    )


@router.post("/limits/rules/{rule_id}/edit", response_class=HTMLResponse)
async def update_limit_rule(
    request: Request,
    rule_id: UUID,
    auth: WebAuthContext = Depends(_require_policies_manage),
    db: Session = Depends(get_db_for_org),
):
    """Update expense limit rule."""
    return await expense_limit_web_service.update_limit_rule_response(
        request=request,
        rule_id=rule_id,
        auth=auth,
        db=db,
    )


@router.post("/limits/rules/{rule_id}/delete", response_class=HTMLResponse)
async def delete_limit_rule(
    request: Request,
    rule_id: UUID,
    auth: WebAuthContext = Depends(_require_policies_manage),
    db: Session = Depends(get_db_for_org),
):
    """Delete expense limit rule."""
    return expense_limit_web_service.delete_limit_rule_response(
        rule_id=rule_id,
        auth=auth,
        db=db,
    )


# =============================================================================
# Approver Limits
# =============================================================================


@router.get("/limits/approvers", response_class=HTMLResponse)
def approver_limits_list(
    request: Request,
    scope_type: str | None = None,
    is_active: str | None = None,
    page: int = Query(1, ge=1),
    auth: WebAuthContext = Depends(require_expense_access),
    db: Session = Depends(get_db_for_org),
):
    """List expense approver limits."""
    return expense_limit_web_service.approver_limits_list_response(
        request=request,
        auth=auth,
        db=db,
        scope_type=scope_type,
        is_active=is_active,
        page=page,
    )


@router.get("/limits/approvers/new", response_class=HTMLResponse)
def new_approver_limit(
    request: Request,
    auth: WebAuthContext = Depends(require_expense_access),
    db: Session = Depends(get_db_for_org),
):
    """New expense approver limit form."""
    return expense_limit_web_service.new_approver_limit_form_response(
        request=request,
        auth=auth,
        db=db,
    )


@router.get("/limits/approvers/{approver_limit_id}/edit", response_class=HTMLResponse)
def edit_approver_limit(
    request: Request,
    approver_limit_id: UUID,
    auth: WebAuthContext = Depends(require_expense_access),
    db: Session = Depends(get_db_for_org),
):
    """Edit expense approver limit form."""
    return expense_limit_web_service.edit_approver_limit_form_response(
        request=request,
        approver_limit_id=approver_limit_id,
        auth=auth,
        db=db,
    )


@router.post("/limits/approvers/{approver_limit_id}/edit", response_class=HTMLResponse)
async def update_approver_limit(
    request: Request,
    approver_limit_id: UUID,
    auth: WebAuthContext = Depends(_require_policies_manage),
    db: Session = Depends(get_db_for_org),
):
    """Update expense approver limit."""
    return await expense_limit_web_service.update_approver_limit_response(
        request=request,
        approver_limit_id=approver_limit_id,
        auth=auth,
        db=db,
    )


@router.get("/limits/approvers/employees/search")
def approver_employee_search(
    q: str = Query(default=""),
    limit: int = Query(default=8, ge=1, le=20),
    auth: WebAuthContext = Depends(require_expense_access),
    db: Session = Depends(get_db_for_org),
):
    """Search active employees for approver limit typeahead."""
    if not q.strip():
        return JSONResponse({"items": []})
    payload = expense_limit_web_service.employee_typeahead(
        db=db,
        organization_id=str(auth.organization_id),
        query=q,
        limit=limit,
    )
    return JSONResponse(payload)


@router.get("/limits/approvers/{approver_limit_id}", response_class=HTMLResponse)
def approver_limit_detail(
    request: Request,
    approver_limit_id: UUID,
    auth: WebAuthContext = Depends(require_expense_access),
    db: Session = Depends(get_db_for_org),
):
    """View expense approver limit details."""
    return expense_limit_web_service.approver_limit_detail_response(
        request=request,
        approver_limit_id=approver_limit_id,
        auth=auth,
        db=db,
    )


@router.post("/limits/approvers/new", response_class=HTMLResponse)
async def create_approver_limit(
    request: Request,
    auth: WebAuthContext = Depends(_require_policies_manage),
    db: Session = Depends(get_db_for_org),
):
    """Create new expense approver limit."""
    return await expense_limit_web_service.create_approver_limit_response(
        request=request,
        auth=auth,
        db=db,
    )


@router.post(
    "/limits/approvers/{approver_limit_id}/delete", response_class=HTMLResponse
)
async def delete_approver_limit(
    request: Request,
    approver_limit_id: UUID,
    auth: WebAuthContext = Depends(_require_policies_manage),
    db: Session = Depends(get_db_for_org),
):
    """Delete expense approver limit."""
    return expense_limit_web_service.delete_approver_limit_response(
        approver_limit_id=approver_limit_id,
        auth=auth,
        db=db,
    )


# =============================================================================
# Reviewer
# =============================================================================


@router.get("/limits/reviewer/approvers", response_class=HTMLResponse)
def reviewer_approvers(
    request: Request,
    q: str | None = None,
    auth: WebAuthContext = Depends(_require_limits_review),
    db: Session = Depends(get_db_for_org),
):
    """List approvers for reviewer reset workflow."""
    return expense_limit_web_service.reviewer_approver_list_response(
        request=request,
        auth=auth,
        db=db,
        q=q,
    )


@router.get("/limits/reviewer/approvers/{approver_id}", response_class=HTMLResponse)
def reviewer_approver_detail(
    request: Request,
    approver_id: UUID,
    from_date: str | None = None,
    to_date: str | None = None,
    auth: WebAuthContext = Depends(_require_limits_review),
    db: Session = Depends(get_db_for_org),
):
    """Review all approval decisions for one approver."""
    return expense_limit_web_service.reviewer_approver_detail_response(
        request=request,
        auth=auth,
        db=db,
        approver_id=approver_id,
        from_date=from_date,
        to_date=to_date,
    )


@router.post(
    "/limits/reviewer/approvers/{approver_id}/reset",
    response_class=HTMLResponse,
)
async def reviewer_reset_approver_budget(
    request: Request,
    approver_id: UUID,
    auth: WebAuthContext = Depends(_require_limits_review),
    db: Session = Depends(get_db_for_org),
):
    """Reset consumed weekly budget for a single approver."""
    form = await request.form()
    form_data = dict(form)
    try:
        response = expense_limit_web_service.reviewer_reset_approver_budget_response(
            auth=auth,
            db=db,
            approver_id=approver_id,
            form_data=form_data,
        )
        db.commit()
        return response
    except Exception as exc:
        db.rollback()
        logger.exception("Failed to reset weekly budget usage")
        return RedirectResponse(
            url=f"/finance/expenses/limits/reviewer/approvers/{approver_id}?error={exc}",
            status_code=303,
        )


# =============================================================================
# Usage & Evaluations
# =============================================================================


@router.get("/limits/usage", response_class=HTMLResponse)
def usage_dashboard(
    request: Request,
    employee_id: str | None = None,
    auth: WebAuthContext = Depends(require_expense_access),
    db: Session = Depends(get_db_for_org),
):
    """Employee expense usage dashboard."""
    return expense_limit_web_service.usage_dashboard_response(
        request=request,
        auth=auth,
        db=db,
        employee_id=employee_id,
    )


@router.get("/limits/evaluations", response_class=HTMLResponse)
def evaluations_list(
    request: Request,
    result: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    page: int = Query(1, ge=1),
    auth: WebAuthContext = Depends(require_expense_access),
    db: Session = Depends(get_db_for_org),
):
    """List expense limit evaluations (audit trail)."""
    return expense_limit_web_service.evaluations_list_response(
        request=request,
        auth=auth,
        db=db,
        result=result,
        from_date=from_date,
        to_date=to_date,
        page=page,
    )
