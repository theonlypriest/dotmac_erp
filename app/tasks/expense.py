"""
Expense Module Background Tasks - Celery tasks for expense workflows.

Handles:
- Period usage cache refresh
- Pending approval reminders
- Batch expense posting
- Expense analytics calculations
"""

import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

try:
    from datetime import UTC  # type: ignore
except ImportError:  # pragma: no cover
    UTC = timezone.utc

from typing import Any

from celery import shared_task
from sqlalchemy import select

from app.db.session_context import for_each_organization, session_for_org
from app.models.expense import (
    ExpenseClaim,
    ExpenseClaimStatus,
    LimitPeriodType,
)
from app.models.people.hr.employee import Employee, EmployeeStatus
from app.services.people.hr.org_resolver import OrgResolver
from app.services.notification import NotificationService
from app.tenant_catalog import active_organization_ids, organization_ids

logger = logging.getLogger(__name__)

AFRICA_LAGOS_TZ = ZoneInfo("Africa/Lagos")


@shared_task
def refresh_period_usage_cache(organization_id: str | None = None) -> dict:
    """
    Refresh period usage cache for expense limits.

    Recalculates period totals for all active employees to ensure
    limit evaluations are accurate without expensive real-time queries.

    Args:
        organization_id: Optional org ID. If None, refreshes all organizations.

    Returns:
        Dict with refresh statistics
    """
    from app.services.expense.limit_service import ExpenseLimitService

    logger.info("Starting period usage cache refresh")

    results: dict[str, Any] = {
        "organizations_processed": 0,
        "employees_refreshed": 0,
        "errors": [],
    }

    org_ids = active_organization_ids(
        only=uuid.UUID(organization_id) if organization_id else None
    )

    for org_id in org_ids:
        try:
            with session_for_org(org_id) as db:
                # Get all active employees
                employees = db.scalars(
                    select(Employee).where(
                        Employee.organization_id == org_id,
                        Employee.status == EmployeeStatus.ACTIVE,
                    )
                ).all()

                limit_service = ExpenseLimitService(db)

                for employee in employees:
                    try:
                        for period_type in [
                            LimitPeriodType.DAY,
                            LimitPeriodType.WEEK,
                            LimitPeriodType.MONTH,
                            LimitPeriodType.QUARTER,
                            LimitPeriodType.YEAR,
                        ]:
                            limit_service.refresh_usage_cache(
                                org_id,
                                employee.employee_id,
                                period_type,
                            )

                        results["employees_refreshed"] += 1

                    except Exception as e:
                        logger.error(
                            "Failed to refresh usage for employee %s: %s",
                            employee.employee_id,
                            e,
                        )
                        results["errors"].append(
                            {
                                "employee_id": str(employee.employee_id),
                                "error": str(e),
                            }
                        )

                db.commit()
            results["organizations_processed"] += 1

        except Exception as e:
            logger.error(
                "Failed to process organization %s: %s",
                org_id,
                e,
            )
            results["errors"].append(
                {
                    "organization_id": str(org_id),
                    "error": str(e),
                }
            )

    logger.info(
        "Period usage cache refresh complete: %d orgs, %d employees",
        results["organizations_processed"],
        results["employees_refreshed"],
    )

    return results


