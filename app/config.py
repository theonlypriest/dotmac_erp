import locale
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _derive_default_currency_code() -> str:
    """Infer a default currency from the host locale with safe fallback."""
    code = (locale.getlocale(locale.LC_MONETARY)[0] or "").upper()
    if not code:
        for env_var in ("LC_ALL", "LC_CTYPE", "LANG"):
            raw = os.environ.get(env_var, "")
            if raw:
                code = raw.split(".", 1)[0].upper()
                break
    if "NG" in code:
        return "NGN"
    if "US" in code:
        return "USD"
    if "GB" in code:
        return "GBP"
    if "EU" in code or "DE" in code or "FR" in code:
        return "EUR"
    return "NGN"


DEFAULT_CURRENCY_CODE = _derive_default_currency_code()


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg://postgres:postgres@localhost:5434/dotmac_erp",
    )
    db_pool_size: int = int(os.getenv("DB_POOL_SIZE", "5"))
    db_max_overflow: int = int(os.getenv("DB_MAX_OVERFLOW", "10"))
    db_pool_timeout: int = int(os.getenv("DB_POOL_TIMEOUT", "30"))
    db_pool_recycle: int = int(os.getenv("DB_POOL_RECYCLE", "1800"))
    # Statement timeout in milliseconds (default 30s, 0 = disabled)
    db_statement_timeout_ms: int = int(os.getenv("DB_STATEMENT_TIMEOUT_MS", "30000"))

    # Avatar settings
    avatar_upload_dir: str = os.getenv("AVATAR_UPLOAD_DIR", "static/avatars")
    avatar_max_size_bytes: int = int(
        os.getenv("AVATAR_MAX_SIZE_BYTES", str(2 * 1024 * 1024))
    )  # 2MB
    avatar_allowed_types: str = os.getenv(
        "AVATAR_ALLOWED_TYPES", "image/jpeg,image/png,image/gif,image/webp"
    )
    avatar_url_prefix: str = os.getenv("AVATAR_URL_PREFIX", "/static/avatars")

    # Branding asset uploads
    branding_upload_dir: str = os.getenv("BRANDING_UPLOAD_DIR", "static/branding")
    branding_max_size_bytes: int = int(
        os.getenv("BRANDING_MAX_SIZE_BYTES", str(5 * 1024 * 1024))
    )  # 5MB
    branding_allowed_types: str = os.getenv(
        "BRANDING_ALLOWED_TYPES",
        "image/jpeg,image/png,image/gif,image/webp,image/svg+xml,image/x-icon,image/vnd.microsoft.icon",
    )
    branding_url_prefix: str = os.getenv("BRANDING_URL_PREFIX", "/static/branding")

    # Branding
    app_version: str = os.getenv("APP_VERSION", "1.33.4")
    brand_name: str = os.getenv("BRAND_NAME", "Dotmac ERP")
    brand_tagline: str = os.getenv(
        "BRAND_TAGLINE",
        "Unified ERP for finance, HR, and operations",
    )
    brand_logo_url: str | None = os.getenv("BRAND_LOGO_URL") or None
    brand_mark: str | None = os.getenv("BRAND_MARK") or None  # Auto-derived if not set

    # Module enablement — comma-separated list of enabled modules.
    # Empty/unset = all modules enabled (default behavior).
    # Example: ENABLED_MODULES=people,fleet,fixed_assets,support
    # Core modules (auth, RBAC, audit, settings, notifications, workflows) are always on.
    enabled_modules: str = os.getenv("ENABLED_MODULES", "")

    # Single organization mode - use this org for all operations
    # Set to a UUID to enable single-org mode (no org selection needed)
    default_organization_id: str | None = os.getenv("DEFAULT_ORGANIZATION_ID") or None

    # Default currency (used for admin org creation when no org context)
    default_functional_currency_code: str = os.getenv(
        "DEFAULT_FUNCTIONAL_CURRENCY_CODE",
        DEFAULT_CURRENCY_CODE,
    )
    default_presentation_currency_code: str = os.getenv(
        "DEFAULT_PRESENTATION_CURRENCY_CODE",
        DEFAULT_CURRENCY_CODE,
    )

    # Landing page content (configurable without code changes)
    landing_hero_badge: str = os.getenv("LANDING_HERO_BADGE", "Dotmac ERP")
    landing_hero_title: str = os.getenv(
        "LANDING_HERO_TITLE", "Run your entire business on one ERP"
    )
    landing_hero_subtitle: str = os.getenv(
        "LANDING_HERO_SUBTITLE",
        "Finance, HR, and operations with real-time reporting.",
    )
    landing_cta_primary: str = os.getenv("LANDING_CTA_PRIMARY", "Get started")
    landing_cta_secondary: str = os.getenv("LANDING_CTA_SECONDARY", "Explore modules")
    landing_content_json: str | None = os.getenv("LANDING_CONTENT_JSON") or None

    # Resume uploads (careers portal)
    resume_upload_dir: str = os.getenv("RESUME_UPLOAD_DIR", "uploads/resumes")
    resume_max_size_bytes: int = int(
        os.getenv("RESUME_MAX_SIZE_BYTES", str(5 * 1024 * 1024))
    )  # 5MB default
    resume_allowed_extensions: str = os.getenv(
        "RESUME_ALLOWED_EXTENSIONS", ".pdf,.doc,.docx"
    )

    # CAPTCHA (Cloudflare Turnstile)
    captcha_site_key: str | None = os.getenv("CAPTCHA_SITE_KEY") or None
    captcha_secret_key: str | None = os.getenv("CAPTCHA_SECRET_KEY") or None

    # Generated documents storage
    generated_docs_dir: str = os.getenv("GENERATED_DOCS_DIR", "uploads/generated_docs")

    # Application URL (for email links)
    app_url: str = os.getenv("APP_URL", "http://localhost:8000")

    # ERP has no external-identity protocol adapter. The OIDC_* settings that
    # used to live here were deleted with the unshipped OIDC implementation and
    # must not be restored ad hoc — see docs/oidc_identity_contract.md.

    # ==========================================================================
    # S3 / MinIO Object Storage
    # ==========================================================================
    s3_endpoint_url: str = os.getenv("S3_ENDPOINT_URL", "")
    s3_access_key: str = os.getenv("S3_ACCESS_KEY", "")
    s3_secret_key: str = os.getenv("S3_SECRET_KEY", "")
    s3_bucket_name: str = os.getenv("S3_BUCKET_NAME", "dotmac-erp")
    s3_region: str = os.getenv("S3_REGION", "us-east-1")
    s3_connect_timeout_s: float = float(os.getenv("S3_CONNECT_TIMEOUT_S", "3.0"))
    s3_read_timeout_s: float = float(os.getenv("S3_READ_TIMEOUT_S", "10.0"))

    # Durable bulk imports.  Files spool to disk before the dotmac-files
    # provider streams them, so this ceiling is an admission limit rather than
    # a process-memory allocation.  Partition bounds are passed explicitly to
    # dotmac-imports and therefore remain deployment policy, not module policy.
    import_max_file_size_bytes: int = int(
        os.getenv("IMPORT_MAX_FILE_SIZE_BYTES", str(512 * 1024 * 1024))
    )
    import_partition_rows: int = int(os.getenv("IMPORT_PARTITION_ROWS", "200"))
    import_partition_max_bytes: int = int(
        os.getenv("IMPORT_PARTITION_MAX_BYTES", str(8 * 1024 * 1024))
    )
    import_validation_workers: int = int(os.getenv("IMPORT_VALIDATION_WORKERS", "2"))

    # ==========================================================================
    # Remita Integration (RRR for government payments)
    # ==========================================================================
    # Remita merchant ID
    remita_merchant_id: str = os.getenv("REMITA_MERCHANT_ID", "")
    # Remita API key
    remita_api_key: str = os.getenv("REMITA_API_KEY", "")
    # Production mode (True for live, False for demo/sandbox)
    remita_is_live: bool = os.getenv("REMITA_IS_LIVE", "false").lower() == "true"

    # ==========================================================================
    # dotmac_sub Integration (subscriber management - selfcare.dotmac.io)
    # ==========================================================================
    # Replaces the legacy Splynx ISP-billing feed. Env values are the
    # bootstrap/fallback; per-org credentials managed from the admin UI live in
    # the integration_config table and take precedence (DotmacSubConfig.for_org).
    dotmac_sub_api_url: str = os.getenv("DOTMAC_SUB_API_URL", "")
    # Service bearer token for dotmac_sub. Staff-credential (session->JWT) login
    # has been retired (audit S1) — a service token is required.
    dotmac_sub_api_token: str = os.getenv("DOTMAC_SUB_API_TOKEN", "")
    dotmac_sub_webhook_secret: str | None = (
        os.getenv("DOTMAC_SUB_WEBHOOK_SECRET") or None
    )
    # Inbound-webhook organization attribution (audit D2). Attribution derives
    # from the credential that verified the signature; per-org
    # IntegrationConfig(DOTMAC_SUB) rows are the single definition authority
    # and the env-secret + DEFAULT_ORGANIZATION_ID path is a retiring legacy
    # authority. Modes (validated at startup, app/startup.py):
    #   legacy — old precedence: the env secret attributes to
    #            DEFAULT_ORGANIZATION_ID first, config rows second. Escape
    #            hatch during the retirement window only.
    #   shadow — (default) legacy precedence still decides, but the config-row
    #            resolution ALWAYS runs too; any divergence (different org, or
    #            one authority resolving when the other does not) emits one
    #            structured warning naming both outcomes and the delivery id —
    #            the cutover evidence for flipping to strict.
    #   strict — config rows ONLY: the env path never attributes; ambiguous or
    #            missing bindings fail closed (reject).
    dotmac_sub_webhook_org_resolution: str = os.getenv(
        "DOTMAC_SUB_WEBHOOK_ORG_RESOLUTION", "shadow"
    )
    dotmac_sub_request_timeout: float = float(
        os.getenv("DOTMAC_SUB_REQUEST_TIMEOUT", "60.0")
    )
    dotmac_sub_max_retries: int = int(os.getenv("DOTMAC_SUB_MAX_RETRIES", "3"))
    # dotmac_academy -> ERP training-completion webhook (records EmployeeCertification).
    dotmac_academy_webhook_secret: str | None = (
        os.getenv("DOTMAC_ACADEMY_WEBHOOK_SECRET") or None
    )
    # Route prefix and credential branding were hardcoded; both are deployment
    # facts, not code facts. The default preserves the existing public path.
    dotmac_academy_webhook_prefix: str = os.getenv(
        "DOTMAC_ACADEMY_WEBHOOK_PREFIX", "/dotmac-academy"
    )
    dotmac_academy_issuing_authority: str = os.getenv(
        "DOTMAC_ACADEMY_ISSUING_AUTHORITY", "Dotmac Academy"
    )
    # Staff sync (ERP -> dotmac_sub staff accounts). Disabled unless enabled
    # explicitly; the API key must carry rbac:assign, rbac:roles:read, and
    # operations:service_team:membership.
    dotmac_sub_staff_sync_enabled: bool = (
        os.getenv("DOTMAC_SUB_STAFF_SYNC_ENABLED", "false").lower() == "true"
    )
    dotmac_sub_staff_default_role: str = os.getenv(
        "DOTMAC_SUB_STAFF_DEFAULT_ROLE", "staff"
    )

    # ==========================================================================
    # Mailcow Employee Offboarding
    # ==========================================================================
    mailcow_offboarding_enabled: bool = (
        os.getenv("MAILCOW_OFFBOARDING_ENABLED", "false").lower() == "true"
    )
    mailcow_base_url: str = os.getenv("MAILCOW_BASE_URL", "").rstrip("/")
    mailcow_api_key: str | None = os.getenv("MAILCOW_API_KEY") or None
    mailcow_request_timeout: float = float(os.getenv("MAILCOW_REQUEST_TIMEOUT", "20.0"))
    mailcow_inactive_forward_to: str = os.getenv(
        "MAILCOW_INACTIVE_FORWARD_TO", "inactives@dotmac.ng"
    )
    mailcow_autoresponder_subject: str = os.getenv(
        "MAILCOW_AUTORESPONDER_SUBJECT", "Mailbox no longer monitored"
    )
    mailcow_autoresponder_template: str = os.getenv(
        "MAILCOW_AUTORESPONDER_TEMPLATE",
        (
            "Thank you for your email.\n\n"
            "Please note that {full_name} ({email}) is no longer with "
            "Dotmac Technologies, and this mailbox is no longer being monitored.\n\n"
            "If your enquiry is related to technical support or an existing service, "
            "please contact support@dotmac.ng.\n\n"
            "For sales, new services, or commercial enquiries, please contact "
            "sales@dotmac.ng.\n\n"
            "Your message will not be forwarded automatically, so please resend "
            "your enquiry to the appropriate email address above.\n\n"
            "Thank you for your understanding."
        ),
    )
    mailcow_sieve_host: str = os.getenv("MAILCOW_SIEVE_HOST", "")
    mailcow_sieve_port: int = int(os.getenv("MAILCOW_SIEVE_PORT", "4190"))
    mailcow_sieve_master_user: str | None = (
        os.getenv("MAILCOW_SIEVE_MASTER_USER") or None
    )
    mailcow_sieve_master_password: str | None = (
        os.getenv("MAILCOW_SIEVE_MASTER_PASSWORD") or None
    )
    mailcow_sieve_script_name: str = os.getenv("MAILCOW_SIEVE_SCRIPT_NAME", "sogo")
    mailcow_sieve_use_starttls: bool = (
        os.getenv("MAILCOW_SIEVE_USE_STARTTLS", "true").lower() == "true"
    )
    mailcow_sogo_db_host: str = os.getenv("MAILCOW_SOGO_DB_HOST", "")
    mailcow_sogo_db_port: int = int(os.getenv("MAILCOW_SOGO_DB_PORT", "3306"))
    mailcow_sogo_db_name: str = os.getenv("MAILCOW_SOGO_DB_NAME", "mailcow")
    mailcow_sogo_db_user: str | None = os.getenv("MAILCOW_SOGO_DB_USER") or None
    mailcow_sogo_db_password: str | None = os.getenv("MAILCOW_SOGO_DB_PASSWORD") or None
    mailcow_sogo_cleanup_url: str = os.getenv("MAILCOW_SOGO_CLEANUP_URL", "").rstrip(
        "/"
    )
    mailcow_sogo_cleanup_token: str | None = (
        os.getenv("MAILCOW_SOGO_CLEANUP_TOKEN") or None
    )

    # ==========================================================================
    # Analytics (pre-computed metric snapshots)
    # ==========================================================================
    analytics_enabled: bool = os.getenv("ANALYTICS_ENABLED", "false").lower() == "true"

    # ==========================================================================
    # Coach / Intelligence Engine (hosted Llama + DeepSeek)
    # ==========================================================================
    coach_enabled: bool = os.getenv("COACH_ENABLED", "false").lower() == "true"

    # Backends are expected to expose an OpenAI-compatible Chat Completions API.
    coach_llm_backends: str = os.getenv("COACH_LLM_BACKENDS", "llama,deepseek")
    coach_llm_default_backend: str = os.getenv("COACH_LLM_DEFAULT_BACKEND", "deepseek")
    coach_llm_fast_backend: str = os.getenv("COACH_LLM_FAST_BACKEND", "llama")
    coach_llm_standard_backend: str = os.getenv(
        "COACH_LLM_STANDARD_BACKEND", "deepseek"
    )
    coach_llm_deep_backend: str = os.getenv("COACH_LLM_DEEP_BACKEND", "deepseek")

    # Llama backend
    coach_llm_llama_base_url: str = os.getenv("COACH_LLM_LLAMA_BASE_URL", "")
    coach_llm_llama_api_key: str = os.getenv("COACH_LLM_LLAMA_API_KEY", "")
    coach_llm_llama_model_fast: str = os.getenv("COACH_LLM_LLAMA_MODEL_FAST", "")
    coach_llm_llama_model_standard: str = os.getenv(
        "COACH_LLM_LLAMA_MODEL_STANDARD", ""
    )
    coach_llm_llama_model_deep: str = os.getenv("COACH_LLM_LLAMA_MODEL_DEEP", "")

    # DeepSeek backend
    coach_llm_deepseek_base_url: str = os.getenv("COACH_LLM_DEEPSEEK_BASE_URL", "")
    coach_llm_deepseek_api_key: str = os.getenv("COACH_LLM_DEEPSEEK_API_KEY", "")
    coach_llm_deepseek_model_fast: str = os.getenv("COACH_LLM_DEEPSEEK_MODEL_FAST", "")
    coach_llm_deepseek_model_standard: str = os.getenv(
        "COACH_LLM_DEEPSEEK_MODEL_STANDARD", ""
    )
    coach_llm_deepseek_model_deep: str = os.getenv("COACH_LLM_DEEPSEEK_MODEL_DEEP", "")

    # Mobile push (FCM HTTP v1) — service-account JSON path OR inline JSON.
    # Empty default = push disabled; in-app/polling remains the baseline.
    fcm_service_account_json: str = os.getenv("FCM_SERVICE_ACCOUNT_JSON", "")

    # Reliability + safety
    coach_llm_timeout_s: int = int(os.getenv("COACH_LLM_TIMEOUT_S", "30"))
    coach_llm_max_retries: int = int(os.getenv("COACH_LLM_MAX_RETRIES", "2"))
    coach_llm_max_output_tokens: int = int(
        os.getenv("COACH_LLM_MAX_OUTPUT_TOKENS", "1200")
    )

    # Budgeting + caching
    coach_monthly_token_budget: int = int(
        os.getenv("COACH_MONTHLY_TOKEN_BUDGET", "500000")
    )
    coach_cache_ttl_hours: int = int(os.getenv("COACH_CACHE_TTL_HOURS", "24"))
    coach_max_insights_per_run: int = int(os.getenv("COACH_MAX_INSIGHTS_PER_RUN", "20"))

    # ==========================================================================
    # Licensing (on-premise deployments)
    # ==========================================================================
    # Path to the Ed25519-signed license file
    license_file_path: str = os.getenv("LICENSE_FILE_PATH", "/app/license/dotmac.lic")
    # Development must opt in explicitly; an omitted flag enforces licensing.
    license_dev_mode: bool = os.getenv("DOTMAC_DEV_MODE", "false").lower() in {
        "1",
        "true",
        "yes",
    }

    # ==========================================================================
    # Multi-org session enforcement (see docs/superpowers/plans/2026-05-10-multi-org-listener.md, spec D5)
    # ==========================================================================
    # When True, attach the SQLAlchemy do_orm_execute listener that enforces
    # organization_id filtering on tenant-scoped queries. Enabled by default
    # now that authenticated API and web routes prime session.info["organization_id"]
    # via get_db_with_org / get_db_for_org, or explicitly bypass tenant scoping
    # for genuine cross-tenant operations.
    #
    # Set ENFORCE_ORG_FILTER=false to opt out during an emergency rollback if
    # an unprimed route is found and needs to be fixed before enforcement.
    enforce_org_filter: bool = os.getenv("ENFORCE_ORG_FILTER", "true").lower() == "true"


settings = Settings()
