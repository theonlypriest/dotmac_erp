import enum
import logging
from dataclasses import dataclass
from typing import cast

from fastapi import HTTPException

from uuid import UUID

from app.models.domain_settings import SettingDomain, SettingValueType
from app.services import domain_settings as settings_service
from app.services.domain_settings import AMBIENT, _Ambient
from app.services.response import ListResponseMixin
from app.services.setting_scopes import register_platform_owned

logger = logging.getLogger(__name__)


class SettingScopeAuthority(str, enum.Enum):
    """Who may own a row for this setting.

    TENANT (the default, and what every pre-existing spec means): an
    organization may hold its own row, and that row outranks the platform row
    on read.

    PLATFORM: no organization row may exist. The value is a deployment-level
    control, and an organization-scoped row for it is refused at the ORM
    boundary (``app.models.domain_settings._require_platform_scope``) and
    discarded on read. Used for controls whose whole purpose is to CONSTRAIN a
    tenant — an SSRF allowlist is not a preference.

    Orthogonal to ``SettingSpec.inherits``; see ``app.services.setting_scopes``.
    """

    TENANT = "tenant"
    PLATFORM = "platform"


@dataclass(frozen=True)
class SettingSpec(ListResponseMixin):
    domain: SettingDomain
    key: str
    env_var: str | None
    value_type: SettingValueType
    default: object | None
    required: bool = False
    allowed: set[str] | None = None
    min_value: int | None = None
    max_value: int | None = None
    is_secret: bool = False
    # Whether a less-specific scope's value is a valid answer for this setting.
    # True for nearly everything — a threshold or toggle set globally is a real
    # answer for an organization that has not overridden it.
    #
    # False for a value that IDENTIFIES something owned by one organization: a
    # ledger account, a bank account, a warehouse. A fallback claims a
    # less-specific value answers the question, and for those it does not —
    # there is no "default GL account", and inheriting one means posting to
    # another organization's books.
    #
    # Mirrors `dotmac_kernel.settings_resolver.SettingSpec.inherits` (ADR-0012)
    # so the kernel cutover is a swap rather than a redesign.
    inherits: bool = True
    # Who may OWN a row for this setting. Orthogonal to `inherits` above, and
    # conflating the two is how a platform control quietly acquires a tenant
    # override: `inherits=False` says a LESS specific row is not a valid
    # answer, while `scope=PLATFORM` says a MORE specific row may not exist at
    # all. A PLATFORM spec therefore ignores `inherits` — there is only one
    # scope left to read.
    scope: SettingScopeAuthority = SettingScopeAuthority.TENANT
    label: str | None = None
    description: str | None = None