@shared_task
def process_expense_approval_reminders() -> dict:
    """
    Send reminders for pending expense approvals.

    Checks for claims that have been pending for configured thresholds
    and sends reminder emails to approvers.

    Default reminder thresholds:
    - First reminder: 3 days
    - Second reminder: 7 days
    - Escalation warning: 14 days

    Returns:
        Dict with reminder statistics
    """
    from app.models.notification import (
        EntityType,
        NotificationChannel,
        NotificationType,
    )
    from app.services.expense.expense_notifications import ExpenseNotificationService

    FIRST_REMINDER_DAYS = 3
    SECOND_REMINDER_DAYS = 7
    ESCALATION_WARNING_DAYS = 14

    logger.info("Processing expense approval reminders")

    results: dict[str, Any] = {
        "first_reminders_sent": 0,
        "second_reminders_sent": 0,
        "escalation_warnings_sent": 0,
        "errors": [],
    }

    today = date.today()
    today_start = datetime.combine(today, datetime.min.time())

    # Fan out over tenants rather than scanning across them. The old shape read
    # every tenant's submitted claims through `cross_org_session`, grouped the
    # ids by organization, then re-fetched each group inside a tenant session.
    # `cross_org_session` lifts only the SQLAlchemy listener, never PostgreSQL
    # RLS, so that first read returns zero rows under `app_user` — and an
    # approval-reminder job that finds no pending claims looks exactly like a
    # fleet with none. The grouping dict and the id re-fetch are deleted rather
    # than adapted: the same SELECT inside the tenant session already returns
    # only this tenant's claims, so both existed solely to serve the cross-tenant
    # read. `include_inactive=True` keeps the old reach — that scan had no
    # `Organization` predicate.
    for _org_id, db in for_each_organization(include_inactive=True):
        # Find this tenant's pending claims. PENDING_APPROVAL is unused —
        # all claims awaiting approval are in SUBMITTED status.
        pending_claims = db.scalars(
            select(ExpenseClaim)
            .where(ExpenseClaim.status == ExpenseClaimStatus.SUBMITTED)
            .order_by(ExpenseClaim.claim_date)
        ).all()

        notification_service = ExpenseNotificationService(db)

        for claim in pending_claims:
            try:
                # Use updated_at (set on status change to SUBMITTED) instead
                # of claim_date (the expense incurrence date) for accuracy.
                pending_since = (
                    claim.updated_at.date() if claim.updated_at else claim.claim_date
                )
                days_pending = (today - pending_since).days

                # Determine reminder type
                if days_pending >= ESCALATION_WARNING_DAYS:
                    reminder_type = "escalation"
                elif days_pending >= SECOND_REMINDER_DAYS:
                    reminder_type = "second"
                elif days_pending >= FIRST_REMINDER_DAYS:
                    reminder_type = "first"
                else:
                    continue  # Too early for reminder

                # Dedup: skip if reminder already sent today for this claim.
                if NotificationService().was_sent_since(
                    db,
                    organization_id=claim.organization_id,
                    entity_type=EntityType.EXPENSE,
                    entity_id=claim.claim_id,
                    notification_type=NotificationType.REMINDER,
                    since=today_start,
                ):
                    continue

                # Get approver
                approver = None
                if claim.approver_id:
                    approver = db.get(Employee, claim.approver_id)

                # Fall back to employee's expense approver, then manager
                if not approver and claim.employee:
                    if claim.employee.expense_approver_id:
                        approver = db.get(Employee, claim.employee.expense_approver_id)
                    if not approver:
                        approver = OrgResolver(db).get_manager(
                            claim.employee.employee_id,
                            claim.organization_id,
                        )

                if not approver:
                    continue  # No approver to remind

                # Send reminder
                success = notification_service.send_pending_approval_reminder(
                    claim,
                    approver,
                    days_pending=days_pending,
                )

                # Record the event regardless of the direct SMTP result. A
                # failed direct send remains pending for the central worker;
                # a successful send is marked delivered to prevent a duplicate.
                notification = NotificationService().create_if_not_sent_since(
                    db,
                    organization_id=claim.organization_id,
                    recipient_id=approver.person_id,
                    entity_type=EntityType.EXPENSE,
                    entity_id=claim.claim_id,
                    notification_type=NotificationType.REMINDER,
                    title=f"Expense Approval Reminder: {claim.claim_number}",
                    message=f"Claim {claim.claim_number} pending {days_pending} days",
                    since=today_start,
                    dedup_by_recipient=False,
                    channel=NotificationChannel.EMAIL,
                )
                if success:
                    if notification is not None:
                        notification.email_sent = True
                        notification.email_sent_at = datetime.now(UTC)
                    if reminder_type == "first":
                        results["first_reminders_sent"] += 1
                    elif reminder_type == "second":
                        results["second_reminders_sent"] += 1
                    else:
                        results["escalation_warnings_sent"] += 1

            except Exception as e:
                logger.error(
                    "Failed to process reminder for claim %s: %s",
                    claim.claim_id,
                    e,
                )
                results["errors"].append(
                    {
                        "claim_id": str(claim.claim_id),
                        "error": str(e),
                    }
                )

        db.commit()

    total_sent = (
        results["first_reminders_sent"]
        + results["second_reminders_sent"]
        + results["escalation_warnings_sent"]
    )
    logger.info("Approval reminders complete: %d sent", total_sent)

    return results


