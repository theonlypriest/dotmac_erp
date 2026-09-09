import csv
import io
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from starlette.requests import Request
from starlette.responses import HTMLResponse

from app.models.expense import (
    ExpenseApproverLimit,
    ExpenseCategory,
    ExpenseClaim,
    ExpenseClaimAction,
    ExpenseClaimActionStatus,
    ExpenseClaimActionType,
    ExpenseClaimApprovalStep,
    ExpenseClaimItem,
    ExpenseClaimStatus,
    ExpenseLimitRule,
)
from app.models.people.hr.department import Department
from app.models.people.hr.employee import Employee, EmployeeStatus
from app.models.people.hr.position import Position
from app.models.people.hr.position_assignment import (
    PositionAssignment,
    PositionAssignmentType,
)
from app.models.notification import Notification, NotificationType
from app.models.person import Person
from app.services.expense import (
    ExpenseApprovalService,
    ExpenseClaimsWebService,
    ExpenseService,
    ExpenseServiceError,
)
from app.web.deps import WebAuthContext


def _ensure_hr_tables(engine) -> None:
    for table in (
        Department.__table__,
        Employee.__table__,
        Position.__table__,
        PositionAssignment.__table__,
        ExpenseApproverLimit.__table__,
        ExpenseLimitRule.__table__,
    ):
        for column in table.columns:
            default = column.server_default
            if default is None:
                continue
            default_text = str(getattr(default, "arg", default)).lower()
            if "gen_random_uuid" in default_text or "uuid_generate" in default_text:
                column.server_default = None
        table.create(engine, checkfirst=True)


def _wire_manager_via_position(
    db_session,
    org_id: uuid.UUID,
    *,
    employee: Employee,
    manager: Employee,
) -> None:
    short_id = uuid.uuid4().hex[:8].upper()
    manager_position = Position(
        position_id=uuid.uuid4(),
        organization_id=org_id,
        position_code=f"MGR-{short_id}",
        position_name=f"Manager Position {short_id}",
        is_vacant=False,
    )
    employee_position = Position(
        position_id=uuid.uuid4(),
        organization_id=org_id,
        position_code=f"EMP-{short_id}",
        position_name=f"Employee Position {short_id}",
        parent_position_id=manager_position.position_id,
        is_vacant=False,
    )
    db_session.add_all([manager_position, employee_position])
    db_session.flush()
    db_session.add_all(
        [
            PositionAssignment(
                position_assignment_id=uuid.uuid4(),
                organization_id=org_id,
                employee_id=manager.employee_id,
                position_id=manager_position.position_id,
                assignment_type=PositionAssignmentType.PRIMARY,
                start_date=date.today(),
            ),
            PositionAssignment(
                position_assignment_id=uuid.uuid4(),
                organization_id=org_id,
                employee_id=employee.employee_id,
                position_id=employee_position.position_id,
                assignment_type=PositionAssignmentType.PRIMARY,
                start_date=date.today(),
            ),
        ]
    )
    db_session.flush()


def _make_approver_limit(
    org_id: uuid.UUID,
    employee_id: uuid.UUID,
    amount: Decimal,
) -> ExpenseApproverLimit:
    return ExpenseApproverLimit(
        approver_limit_id=uuid.uuid4(),
        organization_id=org_id,
        scope_type="EMPLOYEE",
        scope_id=employee_id,
        max_approval_amount=amount,
        currency_code="NGN",
        dimension_filters="{}",
        is_active=True,
    )


def _make_person(org_id: uuid.UUID, email: str) -> Person:
    return Person(
        id=uuid.uuid4(),
        organization_id=org_id,
        first_name=email.split("@", 1)[0].title(),
        last_name="User",
        email=email,
    )


def _make_employee(
    org_id: uuid.UUID,
    person: Person,
    code: str,
    *,
    reports_to_id: uuid.UUID | None = None,
) -> Employee:
    return Employee(
        employee_id=uuid.uuid4(),
        organization_id=org_id,
        person_id=person.id,
        employee_code=code,
        date_of_joining=date.today(),
        reports_to_id=reports_to_id,
        status=EmployeeStatus.ACTIVE,
    )


def _make_claim(
    org_id: uuid.UUID,
    employee_id: uuid.UUID,
    claim_number: str,
    *,
    requested_approver_id: uuid.UUID | None = None,
    status: ExpenseClaimStatus = ExpenseClaimStatus.DRAFT,
    amount: Decimal = Decimal("100.00"),
) -> ExpenseClaim:
    return ExpenseClaim(
        claim_id=uuid.uuid4(),
        organization_id=org_id,
        claim_number=claim_number,
        employee_id=employee_id,
        requested_approver_id=requested_approver_id,
        claim_date=date.today(),
        purpose=f"Purpose {claim_number}",
        total_claimed_amount=amount,
        status=status,
        currency_code="NGN",
    )


