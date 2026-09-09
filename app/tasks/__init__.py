# Register additional task modules used via .delay()
from app.tasks.analytics import (
    refresh_cash_flow_metrics,
    refresh_compliance_metrics,
    refresh_efficiency_metrics,
    refresh_revenue_metrics,
    refresh_supply_chain_metrics,
    refresh_workforce_metrics,
)
from app.tasks.ap_posting import post_unposted_ap_invoices
from app.tasks.ar_allocation import allocate_exact_match_payments
from app.tasks.gl_posting import (
    post_approved_journal_backlog,
    post_expense_claim_backlog,
    post_stranded_source_journals,
)
from app.tasks.ar_reconciliation import reconcile_invoice_amount_paid
from app.tasks.payments_sync import sync_customers_to_paystack
from app.tasks.audit import log_audit_event
from app.tasks.audit_integrity import verify_audit_hash_chain
from app.tasks.automation import (
    execute_workflow_action,
    process_recurring_templates,
    process_scheduled_workflow_rules,
)
from app.tasks.banking import auto_match_unreconciled_statements
from app.tasks.coach import (
    generate_daily_ap_due_insights,
    generate_daily_ar_overdue_insights,
    generate_daily_banking_health_insights,
    generate_daily_cash_flow_insights,
    generate_daily_compliance_insights,
    generate_daily_data_quality_insights,
    generate_daily_efficiency_insights,
    generate_daily_expense_approval_insights,
    generate_daily_revenue_insights,
    generate_daily_supply_chain_insights,
    generate_daily_workforce_insights,
    generate_weekly_finance_report,
    generate_weekly_hr_report,
)
from app.tasks.data_health import (
    auto_post_approved_invoices,
    cleanup_old_notifications,
    cleanup_stale_drafts,
    fix_unbalanced_posted_journals,
    process_stuck_outbox_events,
    rebuild_account_balances,
    reconcile_invoice_statuses,
    reconcile_payment_allocations,
    run_data_health_check,
)
from app.tasks.dotmac_sub import (
    cleanup_stale_dotmac_sub_sync_history,
    process_dotmac_sub_webhook,
    run_dotmac_sub_daily_reconciliation,
    run_dotmac_sub_full_reconciliation,
    run_dotmac_sub_incremental_sync,
    run_dotmac_sub_incremental_sync_phase,
)
from app.tasks.email import send_email_async
from app.tasks.expense import (
    calculate_expense_analytics,
    poll_stuck_expense_transfers,
    post_approved_expense,
    reconcile_unresolved_expense_transfers,
    post_cash_advance_disbursement,
    process_expense_approval_reminders,
    refresh_period_usage_cache,
    settle_cash_advance_with_claim,
)
from app.tasks.exchange_rates import fetch_daily_exchange_rates
from app.tasks.feature_flags import archive_expired_feature_flags
from app.tasks.finance import (
    process_ar_invoices_export,
    process_ar_receipts_export,
    process_approved_fixed_asset_gl_reconciliation_drafts,
    process_depreciation_gl_reconciliation,
    process_fixed_asset_gl_reconciliation_package,
    process_gl_journals_export,
    process_general_ledger_export,
    process_monthly_depreciation_runs,
    refresh_analysis_cubes,
    refresh_stale_balances,
    release_expired_stock_reservations,
    sync_mono_transactions,
    sync_paystack_transactions,
)
from app.tasks.fleet import (
    process_document_expiry_notifications,
)
from app.tasks.hooks import cleanup_old_hook_executions, execute_async_hook
from app.tasks.hr import (
    calculate_hr_analytics,
    process_birthday_notifications,
    process_certification_expiry_notifications,
    process_contract_expiry_notifications,
    process_performance_review_reminders,
    process_probation_ending_notifications,
    run_employee_mailcow_offboarding,
    process_work_anniversary_notifications,
    send_hr_birthday_morning_email,
)
from app.tasks.inventory import (
    auto_issue_pending_stock_material_requests,
    send_low_stock_notifications,
)
from app.tasks.imports import process_customer_import_partitions
from app.tasks.license import revalidate_license
from app.tasks.notifications import (
    process_pending_nextcloud_notifications,
    process_pending_notification_emails,
)
from app.tasks.outbox_relay import (
    cleanup_published_outbox_events,
    reconcile_outbox_balance_projection,
    relay_outbox_events,
)
from app.tasks.infrastructure_health import run_infrastructure_health_checks_task
from app.tasks.payroll import (
    process_payroll_entry_notifications,
    send_payslip_email,
)
from app.tasks.performance import (
    activate_cycle,
    calculate_cycle_progress,
    check_upcoming_deadlines,
    complete_cycle,
    generate_cycle_appraisals,
    process_pms_dispute_deadline_reminders,
    process_pms_dispute_sla_enforcement,
    process_cycle_phase_transitions,
    sync_all_cycle_progress,
)
from app.tasks.staff_sync import (
    run_staff_sync_reconcile,
    sync_employee_staff_account,
)
from app.tasks.project_sla import (
    process_project_sla_breaches,
)
from app.tasks.weekly_meeting_reports import send_weekly_meeting_report_hr_email