@shared_task
def reset_weekly_approver_budgets() -> dict:
    """Reset weekly approver budgets every Monday at 07:00 Africa/Lagos."""

    from app.services.expense.limit_service import ExpenseLimitService

    logger.info("Running scheduled weekly approver budget reset")

    results: dict[str, Any] = {
        "organizations_processed": 0,
        "employees_evaluated": 0,
        "resets_created": 0,
        "already_reset": 0,
        "errors": [],
    }

    now_lagos = datetime.now(AFRICA_LAGOS_TZ)
    current_week_start = (now_lagos - timedelta(days=now_lagos.weekday())).date()
    current_week_end = current_week_start + timedelta(days=6)
    previous_week_start = current_week_start - timedelta(days=7)
    previous_week_end = current_week_end - timedelta(days=7)
    week_window_start = datetime.combine(
        current_week_start, datetime.min.time(), tzinfo=AFRICA_LAGOS_TZ
    )

    org_ids = active_organization_ids()

    for org_id in org_ids:
        try:
            with session_for_org(org_id) as db:
                employees = db.scalars(
                    select(Employee).where(
                        Employee.organization_id == org_id,
                        Employee.status == EmployeeStatus.ACTIVE,
                    )
                ).all()
                limit_service = ExpenseLimitService(db)

                for employee in employees:
                    results["employees_evaluated"] += 1
                    budget_info = limit_service._get_approver_weekly_budget(
                        org_id, employee
                    )
                    if budget_info is None:
                        continue

                    _, approver_limit_id = budget_info
                    already_reset = limit_service.get_latest_weekly_reset(
                        org_id,
                        employee.employee_id,
                        approver_limit_id,
                        from_datetime=week_window_start,
                    )
                    if already_reset is not None:
                        results["already_reset"] += 1
                        continue

                    limit_service.create_weekly_budget_reset(
                        org_id,
                        approver_id=employee.employee_id,
                        reviewed_by_id=employee.person_id,
                        reset_reason="Scheduled weekly budget reset",
                        reviewed_from=previous_week_start,
                        reviewed_to=previous_week_end,
                    )
                    results["resets_created"] += 1

                db.commit()
            results["organizations_processed"] += 1

        except Exception as e:
            logger.error(
                "Failed to process automatic budget reset for org %s: %s",
                org_id,
                e,
            )
            results["errors"].append(
                {
                    "organization_id": str(org_id),
                    "error": str(e),
                }
            )

    logger.info(
        "Weekly approver budget reset complete: %d orgs, %d resets",
        results["organizations_processed"],
        results["resets_created"],
    )
    return results


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def post_approved_expense(
    self,
    organization_id: str,
    claim_id: str,
    user_id: str,
    *,
    create_supplier_invoice: bool = False,
    auto_post_gl: bool = True,
) -> dict:
    """
    Post an approved expense claim to the general ledger.

    This task is triggered after claim approval to create GL entries
    and optionally a supplier invoice for AP processing.

    Args:
        organization_id: UUID of the organization
        claim_id: UUID of the expense claim
        user_id: UUID of the user posting
        create_supplier_invoice: Whether to create AP invoice
        auto_post_gl: Whether to auto-post to ledger

    Returns:
        Dict with posting result
    """
    from app.services.expense.expense_posting_adapter import ExpensePostingAdapter

    logger.info("Posting approved expense claim %s", claim_id)

    org_id = uuid.UUID(organization_id)
    with session_for_org(org_id) as db:
        try:
            c_id = uuid.UUID(claim_id)
            u_id = uuid.UUID(user_id)

            # Post to GL
            result = ExpensePostingAdapter.post_expense_claim(
                db,
                org_id,
                c_id,
                date.today(),
                u_id,
                auto_post=auto_post_gl,
            )

            if not result.success:
                logger.error("GL posting failed: %s", result.message)
                return {
                    "success": False,
                    "error": result.message,
                }

            # Optionally create supplier invoice
            invoice_id = None
            if create_supplier_invoice:
                invoice_result = (
                    ExpensePostingAdapter.create_supplier_invoice_from_expense(
                        db,
                        org_id,
                        c_id,
                        u_id,
                    )
                )
                if invoice_result.success:
                    invoice_id = str(invoice_result.supplier_invoice_id)
                else:
                    logger.warning(
                        "Supplier invoice creation failed: %s",
                        invoice_result.message,
                    )

            db.commit()

            return {
                "success": True,
                "journal_entry_id": str(result.journal_entry_id)
                if result.journal_entry_id
                else None,
                "posting_batch_id": str(result.posting_batch_id)
                if result.posting_batch_id
                else None,
                "supplier_invoice_id": invoice_id,
            }

        except Exception as e:
            logger.exception("Expense posting failed: %s", e)
            db.rollback()
            raise self.retry(exc=e)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def post_cash_advance_disbursement(
    self,
    organization_id: str,
    advance_id: str,
    user_id: str,
    bank_account_id: str,
) -> dict:
    """
    Post a cash advance disbursement to the general ledger.

    Creates GL entries for the cash advance:
    - Debit: Employee advance account
    - Credit: Bank/Cash account

    Args:
        organization_id: UUID of the organization
        advance_id: UUID of the cash advance
        user_id: UUID of the user posting
        bank_account_id: UUID of the bank account for credit

    Returns:
        Dict with posting result
    """
    from app.services.expense.expense_posting_adapter import ExpensePostingAdapter

    logger.info("Posting cash advance disbursement %s", advance_id)

    org_id = uuid.UUID(organization_id)
    with session_for_org(org_id) as db:
        try:
            result = ExpensePostingAdapter.post_cash_advance(
                db,
                org_id,
                uuid.UUID(advance_id),
                date.today(),
                uuid.UUID(user_id),
                bank_account_id=uuid.UUID(bank_account_id),
            )

            if not result.success:
                logger.error("Advance posting failed: %s", result.message)
                return {
                    "success": False,
                    "error": result.message,
                }

            db.commit()

            return {
                "success": True,
                "journal_entry_id": str(result.journal_entry_id)
                if result.journal_entry_id
                else None,
                "posting_batch_id": str(result.posting_batch_id)
                if result.posting_batch_id
                else None,
            }

        except Exception as e:
            logger.exception("Advance posting failed: %s", e)
            db.rollback()
            raise self.retry(exc=e)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def settle_cash_advance_with_claim(
    self,
    organization_id: str,
    advance_id: str,
    claim_id: str,
    user_id: str,
    settlement_amount: str | None = None,
) -> dict:
    """
    Settle a cash advance against an expense claim.

    Posts GL entries to offset the advance against expenses.

    Args:
        organization_id: UUID of the organization
        advance_id: UUID of the cash advance
        claim_id: UUID of the expense claim
        user_id: UUID of the user posting
        settlement_amount: Optional specific amount to settle (default: auto-calculate)

    Returns:
        Dict with settlement result
    """
    from decimal import Decimal

    from app.services.expense.expense_posting_adapter import ExpensePostingAdapter

    logger.info(
        "Settling cash advance %s with claim %s",
        advance_id,
        claim_id,
    )

    org_id = uuid.UUID(organization_id)
    with session_for_org(org_id) as db:
        try:
            settle_amt = Decimal(settlement_amount) if settlement_amount else None

            result = ExpensePostingAdapter.settle_cash_advance(
                db,
                org_id,
                uuid.UUID(advance_id),
                uuid.UUID(claim_id),
                date.today(),
                uuid.UUID(user_id),
                settlement_amount=settle_amt,
            )

            if not result.success:
                logger.error("Settlement posting failed: %s", result.message)
                return {
                    "success": False,
                    "error": result.message,
                }

            db.commit()

            return {
                "success": True,
                "journal_entry_id": str(result.journal_entry_id)
                if result.journal_entry_id
                else None,
                "message": result.message,
            }

        except Exception as e:
            logger.exception("Settlement posting failed: %s", e)
            db.rollback()
            raise self.retry(exc=e)