def _make_category(org_id: uuid.UUID) -> ExpenseCategory:
    return ExpenseCategory(
        category_id=uuid.uuid4(),
        organization_id=org_id,
        category_code="TRAVEL",
        category_name="Travel",
        requires_receipt=False,
        is_active=True,
    )


def _make_item(
    org_id: uuid.UUID,
    claim_id: uuid.UUID,
    category_id: uuid.UUID,
    amount: Decimal,
) -> ExpenseClaimItem:
    return ExpenseClaimItem(
        item_id=uuid.uuid4(),
        organization_id=org_id,
        claim_id=claim_id,
        expense_date=date.today(),
        category_id=category_id,
        description="Taxi",
        claimed_amount=amount,
    )


def _request(path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "query_string": b"",
            "headers": [],
        }
    )


def test_approve_claim_requires_employee_backed_approver(db_session, engine):
    _ensure_hr_tables(engine)
    org_id = uuid.uuid4()

    claimant_person = _make_person(org_id, "claimant@example.com")
    claimant = _make_employee(org_id, claimant_person, "EMP-001")
    claim = _make_claim(
        org_id,
        claimant.employee_id,
        "CLM-001",
        status=ExpenseClaimStatus.PENDING_APPROVAL,
    )

    db_session.add_all([claimant_person, claimant, claim])
    db_session.commit()

    svc = ExpenseService(db_session)
    with pytest.raises(ExpenseServiceError, match="employee record"):
        svc.approve_claim(
            org_id, claim.claim_id, approver_id=None, send_notification=False
        )


def test_multi_approval_claim_remains_pending_until_all_steps_complete(
    db_session, engine, monkeypatch
):
    _ensure_hr_tables(engine)
    org_id = uuid.uuid4()

    claimant_person = _make_person(org_id, "claimant2@example.com")
    approver1_person = _make_person(org_id, "approver1@example.com")
    approver2_person = _make_person(org_id, "approver2@example.com")
    claimant = _make_employee(org_id, claimant_person, "EMP-101")
    approver1 = _make_employee(org_id, approver1_person, "EMP-102")
    approver2 = _make_employee(org_id, approver2_person, "EMP-103")
    claim = _make_claim(
        org_id,
        claimant.employee_id,
        "CLM-101",
        status=ExpenseClaimStatus.PENDING_APPROVAL,
    )

    db_session.add_all(
        [
            claimant_person,
            approver1_person,
            approver2_person,
            claimant,
            approver1,
            approver2,
            claim,
            ExpenseClaimApprovalStep(
                organization_id=org_id,
                claim_id=claim.claim_id,
                submission_round=1,
                step_number=1,
                approver_id=approver1.employee_id,
                approver_name=approver1.full_name,
                max_amount=Decimal("1000.00"),
                requires_all_approvals=True,
            ),
            ExpenseClaimApprovalStep(
                organization_id=org_id,
                claim_id=claim.claim_id,
                submission_round=1,
                step_number=2,
                approver_id=approver2.employee_id,
                approver_name=approver2.full_name,
                max_amount=Decimal("1000.00"),
                requires_all_approvals=True,
            ),
        ]
    )
    db_session.commit()

    svc = ExpenseService(db_session)
    monkeypatch.setattr(
        svc, "_validate_approver_authority", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        svc,
        "_validate_approver_weekly_budget",
        lambda *args, **kwargs: None,
    )

    first = svc.approve_claim(
        org_id,
        claim.claim_id,
        approver_id=approver1.employee_id,
        send_notification=False,
    )
    db_session.commit()

    assert first.status == ExpenseClaimStatus.PENDING_APPROVAL
    refreshed = svc.get_claim(org_id, claim.claim_id)
    assert refreshed.status == ExpenseClaimStatus.PENDING_APPROVAL
    assert refreshed.approver_id is None

    second = svc.approve_claim(
        org_id,
        claim.claim_id,
        approver_id=approver2.employee_id,
        send_notification=False,
    )
    db_session.commit()

    assert second.status == ExpenseClaimStatus.APPROVED
    steps = list(
        db_session.scalars(
            select(ExpenseClaimApprovalStep)
            .where(ExpenseClaimApprovalStep.claim_id == claim.claim_id)
            .order_by(ExpenseClaimApprovalStep.step_number)
        ).all()
    )
    assert [step.decision for step in steps] == ["APPROVED", "APPROVED"]