SETTINGS_SPECS: list[SettingSpec] = [
    SettingSpec(
        domain=SettingDomain.auth,
        key="jwt_secret",
        env_var="JWT_SECRET",
        value_type=SettingValueType.string,
        default=None,
        required=True,
        is_secret=True,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="jwt_algorithm",
        env_var="JWT_ALGORITHM",
        value_type=SettingValueType.string,
        default="HS256",
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="jwt_access_ttl_minutes",
        env_var="JWT_ACCESS_TTL_MINUTES",
        value_type=SettingValueType.integer,
        # 60 because that is what runs. 15 is tighter and is a genuine
        # security/UX tradeoff (users re-authenticate four times as often),
        # so it is a decision to take deliberately rather than inherit from a
        # spec value that never took effect. Left as a candidate improvement.
        default=60,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="jwt_refresh_ttl_days",
        env_var="JWT_REFRESH_TTL_DAYS",
        value_type=SettingValueType.integer,
        default=30,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="refresh_cookie_name",
        env_var="REFRESH_COOKIE_NAME",
        value_type=SettingValueType.string,
        default="refresh_token",
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="password_reset_ttl_minutes",
        env_var="PASSWORD_RESET_TTL_MINUTES",
        value_type=SettingValueType.integer,
        # 60 because that is what `auth_flow._password_reset_ttl_minutes`
        # returns today. The key was read but never declared, so it resolved to
        # nothing and only the env var and this literal were ever live.
        default=60,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="refresh_cookie_secure",
        env_var="REFRESH_COOKIE_SECURE",
        value_type=SettingValueType.boolean,
        # True because that is what runs: `auth_flow._refresh_cookie_secure`
        # falls back to True "for production safety". The spec said False, so
        # the admin screen showed one answer while the app used another.
        default=True,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="refresh_cookie_samesite",
        env_var="REFRESH_COOKIE_SAMESITE",
        value_type=SettingValueType.string,
        # "strict" because that is what runs. The spec said "lax", which is
        # WEAKER — resolving through the spec would have loosened CSRF
        # protection on every deployment with no stored row.
        default="strict",
        allowed={"lax", "strict", "none"},
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="refresh_cookie_domain",
        env_var="REFRESH_COOKIE_DOMAIN",
        value_type=SettingValueType.string,
        default=None,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="refresh_cookie_path",
        env_var="REFRESH_COOKIE_PATH",
        value_type=SettingValueType.string,
        # "/" because that is what runs. "/auth" is tighter and may well be the
        # intent, but narrowing a cookie's path INVALIDATES existing sessions
        # for every other path — a deliberate change with user-visible effect,
        # not a reconciliation. Left as a candidate improvement.
        default="/",
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="totp_issuer",
        env_var="TOTP_ISSUER",
        value_type=SettingValueType.string,
        default="dotmac_erp",
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="totp_encryption_key",
        env_var="TOTP_ENCRYPTION_KEY",
        value_type=SettingValueType.string,
        default=None,
        required=True,
        is_secret=True,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="api_key_rate_window_seconds",
        env_var="API_KEY_RATE_WINDOW_SECONDS",
        value_type=SettingValueType.integer,
        default=60,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="api_key_rate_max",
        env_var="API_KEY_RATE_MAX",
        value_type=SettingValueType.integer,
        default=5,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="default_auth_provider",
        env_var="AUTH_DEFAULT_AUTH_PROVIDER",
        value_type=SettingValueType.string,
        default="local",
        allowed={"local", "sso"},
    ),
    SettingSpec(
        domain=SettingDomain.audit,
        key="enabled",
        env_var="AUDIT_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
    ),
    SettingSpec(
        domain=SettingDomain.audit,
        key="methods",
        env_var="AUDIT_METHODS",
        value_type=SettingValueType.json,
        default=["POST", "PUT", "PATCH", "DELETE"],
    ),
    SettingSpec(
        domain=SettingDomain.audit,
        key="skip_paths",
        env_var="AUDIT_SKIP_PATHS",
        value_type=SettingValueType.json,
        default=["/static", "/web", "/health"],
    ),
    SettingSpec(
        domain=SettingDomain.audit,
        key="read_trigger_header",
        env_var="AUDIT_READ_TRIGGER_HEADER",
        value_type=SettingValueType.string,
        default="x-audit-read",
    ),
    SettingSpec(
        domain=SettingDomain.audit,
        key="read_trigger_query",
        env_var="AUDIT_READ_TRIGGER_QUERY",
        value_type=SettingValueType.string,
        default="audit",
    ),
    SettingSpec(
        domain=SettingDomain.scheduler,
        key="broker_url",
        env_var="CELERY_BROKER_URL",
        value_type=SettingValueType.string,
        default=None,
    ),
    SettingSpec(
        domain=SettingDomain.scheduler,
        key="result_backend",
        env_var="CELERY_RESULT_BACKEND",
        value_type=SettingValueType.string,
        default=None,
    ),
    SettingSpec(
        domain=SettingDomain.scheduler,
        key="timezone",
        env_var="CELERY_TIMEZONE",
        value_type=SettingValueType.string,
        default="Africa/Lagos",
    ),
    SettingSpec(
        domain=SettingDomain.scheduler,
        key="beat_max_loop_interval",
        env_var="CELERY_BEAT_MAX_LOOP_INTERVAL",
        value_type=SettingValueType.integer,
        default=5,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.scheduler,
        key="beat_refresh_seconds",
        env_var="CELERY_BEAT_REFRESH_SECONDS",
        value_type=SettingValueType.integer,
        default=30,
        min_value=1,
    ),
    # Email Domain Settings
    SettingSpec(
        domain=SettingDomain.email,
        key="smtp_host",
        env_var="SMTP_HOST",
        value_type=SettingValueType.string,
        default="localhost",
    ),
    SettingSpec(
        domain=SettingDomain.email,
        key="smtp_port",
        env_var="SMTP_PORT",
        value_type=SettingValueType.integer,
        default=587,
        min_value=1,
        max_value=65535,
    ),
    SettingSpec(
        domain=SettingDomain.email,
        key="smtp_username",
        env_var="SMTP_USERNAME",
        value_type=SettingValueType.string,
        default=None,
    ),
    SettingSpec(
        domain=SettingDomain.email,
        key="smtp_password",
        env_var="SMTP_PASSWORD",
        value_type=SettingValueType.string,
        default=None,
        is_secret=True,
    ),
    SettingSpec(
        domain=SettingDomain.email,
        key="smtp_use_tls",
        env_var="SMTP_USE_TLS",
        value_type=SettingValueType.boolean,
        default=True,
    ),
    SettingSpec(
        domain=SettingDomain.email,
        key="smtp_use_ssl",
        env_var="SMTP_USE_SSL",
        value_type=SettingValueType.boolean,
        default=False,
    ),
    SettingSpec(
        domain=SettingDomain.email,
        key="smtp_from_email",
        env_var="SMTP_FROM_EMAIL",
        value_type=SettingValueType.string,
        default="noreply@example.com",
    ),
    SettingSpec(
        domain=SettingDomain.email,
        key="smtp_from_name",
        env_var="SMTP_FROM_NAME",
        value_type=SettingValueType.string,
        default="Dotmac ERP",
    ),
    SettingSpec(
        domain=SettingDomain.email,
        key="email_reply_to",
        env_var="EMAIL_REPLY_TO",
        value_type=SettingValueType.string,
        default=None,
    ),
    # Automation Domain Settings
    SettingSpec(
        domain=SettingDomain.automation,
        key="recurring_default_frequency",
        env_var=None,
        value_type=SettingValueType.string,
        default="MONTHLY",
        allowed={"DAILY", "WEEKLY", "MONTHLY", "QUARTERLY", "YEARLY"},
    ),
    SettingSpec(
        domain=SettingDomain.automation,
        key="recurring_max_occurrences",
        env_var=None,
        value_type=SettingValueType.integer,
        default=999,
        min_value=1,
        max_value=9999,
    ),
    SettingSpec(
        domain=SettingDomain.automation,
        key="recurring_lookback_days",
        env_var=None,
        value_type=SettingValueType.integer,
        default=7,
        min_value=1,
        max_value=90,
    ),
    SettingSpec(
        domain=SettingDomain.automation,
        key="workflow_max_actions_per_event",
        env_var=None,
        value_type=SettingValueType.integer,
        default=10,
        min_value=1,
        max_value=100,
    ),
    SettingSpec(
        domain=SettingDomain.automation,
        key="workflow_async_timeout_seconds",
        env_var=None,
        value_type=SettingValueType.integer,
        default=300,
        min_value=30,
        max_value=3600,
    ),
    SettingSpec(
        domain=SettingDomain.automation,
        key="custom_fields_max_per_entity",
        env_var=None,
        value_type=SettingValueType.integer,
        default=20,
        min_value=1,
        max_value=100,
    ),
    # ── Webhook security: the PLATFORM ceiling ────────────────────────────
    # These four are the outbound-SSRF boundary. They exist to CONSTRAIN an
    # organization, so an organization may not hold a row for any of them:
    # a constrained party that can rewrite its own constraint is not
    # constrained. An organization narrows them through the
    # `webhook_tenant_*` keys below, composed by conjunction in
    # `app/services/finance/automation/webhook_policy.py`.
    SettingSpec(
        domain=SettingDomain.automation,
        key="webhook_allowed_hosts",
        env_var="WEBHOOK_ALLOWED_HOSTS",
        value_type=SettingValueType.string,
        default="",
        scope=SettingScopeAuthority.PLATFORM,
    ),
    SettingSpec(
        domain=SettingDomain.automation,
        key="webhook_allowed_domains",
        env_var="WEBHOOK_ALLOWED_DOMAINS",
        value_type=SettingValueType.string,
        default="",
        scope=SettingScopeAuthority.PLATFORM,
    ),
    SettingSpec(
        domain=SettingDomain.automation,
        key="webhook_allow_insecure",
        env_var="WEBHOOK_ALLOW_INSECURE",
        value_type=SettingValueType.boolean,
        default=False,
        scope=SettingScopeAuthority.PLATFORM,
    ),
    SettingSpec(
        domain=SettingDomain.automation,
        key="webhook_allow_localhost",
        env_var="WEBHOOK_ALLOW_LOCALHOST",
        value_type=SettingValueType.boolean,
        default=False,
        scope=SettingScopeAuthority.PLATFORM,
    ),
    SettingSpec(
        domain=SettingDomain.automation,
        key="webhook_max_timeout_seconds",
        env_var="WEBHOOK_MAX_TIMEOUT_SECONDS",
        value_type=SettingValueType.integer,
        # 300 because that is exactly `webhook_timeout_seconds`' existing
        # max_value, so no deployment's effective timeout changes on the day
        # this lands. An operator lowers it deliberately; the spec does not
        # pick a tighter number on their behalf and silently shorten live
        # outbound calls.
        default=300,
        min_value=1,
        max_value=300,
        scope=SettingScopeAuthority.PLATFORM,
    ),
    # ── Webhook security: the optional TENANT narrowing ───────────────────
    # `default=None` on all four, because None is the IDENTITY element of the
    # narrow-only conjunction. A default of "" or False would not be: it would
    # make an organization that has never touched the setting look like one
    # that had asked for an empty allowlist, or asked to force a platform
    # `True` off. `inherits=False` for the same reason — a platform row for a
    # `webhook_tenant_*` key would be a global narrowing masquerading as one
    # organization's own choice.
    SettingSpec(
        domain=SettingDomain.automation,
        key="webhook_tenant_allowed_hosts",
        env_var=None,
        value_type=SettingValueType.string,
        default=None,
        inherits=False,
    ),
    SettingSpec(
        domain=SettingDomain.automation,
        key="webhook_tenant_allowed_domains",
        env_var=None,
        value_type=SettingValueType.string,
        default=None,
        inherits=False,
    ),
    SettingSpec(
        domain=SettingDomain.automation,
        key="webhook_tenant_allow_insecure",
        env_var=None,
        value_type=SettingValueType.boolean,
        default=None,
        inherits=False,
    ),
    SettingSpec(
        domain=SettingDomain.automation,
        key="webhook_tenant_allow_localhost",
        env_var=None,
        value_type=SettingValueType.boolean,
        default=None,
        inherits=False,
    ),
    # A timeout is a preference, not an SSRF control, so this one stays
    # organization-owned. It is bounded by `webhook_max_timeout_seconds`
    # above at the point of use, in both outbound channels.
    SettingSpec(
        domain=SettingDomain.automation,
        key="webhook_timeout_seconds",
        env_var="WEBHOOK_TIMEOUT_SECONDS",
        value_type=SettingValueType.integer,
        default=10,
        min_value=1,
        max_value=300,
    ),
    # ── Secrets provider ──────────────────────────────────────────────────
    # PLATFORM, and this is a FIFTH key beyond the four the webhook ruling
    # names. It is here because the protection it had was the same literal
    # `restricted_keys` set in `app/web/finance/settings.py` that this change
    # deletes, and it is the same failure class as `webhook_allow_insecure`: a
    # tenant-writable row that turns off TLS verification, here against the
    # secret store holding every other secret. Deleting the set without
    # carrying this key would have left it strictly less protected than before
    # — a silent regression riding along on a hardening change.
    #
    # This is a real tightening, not a like-for-like carry: today a tenant
    # ADMIN may flip it (the old set was skipped entirely for `is_admin`);
    # after this only the platform settings route
    # `PUT /api/v1/settings/automation/openbao_allow_insecure` may. Preserving
    # the old protection exactly would have meant re-introducing a role literal in
    # a route handler, which is the convention being retired.
    SettingSpec(
        domain=SettingDomain.automation,
        key="openbao_allow_insecure",
        env_var="OPENBAO_ALLOW_INSECURE",
        value_type=SettingValueType.boolean,
        default=False,
        scope=SettingScopeAuthority.PLATFORM,
    ),
    SettingSpec(
        domain=SettingDomain.automation,
        key="fa_depreciation_auto_run_enabled",
        env_var=None,
        value_type=SettingValueType.boolean,
        default=False,
        label="Auto Run FA Depreciation",
        description=(
            "Automatically create the next monthly fixed-asset depreciation run "
            "after the fiscal period ends."
        ),
    ),
    SettingSpec(
        domain=SettingDomain.automation,
        key="fa_depreciation_auto_post_enabled",
        env_var=None,
        value_type=SettingValueType.boolean,
        default=False,
        label="Auto Post FA Depreciation",
        description=(
            "Automatically post system-generated fixed-asset depreciation runs "
            "after calculation."
        ),
    ),
    # Feature flags are now managed dynamically via feature_flag_registry table.
    # See app/services/feature_flag_service.py and app/models/feature_flag.py.
    # Reporting Domain Settings
    SettingSpec(
        domain=SettingDomain.reporting,
        key="default_export_format",
        env_var=None,
        value_type=SettingValueType.string,
        default="PDF",
        allowed={"PDF", "EXCEL", "CSV"},
    ),
    SettingSpec(
        domain=SettingDomain.reporting,
        key="report_page_size",
        env_var=None,
        value_type=SettingValueType.string,
        default="A4",
        allowed={"A4", "LETTER", "LEGAL"},
    ),
    SettingSpec(
        domain=SettingDomain.reporting,
        key="report_orientation",
        env_var=None,
        value_type=SettingValueType.string,
        default="PORTRAIT",
        allowed={"PORTRAIT", "LANDSCAPE"},
    ),
    SettingSpec(
        domain=SettingDomain.reporting,
        key="include_logo_in_reports",
        env_var=None,
        value_type=SettingValueType.boolean,
        default=True,
    ),
    SettingSpec(
        domain=SettingDomain.reporting,
        key="report_watermark_text",
        env_var=None,
        value_type=SettingValueType.string,
        default=None,
    ),
    # Payments Domain Settings
    #
    # Sub-cent rounding dust: a balance at or under this is not a debt. ADR-0016
    # §4 — the tolerance is business policy, so it is a setting rather than a
    # constant welded into a module (AR and AP each declared their own
    # `Decimal("0.01")` before this existed) or into the generated column's DDL
    # (where changing it would mean dropping and re-adding the column).
    # Read through `app.services.finance.coverage.resolve_payment_dust`.
    SettingSpec(
        domain=SettingDomain.payments,
        key="payment_dust",
        env_var="PAYMENT_DUST",
        # A string, parsed with `Decimal` — money is never a float, and this
        # spec system has no decimal value type.
        value_type=SettingValueType.string,
        default="0.01",
    ),
    # Paystack integration
    SettingSpec(
        domain=SettingDomain.payments,
        key="paystack_enabled",
        env_var="PAYSTACK_ENABLED",
        value_type=SettingValueType.boolean,
        default=False,
    ),
    SettingSpec(
        domain=SettingDomain.payments,
        key="paystack_public_key",
        env_var="PAYSTACK_PUBLIC_KEY",
        value_type=SettingValueType.string,
        default=None,
    ),
    SettingSpec(
        domain=SettingDomain.payments,
        key="paystack_secret_key",
        env_var="PAYSTACK_SECRET_KEY",
        value_type=SettingValueType.string,
        default=None,
        is_secret=True,
    ),
    SettingSpec(
        domain=SettingDomain.payments,
        key="paystack_webhook_secret",
        env_var="PAYSTACK_WEBHOOK_SECRET",
        value_type=SettingValueType.string,
        default=None,
        is_secret=True,
    ),
    SettingSpec(
        domain=SettingDomain.payments,
        key="paystack_callback_base_url",
        env_var="PAYSTACK_CALLBACK_BASE_URL",
        value_type=SettingValueType.string,
        default=None,
    ),
    # Paystack Bank Account Linkage
    # Bank account UUID where Paystack collections are settled
    SettingSpec(
        domain=SettingDomain.payments,
        key="paystack_collection_bank_account_id",
        env_var=None,
        value_type=SettingValueType.string,
        default=None,
    ),
    # Bank account UUID used as source for Paystack transfers (payouts)
    SettingSpec(
        domain=SettingDomain.payments,
        key="paystack_transfer_bank_account_id",
        env_var=None,
        value_type=SettingValueType.string,
        default=None,
    ),
    # Enable Paystack Transfer API for expense reimbursements
    SettingSpec(
        domain=SettingDomain.payments,
        key="paystack_transfers_enabled",
        env_var="PAYSTACK_TRANSFERS_ENABLED",
        value_type=SettingValueType.boolean,
        default=False,
    ),
    # GL Account for posting transfer fees (bank charges)
    SettingSpec(
        domain=SettingDomain.payments,
        key="paystack_transfer_fee_account_id",
        env_var=None,
        value_type=SettingValueType.string,
        default=None,
    ),
    # How long a transfer may sit INDETERMINATE (outcome unobserved) before the
    # reconciler escalates it to an operator. Six hours is one Nigerian banking
    # business half-day: long enough that a Paystack incident or a NIBSS
    # settlement delay resolves itself without paging anyone, short enough that
    # unresolved money is on someone's desk the same day it went unresolved.
    # Not a retry budget — nothing is retried on this clock; it is purely when
    # "we do not know" stops being tolerable to leave alone (ADR-0007).
    SettingSpec(
        domain=SettingDomain.payments,
        key="paystack_transfer_unresolved_alert_hours",
        env_var="PAYSTACK_TRANSFER_UNRESOLVED_ALERT_HOURS",
        value_type=SettingValueType.integer,
        default=6,
        min_value=1,
        label="Unresolved Transfer Alert Threshold (hours)",
        description=(
            "How long an outbound transfer may stay INDETERMINATE — outcome "
            "not observed — before it is escalated as needing a human."
        ),
    ),
    # Banking Domain Settings (Mono Connect Integration)
    SettingSpec(
        domain=SettingDomain.banking,
        key="mono_enabled",
        env_var="MONO_ENABLED",
        value_type=SettingValueType.boolean,
        default=False,
        label="Mono Connect Enabled",
        description="Enable Mono Connect for automatic bank statement retrieval",
    ),
    SettingSpec(
        domain=SettingDomain.banking,
        key="mono_public_key",
        env_var="MONO_PUBLIC_KEY",
        value_type=SettingValueType.string,
        default=None,
        label="Mono Public Key",
        description="Public key for the Mono Connect widget (frontend)",
    ),
    SettingSpec(
        domain=SettingDomain.banking,
        key="mono_secret_key",
        env_var="MONO_SECRET_KEY",
        value_type=SettingValueType.string,
        default=None,
        is_secret=True,
        label="Mono Secret Key",
        description="Secret key for Mono API calls (backend only)",
    ),
    SettingSpec(
        domain=SettingDomain.banking,
        key="mono_webhook_secret",
        env_var="MONO_WEBHOOK_SECRET",
        value_type=SettingValueType.string,
        default=None,
        is_secret=True,
        label="Mono Webhook Secret",
        description="Secret for verifying Mono webhook requests",
    ),
    # Module Settings: Support
    SettingSpec(
        domain=SettingDomain.support,
        key="support_default_sla_response_hours",
        env_var=None,
        value_type=SettingValueType.integer,
        default=24,
        min_value=1,
        max_value=168,
        label="Default SLA Response Time (hours)",
        description="Time allowed for initial response to tickets",
    ),
    SettingSpec(
        domain=SettingDomain.support,
        key="support_default_sla_resolution_hours",
        env_var=None,
        value_type=SettingValueType.integer,
        default=72,
        min_value=1,
        max_value=720,
        label="Default SLA Resolution Time (hours)",
        description="Time allowed for ticket resolution",
    ),
    SettingSpec(
        domain=SettingDomain.support,
        key="support_auto_assignment_enabled",
        env_var=None,
        value_type=SettingValueType.boolean,
        default=False,
        label="Auto-assignment Enabled",
        description="Automatically assign tickets to available agents",
    ),
    SettingSpec(
        domain=SettingDomain.support,
        key="support_ticket_prefix",
        env_var=None,
        value_type=SettingValueType.string,
        default="TKT",
        label="Ticket Number Prefix",
        description="Prefix for support ticket numbers",
    ),
    # Module Settings: Inventory
    SettingSpec(
        domain=SettingDomain.inventory,
        key="inventory_low_stock_threshold_percent",
        env_var=None,
        value_type=SettingValueType.integer,
        default=20,
        min_value=1,
        max_value=100,
        label="Reorder Approach Threshold (%)",
        description=(
            "Percentage above an item's reorder level that is considered approaching"
        ),
    ),
    SettingSpec(
        domain=SettingDomain.inventory,
        key="inventory_default_warehouse_id",
        env_var=None,
        value_type=SettingValueType.string,
        default=None,
        label="Default Warehouse",
        description="Default warehouse for new inventory transactions",
    ),
    SettingSpec(
        domain=SettingDomain.inventory,
        key="inventory_enable_lot_tracking",
        env_var=None,
        value_type=SettingValueType.boolean,
        default=False,
        label="Enable Lot Tracking",
        description="Track inventory items by lot/batch number",
    ),
    SettingSpec(
        domain=SettingDomain.inventory,
        key="inventory_enable_serial_tracking",
        env_var=None,
        value_type=SettingValueType.boolean,
        default=False,
        label="Enable Serial Tracking",
        description="Track inventory items by serial number",
    ),
    SettingSpec(
        domain=SettingDomain.inventory,
        key="inventory_valuation_mode",
        env_var=None,
        value_type=SettingValueType.string,
        default="manual",
        allowed={"manual", "real_time"},
        label="Inventory Valuation Mode",
        description="manual posts GL separately; real_time posts GL with inventory transactions.",
    ),
    SettingSpec(
        domain=SettingDomain.inventory,
        key="stock_reservation_enabled",
        env_var=None,
        value_type=SettingValueType.boolean,
        default=False,
        label="Enable Stock Reservation",
        description="Reserve inventory against demand lines.",
    ),
    SettingSpec(
        domain=SettingDomain.inventory,
        key="stock_reservation_expiry_hours",
        env_var=None,
        value_type=SettingValueType.integer,
        default=0,
        min_value=0,
        max_value=720,
        label="Reservation Expiry (hours)",
        description="Auto-release reservations after this many hours. 0 disables expiry.",
    ),
    SettingSpec(
        domain=SettingDomain.inventory,
        key="stock_reservation_allow_partial",
        env_var=None,
        value_type=SettingValueType.boolean,
        default=True,
        label="Allow Partial Reservation",
        description="If stock is insufficient, reserve available quantity.",
    ),
    SettingSpec(
        domain=SettingDomain.inventory,
        key="stock_reservation_auto_on_confirm",
        env_var=None,
        value_type=SettingValueType.boolean,
        default=True,
        label="Auto-Reserve on SO Confirm",
        description="Attempt reservation automatically when a sales order is confirmed.",
    ),
    # Module Settings: Projects
    SettingSpec(
        domain=SettingDomain.projects,
        key="project_default_status",
        env_var=None,
        value_type=SettingValueType.string,
        default="PLANNING",
        allowed={"PLANNING", "ACTIVE", "ON_HOLD"},
        label="Default Project Status",
        description="Initial status for new projects",
    ),
    SettingSpec(
        domain=SettingDomain.projects,
        key="project_enable_time_tracking",
        env_var=None,
        value_type=SettingValueType.boolean,
        default=True,
        label="Enable Time Tracking",
        description="Allow time entries on project tasks",
    ),
    SettingSpec(
        domain=SettingDomain.projects,
        key="project_task_prefix",
        env_var=None,
        value_type=SettingValueType.string,
        default="TASK",
        label="Task Number Prefix",
        description="Prefix for task numbers",
    ),
    # Module Settings: Fleet
    SettingSpec(
        domain=SettingDomain.fleet,
        key="fleet_reservation_lead_days",
        env_var=None,
        value_type=SettingValueType.integer,
        default=3,
        min_value=0,
        max_value=30,
        label="Minimum Reservation Lead (days)",
        description="Minimum lead time required before a reservation starts",
    ),
    SettingSpec(
        domain=SettingDomain.fleet,
        key="fleet_reservation_default_duration_hours",
        env_var=None,
        value_type=SettingValueType.integer,
        default=8,
        min_value=1,
        max_value=168,
        label="Default Reservation Duration (hours)",
        description="Default duration for new reservations",
    ),
    SettingSpec(
        domain=SettingDomain.fleet,
        key="fleet_require_driver_license",
        env_var=None,
        value_type=SettingValueType.boolean,
        default=True,
        label="Require Driver License",
        description="Require a license on file to create reservations",
    ),
    # Module Settings: Procurement
    SettingSpec(
        domain=SettingDomain.procurement,
        key="procurement_default_payment_terms_days",
        env_var=None,
        value_type=SettingValueType.integer,
        default=30,
        min_value=0,
        max_value=180,
        label="Default Payment Terms (days)",
        description="Default payment terms for purchase documents",
    ),
    SettingSpec(
        domain=SettingDomain.procurement,
        key="procurement_require_rfq_for_po",
        env_var=None,
        value_type=SettingValueType.boolean,
        default=False,
        label="Require RFQ Before PO",
        description="Require RFQ completion before creating purchase orders",
    ),
    SettingSpec(
        domain=SettingDomain.procurement,
        key="procurement_threshold_direct_max",
        env_var=None,
        value_type=SettingValueType.integer,
        default=2500000,
        min_value=0,
        label="Direct Procurement Threshold",
        description="Maximum value for direct procurement method (PPA 2007 default: 2,500,000)",
    ),
    SettingSpec(
        domain=SettingDomain.procurement,
        key="procurement_threshold_selective_max",
        env_var=None,
        value_type=SettingValueType.integer,
        default=50000000,
        min_value=0,
        label="Selective Procurement Threshold",
        description="Maximum value for selective procurement method (PPA 2007 default: 50,000,000)",
    ),
    SettingSpec(
        domain=SettingDomain.procurement,
        key="procurement_threshold_ministerial_max",
        env_var=None,
        value_type=SettingValueType.integer,
        default=1000000000,
        min_value=0,
        label="Ministerial Threshold",
        description="Maximum value for Ministerial Tenders Board (PPA 2007 default: 1,000,000,000)",
    ),
    # Module Settings: Expense
    SettingSpec(
        domain=SettingDomain.expense,
        key="expense_route_to_ap",
        env_var=None,
        value_type=SettingValueType.boolean,
        default=False,
        label="Route Reimbursements through AP",
        description="Automatically create a supplier invoice in Accounts Payable when an expense claim is approved",
    ),
    # Settings Domain: App-level configuration content
    SettingSpec(
        domain=SettingDomain.settings,
        key="help_center_content_json",
        env_var=None,
        value_type=SettingValueType.json,
        default=None,
        label="Help Center Content Override",
        description="Optional JSON override for /help manuals, journeys, workflows, and troubleshooting.",
    ),
    # Payroll Domain Settings
    SettingSpec(
        domain=SettingDomain.payroll,
        key="auto_generate_enabled",
        env_var=None,
        value_type=SettingValueType.boolean,
        default=False,
    ),
    SettingSpec(
        domain=SettingDomain.payroll,
        key="auto_generate_days_before",
        env_var=None,
        value_type=SettingValueType.integer,
        default=5,
        min_value=1,
        max_value=15,
    ),
    SettingSpec(
        domain=SettingDomain.payroll,
        key="auto_generate_notify_emails",
        env_var=None,
        value_type=SettingValueType.json,
        default=[],
    ),
    SettingSpec(
        domain=SettingDomain.payroll,
        key="auto_post_gl_on_approval",
        env_var=None,
        value_type=SettingValueType.boolean,
        default=True,
    ),
    SettingSpec(
        domain=SettingDomain.payroll,
        key="payroll_rounding_account_id",
        env_var=None,
        value_type=SettingValueType.string,
        default=None,
    ),
    # Banking Domain Settings (Auto-Match Rules)
    # The 11 ``automatch_*`` keys that used to live here were removed on
    # 2026-05-15 after their values were backfilled into
    # ``banking.reconciliation_policy_profile`` (the canonical config home).
    # See the 20260515_extend_recon_profile / backfill_automatch_profile /
    # backfill_global_automatch migrations and ReconciliationPolicyService
    # for the current resolution path.
    # Coach Domain Settings (LLM Backend Configuration)
    SettingSpec(
        domain=SettingDomain.coach,
        key="deepseek_base_url",
        env_var="COACH_LLM_DEEPSEEK_BASE_URL",
        value_type=SettingValueType.string,
        default="https://api.deepseek.com/v1",
        label="DeepSeek API Base URL",
        description="Base URL for the DeepSeek OpenAI-compatible API",
    ),
    SettingSpec(
        domain=SettingDomain.coach,
        key="deepseek_api_key",
        env_var="COACH_LLM_DEEPSEEK_API_KEY",
        value_type=SettingValueType.string,
        default=None,
        is_secret=True,
        label="DeepSeek API Key",
        description="API key for authenticating with the DeepSeek API",
    ),
    SettingSpec(
        domain=SettingDomain.coach,
        key="deepseek_model_fast",
        env_var="COACH_LLM_DEEPSEEK_MODEL_FAST",
        value_type=SettingValueType.string,
        default="deepseek-chat",
        label="DeepSeek Fast Model",
        description="Model name for fast-tier LLM requests (low latency)",
    ),
    SettingSpec(
        domain=SettingDomain.coach,
        key="deepseek_model_standard",
        env_var="COACH_LLM_DEEPSEEK_MODEL_STANDARD",
        value_type=SettingValueType.string,
        default="deepseek-chat",
        label="DeepSeek Standard Model",
        description="Model name for standard-tier LLM requests",
    ),
    SettingSpec(
        domain=SettingDomain.coach,
        key="deepseek_model_deep",
        env_var="COACH_LLM_DEEPSEEK_MODEL_DEEP",
        value_type=SettingValueType.string,
        default="deepseek-reasoner",
        label="DeepSeek Reasoning Model",
        description="Model name for deep-tier LLM requests (chain-of-thought reasoning)",
    ),
    SettingSpec(
        domain=SettingDomain.coach,
        key="llama_base_url",
        env_var="COACH_LLM_LLAMA_BASE_URL",
        value_type=SettingValueType.string,
        default=None,
        label="Llama API Base URL",
        description="Base URL for the Llama OpenAI-compatible API",
    ),
    SettingSpec(
        domain=SettingDomain.coach,
        key="llama_api_key",
        env_var="COACH_LLM_LLAMA_API_KEY",
        value_type=SettingValueType.string,
        default=None,
        is_secret=True,
        label="Llama API Key",
        description="API key for authenticating with the Llama API",
    ),
    SettingSpec(
        domain=SettingDomain.coach,
        key="llama_model_fast",
        env_var="COACH_LLM_LLAMA_MODEL_FAST",
        value_type=SettingValueType.string,
        default=None,
        label="Llama Fast Model",
        description="Model name for fast-tier Llama requests",
    ),
    SettingSpec(
        domain=SettingDomain.coach,
        key="llama_model_standard",
        env_var="COACH_LLM_LLAMA_MODEL_STANDARD",
        value_type=SettingValueType.string,
        default=None,
        label="Llama Standard Model",
        description="Model name for standard-tier Llama requests",
    ),
    SettingSpec(
        domain=SettingDomain.coach,
        key="llama_model_deep",
        env_var="COACH_LLM_LLAMA_MODEL_DEEP",
        value_type=SettingValueType.string,
        default=None,
        label="Llama Deep Model",
        description="Model name for deep-tier Llama requests (reasoning)",
    ),
    SettingSpec(
        domain=SettingDomain.coach,
        key="timeout_seconds",
        env_var="COACH_LLM_TIMEOUT_S",
        value_type=SettingValueType.integer,
        default=30,
        min_value=5,
        max_value=120,
        label="LLM Request Timeout (seconds)",
        description="Maximum time to wait for an LLM API response",
    ),
    SettingSpec(
        domain=SettingDomain.coach,
        key="max_retries",
        env_var="COACH_LLM_MAX_RETRIES",
        value_type=SettingValueType.integer,
        default=2,
        min_value=0,
        max_value=5,
        label="LLM Max Retries",
        description="Number of repair retries for invalid structured output",
    ),
    # =========================================================================
    # Notifications — Nextcloud Talk
    # =========================================================================
    SettingSpec(
        domain=SettingDomain.notifications,
        key="nextcloud_server_url",
        env_var="NEXTCLOUD_SERVER_URL",
        value_type=SettingValueType.string,
        default="",
        label="Nextcloud Server URL",
        description="Base URL of the Nextcloud server (e.g. https://cloud.example.com)",
    ),
    SettingSpec(
        domain=SettingDomain.notifications,
        key="nextcloud_username",
        env_var="NEXTCLOUD_USERNAME",
        value_type=SettingValueType.string,
        default="",
        label="Nextcloud Username",
        description="Bot account username for sending Talk notifications",
    ),
    SettingSpec(
        domain=SettingDomain.notifications,
        key="nextcloud_password",
        env_var="NEXTCLOUD_PASSWORD",
        value_type=SettingValueType.string,
        default="",
        is_secret=True,
        label="Nextcloud Password",
        description="App password for the bot account (generate in Nextcloud → Security)",
    ),
    SettingSpec(
        domain=SettingDomain.notifications,
        key="nextcloud_request_timeout",
        env_var="NEXTCLOUD_REQUEST_TIMEOUT",
        value_type=SettingValueType.integer,
        default=30,
        min_value=5,
        max_value=120,
        label="Request Timeout (seconds)",
        description="HTTP timeout for Nextcloud API calls",
    ),
    # GL Domain Settings (FX revaluation accounts)
    SettingSpec(
        domain=SettingDomain.gl,
        key="fx_gain_account_id",
        env_var=None,
        value_type=SettingValueType.string,
        default="",
        inherits=False,
        label="FX Gain Account",
        description=(
            "GL account that receives credit-side FX gains during period-end "
            "revaluation. Must be set per organization before FX revaluation "
            "can run."
        ),
    ),
    SettingSpec(
        domain=SettingDomain.gl,
        key="fx_loss_account_id",
        env_var=None,
        value_type=SettingValueType.string,
        default="",
        inherits=False,
        label="FX Loss Account",
        description=(
            "GL account that receives debit-side FX losses during period-end "
            "revaluation. Must be set per organization before FX revaluation "
            "can run."
        ),
    ),
]


