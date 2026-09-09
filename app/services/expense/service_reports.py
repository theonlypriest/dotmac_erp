"""Expense reporting and dashboard data operations."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import extract, func, or_, select

from app.models.expense import (
    CashAdvance,
    CashAdvanceStatus,
    ExpenseCategory,
    ExpenseClaim,
    ExpenseClaimApprovalStep,
    ExpenseClaimItem,
    ExpenseClaimStatus,
)
from app.services.expense.service_common import (
    REPORTABLE_EXPENSE_CLAIM_STATUSES,
    ExpenseServiceBase,
)


class ExpenseReportingMixin(ExpenseServiceBase):
    def get_expense_stats(self, org_id: UUID) -> dict:
        today = date.today()
        month_start = today.replace(day=1)
        pending_statuses = [
            ExpenseClaimStatus.SUBMITTED,
            ExpenseClaimStatus.PENDING_APPROVAL,
        ]

        pending_claims = (
            self.db.scalar(
                select(func.count(ExpenseClaim.claim_id)).where(
                    ExpenseClaim.organization_id == org_id,
                    ExpenseClaim.status.in_(pending_statuses),
                )
            )
            or 0
        )
        total_pending = self.db.scalar(
            select(func.sum(ExpenseClaim.total_claimed_amount)).where(
                ExpenseClaim.organization_id == org_id,
                ExpenseClaim.status.in_(pending_statuses),
            )
        ) or Decimal("0")
        claims_this_month = (
            self.db.scalar(
                select(func.count(ExpenseClaim.claim_id)).where(
                    ExpenseClaim.organization_id == org_id,
                    ExpenseClaim.claim_date >= month_start,
                )
            )
            or 0
        )
        amount_this_month = self.db.scalar(
            select(func.sum(ExpenseClaim.total_claimed_amount)).where(
                ExpenseClaim.organization_id == org_id,
                ExpenseClaim.claim_date >= month_start,
            )
        ) or Decimal("0")
        outstanding_advances = (
            self.db.scalar(
                select(func.count(CashAdvance.advance_id)).where(
                    CashAdvance.organization_id == org_id,
                    CashAdvance.status == CashAdvanceStatus.DISBURSED,
                )
            )
            or 0
        )
        advance_amount = self.db.scalar(
            select(
                func.sum(
                    CashAdvance.approved_amount
                    - CashAdvance.amount_settled
                    - CashAdvance.amount_refunded
                )
            ).where(
                CashAdvance.organization_id == org_id,
                CashAdvance.status == CashAdvanceStatus.DISBURSED,
            )
        ) or Decimal("0")
        return {
            "pending_claims": pending_claims,
            "total_pending_amount": total_pending,
            "claims_this_month": claims_this_month,
            "amount_this_month": amount_this_month,
            "outstanding_advances": outstanding_advances,
            "advance_outstanding_amount": advance_amount,
        }

    def get_employee_expense_summary(
        self,
        org_id: UUID,
        employee_id: UUID,
        *,
        year: int | None = None,
        month: int | None = None,
    ) -> dict:
        today = date.today()
        target_year = year or today.year
        target_month = month or today.month
        period_start = date(target_year, target_month, 1)
        period_end = (
            date(target_year + 1, 1, 1)
            if target_month == 12
            else date(target_year, target_month + 1, 1)
        )

        claims_in_period = (
            self.db.scalar(
                select(func.count(ExpenseClaim.claim_id)).where(
                    ExpenseClaim.organization_id == org_id,
                    ExpenseClaim.employee_id == employee_id,
                    ExpenseClaim.claim_date >= period_start,
                    ExpenseClaim.claim_date < period_end,
                )
            )
            or 0
        )
        total_claimed = self.db.scalar(
            select(func.sum(ExpenseClaim.total_claimed_amount)).where(
                ExpenseClaim.organization_id == org_id,
                ExpenseClaim.employee_id == employee_id,
                ExpenseClaim.claim_date >= period_start,
                ExpenseClaim.claim_date < period_end,
            )
        ) or Decimal("0")
        total_approved = self.db.scalar(
            select(func.sum(ExpenseClaim.total_approved_amount)).where(
                ExpenseClaim.organization_id == org_id,
                ExpenseClaim.employee_id == employee_id,
                ExpenseClaim.status == ExpenseClaimStatus.APPROVED,
                ExpenseClaim.claim_date >= period_start,
                ExpenseClaim.claim_date < period_end,
            )
        ) or Decimal("0")
        pending_claims = (
            self.db.scalar(
                select(func.count(ExpenseClaim.claim_id)).where(
                    ExpenseClaim.organization_id == org_id,
                    ExpenseClaim.employee_id == employee_id,
                    ExpenseClaim.status.in_(
                        [
                            ExpenseClaimStatus.SUBMITTED,
                            ExpenseClaimStatus.PENDING_APPROVAL,
                        ]
                    ),
                )
            )
            or 0
        )
        outstanding_advances = self.db.scalar(
            select(
                func.sum(
                    CashAdvance.approved_amount
                    - CashAdvance.amount_settled
                    - CashAdvance.amount_refunded
                )
            ).where(
                CashAdvance.organization_id == org_id,
                CashAdvance.employee_id == employee_id,
                CashAdvance.status == CashAdvanceStatus.DISBURSED,
            )
        ) or Decimal("0")
        return {
            "year": target_year,
            "month": target_month,
            "claims_in_period": claims_in_period,
            "total_claimed": total_claimed,
            "total_approved": total_approved,
            "pending_claims": pending_claims,
            "outstanding_advances": outstanding_advances,
        }

    def get_expense_summary_report(
        self,
        org_id: UUID,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> dict:
        today = date.today()
        start_date = start_date or today.replace(day=1)
        end_date = end_date or today
        base_filters = [
            ExpenseClaim.organization_id == org_id,
            ExpenseClaim.claim_date >= start_date,
            ExpenseClaim.claim_date <= end_date,
        ]
        reportable_filters = [
            *base_filters,
            ExpenseClaim.status.in_(REPORTABLE_EXPENSE_CLAIM_STATUSES),
        ]

        total_claims = (
            self.db.scalar(
                select(func.count(ExpenseClaim.claim_id)).where(*reportable_filters)
            )
            or 0
        )
        total_claimed = self.db.scalar(
            select(func.sum(ExpenseClaim.total_claimed_amount)).where(
                *reportable_filters
            )
        ) or Decimal("0")

        status_breakdown = []
        for status in ExpenseClaimStatus:
            count = (
                self.db.scalar(
                    select(func.count(ExpenseClaim.claim_id)).where(
                        *base_filters,
                        ExpenseClaim.status == status,
                    )
                )
                or 0
            )
            amount = self.db.scalar(
                select(func.sum(ExpenseClaim.total_claimed_amount)).where(
                    *base_filters,
                    ExpenseClaim.status == status,
                )
            ) or Decimal("0")
            if count > 0:
                status_breakdown.append(
                    {"status": status.value, "count": count, "amount": amount}
                )

        approved_count = (
            self.db.scalar(
                select(func.count(ExpenseClaim.claim_id)).where(
                    *base_filters,
                    ExpenseClaim.status.in_(
                        [ExpenseClaimStatus.APPROVED, ExpenseClaimStatus.PAID]
                    ),
                )
            )
            or 0
        )
        approved_amount = self.db.scalar(
            select(func.sum(ExpenseClaim.total_approved_amount)).where(
                *base_filters,
                ExpenseClaim.status.in_(
                    [ExpenseClaimStatus.APPROVED, ExpenseClaimStatus.PAID]
                ),
            )
        ) or Decimal("0")
        rejected_count = (
            self.db.scalar(
                select(func.count(ExpenseClaim.claim_id)).where(
                    *base_filters,
                    ExpenseClaim.status == ExpenseClaimStatus.REJECTED,
                )
            )
            or 0
        )
        return {
            "start_date": start_date,
            "end_date": end_date,
            "total_claims": total_claims,
            "total_claimed": total_claimed,
            "approved_count": approved_count,
            "approved_amount": approved_amount,
            "rejected_count": rejected_count,
            "status_breakdown": status_breakdown,
        }

    def get_expense_by_category_report(
        self,
        org_id: UUID,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> dict:
        today = date.today()
        start_date = start_date or today.replace(day=1)
        end_date = end_date or today

        results = self.db.execute(
            select(
                ExpenseCategory.category_code,
                ExpenseCategory.category_name,
                func.count(ExpenseClaimItem.item_id).label("item_count"),
                func.sum(ExpenseClaimItem.claimed_amount).label("claimed_amount"),
                func.sum(ExpenseClaimItem.approved_amount).label("approved_amount"),
            )
            .join(
                ExpenseClaimItem,
                ExpenseClaimItem.category_id == ExpenseCategory.category_id,
            )
            .join(ExpenseClaim, ExpenseClaim.claim_id == ExpenseClaimItem.claim_id)
            .where(
                ExpenseClaim.organization_id == org_id,
                ExpenseClaim.claim_date >= start_date,
                ExpenseClaim.claim_date <= end_date,
                ExpenseClaim.status.in_(REPORTABLE_EXPENSE_CLAIM_STATUSES),
            )
            .group_by(
                ExpenseCategory.category_id,
                ExpenseCategory.category_code,
                ExpenseCategory.category_name,
            )
            .order_by(func.sum(ExpenseClaimItem.claimed_amount).desc())
        ).all()

        categories = []
        total_claimed = Decimal("0")
        total_approved = Decimal("0")
        for row in results:
            claimed = row.claimed_amount or Decimal("0")
            approved = row.approved_amount or Decimal("0")
            categories.append(
                {
                    "category_code": row.category_code,
                    "category_name": row.category_name,
                    "item_count": row.item_count,
                    "claimed_amount": claimed,
                    "approved_amount": approved,
                }
            )
            total_claimed += claimed
            total_approved += approved

        for category in categories:
            category["percentage"] = (
                float(category["claimed_amount"] / total_claimed * 100)
                if total_claimed > 0
                else 0.0
            )
        return {
            "start_date": start_date,
            "end_date": end_date,
            "categories": categories,
            "total_claimed": total_claimed,
            "total_approved": total_approved,
        }

    def get_expense_by_employee_report(
        self,
        org_id: UUID,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        department_id: UUID | None = None,
    ) -> dict:
        from app.models.people.hr.department import Department
        from app.models.people.hr.employee import Employee
        from app.models.person import Person

        today = date.today()
        start_date = start_date or today.replace(day=1)
        end_date = end_date or today
        employee_name_col = Person.name_expr().label("employee_name")
        query = (
            select(
                Employee.employee_id,
                employee_name_col,
                Department.department_name.label("department_name"),
                func.count(ExpenseClaim.claim_id).label("claim_count"),
                func.sum(ExpenseClaim.total_claimed_amount).label("claimed_amount"),
                func.sum(ExpenseClaim.total_approved_amount).label("approved_amount"),
            )
            .join(ExpenseClaim, ExpenseClaim.employee_id == Employee.employee_id)
            .join(Person, Person.id == Employee.person_id)
            .outerjoin(Department, Employee.department_id == Department.department_id)
            .where(
                ExpenseClaim.organization_id == org_id,
                ExpenseClaim.claim_date >= start_date,
                ExpenseClaim.claim_date <= end_date,
                ExpenseClaim.status.in_(REPORTABLE_EXPENSE_CLAIM_STATUSES),
            )
        )
        if department_id:
            query = query.where(Employee.department_id == department_id)

        results = self.db.execute(
            query.group_by(
                Employee.employee_id,
                employee_name_col,
                Department.department_name,
            ).order_by(func.sum(ExpenseClaim.total_claimed_amount).desc())
        ).all()

        employees = []
        total_claimed = Decimal("0")
        total_approved = Decimal("0")
        for row in results:
            claimed = row.claimed_amount or Decimal("0")
            approved = row.approved_amount or Decimal("0")
            employees.append(
                {
                    "employee_id": str(row.employee_id),
                    "employee_name": row.employee_name,
                    "department_name": row.department_name or "No Department",
                    "claim_count": row.claim_count,
                    "claimed_amount": claimed,
                    "approved_amount": approved,
                }
            )
            total_claimed += claimed
            total_approved += approved
        return {
            "start_date": start_date,
            "end_date": end_date,
            "employees": employees,
            "total_claimed": total_claimed,
            "total_approved": total_approved,
        }

    def get_expense_trends_report(self, org_id: UUID, *, months: int = 12) -> dict:
        from dateutil.relativedelta import relativedelta  # type: ignore[import-untyped]

        today = date.today()
        end_date = today.replace(day=1)
        start_date = end_date - relativedelta(months=months - 1)
        year_bucket = extract("year", ExpenseClaim.claim_date)
        month_bucket = extract("month", ExpenseClaim.claim_date)
        results = self.db.execute(
            select(
                year_bucket.label("year"),
                month_bucket.label("month"),
                func.count(ExpenseClaim.claim_id).label("claim_count"),
                func.sum(ExpenseClaim.total_claimed_amount).label("claimed_amount"),
                func.sum(ExpenseClaim.total_approved_amount).label("approved_amount"),
            )
            .where(
                ExpenseClaim.organization_id == org_id,
                ExpenseClaim.claim_date >= start_date,
                ExpenseClaim.claim_date <= today,
                ExpenseClaim.status.in_(REPORTABLE_EXPENSE_CLAIM_STATUSES),
            )
            .group_by(year_bucket, month_bucket)
            .order_by(year_bucket, month_bucket)
        ).all()

        monthly_data = {}
        for row in results:
            month_date = date(int(row.year), int(row.month), 1)
            month_key = month_date.strftime("%Y-%m")
            monthly_data[month_key] = {
                "month": month_key,
                "month_label": month_date.strftime("%b %Y"),
                "claim_count": row.claim_count,
                "claimed_amount": row.claimed_amount or Decimal("0"),
                "approved_amount": row.approved_amount or Decimal("0"),
            }

        months_list = []
        current = start_date
        while current <= today:
            month_key = current.strftime("%Y-%m")
            months_list.append(
                monthly_data.get(
                    month_key,
                    {
                        "month": month_key,
                        "month_label": current.strftime("%b %Y"),
                        "claim_count": 0,
                        "claimed_amount": Decimal("0"),
                        "approved_amount": Decimal("0"),
                    },
                )
            )
            current = current + relativedelta(months=1)

        total_claimed = sum(month["claimed_amount"] for month in months_list)
        total_approved = sum(month["approved_amount"] for month in months_list)
        num_months = len(months_list)
        average_monthly = total_claimed / num_months if num_months > 0 else Decimal("0")
        return {
            "months": months_list,
            "total_months": num_months,
            "total_claimed": total_claimed,
            "total_approved": total_approved,
            "average_monthly": average_monthly,
        }

    def get_my_approvals_report(
        self,
        org_id: UUID,
        *,
        approver_id: UUID,
        start_date: date | None = None,
        end_date: date | None = None,
        decision: str | None = None,
        claim_status: str | ExpenseClaimStatus | None = None,
        employee_id: UUID | None = None,
        include_pending: bool = False,
        filter_by_claim_date: bool = False,
        use_default_date_range: bool = True,
        limit: int = 200,
    ) -> dict:
        from app.models.people.hr.employee import Employee
        from app.models.person import Person

        today = date.today()
        if use_default_date_range:
            start_date = start_date or today.replace(day=1)
            end_date = end_date or today

        decision = decision.upper() if decision else None
        if decision and decision not in {"APPROVED", "REJECTED", "PENDING"}:
            raise ValueError("Invalid decision")

        if isinstance(claim_status, ExpenseClaimStatus):
            claim_status_value = claim_status
        elif claim_status:
            try:
                claim_status_value = ExpenseClaimStatus(claim_status.upper())
            except ValueError as exc:
                raise ValueError("Invalid claim status") from exc
        else:
            claim_status_value = None

        decision_conditions = [
            ExpenseClaimApprovalStep.decision.in_(["APPROVED", "REJECTED"])
        ]
        if include_pending:
            decision_conditions.append(ExpenseClaimApprovalStep.decision.is_(None))

        filters = [
            ExpenseClaim.organization_id == org_id,
            ExpenseClaimApprovalStep.approver_id == approver_id,
            or_(*decision_conditions),
        ]

        if decision:
            if decision == "PENDING":
                filters.append(ExpenseClaimApprovalStep.decision.is_(None))
            else:
                filters.append(ExpenseClaimApprovalStep.decision == decision)
                filters.append(ExpenseClaimApprovalStep.decided_at.is_not(None))
        elif not include_pending:
            filters.append(ExpenseClaimApprovalStep.decided_at.is_not(None))

        if claim_status_value is not None:
            filters.append(ExpenseClaim.status == claim_status_value)
        if employee_id is not None:
            filters.append(ExpenseClaim.employee_id == employee_id)

        date_column = (
            ExpenseClaim.claim_date
            if filter_by_claim_date
            else func.date(ExpenseClaimApprovalStep.decided_at)
        )
        if start_date is not None:
            filters.append(date_column >= start_date)
        if end_date is not None:
            filters.append(date_column <= end_date)

        activity_at = func.coalesce(
            ExpenseClaimApprovalStep.decided_at,
            ExpenseClaim.updated_at,
            ExpenseClaim.created_at,
        ).label("activity_at")
        employee_rows = self.db.execute(
            select(
                ExpenseClaim.employee_id,
                Person.name_expr().label("claimant_name"),
            )
            .join(
                ExpenseClaim,
                ExpenseClaim.claim_id == ExpenseClaimApprovalStep.claim_id,
            )
            .outerjoin(Employee, Employee.employee_id == ExpenseClaim.employee_id)
            .outerjoin(Person, Person.id == Employee.person_id)
            .where(
                ExpenseClaim.organization_id == org_id,
                ExpenseClaimApprovalStep.approver_id == approver_id,
                or_(*decision_conditions),
            )
            .distinct()
            .order_by(Person.name_expr())
        ).all()
        employee_options = [
            {
                "employee_id": str(row.employee_id),
                "name": row.claimant_name or "Unknown",
            }
            for row in employee_rows
        ]
        rows = self.db.execute(
            select(
                activity_at,
                ExpenseClaimApprovalStep.decided_at.label("action_at"),
                ExpenseClaimApprovalStep.decision.label("action_type"),
                ExpenseClaim.employee_id,
                ExpenseClaim.claim_id,
                ExpenseClaim.claim_number,
                ExpenseClaim.claim_date,
                ExpenseClaim.status,
                ExpenseClaim.purpose,
                ExpenseClaim.currency_code,
                ExpenseClaim.total_claimed_amount,
                ExpenseClaim.total_approved_amount,
                Person.name_expr().label("claimant_name"),
            )
            .join(
                ExpenseClaim,
                ExpenseClaim.claim_id == ExpenseClaimApprovalStep.claim_id,
            )
            .outerjoin(Employee, Employee.employee_id == ExpenseClaim.employee_id)
            .outerjoin(Person, Person.id == Employee.person_id)
            .where(*filters)
            .order_by(
                activity_at.desc(),
                ExpenseClaim.claim_date.desc(),
                ExpenseClaim.claim_number.desc(),
            )
            .limit(limit)
        ).all()

        decisions = []
        approved_count = 0
        rejected_count = 0
        pending_count = 0
        paid_count = 0
        approved_total = Decimal("0")
        rejected_total = Decimal("0")
        pending_total = Decimal("0")
        paid_total = Decimal("0")
        for row in rows:
            action_type = row.action_type or "PENDING"
            claimed_amount = row.total_claimed_amount or Decimal("0")
            approved_amount = row.total_approved_amount or Decimal("0")
            status_value = getattr(row.status, "value", row.status)
            if action_type == "APPROVED":
                approved_count += 1
                approved_total += approved_amount
            elif action_type == "REJECTED":
                rejected_count += 1
                rejected_total += claimed_amount
            else:
                pending_count += 1
                pending_total += claimed_amount
            if status_value == ExpenseClaimStatus.PAID.value:
                paid_count += 1
                paid_total += approved_amount

            claimant_name = row.claimant_name or "Unknown"
            decisions.append(
                {
                    "activity_at": row.activity_at,
                    "action_at": row.action_at,
                    "action_type": action_type,
                    "employee_id": row.employee_id,
                    "claim_id": row.claim_id,
                    "claim_number": row.claim_number,
                    "claim_date": row.claim_date,
                    "status": status_value,
                    "purpose": row.purpose,
                    "claimant_name": claimant_name,
                    "currency_code": self._resolve_currency_code(
                        org_id, row.currency_code
                    ),
                    "claimed_amount": claimed_amount,
                    "approved_amount": approved_amount,
                }
            )
        return {
            "start_date": start_date,
            "end_date": end_date,
            "decisions": decisions,
            "approved_count": approved_count,
            "rejected_count": rejected_count,
            "pending_count": pending_count,
            "paid_count": paid_count,
            "approved_total": approved_total,
            "rejected_total": rejected_total,
            "pending_total": pending_total,
            "paid_total": paid_total,
            "employee_options": employee_options,
        }