def test_expense_approval_chain_uses_position_manager_when_legacy_manager_empty(
    db_session,
    engine,
):
    _ensure_hr_tables(engine)
    org_id = uuid.uuid4()

    claimant_person = _make_person(org_id, "claimant-position@example.com")
    manager_person = _make_person(org_id, "manager-position@example.com")
    claimant = _make_employee(org_id, claimant_person, "EMP-POS-001")
    manager = _make_employee(org_id, manager_person, "MGR-POS-001")
    claim = _make_claim(
        org_id,
        claimant.employee_id,
        "CLM-POS-001",
        amount=Decimal("250.00"),
    )

    db_session.add_all([claimant_person, manager_person, claimant, manager, claim])
    db_session.flush()
    _wire_manager_via_position(
        db_session,
        org_id,
        employee=claimant,
        manager=manager,
    )
    db_session.add(_make_approver_limit(org_id, manager.employee_id, Decimal("500.00")))
    db_session.commit()

    chain = ExpenseApprovalService(db_session).get_approval_chain(
        claim,
        force_rebuild=True,
    )

    assert chain.total_steps == 1
    assert chain.steps[0].approver_id == manager.employee_id
    assert chain.steps[0].is_escalation is False


def test_expense_approval_chain_escalates_via_expense_approver_position_manager(
    db_session,
    engine,
):
    _ensure_hr_tables(engine)
    org_id = uuid.uuid4()

    claimant_person = _make_person(org_id, "claimant-escalation@example.com")
    approver_person = _make_person(org_id, "approver-escalation@example.com")
    director_person = _make_person(org_id, "director-escalation@example.com")
    claimant = _make_employee(org_id, claimant_person, "EMP-ESC-001")
    approver = _make_employee(org_id, approver_person, "APR-ESC-001")
    director = _make_employee(org_id, director_person, "DIR-ESC-001")
    claimant.expense_approver_id = approver.employee_id
    claim = _make_claim(
        org_id,
        claimant.employee_id,
        "CLM-ESC-001",
        amount=Decimal("750.00"),
    )

    db_session.add_all(
        [
            claimant_person,
            approver_person,
            director_person,
            claimant,
            approver,
            director,
            claim,
        ]
    )
    db_session.flush()
    _wire_manager_via_position(
        db_session,
        org_id,
        employee=approver,
        manager=director,
    )
    db_session.add_all(
        [
            _make_approver_limit(org_id, approver.employee_id, Decimal("100.00")),
            _make_approver_limit(org_id, director.employee_id, Decimal("1000.00")),
        ]
    )
    db_session.commit()

    chain = ExpenseApprovalService(db_session).get_approval_chain(
        claim,
        force_rebuild=True,
    )

    assert chain.total_steps == 1
    assert chain.steps[0].approver_id == director.employee_id
    assert chain.steps[0].is_escalation is True


def test_manual_expense_escalation_uses_current_approver_position_manager(
    db_session,
    engine,
):
    _ensure_hr_tables(engine)
    org_id = uuid.uuid4()

    approver_person = _make_person(org_id, "manual-approver@example.com")
    manager_person = _make_person(org_id, "manual-manager@example.com")
    claimant_person = _make_person(org_id, "manual-claimant@example.com")
    approver = _make_employee(org_id, approver_person, "APR-MAN-001")
    manager = _make_employee(org_id, manager_person, "MGR-MAN-001")
    claimant = _make_employee(org_id, claimant_person, "EMP-MAN-001")
    claim = _make_claim(
        org_id,
        claimant.employee_id,
        "CLM-MAN-001",
        amount=Decimal("900.00"),
    )

    db_session.add_all(
        [
            approver_person,
            manager_person,
            claimant_person,
            approver,
            manager,
            claimant,
            claim,
            _make_approver_limit(org_id, approver.employee_id, Decimal("100.00")),
        ]
    )
    db_session.flush()
    _wire_manager_via_position(
        db_session,
        org_id,
        employee=approver,
        manager=manager,
    )
    db_session.commit()

    escalated_to = ExpenseApprovalService(db_session).escalate_approval(
        claim,
        approver.employee_id,
    )

    assert escalated_to == manager.employee_id