# Index the PLATFORM declarations into the leaf registry the ORM listener asks.
# At import time and unconditionally: the listener's question is a security
# check, and a registry populated lazily by whoever happens to import this
# module first would answer "not platform-owned" for every key in a process
# that never imported it — a check that fails open.
for _spec in SETTINGS_SPECS:
    if _spec.scope is SettingScopeAuthority.PLATFORM:
        register_platform_owned(str(_spec.domain), _spec.key)
del _spec


DOMAIN_SETTINGS_SERVICE = {
    SettingDomain.auth: settings_service.auth_settings,
    SettingDomain.audit: settings_service.audit_settings,
    SettingDomain.scheduler: settings_service.scheduler_settings,
    SettingDomain.automation: settings_service.automation_settings,
    SettingDomain.email: settings_service.email_settings,
    SettingDomain.features: settings_service.features_settings,
    SettingDomain.reporting: settings_service.reporting_settings,
    SettingDomain.payments: settings_service.payments_settings,
    SettingDomain.support: settings_service.support_settings,
    SettingDomain.inventory: settings_service.inventory_settings,
    SettingDomain.projects: settings_service.projects_settings,
    SettingDomain.fleet: settings_service.fleet_settings,
    SettingDomain.procurement: settings_service.procurement_settings,
    SettingDomain.settings: settings_service.settings_settings,
    SettingDomain.gl: settings_service.gl_settings,
    SettingDomain.payroll: settings_service.payroll_settings,
    SettingDomain.banking: settings_service.banking_settings,
    SettingDomain.coach: settings_service.coach_settings,
    SettingDomain.notifications: settings_service.notifications_settings,
    SettingDomain.expense: settings_service.expense_settings,
}


