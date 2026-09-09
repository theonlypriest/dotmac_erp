import logging
import os
from datetime import timedelta

from celery.schedules import crontab
from sqlalchemy import select

from app.db import SessionLocal
from app.db.session_context import allow_cross_org
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.scheduler import ScheduledTask, ScheduleType

logger = logging.getLogger(__name__)


def _is_mock_like(value: object) -> bool:
    return type(value).__module__.startswith("unittest.mock")


def _env_value(name: str) -> str | None:
    value = os.getenv(name)
    if value is None or value == "":
        return None
    return value


def _env_int(name: str) -> int | None:
    raw = _env_value(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _env_bool(name: str, default: bool = True) -> bool:
    raw = _env_value(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _get_setting_value(db, domain: SettingDomain, key: str) -> object | None:
    """
    Read a PLATFORM-WIDE setting row for Celery bootstrap.

    This is a third read path alongside ``settings_spec.resolve_value`` and
    ``settings_cache``, and deliberately so: it runs while the Celery app is
    being configured, before any request or task has a tenant, and it must not
    depend on the Redis cache whose own URL it is here to discover. What it must
    not be is a *differently behaved* path, so:

    - Scope is explicit. The broker, result backend, timezone and beat intervals
      are process-wide configuration, so only the platform row
      (``organization_id IS NULL``) is eligible. Without that predicate this
      returned whichever tenant's row came back first, and one tenant's setting
      could reconfigure the whole worker fleet.
    - ``allow_cross_org`` is what lets the platform row be read at all: the
      session is unprimed (there is no tenant), so the ORM org-filter listener
      would otherwise raise ``MissingOrgContextError`` — swallowed by the caller's
      broad ``except``, silently disabling every database-configured scheduler
      setting. The predicate above is the scoping authority.
    - The stored value is interpreted by its registered spec, the same
      ``coerce_resolved_value`` the other two paths use, so a value the settings
      screen would reject (``beat_max_loop_interval = 0``, below the spec's
      minimum of 1) cannot reach Celery from here either.
    """
    from app.services.settings_spec import coerce_resolved_value, get_spec

    stmt = (
        select(DomainSetting)
        .where(DomainSetting.domain == domain)
        .where(DomainSetting.key == key)
        .where(DomainSetting.is_active.is_(True))
        .where(DomainSetting.organization_id.is_(None))
    )
    with allow_cross_org(db):
        try:
            setting = db.scalars(stmt).first()
        except Exception:
            setting = db.scalar(stmt)
    if not setting:
        return None

    raw: object | None = None
    value_text = getattr(setting, "value_text", None)
    if value_text is not None and not _is_mock_like(value_text):
        text = str(value_text)
        if text:
            raw = text
    if raw is None:
        value_json = getattr(setting, "value_json", None)
        if value_json is not None and not _is_mock_like(value_json):
            raw = str(value_json)
    if raw is None:
        return None

    spec = get_spec(domain, key)
    if spec is None:
        return raw
    return coerce_resolved_value(spec, raw)


def _effective_int(
    db, domain: SettingDomain, key: str, env_key: str, default: int
) -> int:
    env_value = _env_int(env_key)
    if env_value is not None:
        return env_value
    value = _get_setting_value(db, domain, key)
    if value is None:
        return default
    try:
        return int(str(value))
    except ValueError:
        return default


def _effective_str(
    db, domain: SettingDomain, key: str, env_key: str, default: str | None
) -> str | None:
    env_value = _env_value(env_key)
    if env_value is not None:
        return env_value
    value = _get_setting_value(db, domain, key)
    if value is None:
        return default
    return str(value)


def get_celery_config() -> dict:
    broker = None
    backend = None
    timezone = None
    beat_max_loop_interval = 5
    beat_refresh_seconds = 30
    session = SessionLocal()
    try:
        broker = _effective_str(
            session, SettingDomain.scheduler, "broker_url", "CELERY_BROKER_URL", None
        )
        backend = _effective_str(
            session,
            SettingDomain.scheduler,
            "result_backend",
            "CELERY_RESULT_BACKEND",
            None,
        )
        timezone = _effective_str(
            session, SettingDomain.scheduler, "timezone", "CELERY_TIMEZONE", None
        )
        beat_max_loop_interval = _effective_int(
            session,
            SettingDomain.scheduler,
            "beat_max_loop_interval",
            "CELERY_BEAT_MAX_LOOP_INTERVAL",
            5,
        )
        beat_refresh_seconds = _effective_int(
            session,
            SettingDomain.scheduler,
            "beat_refresh_seconds",
            "CELERY_BEAT_REFRESH_SECONDS",
            30,
        )
    except Exception:
        logger.exception("Failed to load scheduler settings from database.")
    finally:
        session.close()

    broker = broker or _env_value("REDIS_URL") or "redis://localhost:6379/0"
    backend = backend or _env_value("REDIS_URL") or "redis://localhost:6379/1"
    timezone = timezone or "Africa/Lagos"
    config: dict[str, str | int] = {
        "broker_url": broker,
        "result_backend": backend,
        "timezone": timezone,
        "beat_max_loop_interval": beat_max_loop_interval,
        "beat_refresh_seconds": beat_refresh_seconds,
    }
    return config


def _builtin_beat_schedule() -> dict[str, dict]:
    """Built-in scheduled tasks that always run, independent of DB config."""
    schedule = {
        "analytics-cash-flow": {
            "task": "app.tasks.analytics.refresh_cash_flow_metrics",
            "schedule": crontab(hour=5, minute=0),  # 5:00 AM — before coach tasks
        },
        "analytics-efficiency": {
            "task": "app.tasks.analytics.refresh_efficiency_metrics",
            "schedule": crontab(hour=5, minute=10),  # 5:10 AM
        },
        "analytics-revenue": {
            "task": "app.tasks.analytics.refresh_revenue_metrics",
            "schedule": crontab(hour=5, minute=20),  # 5:20 AM
        },
        "analytics-workforce": {
            "task": "app.tasks.analytics.refresh_workforce_metrics",
            "schedule": crontab(hour=5, minute=30),  # 5:30 AM
        },
        "analytics-supply-chain": {
            "task": "app.tasks.analytics.refresh_supply_chain_metrics",
            "schedule": crontab(hour=5, minute=40),  # 5:40 AM
        },
        "analytics-compliance": {
            "task": "app.tasks.analytics.refresh_compliance_metrics",
            "schedule": crontab(hour=5, minute=50),  # 5:50 AM
        },
        "coach-daily-data-quality": {
            "task": "app.tasks.coach.generate_daily_data_quality_insights",
            "schedule": crontab(hour=6, minute=0),  # Daily at 6 AM
        },
        "hr-birthday-morning-email": {
            "task": "app.tasks.hr.send_hr_birthday_morning_email",
            "schedule": crontab(hour=6, minute=0),  # Daily at 6 AM
        },
        "coach-daily-banking-health": {
            "task": "app.tasks.coach.generate_daily_banking_health_insights",
            "schedule": crontab(hour=6, minute=10),  # Daily at 6:10 AM
        },
        "coach-daily-expense-approvals": {
            "task": "app.tasks.coach.generate_daily_expense_approval_insights",
            "schedule": crontab(hour=6, minute=20),  # Daily at 6:20 AM
        },
        "coach-daily-ar-overdue": {
            "task": "app.tasks.coach.generate_daily_ar_overdue_insights",
            "schedule": crontab(hour=6, minute=30),  # Daily at 6:30 AM
        },
        "coach-daily-ap-due": {
            "task": "app.tasks.coach.generate_daily_ap_due_insights",
            "schedule": crontab(hour=6, minute=40),  # Daily at 6:40 AM
        },
        "coach-daily-cash-flow": {
            "task": "app.tasks.coach.generate_daily_cash_flow_insights",
            "schedule": crontab(hour=6, minute=45),  # Daily at 6:45 AM
        },
        "coach-daily-compliance": {
            "task": "app.tasks.coach.generate_daily_compliance_insights",
            "schedule": crontab(hour=6, minute=50),  # Daily at 6:50 AM
        },
        "coach-daily-workforce": {
            "task": "app.tasks.coach.generate_daily_workforce_insights",
            "schedule": crontab(hour=6, minute=55),  # Daily at 6:55 AM
        },
        "coach-daily-supply-chain": {
            "task": "app.tasks.coach.generate_daily_supply_chain_insights",
            "schedule": crontab(hour=7, minute=0),  # Daily at 7:00 AM
        },
        "coach-daily-revenue": {
            "task": "app.tasks.coach.generate_daily_revenue_insights",
            "schedule": crontab(hour=7, minute=5),  # Daily at 7:05 AM
        },
        "coach-daily-efficiency": {
            "task": "app.tasks.coach.generate_daily_efficiency_insights",
            "schedule": crontab(hour=7, minute=10),  # Daily at 7:10 AM
        },
        "coach-weekly-finance-report": {
            "task": "app.tasks.coach.generate_weekly_finance_report",
            "schedule": crontab(hour=7, minute=30, day_of_week=1),  # Monday 7:30 AM
        },
        "coach-weekly-hr-report": {
            "task": "app.tasks.coach.generate_weekly_hr_report",
            "schedule": crontab(hour=7, minute=40, day_of_week=1),  # Monday 7:40 AM
        },
        "finance-reminders": {
            "task": "app.tasks.finance.process_all_finance_reminders",
            "schedule": crontab(hour=8, minute=0),  # Daily at 8 AM
        },
        "expense-approval-reminders": {
            "task": "app.tasks.expense.process_expense_approval_reminders",
            "schedule": crontab(hour=8, minute=15),  # Daily at 8:15 AM
        },
        "expense-weekly-budget-reset": {
            "task": "app.tasks.expense.reset_weekly_approver_budgets",
            "schedule": crontab(
                hour=7,
                minute=0,
                day_of_week=1,
            ),  # Monday 7:00 AM (Africa/Lagos timezone)
        },
        "expense-stuck-transfers": {
            "task": "app.tasks.expense.poll_stuck_expense_transfers",
            "schedule": crontab(minute="*/2"),  # Every 2 minutes
        },
        # Slower lane: payouts whose outcome was never observed. Hourly, not
        # every two minutes — these have already exhausted the fast loop, and
        # re-asking at that cadence is load without information. See
        # INDETERMINATE_RECHECK_INTERVAL in the payment service (ADR-0007).
        "expense-unresolved-transfers": {
            "task": "app.tasks.expense.reconcile_unresolved_expense_transfers",
            "schedule": crontab(minute=17),  # Hourly, off the busy minute
        },
        "notification-email-dispatch": {
            "task": "app.tasks.notifications.process_pending_notification_emails",
            "schedule": timedelta(minutes=1),  # Every minute
        },
        "notification-nextcloud-dispatch": {
            "task": "app.tasks.notifications.process_pending_nextcloud_notifications",
            "schedule": timedelta(minutes=1),  # Every minute
        },
        "notification-push-dispatch": {
            "task": "app.tasks.notifications.process_pending_push_notifications",
            "schedule": timedelta(minutes=1),  # Every minute
        },
        "daily-exchange-rate-fetch": {
            "task": "app.tasks.exchange_rates.fetch_daily_exchange_rates",
            "schedule": crontab(hour=14, minute=0),  # 2 PM UTC daily
        },
        "daily-leave-attendance-sync": {
            "task": "app.tasks.hr.sync_leave_attendance",
            "schedule": crontab(minute=0),  # Hourly at minute 0
        },
        "fleet-document-expiry-reminders": {
            "task": "app.tasks.fleet.process_document_expiry_notifications",
            "schedule": crontab(hour=7, minute=0),  # 7 AM daily
        },
        "banking-auto-match-statements": {
            "task": "app.tasks.banking.auto_match_unreconciled_statements",
            "schedule": crontab(hour="*/6", minute=15),  # Every 6 hours at :15
        },
        "inventory-auto-issue-pending-stock-material-requests": {
            "task": "app.tasks.inventory.auto_issue_pending_stock_material_requests",
            "schedule": crontab(minute="*/5"),  # Every 5 minutes
            "kwargs": {"limit_per_org": 100},
        },
        "inventory-low-stock-notifications": {
            "task": "app.tasks.inventory.send_low_stock_notifications",
            "schedule": crontab(hour=7, minute=15),  # Daily at 7:15 AM
        },
        # dotmac_sub incremental sync is DB-owned through ScheduledTask. Keeping
        # a builtin entry here as well dispatches two overlapping runs every 30
        # minutes and can exhaust the worker pool when the connector is slow.
        "recurring-templates": {
            "task": "app.tasks.automation.process_recurring_templates",
            "schedule": crontab(hour="*/6", minute=5),  # Every 6 hours at :05
        },
        "staff-sync-reconcile": {
            "task": "app.tasks.staff_sync.run_staff_sync_reconcile",
            "schedule": crontab(hour=2, minute=30),
        },
        "dotmac-sub-daily-reconciliation": {
            "task": "app.tasks.dotmac_sub.run_dotmac_sub_daily_reconciliation",
            "schedule": crontab(hour=1, minute=0),  # 1 AM daily
        },
        "dotmac-sub-full-reconciliation": {
            "task": "app.tasks.dotmac_sub.run_dotmac_sub_full_reconciliation",
            "schedule": crontab(hour=2, minute=0, day_of_week=0),  # Sunday 2 AM
        },
        "dotmac-sub-stale-history-cleanup": {
            "task": "app.tasks.dotmac_sub.cleanup_stale_dotmac_sub_sync_history",
            "schedule": crontab(minute="*/5"),
            "kwargs": {"stale_after_minutes": 15, "limit": 500},
        },
        # ── Outbox relay tasks ──────────────────────────────────
        "outbox-relay": {
            "task": "app.tasks.outbox_relay.relay_outbox_events",
            "schedule": timedelta(seconds=30),  # Every 30 seconds
            "kwargs": {"batch_size": 100},
        },
        "refresh-stale-balances": {
            "task": "app.tasks.finance.refresh_stale_balances",
            "schedule": timedelta(seconds=30),  # Every 30 seconds
            "kwargs": {"batch_size": 200},
        },
        "refresh-analysis-cubes": {
            "task": "app.tasks.finance.refresh_analysis_cubes",
            "schedule": crontab(minute="*/15"),  # Every 15 minutes
        },
        "release-expired-stock-reservations": {
            "task": "app.tasks.finance.release_expired_stock_reservations",
            "schedule": crontab(minute="*/15"),  # Every 15 minutes
            "kwargs": {"batch_size": 200},
        },
        "outbox-cleanup": {
            "task": "app.tasks.outbox_relay.cleanup_published_outbox_events",
            "schedule": crontab(hour=2, minute=30),  # 2:30 AM daily
            "kwargs": {"retention_days": 30, "batch_size": 5000},
        },
        "outbox-balance-reconciliation": {
            "task": "app.tasks.outbox_relay.reconcile_outbox_balance_projection",
            "schedule": crontab(minute=40),  # Hourly at :40
            "kwargs": {"lookback_hours": 26},
        },
        "service-hook-execution-cleanup": {
            "task": "app.tasks.hooks.cleanup_old_hook_executions",
            "schedule": crontab(hour=2, minute=45),  # 2:45 AM daily
            "kwargs": {"retention_days": 90, "batch_size": 5000},
        },
        # ── Data health tasks ────────────────────────────────────
        "data-health-check": {
            "task": "app.tasks.data_health.run_data_health_check",
            "schedule": crontab(hour=4, minute=0),  # 4:00 AM daily
        },
        "notification-cleanup": {
            "task": "app.tasks.data_health.cleanup_old_notifications",
            "schedule": crontab(hour=3, minute=0),  # 3:00 AM daily
            "kwargs": {"read_days": 7, "unread_days": 30},
        },
        "feature-flag-archival": {
            "task": "app.tasks.feature_flags.archive_expired_feature_flags",
            "schedule": crontab(hour=2, minute=0),  # 2:00 AM daily
        },
        "outbox-stuck-recovery": {
            "task": "app.tasks.data_health.process_stuck_outbox_events",
            "schedule": crontab(minute="*/15"),  # Every 15 minutes
            "kwargs": {"stuck_minutes": 30, "batch_size": 500},
        },
        "infrastructure-health-check": {
            "task": "app.tasks.infrastructure_health.run_infrastructure_health_checks",
            "schedule": timedelta(minutes=5),
        },
        "invoice-status-reconciliation": {
            "task": "app.tasks.data_health.reconcile_invoice_statuses",
            "schedule": crontab(hour=4, minute=30),  # 4:30 AM daily
        },
        "auto-post-approved-invoices": {
            "task": "app.tasks.data_health.auto_post_approved_invoices",
            "schedule": crontab(hour=8, minute=30),  # 8:30 AM daily
            "kwargs": {"max_age_days": 7},
        },
        "stale-draft-report": {
            "task": "app.tasks.data_health.cleanup_stale_drafts",
            "schedule": crontab(hour=3, minute=30, day_of_week=1),  # Monday 3:30 AM
            "kwargs": {"draft_age_days": 180, "dry_run": True},
        },
        "rebuild-account-balances": {
            "task": "app.tasks.data_health.rebuild_account_balances",
            "schedule": crontab(hour=4, minute=45),  # 4:45 AM daily
        },
        "payment-allocation-reconciliation": {
            "task": "app.tasks.data_health.reconcile_payment_allocations",
            "schedule": crontab(hour=5, minute=0, day_of_week=0),  # Sunday 5:00 AM
            "kwargs": {"batch_size": 1000, "dry_run": True},
        },
        "unbalanced-journal-check": {
            "task": "app.tasks.data_health.fix_unbalanced_posted_journals",
            "schedule": crontab(hour=4, minute=15),  # 4:15 AM daily
            "kwargs": {"dry_run": True, "batch_size": 100},
        },
        # --- Licensing ---
        "license-revalidation": {
            "task": "app.tasks.license.revalidate_license",
            "schedule": crontab(hour=3, minute=0),  # 3:00 AM daily
        },
    }
    if not _env_bool("BANKING_AUTO_MATCH_ENABLED", default=True):
        schedule.pop("banking-auto-match-statements", None)
    return schedule


def build_beat_schedule() -> dict:
    schedule: dict[str, dict] = _builtin_beat_schedule()
    session = SessionLocal()
    try:
        tasks = list(
            session.scalars(
                select(ScheduledTask).where(ScheduledTask.enabled.is_(True))
            ).all()
        )
        for task in tasks:
            task_schedule = None

            if task.schedule_type == ScheduleType.interval:
                interval_seconds = max(task.interval_seconds or 0, 1)
                task_schedule = timedelta(seconds=interval_seconds)
            elif task.schedule_type == ScheduleType.crontab:
                task_schedule = crontab(
                    minute=task.cron_minute or "0",
                    hour=task.cron_hour or "8",
                    day_of_week=task.cron_day_of_week or "*",
                    day_of_month=task.cron_day_of_month or "*",
                    month_of_year=task.cron_month_of_year or "*",
                )

            if task_schedule is None:
                continue

            schedule[f"scheduled_task_{task.id}"] = {
                "task": task.task_name,
                "schedule": task_schedule,
                "args": task.args_json or [],
                "kwargs": task.kwargs_json or {},
            }
    except Exception:
        logger.exception("Failed to build Celery beat schedule.")
    finally:
        session.close()
    return schedule