@shared_task
def calculate_expense_analytics(
    organization_id: str,
    period: str = "month",
) -> dict:
    """
    Calculate expense analytics for reporting dashboards.

    Generates aggregate statistics for:
    - Total expenses by category
    - Top spenders by department
    - Approval time metrics
    - Limit utilization rates

    Args:
        organization_id: UUID of the organization
        period: "day", "week", "month", "quarter", "year"

    Returns:
        Dict with calculated analytics
    """
    from app.services.expense.expense_service import ExpenseService

    logger.info(
        "Calculating expense analytics for org %s, period %s",
        organization_id,
        period,
    )

    org_id = uuid.UUID(organization_id)
    with session_for_org(org_id) as db:
        try:
            service = ExpenseService(db)

            # Get date range based on period
            today = date.today()
            if period == "day":
                start_date = today
            elif period == "week":
                start_date = today - timedelta(days=today.weekday())
            elif period == "month":
                start_date = today.replace(day=1)
            elif period == "quarter":
                quarter = (today.month - 1) // 3
                start_date = date(today.year, quarter * 3 + 1, 1)
            else:  # year
                start_date = date(today.year, 1, 1)

            # Get analytics
            summary = service.get_expense_summary_report(
                org_id,
                start_date=start_date,
                end_date=today,
            )

            category_breakdown = service.get_expense_by_category_report(
                org_id,
                start_date=start_date,
                end_date=today,
            )

            # Calculate average approval time
            approved_claims = db.scalars(
                select(ExpenseClaim).where(
                    ExpenseClaim.organization_id == org_id,
                    ExpenseClaim.status.in_(
                        [
                            ExpenseClaimStatus.APPROVED,
                            ExpenseClaimStatus.PAID,
                        ]
                    ),
                    ExpenseClaim.claim_date >= start_date,
                    ExpenseClaim.approved_on.isnot(None),
                )
            ).all()

            total_days = 0
            count = 0
            for claim in approved_claims:
                if claim.approved_on:
                    total_days += (claim.approved_on - claim.claim_date).days
                    count += 1

            avg_approval_days = total_days / count if count > 0 else 0

            return {
                "success": True,
                "period": period,
                "start_date": str(start_date),
                "end_date": str(today),
                "summary": {
                    "total_claims": summary["total_claims"],
                    "total_claimed": float(summary["total_claimed"]),
                    "approved_count": summary["approved_count"],
                    "approved_amount": float(summary["approved_amount"]),
                    "rejected_count": summary["rejected_count"],
                },
                "category_breakdown": [
                    {
                        "category": cat["category_name"],
                        "amount": float(cat["claimed_amount"]),
                        "percentage": cat["percentage"],
                    }
                    for cat in category_breakdown["categories"][:10]
                ],
                "metrics": {
                    "avg_approval_days": round(avg_approval_days, 1),
                    "claims_approved": count,
                },
            }

        except Exception as e:
            logger.exception("Analytics calculation failed: %s", e)
            return {
                "success": False,
                "error": str(e),
            }