def get_spec(domain: SettingDomain, key: str) -> SettingSpec | None:
    for spec in SETTINGS_SPECS:
        if spec.domain == domain and spec.key == key:
            return spec
    return None


def list_specs(domain: SettingDomain) -> list[SettingSpec]:
    return [spec for spec in SETTINGS_SPECS if spec.domain == domain]


def resolve_value(
    db,
    domain: SettingDomain,
    key: str,
    strict: bool = False,
    *,
    organization_id: "UUID | None | _Ambient" = AMBIENT,
) -> object | None:
    """
    Resolve a setting value from database, falling back to spec defaults.

    Args:
        db: Database session
        domain: Setting domain
        key: Setting key
        strict: If True, raise ValueError for required settings that are missing
                or have no default. Use strict=True during startup validation.
        organization_id: Whose value to read. Keyword-only. A UUID reads that
                organization's row falling back to the global one; an explicit
                ``None`` reads the global row and only that. Omitting it uses
                the session's ambient context and logs the call site — that
                fallback is being removed, because a read with no scope
                silently returned the most recently updated row of ANY
                organization.

    Returns:
        Resolved setting value, or None if not found and no default

    Raises:
        ValueError: If strict=True and a required setting is missing/invalid
    """
    spec = get_spec(domain, key)
    if not spec:
        if strict:
            raise ValueError(f"Unknown setting: {domain.value}/{key}")
        return None

    if spec.scope is SettingScopeAuthority.PLATFORM:
        # A platform-owned setting has exactly one scope, so whatever
        # organization the caller passed is DISCARDED rather than honoured.
        # The ORM listener already refuses to create an organization row for
        # such a key; this is the second half of that, and it is what makes a
        # row created outside the ORM — raw SQL, a psql session, a replica
        # that predates the migration — inert rather than merely irregular.
        # `public.domain_settings` carries no RLS policy, so this application
        # layer is the whole boundary; see `app/services/setting_scopes.py`.
        organization_id = None

    service = DOMAIN_SETTINGS_SERVICE.get(domain)
    setting = None
    if service:
        try:
            setting = service.get_by_key(
                db, key, organization_id=organization_id, inherit=spec.inherits
            )
        except HTTPException:
            setting = None

    raw = extract_db_value(setting)

    return coerce_resolved_value(spec, raw, strict=strict)