def test_resubmit_preserves_prior_approval_round_and_submit_creates_new_round(
    db_session, engine, monkeypatch
):
    _ensure_hr_tables(engine)
    org_id = uuid.uuid4()

    claimant_person = _make_person(org_id, "claimant3@example.com")
    approver_person = _make_person(org_id, "approver3@example.com")
    claimant = _make_employee(org_id, claimant_person, "EMP-201")
    approver = _make_employee(org_id, approver_person, "EMP-202")
    category = _make_category(org_id)
    claim = _make_claim(
        org_id,
        claimant.employee_id,
        "CLM-201",
        requested_approver_id=approver.employee_id,
        amount=Decimal("250.00"),
    )
    item = _make_item(org_id, claim.claim_id, category.category_id, Decimal("250.00"))

    db_session.add_all(
        [
            claimant_person,
            approver_person,
            claimant,
            approver,
            category,
            claim,
            item,
        ]
    )
    db_session.commit()

    monkeypatch.setattr(
        "app.services.expense.approval_service.ExpenseApprovalService._get_triggered_rules",
        lambda self, claim, employee: [],
    )
    monkeypatch.setattr(
        "app.services.expense.approval_service.ExpenseApprovalService._get_approver_max_amount",
        lambda self, org_id, approver: Decimal("1000.00"),
    )
    monkeypatch.setattr(
        "app.services.expense.expense_service.fire_audit_event",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.expense.expense_service.ExpenseService._notify_submission_confirmed",
        lambda self, claim: None,
    )
    monkeypatch.setattr(
        "app.services.finance.automation.event_dispatcher.fire_workflow_event",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.expense.expense_notifications.ExpenseNotificationService.notify_approval_needed",
        lambda self, claim, approver: True,
    )

    svc = ExpenseService(db_session)
    submit_result = svc.submit_claim(org_id, claim.claim_id, skip_limit_check=True)
    db_session.commit()

    assert submit_result.claim.status == ExpenseClaimStatus.PENDING_APPROVAL
    first_round_notifications = list(
        db_session.scalars(
            select(Notification).where(
                Notification.entity_id == claim.claim_id,
                Notification.notification_type == NotificationType.SUBMITTED,
            )
        ).all()
    )
    assert len(first_round_notifications) == 1
    assert first_round_notifications[0].recipient_id == approver.person_id
    assert first_round_notifications[0].email_sent is True

    rejected = svc.reject_claim(
        org_id,
        claim.claim_id,
        approver_id=approver.employee_id,
        reason="Missing details",
        send_notification=False,
    )
    db_session.commit()
    assert rejected.status == ExpenseClaimStatus.REJECTED

    reset = svc.resubmit_claim(org_id, claim.claim_id)
    db_session.commit()
    assert reset.status == ExpenseClaimStatus.DRAFT

    resubmitted = svc.submit_claim(org_id, claim.claim_id, skip_limit_check=True)
    db_session.commit()
    assert resubmitted.claim.status == ExpenseClaimStatus.PENDING_APPROVAL

    rounds = list(
        db_session.execute(
            select(
                ExpenseClaimApprovalStep.submission_round,
                ExpenseClaimApprovalStep.decision,
            )
            .where(ExpenseClaimApprovalStep.claim_id == claim.claim_id)
            .order_by(
                ExpenseClaimApprovalStep.submission_round,
                ExpenseClaimApprovalStep.step_number,
            )
        ).all()
    )
    assert rounds[0] == (1, "REJECTED")
    assert rounds[-1] == (2, None)

    submission_notifications = list(
        db_session.scalars(
            select(Notification).where(
                Notification.entity_id == claim.claim_id,
                Notification.notification_type == NotificationType.SUBMITTED,
            )
        ).all()
    )
    assert len(submission_notifications) == 2

    submit_marker = db_session.scalar(
        select(ExpenseClaimAction).where(
            ExpenseClaimAction.claim_id == claim.claim_id,
            ExpenseClaimAction.action_type == ExpenseClaimActionType.SUBMIT,
        )
    )
    assert submit_marker.status == ExpenseClaimActionStatus.COMPLETED


def test_submitted_to_me_view_uses_pending_approval_steps(
    db_session, engine, monkeypatch
):
    _ensure_hr_tables(engine)
    org_id = uuid.uuid4()

    claimant_person = _make_person(org_id, "claimant4@example.com")
    approver_person = _make_person(org_id, "approver4@example.com")
    claimant = _make_employee(org_id, claimant_person, "EMP-301")
    approver = _make_employee(org_id, approver_person, "EMP-302")
    claim = _make_claim(
        org_id,
        claimant.employee_id,
        "CLM-301",
        status=ExpenseClaimStatus.PENDING_APPROVAL,
    )

    db_session.add_all(
        [
            claimant_person,
            approver_person,
            claimant,
            approver,
            claim,
            ExpenseClaimApprovalStep(
                organization_id=org_id,
                claim_id=claim.claim_id,
                submission_round=1,
                step_number=1,
                approver_id=approver.employee_id,
                approver_name=approver.full_name,
                max_amount=Decimal("1000.00"),
                requires_all_approvals=False,
            ),
        ]
    )
    db_session.commit()

    auth = WebAuthContext(
        is_authenticated=True,
        person_id=approver.person_id,
        employee_id=approver.employee_id,
        organization_id=org_id,
        roles=["admin"],
    )
    monkeypatch.setattr(
        "app.services.expense.web.base_context",
        lambda request, auth, title, section: {"user": auth.user},
    )
    monkeypatch.setattr(
        "app.services.expense.web.templates.TemplateResponse",
        lambda request, template_name, context: HTMLResponse(
            " ".join(claim.claim_number for claim in context["claims"])
        ),
    )
    response = ExpenseClaimsWebService.claims_list_response(
        request=_request("/expense/claims/list?view=submitted_to_me"),
        auth=auth,
        db=db_session,
        view="submitted_to_me",
        status=None,
        start_date=None,
        end_date=None,
    )

    assert response.status_code == 200
    assert "CLM-301" in response.body.decode()