@shared_task
def poll_stuck_expense_transfers() -> dict:
    """Reconcile expense reimbursement transfers whose webhook never arrived.

    Thin adapter. It discovers which tenants have work, opens one session per
    tenant, hands each intent to :class:`PaymentService` and adds up what it is
    told. Every decision about a transfer — whether it is stale, whether to
    promote it, whether to give up on it — belongs to that service, which is
    the sole writer of ``PaymentIntent.status``
    (``tests/architecture/test_payment_intent_status_single_owner.py``).

    Returns:
        Dict with polling results
    """
    from app.services.finance.payments.payment_service import (
        PaymentService,
        TransferPollOutcome,
    )

    logger.info("Polling stuck expense transfers")

    results: dict[str, Any] = {
        "intents_checked": 0,
        "completed": 0,
        "failed": 0,
        "still_pending": 0,
        "abandoned": 0,
        # Distinct from `abandoned` on purpose: abandoned means Paystack
        # answered and refused, indeterminate means nobody ever answered.
        # Collapsing the two here would make the job's own report repeat the
        # conflation ADR-0007 removes from the column.
        "indeterminate": 0,
        "errors": [],
    }

    # One clock for both selections, so an intent cannot be judged stale by one
    # pass and fresh by the next within a single run.
    now = datetime.now(UTC)

    # Both sweeps below fan out over tenants instead of scanning across them.
    # The cross-tenant SELECTs they replace bypassed only the SQLAlchemy
    # listener — never PostgreSQL RLS — so under `app_user` they found no
    # intents at all: stale transfers would never expire, stuck ones would never
    # be polled, and the task would report a clean run every two minutes while
    # doing nothing.
    #
    # The selections stay in `PaymentService`, which remains the sole writer of
    # `PaymentIntent.status`; only the session they run on changes. Each returns
    # organization -> intent ids, so inside a tenant session it has at most one
    # key, and reading it back by `org_id` is the isolation assertion: a row
    # grouped under any other tenant is not this session's to touch.
    #
    # `include_inactive=True` matches the old reach — neither SELECT had an
    # `Organization` predicate, and a deactivated tenant's in-flight payout still
    # has to settle.

    # Expire PENDING intents that never had a transfer initiated (step 2 was
    # never called) and are past their expires_at.
    for org_id, db in for_each_organization(include_inactive=True):
        intent_ids = PaymentService.find_stale_pending_transfer_intents(
            db, now=now
        ).get(org_id, [])
        if not intent_ids:
            continue
        svc = PaymentService(db, org_id)
        for intent_id in intent_ids:
            if svc.expire_stale_pending_transfer(intent_id, now=now):
                results["expired"] = results.get("expired", 0) + 1
        db.commit()

    # Fast fallback for transfers that are in flight but have no verdict.
    stuck_found = False
    for org_id, db in for_each_organization(include_inactive=True):
        intent_ids = PaymentService.find_stuck_transfer_intents(db, now=now).get(
            org_id, []
        )
        if not intent_ids:
            # Nothing stuck here. Skipping before the config read keeps the
            # "No Paystack keys" warning to tenants that actually have a
            # transfer waiting on those keys, as the old grouping did.
            continue
        stuck_found = True

        svc = PaymentService(db, org_id)
        config = svc.resolve_transfer_polling_config()
        if config is None:
            logger.warning("No Paystack keys for org %s", org_id)
            continue

        for intent_id in intent_ids:
            results["intents_checked"] += 1
            outcome = svc.reconcile_stuck_transfer(intent_id, config)

            if outcome.outcome is TransferPollOutcome.ERRORED:
                results["errors"].append(
                    {
                        "intent_id": str(outcome.intent_id),
                        "error": outcome.error,
                        "poll_count": outcome.poll_count,
                    }
                )
                continue

            # TransferPollOutcome values ARE the result keys; SKIPPED is
            # the only one not pre-seeded, so it appears only when a
            # tenant actually had an intent that moved under the worker.
            counter = outcome.outcome.value
            results[counter] = results.get(counter, 0) + 1

        db.commit()

    if not stuck_found:
        logger.info("No stuck transfers found")

    logger.info(
        "Transfer polling complete: %d checked, %d completed, %d failed, "
        "%d abandoned, %d unresolved",
        results["intents_checked"],
        results["completed"],
        results["failed"],
        results["abandoned"],
        results["indeterminate"],
    )

    return results