def coerce_resolved_value(
    spec: SettingSpec, raw: object | None, *, strict: bool = False
) -> object | None:
    """
    Apply a spec's rules to a raw value read out of ``domain_settings``.

    This is everything :func:`resolve_value` does once the stored value is in
    hand: the declared ``value_type``, the ``allowed`` membership check, the
    ``min_value``/``max_value`` bounds, and the fall back to ``spec.default``
    when the stored value fails any of them.

    It lives on its own because the cached read path in
    ``app.services.settings_cache`` must apply exactly the same rules. Two
    implementations meant one key could answer with an out-of-range or
    wrongly-typed value or the spec default depending on which path served the
    request; there is now one implementation and no such divergence.

    Args:
        spec: The registered spec that governs this key
        raw: The value extracted from the row, or None when there is no row
        strict: Raise ``ValueError`` instead of falling back to the default.
            Use during startup validation.
    """
    domain = spec.domain
    key = spec.key

    # For required settings with no value and no default, fail in strict mode
    if raw is None and spec.required and spec.default is None:
        if strict:
            raise ValueError(
                f"Required setting '{domain.value}/{key}' is not configured "
                f"and has no default value"
            )
        # In non-strict mode, log a warning but continue
        import logging

        logging.getLogger(__name__).warning(
            "Required setting %s/%s is missing (no DB value, no default)",
            domain.value,
            key,
        )

    if raw is None:
        raw = spec.default

    value, error = coerce_value(spec, raw)
    if error:
        if strict:
            raise ValueError(f"Invalid value for {domain.value}/{key}: {error}")
        value = spec.default

    if spec.allowed and value is not None and value not in spec.allowed:
        if strict:
            allowed_str = ", ".join(str(v) for v in spec.allowed)
            raise ValueError(
                f"Invalid value '{value}' for {domain.value}/{key}. "
                f"Allowed: {allowed_str}"
            )
        value = spec.default

    if spec.value_type == SettingValueType.integer and value is not None:
        parsed: int | None
        try:
            parsed = int(str(value))
        except (TypeError, ValueError):
            if strict:
                raise ValueError(
                    f"Setting {domain.value}/{key} must be an integer, got: {value}"
                )
            parsed = spec.default if isinstance(spec.default, int) else None
        if (
            spec.min_value is not None
            and parsed is not None
            and parsed < spec.min_value
        ):
            if strict:
                raise ValueError(
                    f"Setting {domain.value}/{key} must be >= {spec.min_value}, got: {parsed}"
                )
            parsed = spec.default if isinstance(spec.default, int) else None
        if (
            spec.max_value is not None
            and parsed is not None
            and parsed > spec.max_value
        ):
            if strict:
                raise ValueError(
                    f"Setting {domain.value}/{key} must be <= {spec.max_value}, got: {parsed}"
                )
            parsed = spec.default if isinstance(spec.default, int) else None
        value = parsed

    return value