def test_claim_detail_allows_reimbursement_scope_to_open_payment_page(
    db_session, engine, monkeypatch
):
    from app.models.finance.payments.payment_intent import PaymentIntent

    _ensure_hr_tables(engine)
    attached = {
        row[1] for row in db_session.execute(text("PRAGMA database_list")).all()
    }
    if "payments" not in attached:
        db_session.execute(text("ATTACH DATABASE ':memory:' AS payments"))
    for column in PaymentIntent.__table__.columns:
        default = column.server_default
        if default is None:
            continue
        default_text = str(getattr(default, "arg", default)).lower()
        if "gen_random_uuid" in default_text or "uuid_generate" in default_text:
            column.server_default = None
    PaymentIntent.__table__.create(engine, checkfirst=True)

    org_id = uuid.uuid4()

    claimant_person = _make_person(org_id, "claimant-pay@example.com")
    claimant = _make_employee(org_id, claimant_person, "EMP-PAY")
    claim = _make_claim(
        org_id,
        claimant.employee_id,
        "CLM-PAY",
        status=ExpenseClaimStatus.APPROVED,
    )
    db_session.add_all([claimant_person, claimant, claim])
    db_session.commit()

    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "app.services.expense.web.base_context",
        lambda request, auth, title, section: {"user": auth.user},
    )
    monkeypatch.setattr(
        "app.services.expense.web_claims.AuthorizationService.check_any_permission",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        "app.services.expense.web_claims.AuthorizationService.check_permission",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        "app.services.expense.web_claims.comment_service.list_comments",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "app.services.expense.web_claims.get_recent_activity_for_record",
        lambda *args, **kwargs: [],
    )

    def _capture_template_response(request, template_name, context):
        captured.update(context)
        return HTMLResponse(template_name)

    monkeypatch.setattr(
        "app.services.expense.web.templates.TemplateResponse",
        _capture_template_response,
    )

    auth = WebAuthContext(
        is_authenticated=True,
        person_id=uuid.uuid4(),
        organization_id=org_id,
        scopes=["expense:claims:reimburse"],
    )
    response = ExpenseClaimsWebService.claim_detail_response(
        request=_request(f"/expense/claims/{claim.claim_id}"),
        auth=auth,
        db=db_session,
        claim_id=str(claim.claim_id),
    )

    assert response.status_code == 200
    assert captured["can_reimburse_expense"] is True
    assert captured["can_approve"] is False


def test_submitted_to_me_view_retains_latest_round_claims_after_decision(
    db_session, engine, monkeypatch
):
    _ensure_hr_tables(engine)
    org_id = uuid.uuid4()

    claimant_person = _make_person(org_id, "claimant7@example.com")
    approver_person = _make_person(org_id, "approver7@example.com")
    claimant = _make_employee(org_id, claimant_person, "EMP-701")
    approver = _make_employee(org_id, approver_person, "EMP-702")
    approved_claim = _make_claim(
        org_id,
        claimant.employee_id,
        "CLM-701",
        status=ExpenseClaimStatus.APPROVED,
    )
    rejected_claim = _make_claim(
        org_id,
        claimant.employee_id,
        "CLM-702",
        status=ExpenseClaimStatus.REJECTED,
    )

    db_session.add_all(
        [
            claimant_person,
            approver_person,
            claimant,
            approver,
            approved_claim,
            rejected_claim,
            ExpenseClaimApprovalStep(
                organization_id=org_id,
                claim_id=approved_claim.claim_id,
                submission_round=1,
                step_number=1,
                approver_id=approver.employee_id,
                approver_name=approver.full_name,
                decision="APPROVED",
                max_amount=Decimal("1000.00"),
                requires_all_approvals=False,
            ),
            ExpenseClaimApprovalStep(
                organization_id=org_id,
                claim_id=rejected_claim.claim_id,
                submission_round=1,
                step_number=1,
                approver_id=approver.employee_id,
                approver_name=approver.full_name,
                decision="REJECTED",
                max_amount=Decimal("1000.00"),
                requires_all_approvals=False,
            ),
        ]
    )
    db_session.commit()

    auth = WebAuthContext(
        is_authenticated=True,
        person_id=approver.person_id,
        employee_id=approver.employee_id,
        organization_id=org_id,
        roles=["admin"],
    )
    captured_context: dict[str, object] = {}

    monkeypatch.setattr(
        "app.services.expense.web.base_context",
        lambda request, auth, title, section: {"user": auth.user},
    )

    def _template_response(request, template_name, context):
        captured_context.update(context)
        return HTMLResponse("ok")

    monkeypatch.setattr(
        "app.services.expense.web.templates.TemplateResponse",
        _template_response,
    )

    response = ExpenseClaimsWebService.claims_list_response(
        request=_request("/expense/claims/list?view=submitted_to_me"),
        auth=auth,
        db=db_session,
        view="submitted_to_me",
        status=None,
        start_date=None,
        end_date=None,
    )

    assert response.status_code == 200
    assert {claim.claim_number for claim in captured_context["claims"]} == {
        "CLM-701",
        "CLM-702",
    }


