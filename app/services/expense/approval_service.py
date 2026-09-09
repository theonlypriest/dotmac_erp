"""
Expense Approval Service - Multi-approval workflow management.

Handles:
- Approval chain determination based on limit rules
- Multi-step approval tracking
- Escalation logic
- Receipt validation
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

try:
    from datetime import UTC  # type: ignore
except ImportError:  # pragma: no cover
    UTC = timezone.utc

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.expense import (
    ExpenseApproverLimit,
    ExpenseCategory,
    ExpenseClaim,
    ExpenseClaimApprovalStep,
    ExpenseLimitRule,
    LimitActionType,
)
from app.services.people.hr.employment_types import EmploymentTypeService

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.models.people.hr.employee import Employee
    from app.web.deps import WebAuthContext

__all__ = [
    "ExpenseApprovalService",
    "ApprovalStep",
    "ApprovalChain",
    "ReceiptValidationResult",
]


@dataclass
class ApprovalStep:
    """Represents a single step in an approval chain."""

    step_number: int
    approver_id: UUID
    approver_name: str
    max_amount: Decimal
    submission_round: int = 1
    is_escalation: bool = False
    is_completed: bool = False
    decision: str | None = None  # "APPROVED", "REJECTED", None
    decided_at: datetime | None = None
    notes: str | None = None


@dataclass
class ApprovalChain:
    """Complete approval chain for an expense claim."""

    claim_id: UUID
    total_steps: int
    completed_steps: int
    current_step: int
    steps: list[ApprovalStep] = field(default_factory=list)
    requires_all_approvals: bool = False
    is_complete: bool = False
    final_decision: str | None = None

    @property
    def pending_steps(self) -> list[ApprovalStep]:
        """Get steps that are pending approval."""
        return [s for s in self.steps if not s.is_completed]

    @property
    def current_approvers(self) -> list[UUID]:
        """Get IDs of approvers who can currently approve."""
        if self.is_complete:
            return []
        pending = self.pending_steps
        if not pending:
            return []
        # Return first pending step's approver (sequential approval)
        # For parallel approval, return all pending approvers
        if self.requires_all_approvals:
            return [s.approver_id for s in pending]
        return [pending[0].approver_id]


@dataclass
class ReceiptValidationResult:
    """Result of receipt validation for an expense claim."""

    is_valid: bool
    missing_receipts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def has_issues(self) -> bool:
        return not self.is_valid or len(self.warnings) > 0


class ExpenseApprovalService:
    """
    Service for expense approval workflow management.

    Handles:
    - Building approval chains based on amount and rules
    - Processing individual approval decisions
    - Escalation when approver authority is insufficient
    - Receipt validation against category requirements
    """

    def __init__(
        self,
        db: Session,
        ctx: WebAuthContext | None = None,
    ) -> None:
        self.db = db
        self.ctx = ctx
        self._receipt_optional_category_tokens = {
            "fuelmileage",
            "fuelmileageexpense",
            "fuelmileageexpenses",
        }

    @staticmethod
    def _normalize_category_token(value: str | None) -> str:
        if not value:
            return ""
        return "".join(ch for ch in value.lower() if ch.isalnum())

    def _is_receipt_optional_category(self, category: ExpenseCategory) -> bool:
        return any(
            self._normalize_category_token(candidate)
            in self._receipt_optional_category_tokens
            for candidate in (category.category_name, category.category_code)
        )

    # =========================================================================
    # Approval Chain Management
    # =========================================================================

    def get_approval_chain(
        self,
        claim: ExpenseClaim,
        *,
        force_rebuild: bool = False,
    ) -> ApprovalChain:
        """
        Get or build the approval chain for an expense claim.

        The approval chain is determined by:
        1. Claim amount vs approver authority limits
        2. Triggered limit rules and their action configs
        3. Organizational hierarchy (manager chain)

        Args:
            claim: The expense claim
            force_rebuild: If True, rebuild chain even if cached

        Returns:
            ApprovalChain with all required approval steps
        """
        persisted_steps = self._get_persisted_steps(claim)
        if persisted_steps and not force_rebuild:
            return self._build_chain_from_persisted_steps(claim, persisted_steps)

        return self._build_dynamic_approval_chain(claim)

    def initialize_approval_chain(
        self,
        claim: ExpenseClaim,
    ) -> ApprovalChain:
        """Build and persist a fresh approval chain for the next submission round."""
        chain = self._build_dynamic_approval_chain(claim)
        submission_round = self._current_submission_round(claim.claim_id) + 1
        for step in chain.steps:
            step.submission_round = submission_round
        self._persist_approval_chain(claim, chain, submission_round)
        return self.get_approval_chain(claim)

    def _build_dynamic_approval_chain(
        self,
        claim: ExpenseClaim,
    ) -> ApprovalChain:
        from app.models.people.hr.employee import Employee, EmployeeStatus

        org_id = claim.organization_id
        claim_amount = claim.total_approved_amount or claim.total_claimed_amount

        # Get employee
        employee = (
            self.db.get(Employee, claim.employee_id) if claim.employee_id else None
        )
        if not employee:
            # No employee - simple single approval
            return ApprovalChain(
                claim_id=claim.claim_id,
                total_steps=1,
                completed_steps=0,
                current_step=1,
                steps=[],
                requires_all_approvals=False,
                is_complete=False,
            )

        steps: list[ApprovalStep] = []
        seen_approvers = set()

        # Step 1: Check for triggered rules requiring multi-approval
        rules = self._get_triggered_rules(claim, employee)
        multi_approval_required = any(
            r.action_type == LimitActionType.REQUIRE_MULTI_APPROVAL for r in rules
        )
        escalation_required = any(
            r.action_type == LimitActionType.AUTO_ESCALATE for r in rules
        )

        # Step 2: Start with expense approver (if set), otherwise position manager
        initial_approver = None
        initial_approver_id = (
            claim.requested_approver_id or employee.expense_approver_id
        )
        if initial_approver_id:
            initial_approver = self.db.scalar(
                select(Employee).where(
                    Employee.employee_id == initial_approver_id,
                    Employee.organization_id == org_id,
                    Employee.status != EmployeeStatus.TERMINATED,
                )
            )
        else:
            from app.services.people.hr.org_resolver import OrgResolver

            resolver = OrgResolver(self.db)
            initial_approver = resolver.get_manager(
                employee.employee_id,
                org_id,
            )
            resolver.notify_hr_for_vacancy_routing_alerts(org_id)

        if initial_approver:
            manager_limit = self._get_approver_max_amount(org_id, initial_approver)
            if manager_limit and manager_limit >= claim_amount:
                steps.append(
                    ApprovalStep(
                        step_number=1,
                        approver_id=initial_approver.employee_id,
                        approver_name=initial_approver.full_name,
                        max_amount=manager_limit,
                        is_escalation=False,
                    )
                )
                seen_approvers.add(initial_approver.employee_id)

        # Step 3: Check if escalation is needed (manager can't approve amount)
        if not steps or escalation_required:
            escalation_approvers = self._get_escalation_approvers(
                org_id, employee, claim_amount, seen_approvers
            )
            for idx, (approver, max_amt, is_esc) in enumerate(
                escalation_approvers, start=len(steps) + 1
            ):
                if approver.employee_id not in seen_approvers:
                    steps.append(
                        ApprovalStep(
                            step_number=idx,
                            approver_id=approver.employee_id,
                            approver_name=approver.full_name,
                            max_amount=max_amt,
                            is_escalation=is_esc,
                        )
                    )
                    seen_approvers.add(approver.employee_id)

        # Step 4: Handle multi-approval requirements
        if multi_approval_required and len(steps) < 2:
            # Need at least 2 approvers for multi-approval
            additional = self._get_additional_approvers(
                org_id, employee, claim_amount, seen_approvers, needed=2 - len(steps)
            )
            for idx, (approver, max_amt) in enumerate(additional, start=len(steps) + 1):
                steps.append(
                    ApprovalStep(
                        step_number=idx,
                        approver_id=approver.employee_id,
                        approver_name=approver.full_name,
                        max_amount=max_amt,
                        is_escalation=False,
                    )
                )

        # Step 5: Apply rule-specific approvers
        for rule in rules:
            if rule.action_config:
                specific_approver_id = rule.action_config.get("approver_id")
                if specific_approver_id:
                    approver = self.db.get(Employee, specific_approver_id)
                    if approver and approver.employee_id not in seen_approvers:
                        steps.append(
                            ApprovalStep(
                                step_number=len(steps) + 1,
                                approver_id=approver.employee_id,
                                approver_name=approver.full_name,
                                max_amount=Decimal(
                                    "999999999"
                                ),  # Rule-specified approvers have no limit
                                is_escalation=False,
                            )
                        )
                        seen_approvers.add(approver.employee_id)

        # Ensure we have at least one approver
        if not steps:
            # Fall back to organization-wide approvers
            fallback = self._get_fallback_approvers(org_id, claim_amount)
            for idx, (approver, max_amt) in enumerate(fallback, start=1):
                steps.append(
                    ApprovalStep(
                        step_number=idx,
                        approver_id=approver.employee_id,
                        approver_name=approver.full_name,
                        max_amount=max_amt,
                        is_escalation=False,
                    )
                )

        return ApprovalChain(
            claim_id=claim.claim_id,
            total_steps=len(steps),
            completed_steps=0,
            current_step=1,
            steps=steps,
            requires_all_approvals=multi_approval_required,
            is_complete=False,
        )

    def _current_submission_round(self, claim_id: UUID) -> int:
        return (
            self.db.scalar(
                select(func.max(ExpenseClaimApprovalStep.submission_round)).where(
                    ExpenseClaimApprovalStep.claim_id == claim_id
                )
            )
            or 0
        )

    def _get_persisted_steps(
        self, claim: ExpenseClaim
    ) -> list[ExpenseClaimApprovalStep]:
        latest_round = self._current_submission_round(claim.claim_id)
        if latest_round <= 0:
            return []
        return list(
            self.db.scalars(
                select(ExpenseClaimApprovalStep)
                .where(
                    ExpenseClaimApprovalStep.claim_id == claim.claim_id,
                    ExpenseClaimApprovalStep.submission_round == latest_round,
                )
                .order_by(
                    ExpenseClaimApprovalStep.step_number,
                    ExpenseClaimApprovalStep.created_at,
                )
            ).all()
        )

    def _build_chain_from_persisted_steps(
        self,
        claim: ExpenseClaim,
        persisted_steps: list[ExpenseClaimApprovalStep],
    ) -> ApprovalChain:
        steps = [
            ApprovalStep(
                step_number=row.step_number,
                approver_id=row.approver_id,
                approver_name=row.approver_name,
                max_amount=row.max_amount,
                submission_round=row.submission_round,
                is_escalation=row.is_escalation,
                is_completed=bool(row.decision),
                decision=row.decision,
                decided_at=row.decided_at,
                notes=row.notes,
            )
            for row in persisted_steps
        ]
        completed_steps = sum(1 for step in steps if step.is_completed)
        requires_all = any(row.requires_all_approvals for row in persisted_steps)
        final_decision = None
        is_complete = False
        if any(step.decision == "REJECTED" for step in steps):
            is_complete = True
            final_decision = "REJECTED"
        elif steps and all(step.decision == "APPROVED" for step in steps):
            is_complete = True
            final_decision = "APPROVED"

        pending_step_numbers = [
            step.step_number for step in steps if not step.is_completed
        ]
        current_step = min(pending_step_numbers) if pending_step_numbers else len(steps)
        return ApprovalChain(
            claim_id=claim.claim_id,
            total_steps=len(steps),
            completed_steps=completed_steps,
            current_step=current_step,
            steps=steps,
            requires_all_approvals=requires_all,
            is_complete=is_complete,
            final_decision=final_decision,
        )

    def _persist_approval_chain(
        self,
        claim: ExpenseClaim,
        chain: ApprovalChain,
        submission_round: int,
    ) -> None:
        rows = [
            ExpenseClaimApprovalStep(
                organization_id=claim.organization_id,
                claim_id=claim.claim_id,
                submission_round=submission_round,
                step_number=step.step_number,
                approver_id=step.approver_id,
                approver_name=step.approver_name,
                max_amount=step.max_amount,
                requires_all_approvals=chain.requires_all_approvals,
                is_escalation=step.is_escalation,
            )
            for step in chain.steps
        ]
        self.db.add_all(rows)
        self.db.flush()

    def check_escalation_needed(
        self,
        claim: ExpenseClaim,
        approver_id: UUID,
    ) -> bool:
        """
        Check if escalation is needed because approver lacks authority.

        Returns True if the claim amount exceeds the approver's authority.
        """
        from app.models.people.hr.employee import Employee

        approver = self.db.get(Employee, approver_id)
        if not approver:
            return True

        claim_amount = claim.total_approved_amount or claim.total_claimed_amount
        max_amount = self._get_approver_max_amount(claim.organization_id, approver)

        if not max_amount:
            return True

        return claim_amount > max_amount

    def escalate_approval(
        self,
        claim: ExpenseClaim,
        current_approver_id: UUID,
        *,
        reason: str | None = None,
    ) -> UUID | None:
        """
        Escalate approval to a higher authority.

        Args:
            claim: The expense claim
            current_approver_id: Current approver who is escalating
            reason: Reason for escalation

        Returns:
            New approver ID if escalation successful, None otherwise
        """
        from app.models.people.hr.employee import Employee, EmployeeStatus

        org_id = claim.organization_id

        # Get current approver's limit config
        approver_limit = self.db.scalar(
            select(ExpenseApproverLimit).where(
                ExpenseApproverLimit.organization_id == org_id,
                ExpenseApproverLimit.scope_type == "EMPLOYEE",
                ExpenseApproverLimit.scope_id == current_approver_id,
                ExpenseApproverLimit.is_active == True,
            )
        )

        # Check for explicit escalation target
        if approver_limit and approver_limit.escalate_to_employee_id:
            return approver_limit.escalate_to_employee_id

        # Fall back to the current approver's position manager
        current_approver = self.db.get(Employee, current_approver_id)
        if current_approver:
            from app.services.people.hr.org_resolver import OrgResolver

            resolver = OrgResolver(self.db)
            manager = resolver.get_manager(
                current_approver.employee_id,
                org_id,
            )
            resolver.notify_hr_for_vacancy_routing_alerts(org_id)
            if manager:
                return manager.employee_id

        # Fall back to grade-based escalation
        if approver_limit and approver_limit.escalate_to_grade_min_rank:
            from app.models.people.hr.grade import Grade

            min_rank = approver_limit.escalate_to_grade_min_rank

            higher_grade_employee = self.db.scalar(
                select(Employee)
                .join(Grade, Employee.grade_id == Grade.grade_id)
                .where(
                    Employee.organization_id == org_id,
                    Grade.rank >= min_rank,
                    Employee.status == EmployeeStatus.ACTIVE,
                    Employee.employee_id != current_approver_id,
                )
                .order_by(Grade.rank)
                .limit(1)
            )
            if higher_grade_employee:
                return higher_grade_employee.employee_id

        return None

    def process_approval_decision(
        self,
        claim: ExpenseClaim,
        approver_id: UUID,
        decision: str,  # "APPROVED" or "REJECTED"
        *,
        notes: str | None = None,
        approved_amounts: list[dict] | None = None,
    ) -> ApprovalChain:
        """
        Process an approval decision for a claim.

        Args:
            claim: The expense claim
            approver_id: ID of the approver making the decision
            decision: "APPROVED" or "REJECTED"
            notes: Optional notes/comments
            approved_amounts: Optional per-item approved amounts

        Returns:
            Updated ApprovalChain
        """
        persisted_steps = self._get_persisted_steps(claim)
        if not persisted_steps:
            chain = self.initialize_approval_chain(claim)
            persisted_steps = self._get_persisted_steps(claim)
        else:
            chain = self._build_chain_from_persisted_steps(claim, persisted_steps)

        pending_steps = [row for row in persisted_steps if not row.decision]
        if not pending_steps:
            raise ValueError("Claim has no pending approval steps")

        allowed_approver_ids = set(chain.current_approvers)
        if approver_id not in allowed_approver_ids:
            raise ValueError("Approver is not assigned to the current approval step")

        step_row = next(
            (
                row
                for row in pending_steps
                if row.approver_id == approver_id
                and (
                    chain.requires_all_approvals
                    or row.step_number == chain.current_step
                )
            ),
            None,
        )
        if step_row is None:
            raise ValueError("Approver is not assigned to the current approval step")

        step_row.decision = decision
        step_row.decided_at = datetime.now(UTC)
        step_row.notes = notes
        self.db.flush()
        return self.get_approval_chain(claim, force_rebuild=False)

    # =========================================================================
    # Receipt Validation
    # =========================================================================

    def validate_receipt_requirements(
        self,
        claim: ExpenseClaim,
    ) -> ReceiptValidationResult:
        """
        Validate that all receipt requirements are met for a claim.

        Checks each item against its category's requires_receipt setting.

        Args:
            claim: The expense claim to validate

        Returns:
            ReceiptValidationResult with validation status and issues
        """
        missing_receipts = []
        warnings = []

        for item in claim.items:
            # Load category
            category = (
                self.db.get(ExpenseCategory, item.category_id)
                if item.category_id
                else None
            )

            # Check if receipt is required unless category is explicitly exempted.
            if (
                category is not None
                and category.requires_receipt
                and not self._is_receipt_optional_category(category)
            ):
                if not item.receipt_url and not item.receipt_number:
                    missing_receipts.append(
                        f"Item '{item.description[:50]}' ({category.category_name}): Receipt required"
                    )

            # Check amount limits
            if category and category.max_amount_per_claim:
                if item.claimed_amount > category.max_amount_per_claim:
                    warnings.append(
                        f"Item '{item.description[:50]}' exceeds category limit "
                        f"({item.claimed_amount} > {category.max_amount_per_claim})"
                    )

        return ReceiptValidationResult(
            is_valid=len(missing_receipts) == 0,
            missing_receipts=missing_receipts,
            warnings=warnings,
        )

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _get_triggered_rules(
        self,
        claim: ExpenseClaim,
        employee: Employee,
    ) -> list[ExpenseLimitRule]:
        """Get limit rules that were triggered for this claim."""
        from app.services.expense.limit_service import ExpenseLimitService

        service = ExpenseLimitService(self.db, self.ctx)
        rules = service.get_applicable_rules(
            claim.organization_id, employee, claim.claim_date
        )

        claim_amount = claim.total_approved_amount or claim.total_claimed_amount

        # Filter to rules that are actually triggered
        triggered = []
        for rule in rules:
            if claim_amount > rule.limit_amount:
                triggered.append(rule)

        return triggered

    def _get_approver_max_amount(
        self,
        organization_id: UUID,
        employee: Employee,
    ) -> Decimal | None:
        """Get the maximum amount an employee can approve."""
        # Check employee-specific limit
        emp_limit = self.db.scalar(
            select(ExpenseApproverLimit.max_approval_amount).where(
                ExpenseApproverLimit.organization_id == organization_id,
                ExpenseApproverLimit.is_active == True,
                ExpenseApproverLimit.scope_type == "EMPLOYEE",
                ExpenseApproverLimit.scope_id == employee.employee_id,
            )
        )
        if emp_limit:
            return emp_limit

        # Check grade-based limit
        if employee.grade_id:
            grade_limit = self.db.scalar(
                select(ExpenseApproverLimit.max_approval_amount).where(
                    ExpenseApproverLimit.organization_id == organization_id,
                    ExpenseApproverLimit.is_active == True,
                    ExpenseApproverLimit.scope_type == "GRADE",
                    ExpenseApproverLimit.scope_id == employee.grade_id,
                )
            )
            if grade_limit:
                return grade_limit

        # Check designation-based limit
        if employee.designation_id:
            desig_limit = self.db.scalar(
                select(ExpenseApproverLimit.max_approval_amount).where(
                    ExpenseApproverLimit.organization_id == organization_id,
                    ExpenseApproverLimit.is_active == True,
                    ExpenseApproverLimit.scope_type == "DESIGNATION",
                    ExpenseApproverLimit.scope_id == employee.designation_id,
                )
            )
            if desig_limit:
                return desig_limit

        return None

    def _get_escalation_approvers(
        self,
        organization_id: UUID,
        employee: Employee,
        amount: Decimal,
        exclude_ids: set,
    ) -> list[tuple]:
        """Get approvers through escalation chain."""
        from app.models.people.hr.employee import Employee as EmployeeModel
        from app.services.people.hr.org_resolver import OrgResolver

        result = []

        # Walk up the position chain, starting from expense approver if set
        base_employee = employee
        if employee.expense_approver_id:
            base = self.db.get(EmployeeModel, employee.expense_approver_id)
            if base:
                base_employee = base

        resolver = OrgResolver(self.db)
        chain = resolver.get_approval_chain(
            base_employee.employee_id,
            organization_id,
        )
        resolver.notify_hr_for_vacancy_routing_alerts(organization_id)
        for manager in chain[:5]:  # Max 5 levels of escalation
            if manager.employee_id in exclude_ids:
                continue

            max_amt = self._get_approver_max_amount(organization_id, manager)
            if max_amt and max_amt >= amount:
                result.append((manager, max_amt, True))
                break

            # Manager can't approve, add and continue
            if max_amt:
                result.append((manager, max_amt, True))
                exclude_ids.add(manager.employee_id)

        return result

    def _get_additional_approvers(
        self,
        organization_id: UUID,
        employee: Employee,
        amount: Decimal,
        exclude_ids: set,
        needed: int,
    ) -> list[tuple]:
        """Get additional approvers for multi-approval requirements."""
        from app.models.people.hr.employee import Employee as EmployeeModel
        from app.models.people.hr.employee import EmployeeStatus

        result = []

        # Find approvers with sufficient authority
        approvers = self.db.execute(
            select(ExpenseApproverLimit, EmployeeModel)
            .join(
                EmployeeModel,
                ExpenseApproverLimit.scope_id == EmployeeModel.employee_id,
            )
            .where(
                ExpenseApproverLimit.organization_id == organization_id,
                ExpenseApproverLimit.is_active == True,
                ExpenseApproverLimit.scope_type == "EMPLOYEE",
                ExpenseApproverLimit.max_approval_amount >= amount,
                EmployeeModel.status == EmployeeStatus.ACTIVE,
                ~EmployeeModel.employee_id.in_(exclude_ids),
                EmployeeModel.employee_id != employee.employee_id,
            )
            .order_by(ExpenseApproverLimit.max_approval_amount)
            .limit(needed)
        ).all()

        for limit, approver in approvers:
            result.append((approver, limit.max_approval_amount))

        return result

    def _get_fallback_approvers(
        self,
        organization_id: UUID,
        amount: Decimal,
    ) -> list[tuple]:
        """Get fallback approvers when no specific chain is available."""
        from app.models.people.hr.employee import Employee as EmployeeModel
        from app.models.people.hr.employee import EmployeeStatus

        result = []

        # Find any approvers with sufficient authority
        approvers = self.db.execute(
            select(ExpenseApproverLimit, EmployeeModel)
            .join(
                EmployeeModel,
                ExpenseApproverLimit.scope_id == EmployeeModel.employee_id,
            )
            .where(
                ExpenseApproverLimit.organization_id == organization_id,
                ExpenseApproverLimit.is_active == True,
                ExpenseApproverLimit.scope_type == "EMPLOYEE",
                ExpenseApproverLimit.max_approval_amount >= amount,
                EmployeeModel.status == EmployeeStatus.ACTIVE,
            )
            .order_by(ExpenseApproverLimit.max_approval_amount)
            .limit(3)
        ).all()

        for limit, approver in approvers:
            result.append((approver, limit.max_approval_amount))

        return result

    # =========================================================================
    # Employment Type Filtering
    # =========================================================================

    def filter_limits_by_employment_type(
        self,
        organization_id: UUID,
        employee: Employee,
    ) -> list[ExpenseLimitRule]:
        """
        Get expense limit rules filtered by employee's employment type.

        Rules can specify employment_type in dimension_filters to only
        apply to certain types (e.g., PERMANENT, CONTRACT, INTERN).
        """
        today = date.today()

        # Get all active rules for organization
        all_rules = list(
            self.db.scalars(
                select(ExpenseLimitRule).where(
                    ExpenseLimitRule.organization_id == organization_id,
                    ExpenseLimitRule.is_active == True,
                    ExpenseLimitRule.effective_from <= today,
                    or_(
                        ExpenseLimitRule.effective_to.is_(None),
                        ExpenseLimitRule.effective_to >= today,
                    ),
                )
            ).all()
        )

        employee_type_code = None
        if employee.employment_type_id is not None:
            employee_type_code = (
                EmploymentTypeService(self.db, organization_id)
                .get_employment_type(employee.employment_type_id)
                .type_code.strip()
                .upper()
            )
        filtered = []

        for rule in all_rules:
            if not rule.dimension_filters:
                filtered.append(rule)
                continue

            # Check employment_type filter
            configured_codes = rule.dimension_filters.get("employment_types", [])
            if not configured_codes:
                filtered.append(rule)
                continue

            normalized_codes = {
                str(code).strip().upper()
                for code in configured_codes
                if str(code).strip()
            }
            if employee_type_code in normalized_codes:
                filtered.append(rule)

        return filtered


# Module-level instance
expense_approval_service = ExpenseApprovalService