@shared_task
def reconcile_unresolved_expense_transfers() -> dict:
    """Keep asking Paystack about payouts whose outcome was never observed.

    The slow lane behind `poll_stuck_expense_transfers`. That job runs every two
    minutes because a webhook is merely late; this one runs hourly because its
    subjects have already exhausted the fast loop, and re-asking at two-minute
    intervals would be load without information.

    The narrow tenant catalogue discovers identifiers, then every payout read
    and decision runs inside that tenant's own RLS session. The adapter hands
    each intent to `PaymentService`; it decides nothing.
    `resolve_indeterminate_transfer` is the only writer permitted to move an
    intent out of INDETERMINATE, and it may only move it to a status Paystack
    itself justified.

    There is deliberately no give-up path here. A transfer nobody can account
    for stays unresolved until someone learns what happened to it; the job's
    job is to keep asking and to make the age of the oldest one visible
    (ADR-0007, adopting `dotmac_starter_mt` ADR-0032).
    """
    from app.metrics import set_transfer_unresolved_oldest_age
    from app.services.finance.payments.payment_service import (
        PaymentService,
        TransferPollOutcome,
    )

    logger.info("Reconciling transfers with unobserved outcomes")

    results: dict[str, Any] = {
        "intents_checked": 0,
        "completed": 0,
        "failed": 0,
        "reversed": 0,
        "still_unresolved": 0,
        "errors": [],
    }

    now = datetime.now(UTC)

    unresolved_by_org: dict[uuid.UUID, list[uuid.UUID]] = {}
    oldest_unresolved_seconds = 0.0

    # Settlement work includes inactive tenants: disabling an organization
    # cannot make a possibly-paid transfer safe to forget. Discovery exposes
    # identifiers only; protected payout rows remain tenant-scoped.
    for org_id in organization_ids(include_inactive=True):
        with session_for_org(org_id) as db:
            intent_ids = PaymentService.find_indeterminate_transfer_intents(
                db,
                organization_id=org_id,
            )
            if intent_ids:
                unresolved_by_org[org_id] = intent_ids
            oldest_unresolved_seconds = max(
                oldest_unresolved_seconds,
                PaymentService.oldest_unresolved_transfer_age(
                    db,
                    organization_id=org_id,
                    now=now,
                ).total_seconds(),
            )

    # Publish the pre-resolution backlog age before any provider I/O. A tenant
    # with missing credentials therefore remains visible instead of vanishing
    # from the metric merely because it cannot be reconciled.
    set_transfer_unresolved_oldest_age(oldest_unresolved_seconds)

    if not unresolved_by_org:
        logger.info("No unresolved transfers found")
        return results

    for org_id, intent_ids in unresolved_by_org.items():
        with session_for_org(org_id) as db:
            svc = PaymentService(db, org_id)
            config = svc.resolve_transfer_polling_config()
            if config is None:
                # No keys means no verdict is obtainable, so nothing is written.
                # The intents stay unresolved and stay in the gauge, which is
                # the correct signal: a tenant that cannot be reconciled is a
                # tenant somebody has to configure.
                logger.warning(
                    "No Paystack keys for org %s - %d unresolved transfer(s) "
                    "cannot be reconciled",
                    org_id,
                    len(intent_ids),
                )
                continue

            for intent_id in intent_ids:
                results["intents_checked"] += 1
                outcome = svc.resolve_indeterminate_transfer(intent_id, config, now=now)

                if outcome.outcome is TransferPollOutcome.STILL_UNRESOLVED:
                    results["still_unresolved"] += 1
                    results["errors"].append(
                        {
                            "intent_id": str(outcome.intent_id),
                            "error": outcome.error,
                            "poll_count": outcome.poll_count,
                        }
                    )
                    continue

                counter = outcome.outcome.value
                results[counter] = results.get(counter, 0) + 1

            db.commit()

    logger.info(
        "Unresolved transfer reconciliation complete: %d checked, %d resolved, "
        "%d still unknown",
        results["intents_checked"],
        results["completed"] + results["failed"] + results["reversed"],
        results["still_unresolved"],
    )

    return results