def test_submitted_to_me_view_supports_status_filter_for_retained_claims(
    db_session, engine, monkeypatch
):
    _ensure_hr_tables(engine)
    org_id = uuid.uuid4()

    claimant_person = _make_person(org_id, "claimant8@example.com")
    approver_person = _make_person(org_id, "approver8@example.com")
    claimant = _make_employee(org_id, claimant_person, "EMP-801")
    approver = _make_employee(org_id, approver_person, "EMP-802")
    approved_claim = _make_claim(
        org_id,
        claimant.employee_id,
        "CLM-801",
        status=ExpenseClaimStatus.APPROVED,
    )
    paid_claim = _make_claim(
        org_id,
        claimant.employee_id,
        "CLM-802",
        status=ExpenseClaimStatus.PAID,
    )

    db_session.add_all(
        [
            claimant_person,
            approver_person,
            claimant,
            approver,
            approved_claim,
            paid_claim,
            ExpenseClaimApprovalStep(
                organization_id=org_id,
                claim_id=approved_claim.claim_id,
                submission_round=1,
                step_number=1,
                approver_id=approver.employee_id,
                approver_name=approver.full_name,
                decision="APPROVED",
                max_amount=Decimal("1000.00"),
                requires_all_approvals=False,
            ),
            ExpenseClaimApprovalStep(
                organization_id=org_id,
                claim_id=paid_claim.claim_id,
                submission_round=1,
                step_number=1,
                approver_id=approver.employee_id,
                approver_name=approver.full_name,
                decision="APPROVED",
                max_amount=Decimal("1000.00"),
                requires_all_approvals=False,
            ),
        ]
    )
    db_session.commit()

    auth = WebAuthContext(
        is_authenticated=True,
        person_id=approver.person_id,
        employee_id=approver.employee_id,
        organization_id=org_id,
        roles=["admin"],
    )
    captured_context: dict[str, object] = {}

    monkeypatch.setattr(
        "app.services.expense.web.base_context",
        lambda request, auth, title, section: {"user": auth.user},
    )

    def _template_response(request, template_name, context):
        captured_context.update(context)
        return HTMLResponse("ok")

    monkeypatch.setattr(
        "app.services.expense.web.templates.TemplateResponse",
        _template_response,
    )

    response = ExpenseClaimsWebService.claims_list_response(
        request=_request("/expense/claims/list?view=submitted_to_me&status=PAID"),
        auth=auth,
        db=db_session,
        view="submitted_to_me",
        status=ExpenseClaimStatus.PAID.value,
        start_date=None,
        end_date=None,
    )

    assert response.status_code == 200
    assert [claim.claim_number for claim in captured_context["claims"]] == ["CLM-802"]


def test_claims_list_includes_selected_employee_filter_label(
    db_session, engine, monkeypatch
):
    _ensure_hr_tables(engine)
    org_id = uuid.uuid4()

    claimant_person = _make_person(org_id, "claimant5@example.com")
    approver_person = _make_person(org_id, "approver5@example.com")
    claimant = _make_employee(org_id, claimant_person, "EMP-401")
    approver = _make_employee(org_id, approver_person, "EMP-402")
    claim = _make_claim(
        org_id,
        claimant.employee_id,
        "CLM-401",
        status=ExpenseClaimStatus.SUBMITTED,
    )

    db_session.add_all([claimant_person, approver_person, claimant, approver, claim])
    db_session.commit()

    auth = WebAuthContext(
        is_authenticated=True,
        person_id=approver.person_id,
        employee_id=approver.employee_id,
        organization_id=org_id,
        roles=["admin"],
    )
    captured_context: dict[str, object] = {}

    monkeypatch.setattr(
        "app.services.expense.web.base_context",
        lambda request, auth, title, section: {"user": auth.user},
    )

    def _template_response(request, template_name, context):
        captured_context.update(context)
        return HTMLResponse("ok")

    monkeypatch.setattr(
        "app.services.expense.web.templates.TemplateResponse",
        _template_response,
    )

    response = ExpenseClaimsWebService.claims_list_response(
        request=_request(f"/expense/claims/list?employee_id={claimant.employee_id}"),
        auth=auth,
        db=db_session,
        view=None,
        status=None,
        start_date=None,
        end_date=None,
        employee_id=str(claimant.employee_id),
    )

    assert response.status_code == 200
    assert [c.claim_number for c in captured_context["claims"]] == ["CLM-401"]
    assert captured_context["selected_employee"].employee_id == claimant.employee_id
    assert [emp.employee_id for emp in captured_context["claim_employees"]] == [
        claimant.employee_id
    ]
    assert captured_context["active_filters"] == [
        {
            "name": "employee_id",
            "value": str(claimant.employee_id),
            "display_value": f"Employee: {claimant.full_name}",
        }
    ]