def extract_db_value(setting) -> object | None:
    if not setting:
        return None
    if setting.value_text is not None:
        return cast(object, setting.value_text)
    if setting.value_json is not None:
        return cast(object, setting.value_json)
    return None


def coerce_value(spec: SettingSpec, raw: object) -> tuple[object | None, str | None]:
    return coerce_by_value_type(spec.value_type, raw)


def coerce_by_value_type(
    value_type: SettingValueType, raw: object
) -> tuple[object | None, str | None]:
    """
    Coerce a raw stored value to its declared type, returning ``(value, error)``.

    Split out from :func:`coerce_value` so a caller holding a row's own
    ``value_type`` — the cached read path, for a key with no registered spec —
    coerces through the same code rather than a second, hand-rolled copy.
    """
    if raw is None:
        return None, None
    if value_type == SettingValueType.boolean:
        if isinstance(raw, bool):
            return raw, None
        if isinstance(raw, str):
            normalized = raw.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True, None
            if normalized in {"0", "false", "no", "off"}:
                return False, None
        return None, "Value must be boolean"
    if value_type == SettingValueType.integer:
        if isinstance(raw, int):
            return raw, None
        if isinstance(raw, str):
            try:
                return int(raw), None
            except ValueError:
                return None, "Value must be an integer"
        return None, "Value must be an integer"
    if value_type == SettingValueType.string:
        if isinstance(raw, str):
            return raw, None
        return str(raw), None
    return raw, None


def normalize_for_db(
    spec: SettingSpec, value: object
) -> tuple[str | None, object | None]:
    if spec.value_type == SettingValueType.boolean:
        bool_value = bool(value)
        return ("true" if bool_value else "false"), bool_value
    if spec.value_type == SettingValueType.integer:
        return str(int(str(value))), None
    if spec.value_type == SettingValueType.string:
        return str(value), None
    return None, value


def validate_required_settings(db) -> list[str]:
    """
    Validate all required settings are configured.

    Call this during application startup to catch missing configuration early.

    Args:
        db: Database session

    Returns:
        List of error messages for missing/invalid required settings.
        Empty list if all required settings are valid.
    """
    errors = []
    required_specs = [spec for spec in SETTINGS_SPECS if spec.required]

    for spec in required_specs:
        try:
            value = resolve_value(db, spec.domain, spec.key, strict=True)
            if value is None and spec.default is None:
                errors.append(
                    f"Required setting '{spec.domain.value}/{spec.key}' is not configured"
                )
        except ValueError as e:
            errors.append(str(e))

    return errors