__all__ = [
    # Expense module tasks
    "refresh_period_usage_cache",
    "process_expense_approval_reminders",
    "post_approved_expense",
    "post_cash_advance_disbursement",
    "settle_cash_advance_with_claim",
    "calculate_expense_analytics",
    "poll_stuck_expense_transfers",
    "reconcile_unresolved_expense_transfers",
    # HR module tasks
    "process_probation_ending_notifications",
    "process_contract_expiry_notifications",
    "process_work_anniversary_notifications",
    "process_birthday_notifications",
    "send_hr_birthday_morning_email",
    "process_performance_review_reminders",
    "process_certification_expiry_notifications",
    "calculate_hr_analytics",
    "run_employee_mailcow_offboarding",
    # Fleet module tasks
    "process_document_expiry_notifications",
    # Performance module tasks
    "process_cycle_phase_transitions",
    "generate_cycle_appraisals",
    "calculate_cycle_progress",
    "check_upcoming_deadlines",
    "sync_all_cycle_progress",
    "activate_cycle",
    "complete_cycle",
    "process_pms_dispute_sla_enforcement",
    "process_pms_dispute_deadline_reminders",
    # Audit tasks
    "post_unposted_ap_invoices",
    "allocate_exact_match_payments",
    "post_approved_journal_backlog",
    "post_expense_claim_backlog",
    "post_stranded_source_journals",
    "reconcile_invoice_amount_paid",
    "sync_customers_to_paystack",
    "log_audit_event",
    "verify_audit_hash_chain",
    # dotmac_sub sync tasks
    "sync_employee_staff_account",
    "run_staff_sync_reconcile",
    "process_dotmac_sub_webhook",
    "run_dotmac_sub_incremental_sync",
    "run_dotmac_sub_incremental_sync_phase",
    "run_dotmac_sub_daily_reconciliation",
    "run_dotmac_sub_full_reconciliation",
    "cleanup_stale_dotmac_sub_sync_history",
    # Exchange-rate tasks
    "fetch_daily_exchange_rates",
    # License tasks
    "revalidate_license",
    # Payroll tasks
    "send_payslip_email",
    "process_payroll_entry_notifications",
    # Email tasks
    "send_email_async",
    # Automation tasks
    "execute_workflow_action",
    "process_recurring_templates",
    "process_scheduled_workflow_rules",
    # Finance tasks
    "process_ar_invoices_export",
    "process_ar_receipts_export",
    "process_gl_journals_export",
    "process_general_ledger_export",
    "process_approved_fixed_asset_gl_reconciliation_drafts",
    "process_depreciation_gl_reconciliation",
    "process_fixed_asset_gl_reconciliation_package",
    "sync_paystack_transactions",
    "sync_mono_transactions",
    "refresh_analysis_cubes",
    "refresh_stale_balances",
    "release_expired_stock_reservations",
    "process_monthly_depreciation_runs",
    # Banking tasks
    "auto_match_unreconciled_statements",
    # Inventory tasks
    "auto_issue_pending_stock_material_requests",
    "send_low_stock_notifications",
    # Durable import tasks
    "process_customer_import_partitions",
    # Analytics tasks
    "refresh_cash_flow_metrics",
    "refresh_compliance_metrics",
    "refresh_efficiency_metrics",
    "refresh_revenue_metrics",
    "refresh_supply_chain_metrics",
    "refresh_workforce_metrics",
    # Coach tasks
    "generate_daily_ap_due_insights",
    "generate_daily_ar_overdue_insights",
    "generate_daily_banking_health_insights",
    "generate_daily_cash_flow_insights",
    "generate_daily_compliance_insights",
    "generate_daily_data_quality_insights",
    "generate_daily_efficiency_insights",
    "generate_daily_expense_approval_insights",
    "generate_daily_revenue_insights",
    "generate_daily_supply_chain_insights",
    "generate_daily_workforce_insights",
    "generate_weekly_finance_report",
    "generate_weekly_hr_report",
    # Feature flags
    "archive_expired_feature_flags",
    # Data health tasks
    "cleanup_old_notifications",
    "process_stuck_outbox_events",
    "reconcile_invoice_statuses",
    "auto_post_approved_invoices",
    "cleanup_stale_drafts",
    "rebuild_account_balances",
    "reconcile_payment_allocations",
    "fix_unbalanced_posted_journals",
    "run_data_health_check",
    # Notification tasks
    "process_pending_notification_emails",
    "process_pending_nextcloud_notifications",
    "execute_async_hook",
    "cleanup_old_hook_executions",
    # Project SLA tasks
    "process_project_sla_breaches",
    # Outbox relay tasks
    "relay_outbox_events",
    "cleanup_published_outbox_events",
    "reconcile_outbox_balance_projection",
    "run_infrastructure_health_checks_task",
    "send_weekly_meeting_report_hr_email",
]