def test_claims_list_filters_by_approver_and_includes_filter_label(
    db_session, engine, monkeypatch
):
    _ensure_hr_tables(engine)
    org_id = uuid.uuid4()

    claimant_person = _make_person(org_id, "claimant10@example.com")
    approver_person = _make_person(org_id, "approver10@example.com")
    other_approver_person = _make_person(org_id, "other10@example.com")
    claimant = _make_employee(org_id, claimant_person, "EMP-1001")
    approver = _make_employee(org_id, approver_person, "EMP-1002")
    other_approver = _make_employee(org_id, other_approver_person, "EMP-1003")
    included_claim = _make_claim(
        org_id,
        claimant.employee_id,
        "CLM-1001",
        status=ExpenseClaimStatus.APPROVED,
    )
    excluded_claim = _make_claim(
        org_id,
        claimant.employee_id,
        "CLM-1002",
        status=ExpenseClaimStatus.APPROVED,
    )

    db_session.add_all(
        [
            claimant_person,
            approver_person,
            other_approver_person,
            claimant,
            approver,
            other_approver,
            included_claim,
            excluded_claim,
            ExpenseClaimApprovalStep(
                organization_id=org_id,
                claim_id=included_claim.claim_id,
                submission_round=1,
                step_number=1,
                approver_id=approver.employee_id,
                approver_name=f"{approver_person.first_name} {approver_person.last_name}",
                decision="APPROVED",
                max_amount=Decimal("1000.00"),
                requires_all_approvals=False,
            ),
            ExpenseClaimApprovalStep(
                organization_id=org_id,
                claim_id=excluded_claim.claim_id,
                submission_round=1,
                step_number=1,
                approver_id=other_approver.employee_id,
                approver_name=(
                    f"{other_approver_person.first_name} "
                    f"{other_approver_person.last_name}"
                ),
                decision="APPROVED",
                max_amount=Decimal("1000.00"),
                requires_all_approvals=False,
            ),
        ]
    )
    db_session.commit()

    auth = WebAuthContext(
        is_authenticated=True,
        person_id=approver.person_id,
        employee_id=approver.employee_id,
        organization_id=org_id,
        roles=["admin"],
    )
    captured_context: dict[str, object] = {}

    monkeypatch.setattr(
        "app.services.expense.web.base_context",
        lambda request, auth, title, section: {"user": auth.user},
    )

    def _template_response(request, template_name, context):
        captured_context.update(context)
        return HTMLResponse("ok")

    monkeypatch.setattr(
        "app.services.expense.web.templates.TemplateResponse",
        _template_response,
    )

    response = ExpenseClaimsWebService.claims_list_response(
        request=_request(f"/expense/claims/list?approver_id={approver.employee_id}"),
        auth=auth,
        db=db_session,
        view=None,
        status=None,
        start_date=None,
        end_date=None,
        approver_id=str(approver.employee_id),
    )

    assert response.status_code == 200
    assert [c.claim_number for c in captured_context["claims"]] == ["CLM-1001"]
    assert captured_context["selected_approver"].employee_id == approver.employee_id
    assert {emp.employee_id for emp in captured_context["claim_approvers"]} == {
        approver.employee_id,
        other_approver.employee_id,
    }
    assert captured_context["active_filters"] == [
        {
            "name": "approver_id",
            "value": str(approver.employee_id),
            "display_value": f"Approver: {approver.full_name}",
        }
    ]
    assert f"approver_id={approver.employee_id}" in captured_context["export_url"]


def test_claims_export_uses_date_filters_and_includes_people(db_session, engine):
    _ensure_hr_tables(engine)
    org_id = uuid.uuid4()

    claimant_person = _make_person(org_id, "claimant9@example.com")
    approver_person = _make_person(org_id, "approver9@example.com")
    claimant = _make_employee(org_id, claimant_person, "EMP-901")
    approver = _make_employee(org_id, approver_person, "EMP-902")
    included_claim = _make_claim(
        org_id,
        claimant.employee_id,
        "CLM-901",
        requested_approver_id=approver.employee_id,
        status=ExpenseClaimStatus.APPROVED,
        amount=Decimal("250.50"),
    )
    included_claim.claim_date = date(2026, 2, 10)
    included_claim.total_approved_amount = Decimal("240.00")
    included_claim.net_payable_amount = Decimal("240.00")
    excluded_claim = _make_claim(
        org_id,
        claimant.employee_id,
        "CLM-902",
        requested_approver_id=approver.employee_id,
        status=ExpenseClaimStatus.APPROVED,
    )
    excluded_claim.claim_date = date(2026, 3, 1)

    db_session.add_all(
        [
            claimant_person,
            approver_person,
            claimant,
            approver,
            included_claim,
            excluded_claim,
            ExpenseClaimApprovalStep(
                organization_id=org_id,
                claim_id=included_claim.claim_id,
                submission_round=1,
                step_number=1,
                approver_id=approver.employee_id,
                approver_name=(
                    f"{approver_person.first_name} {approver_person.last_name}"
                ),
                decision="APPROVED",
                max_amount=Decimal("1000.00"),
                requires_all_approvals=False,
            ),
        ]
    )
    db_session.commit()

    auth = WebAuthContext(
        is_authenticated=True,
        person_id=approver.person_id,
        employee_id=approver.employee_id,
        organization_id=org_id,
        roles=["admin"],
    )

    response = ExpenseClaimsWebService.claims_export_response(
        auth=auth,
        db=db_session,
        view=None,
        status=None,
        start_date="2026-02-01",
        end_date="2026-02-28",
    )

    rows = list(csv.DictReader(io.StringIO(response.body.decode())))
    assert response.media_type == "text/csv"
    assert (
        'filename="expense_claims_from_2026-02-01_to_2026-02-28.csv"'
        in response.headers["Content-Disposition"]
    )
    assert len(rows) == 1
    assert rows[0]["Claim Number"] == "CLM-901"
    assert rows[0]["Raised By"] == claimant.full_name
    assert rows[0]["Employee Code"] == "EMP-901"
    assert rows[0]["Approver"] == approver.full_name
    assert rows[0]["Claimed Amount"] == "250.50"
    assert rows[0]["Approved Amount"] == "240.00"


def test_claims_export_filters_by_approver(db_session, engine):
    _ensure_hr_tables(engine)
    org_id = uuid.uuid4()

    claimant_person = _make_person(org_id, "claimant11@example.com")
    approver_person = _make_person(org_id, "approver11@example.com")
    other_approver_person = _make_person(org_id, "other11@example.com")
    claimant = _make_employee(org_id, claimant_person, "EMP-1101")
    approver = _make_employee(org_id, approver_person, "EMP-1102")
    other_approver = _make_employee(org_id, other_approver_person, "EMP-1103")
    included_claim = _make_claim(
        org_id,
        claimant.employee_id,
        "CLM-1101",
        status=ExpenseClaimStatus.APPROVED,
    )
    excluded_claim = _make_claim(
        org_id,
        claimant.employee_id,
        "CLM-1102",
        status=ExpenseClaimStatus.APPROVED,
    )
    approver_name = f"{approver_person.first_name} {approver_person.last_name}"
    other_approver_name = (
        f"{other_approver_person.first_name} {other_approver_person.last_name}"
    )

    db_session.add_all(
        [
            claimant_person,
            approver_person,
            other_approver_person,
            claimant,
            approver,
            other_approver,
            included_claim,
            excluded_claim,
            ExpenseClaimApprovalStep(
                organization_id=org_id,
                claim_id=included_claim.claim_id,
                submission_round=1,
                step_number=1,
                approver_id=approver.employee_id,
                approver_name=approver_name,
                decision="APPROVED",
                max_amount=Decimal("1000.00"),
                requires_all_approvals=False,
            ),
            ExpenseClaimApprovalStep(
                organization_id=org_id,
                claim_id=excluded_claim.claim_id,
                submission_round=1,
                step_number=1,
                approver_id=other_approver.employee_id,
                approver_name=other_approver_name,
                decision="APPROVED",
                max_amount=Decimal("1000.00"),
                requires_all_approvals=False,
            ),
        ]
    )
    db_session.commit()

    auth = WebAuthContext(
        is_authenticated=True,
        person_id=approver.person_id,
        employee_id=approver.employee_id,
        organization_id=org_id,
        roles=["admin"],
    )

    response = ExpenseClaimsWebService.claims_export_response(
        auth=auth,
        db=db_session,
        view=None,
        status=None,
        start_date=None,
        end_date=None,
        approver_id=str(approver.employee_id),
    )

    rows = list(csv.DictReader(io.StringIO(response.body.decode())))
    assert len(rows) == 1
    assert rows[0]["Claim Number"] == "CLM-1101"
    assert rows[0]["Approver"] == approver.full_name


def test_claim_employee_typeahead_returns_claim_employees_only(db_session, engine):
    _ensure_hr_tables(engine)
    org_id = uuid.uuid4()

    claimant_person = _make_person(org_id, "claimant6@example.com")
    other_person = _make_person(org_id, "observer6@example.com")
    claimant = _make_employee(org_id, claimant_person, "EMP-501")
    other_employee = _make_employee(org_id, other_person, "EMP-502")
    claim = _make_claim(
        org_id,
        claimant.employee_id,
        "CLM-501",
        status=ExpenseClaimStatus.SUBMITTED,
    )

    db_session.add_all([claimant_person, other_person, claimant, other_employee, claim])
    db_session.commit()

    payload = ExpenseClaimsWebService.claim_employee_typeahead(
        db=db_session,
        organization_id=str(org_id),
        query="claimant",
        limit=10,
    )

    assert payload == {
        "items": [
            {
                "ref": str(claimant.employee_id),
                "label": f"{claimant.full_name} ({claimant.employee_code})",
                "name": claimant.full_name,
                "employee_code": claimant.employee_code,
            }
        ]
    }
