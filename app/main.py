import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock
from time import monotonic
from unittest.mock import Mock
from urllib.parse import parse_qs, unquote_plus, urlparse
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse, RedirectResponse, Response

# Ensure all models are registered with SQLAlchemy metadata at startup.
import app.models as app_models  # noqa: F401
from app.api.audit import router as audit_router
from app.api.auth import router as auth_router
from app.api.auth_flow import router as auth_flow_router
from app.api.careers import router as careers_api_router
from app.api.coach import router as coach_router
from app.api.deps import require_role, require_tenant_auth
from app.api.dotmac_academy import webhook_router as dotmac_academy_webhook_router
from app.api.dotmac_sub import webhook_router as dotmac_sub_webhook_router
from app.api.expense import router as expense_router
from app.api.expense_limits import router as expense_limits_router
from app.api.files import legacy_router as files_legacy_router
from app.api.files import router as files_router
from app.api.finance import (
    analysis_router,
    ap_router,
    ar_router,
    banking_router,
    cons_router,
    fx_router,
    gl_router,
    import_export_router,
    ipsas_router,
    lease_router,
    mono_webhook_router,
    ncc_router as finance_ncc_router,
    opening_balance_router,
    payments_router,
    payments_webhook_router,
    rpt_router,
    search_router,
    tax_router,
)
from app.api.fixed_assets import router as fa_api_router
from app.api.fleet import router as fleet_router
from app.api.inventory import router as inv_api_router
from app.api.me import router as me_router
from app.api.people import router as people_hr_router
from app.api.persons import router as people_router
from app.api.pm import router as pm_router
from app.api.procurement import router as procurement_router
from app.api.rbac import router as rbac_router
from app.api.scheduler import router as scheduler_router
from app.api.service_hooks import router as service_hooks_router
from app.api.settings import router as settings_router
from app.api.support import router as support_router
from app.api.sync.dotmac_sub import router as sub_sync_router
from app.api.sync.backoffice_people import router as backoffice_people_router
from app.api.sync.staff_access import router as staff_access_router
from app.api.sync.sub_attendance import router as sub_attendance_router
from app.api.workflow_tasks import router as workflow_tasks_router
from app.config import settings
from app.db import SessionLocal
from app.db.session_context import allow_cross_org, prime_tenant_context
from app.errors import register_error_handlers
from app.logging import configure_logging
from app.middleware.csp import add_unsafe_eval_to_csp
from app.middleware.rate_limit import rate_limit_middleware
from app.monitoring import setup_monitoring
from app.models.domain_settings import DomainSetting, SettingDomain
from app.observability import ObservabilityMiddleware
from app.services import audit as audit_service
from app.services.htmx import is_htmx_request
from app.services.integration_config import seed_dotmac_sub_webhook_binding
from app.services.settings_seed import seed_all_settings
from app.ui import UI_ASSET_DIRECTORY, UI_ASSET_MOUNT
from app.startup import (
    log_startup_info,
    validate_startup,
    warn_unconfigured_webhook_allowlist,
)
from app.telemetry import setup_otel
from app.templates import templates
from app.web.admin import router as admin_web_router
from app.web.admin_batch_operations import router as admin_batch_operations_router
from app.web.admin_sla_policies import router as admin_sla_policies_web_router
from app.web.auth import router as auth_web_router
from app.web.careers import router as careers_web_router
from app.web.careers import short_router as careers_short_web_router
from app.web.coach import router as coach_web_router
from app.web.csrf import csrf_middleware
from app.web.finance import automation_router as automation_web_router
from app.web.finance import expense_router as expense_web_router
from app.web.finance import router as finance_web_router
from app.web.finance import settings_router as finance_settings_web_router
from app.web.fixed_assets import router as fixed_assets_web_router
from app.web.fleet import router as fleet_web_router
from app.web.help import router as help_web_router
from app.web.inventory import router as inventory_web_router
from app.web.notifications import router as notifications_web_router
from app.web.onboarding_portal import router as onboarding_portal_router
from app.web.payroll_alias import router as payroll_alias_web_router
from app.web.people import router as people_web_router
from app.web.procurement import router as procurement_web_router
from app.web.profile import router as profile_web_router
from app.web.projects import router as projects_web_router
from app.web.public_sector import router as public_sector_web_router
from app.web.settings import router as module_settings_web_router
from app.web.sla_policies import router as sla_policies_web_router
from app.web.support import router as support_web_router
from app.web.workflow_tasks import router as workflow_tasks_web_router
from app.web_home import router as web_home_router

# ---------------------------------------------------------------------------
# Module enablement — driven by ENABLED_MODULES env var.
# Empty = all modules on (default). Core is always on.
# ---------------------------------------------------------------------------
_ALL_MODULES = frozenset(
    {
        "finance",
        "people",
        "fleet",
        "fixed_assets",
        "support",
        "procurement",
        "projects",
        "expense",
        "inventory",
        "coach",
        "training",
        "public_sector",
    }
)

_raw_enabled = os.getenv("ENABLED_MODULES", "").strip()
_ENABLED_MODULES: frozenset[str] = (
    frozenset(m.strip() for m in _raw_enabled.split(",") if m.strip())
    if _raw_enabled
    else _ALL_MODULES
)

logger = logging.getLogger(__name__)
logger.info("Enabled modules: %s", sorted(_ENABLED_MODULES))


def is_module_enabled(module: str) -> bool:
    """Check if a module is enabled for this deployment.

    A module must pass two gates:
    1. ``ENABLED_MODULES`` env var (deployment config)
    2. License entitlement (on-prem only; dev mode = all modules)
    """
    if module not in _ENABLED_MODULES:
        return False
    from app.licensing.enforcement import get_licensed_modules

    licensed = get_licensed_modules()  # None in dev mode
    if licensed is None:
        return True
    return module in licensed


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Log startup info for debugging
    log_startup_info()

    db = SessionLocal()
    try:
        # Validate required configuration before accepting requests
        # This will exit(1) if critical config is missing
        validate_startup(db, exit_on_failure=True)

        # Seed default settings for all domains
        if settings.default_organization_id:
            prime_tenant_context(db, UUID(settings.default_organization_id))
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(451002, 20260522)"))
        with allow_cross_org(db):
            seed_all_settings(db)
            # Audit D2: migrate the env webhook secret into the default org's
            # IntegrationConfig(DOTMAC_SUB) binding (idempotent; the binding
            # rows are the webhook org-attribution authority).
            seed_dotmac_sub_webhook_binding(db)

        # The warning must observe the post-seed state. Running it from
        # `validate_startup` above produced a false outage warning on first
        # boot immediately before the configured environment value was seeded.
        warn_unconfigured_webhook_allowlist(db)

        # Register payroll lifecycle event handlers so posted runs/slips
        # can create notifications and queue payslip emails.
        from app.services.people.payroll.event_handlers import (
            register_payroll_handlers,
        )

        register_payroll_handlers()
        if os.getenv("DOTMAC_PRECOMPUTE_OPENAPI", "false").lower() == "true":
            try:
                _get_cached_openapi_schema()
            except Exception:
                logger.exception("Failed to precompute OpenAPI schema during startup")
    finally:
        db.close()
    yield


app = FastAPI(title="DotMac ERP API", lifespan=lifespan)


# Redirect legacy AR customers path to web UI (avoid JSON API response)
@app.get("/ar/customers", include_in_schema=False)
@app.get("/ar/customers/", include_in_schema=False)
def ar_customers_redirect(request: Request):
    query = request.url.query
    url = "/finance/ar/customers"
    if query:
        url = f"{url}?{query}"
    return RedirectResponse(url=url, status_code=302)


_AUDIT_SETTINGS_CACHE: dict | None = None
_AUDIT_SETTINGS_CACHE_AT: float | None = None
_AUDIT_SETTINGS_CACHE_TTL_SECONDS = 30.0
_AUDIT_SETTINGS_LOCK = Lock()
_runtime_observability_pid: int | None = None
_OPENAPI_SCHEMA_LOCK = Lock()


def _get_cached_openapi_schema() -> dict:
    with _OPENAPI_SCHEMA_LOCK:
        if app.openapi_schema is None:
            app.openapi_schema = app.openapi()
        return app.openapi_schema


def bootstrap_runtime_observability(app_instance: FastAPI | None = None) -> None:
    """Configure logging/monitoring once per process.

    Gunicorn runs with ``preload_app = True`` in production, so runtime
    integrations that own process-local resources (threads, sockets, handlers)
    must be initialized after fork in worker processes, not during master
    import. Direct app execution still initializes immediately below.
    """
    global _runtime_observability_pid  # noqa: PLW0603

    pid = os.getpid()
    if _runtime_observability_pid == pid:
        return

    configure_logging()
    setup_monitoring()
    setup_otel(app_instance)
    _runtime_observability_pid = pid


if os.getenv("DOTMAC_DEFER_RUNTIME_OBSERVABILITY", "").strip().lower() not in {
    "1",
    "true",
    "yes",
    "on",
}:
    bootstrap_runtime_observability(app)

# Register automatic ORM audit listeners (captures all model changes)
from app.services.audit_listener import register_audit_listeners  # noqa: E402

register_audit_listeners()

# Register field-level change tracking (user-facing change history)
from app.services.audit.field_tracker import register_field_tracking  # noqa: E402

register_field_tracking()

app.add_middleware(ObservabilityMiddleware)
register_error_handlers(app)
# Rate limiting must come before CSRF to reject early
app.middleware("http")(rate_limit_middleware)
app.middleware("http")(csrf_middleware)


def _is_html_request(request: Request) -> bool:
    """Check if request expects HTML."""
    accept = request.headers.get("accept", "")
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        return False
    if request.url.path.startswith("/api/"):
        return False
    if request.url.path.startswith("/auth/") and not request.url.path.startswith(
        "/auth/me"
    ):
        return False
    if "text/html" in accept:
        return True
    return not request.url.path.startswith("/api/")


def _map_error_token_to_status(error_value: str) -> int:
    token = error_value.strip().lower().replace(" ", "_")
    if token in {"forbidden", "access_denied", "unauthorized"}:
        return 403
    if token in {"not_found", "missing", "does_not_exist"}:
        return 404
    return 400


def _friendly_redirect_error_message(error_value: str, status_code: int) -> str:
    token = error_value.strip().lower().replace(" ", "_")
    if token in {"forbidden", "access_denied", "unauthorized"}:
        return "You do not have permission to access this page."
    if token in {"not_found", "missing", "does_not_exist"}:
        return "The requested item could not be found."
    if status_code == 400 and token in {"invalid", "bad_request"}:
        return "Some required information is missing or invalid. Please check the form and try again."
    if "_" in error_value and error_value.lower() == token:
        return error_value.replace("_", " ").capitalize()
    return error_value


def _is_no_response_runtime_error(exc: BaseException) -> bool:
    """Return True only when the whole failure is a response-stream disconnect."""
    if isinstance(exc, RuntimeError) and str(exc) == "No response returned.":
        return True

    try:
        from anyio import EndOfStream

        if isinstance(exc, EndOfStream):
            return True
    except Exception:
        # Compatibility guard in case anyio isn't available or class resolution fails.
        if exc.__class__.__name__ == "EndOfStream":
            return True

    children = getattr(exc, "exceptions", None)
    if children:
        # BaseHTTPMiddleware can combine a real application error with an
        # EndOfStream. Treating "any disconnect-shaped child" as a disconnect
        # swallowed the real exception and returned an empty 204. A group is a
        # disconnect only when every child is one.
        return all(
            isinstance(child, BaseException) and _is_no_response_runtime_error(child)
            for child in children
        )

    return False


@app.middleware("http")
async def csp_middleware(request: Request, call_next):
    try:
        response = await call_next(request)
    except BaseException as exc:
        if _is_no_response_runtime_error(exc):
            logger.info(
                "Request ended before response was returned: %s %s",
                request.method,
                request.url.path,
            )
            return _client_disconnect_response()
        raise

    response.headers["Content-Security-Policy"] = add_unsafe_eval_to_csp(
        response.headers.get("Content-Security-Policy")
    )
    route = getattr(request, "scope", {}).get("route")
    allow_sla_document_frame = (
        getattr(route, "name", None) == "sla_policy_document_view"
        and getattr(request.state, "allow_sla_document_frame", False) is True
    )
    response.headers["X-Frame-Options"] = (
        "SAMEORIGIN" if allow_sla_document_frame else "DENY"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Strict-Transport-Security"] = (
        "max-age=31536000; includeSubDomains"
    )
    return response


def _client_disconnect_response() -> Response:
    """Return a minimal response for requests that ended before a response was sent."""
    return Response(status_code=204)


@app.middleware("http")
async def redirect_error_template_middleware(request: Request, call_next):
    """Convert redirect error query params into user-facing error templates."""
    response = await call_next(request)

    if not _is_html_request(request):
        return response
    if is_htmx_request(request):
        return response
    if response.status_code not in {301, 302, 303, 307, 308}:
        return response

    location = response.headers.get("location")
    if not location or "error=" not in location:
        return response

    try:
        parsed = urlparse(location)
        params = parse_qs(parsed.query or "")
        raw_error = params.get("error", [None])[0]
        if not raw_error:
            return response
        raw_message = unquote_plus(str(raw_error)).strip()
        status_code = _map_error_token_to_status(raw_message)
        message = _friendly_redirect_error_message(raw_message, status_code)
        template_name = f"errors/{status_code}.html"
        return templates.TemplateResponse(
            request,
            template_name,
            {"message": message},
            status_code=status_code,
        )
    except Exception:
        return response


# Sensitive parameter names to redact from audit logs
_SENSITIVE_PARAMS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "api-key",
        "auth",
        "authorization",
        "credential",
        "credentials",
        "access_token",
        "refresh_token",
        "private_key",
        "privatekey",
    }
)


def _sanitize_query_params(params: dict) -> dict:
    """Redact sensitive query parameters from audit logs."""
    return {
        k: "***REDACTED***" if k.lower() in _SENSITIVE_PARAMS else v
        for k, v in params.items()
    }


def _extract_audit_data(request: Request, response: Response) -> dict:
    """Extract audit data from request/response for async logging."""
    from app.models.audit import AuditActorType
    from app.services.common import coerce_uuid

    request_state = getattr(request, "state", None)
    state_actor_id = getattr(request_state, "actor_id", None) if request_state else None
    state_actor_type = (
        getattr(request_state, "actor_type", None) if request_state else None
    )
    state_request_id = (
        getattr(request_state, "request_id", None) if request_state else None
    )
    state_organization_id = (
        getattr(request_state, "organization_id", None) if request_state else None
    )

    actor_id = request.headers.get("x-actor-id") or state_actor_id
    if actor_id is not None:
        actor_id = str(actor_id)
    actor_person_id = None
    if actor_id:
        try:
            actor_person_id = coerce_uuid(actor_id, raise_http=False)
        except (TypeError, ValueError):
            actor_person_id = None

    actor_type = request.headers.get("x-actor-type")
    if not actor_type and state_actor_type:
        actor_type = str(state_actor_type)
    if not actor_type:
        actor_type = (
            AuditActorType.user.value if actor_id else AuditActorType.system.value
        )

    request_id = request.headers.get("x-request-id") or state_request_id
    organization_id = state_organization_id
    entity_id = request.headers.get("x-entity-id")
    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")

    try:
        query_params = _sanitize_query_params(dict(request.query_params))
    except (KeyError, Exception):
        query_params = {}

    return {
        "actor_type": actor_type,
        "organization_id": organization_id,
        "actor_person_id": str(actor_person_id) if actor_person_id else None,
        "actor_id": actor_id,
        "action": request.method,
        "entity_type": request.url.path,
        "entity_id": entity_id,
        "status_code": response.status_code,
        "is_success": response.status_code < 400,
        "ip_address": ip_address,
        "user_agent": user_agent,
        "request_id": request_id,
        "metadata_": {
            "path": request.url.path,
            "query": query_params,
        },
    }


@app.middleware("http")
async def audit_middleware(request: Request, call_next):
    from app.tasks.audit import log_audit_event

    response: Response
    path = request.url.path
    logger = logging.getLogger(__name__)
    db = SessionLocal()
    try:
        audit_settings = _load_audit_settings(db)
    except BaseException as exc:
        # Fail open for audit to preserve availability when DB is down.
        # Use conservative defaults (disabled).
        logger.warning("Audit settings unavailable, skipping audit: %s", exc)
        return await call_next(request)
    finally:
        db.close()
    if not audit_settings["enabled"]:
        return await call_next(request)
    header_key = audit_settings.get("read_trigger_header") or ""
    header_value = request.headers.get(header_key, "") if header_key else ""
    track_read = request.method == "GET" and (
        (header_value or "").lower() == "true"
        or request.query_params.get(audit_settings["read_trigger_query"]) == "true"
    )
    should_log = request.method in audit_settings["methods"] or track_read
    if _is_audit_path_skipped(path, audit_settings["skip_paths"]):
        should_log = False
    try:
        response = await call_next(request)
    except BaseException as exc:
        if _is_no_response_runtime_error(exc):
            logger.info(
                "Skipping audit log for request without response: %s %s",
                request.method,
                request.url.path,
            )
            return _client_disconnect_response()
        if should_log:
            # Log error response asynchronously (or synchronously in tests)
            audit_response = Response(status_code=500)
            if os.getenv("PYTEST_CURRENT_TEST"):
                with SessionLocal() as log_db:
                    audit_service.audit_events.log_request(
                        log_db, request, audit_response
                    )
            else:
                audit_data = _extract_audit_data(request, audit_response)
                log_audit_event.delay(**audit_data)
        raise
    if should_log:
        # Log response asynchronously via Celery (or synchronously in tests)
        if os.getenv("PYTEST_CURRENT_TEST"):
            with SessionLocal() as log_db:
                audit_service.audit_events.log_request(log_db, request, response)
        else:
            audit_data = _extract_audit_data(request, response)
            log_audit_event.delay(**audit_data)
    return response


def _load_audit_settings(db: Session):
    global _AUDIT_SETTINGS_CACHE, _AUDIT_SETTINGS_CACHE_AT
    now = monotonic()
    with _AUDIT_SETTINGS_LOCK:
        if (
            _AUDIT_SETTINGS_CACHE
            and _AUDIT_SETTINGS_CACHE_AT
            and now - _AUDIT_SETTINGS_CACHE_AT < _AUDIT_SETTINGS_CACHE_TTL_SECONDS
        ):
            return _AUDIT_SETTINGS_CACHE
    defaults = {
        "enabled": True,
        "methods": {"POST", "PUT", "PATCH", "DELETE"},
        "skip_paths": ["/static", "/web", "/health"],
        "read_trigger_header": "x-audit-read",
        "read_trigger_query": "audit",
    }
    if not db.info.get("organization_id") and settings.default_organization_id:
        prime_tenant_context(db, UUID(settings.default_organization_id))
    with allow_cross_org(db):
        rows = list(
            db.scalars(
                select(DomainSetting)
                .where(DomainSetting.domain == SettingDomain.audit)
                .where(DomainSetting.is_active.is_(True))
            ).all()
        )
    if isinstance(rows, Mock):
        rows = []
    values = {row.key: row for row in rows}
    if "enabled" in values:
        defaults["enabled"] = _to_bool(values["enabled"])
    if "methods" in values:
        defaults["methods"] = _to_list(values["methods"], upper=True)
    if "skip_paths" in values:
        defaults["skip_paths"] = _to_list(values["skip_paths"], upper=False)
    if "read_trigger_header" in values:
        defaults["read_trigger_header"] = _to_str(values["read_trigger_header"])
    if "read_trigger_query" in values:
        defaults["read_trigger_query"] = _to_str(values["read_trigger_query"])
    with _AUDIT_SETTINGS_LOCK:
        _AUDIT_SETTINGS_CACHE = defaults
        _AUDIT_SETTINGS_CACHE_AT = now
    return defaults


def _to_bool(setting: DomainSetting) -> bool:
    value = setting.value_json if setting.value_json is not None else setting.value_text
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _to_str(setting: DomainSetting) -> str:
    value = setting.value_text if setting.value_text is not None else setting.value_json
    if value is None:
        return ""
    return str(value)


def _to_list(setting: DomainSetting, upper: bool) -> set[str] | list[str]:
    value = setting.value_json if setting.value_json is not None else setting.value_text
    items: list[str]
    if isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
    elif isinstance(value, str):
        items = [item.strip() for item in value.split(",") if item.strip()]
    else:
        items = []
    if upper:
        return {item.upper() for item in items}
    return items


def _is_audit_path_skipped(path: str, skip_paths: list[str]) -> bool:
    return any(path.startswith(prefix) for prefix in skip_paths)


def _include_api_router(router, dependencies=None):
    """Mount an API router at both the bare path and ``/api/v1``.

    Versioning policy:

    - ``/api/v1/<path>`` is the **canonical** surface. New external clients,
      docs, and integrations must use it.
    - The bare ``/<path>`` mount is a **legacy compatibility alias** kept for
      older in-tree callers and the server-rendered UI's fetches. Both mounts
      share identical dependencies and response models, so they never diverge.
    - When a breaking change is needed, introduce ``/api/v2`` as a *new* router
      mounted with ``prefix="/api/v2"`` (do not re-point this helper), and
      deprecate ``/api/v1`` on a published timeline rather than mutating it.

    Web (HTML) routers are intentionally NOT routed through this helper — they
    mount once at their module path.
    """
    app.include_router(router, dependencies=dependencies)
    app.include_router(router, prefix="/api/v1", dependencies=dependencies)


# ---------------------------------------------------------------------------
# Core routers (always on)
# ---------------------------------------------------------------------------
_include_api_router(auth_router, dependencies=[Depends(require_role("admin"))])
_include_api_router(auth_flow_router)
_include_api_router(rbac_router, dependencies=[Depends(require_tenant_auth)])
_include_api_router(me_router)
_include_api_router(workflow_tasks_router, dependencies=[Depends(require_tenant_auth)])
_include_api_router(audit_router)
app.include_router(
    settings_router,
    prefix="/api/v1",
    dependencies=[Depends(require_tenant_auth)],
)
_include_api_router(scheduler_router, dependencies=[Depends(require_tenant_auth)])
_include_api_router(service_hooks_router, dependencies=[Depends(require_tenant_auth)])
app.include_router(web_home_router)
app.include_router(sla_policies_web_router)
app.include_router(help_web_router)
app.include_router(auth_web_router)
app.include_router(admin_web_router)
app.include_router(admin_sla_policies_web_router)
app.include_router(admin_batch_operations_router)
app.include_router(profile_web_router)
app.include_router(notifications_web_router)
app.include_router(workflow_tasks_web_router)
app.include_router(careers_api_router, prefix="/api/v1")
app.include_router(careers_web_router)
app.include_router(careers_short_web_router)
# ---------------------------------------------------------------------------
# People/HR module
# ---------------------------------------------------------------------------
if is_module_enabled("people"):
    app.include_router(
        people_router,
        prefix="/api/v1",
        dependencies=[Depends(require_tenant_auth)],
    )
    app.include_router(
        people_hr_router,
        prefix="/api/v1",
        dependencies=[Depends(require_tenant_auth)],
    )
    app.include_router(people_web_router)
    app.include_router(payroll_alias_web_router)
    app.include_router(onboarding_portal_router)
    _include_api_router(sub_attendance_router)
    app.include_router(staff_access_router, prefix="/api/v1")
    app.include_router(backoffice_people_router, prefix="/api/v1")

# ---------------------------------------------------------------------------
# Finance module
# ---------------------------------------------------------------------------
if is_module_enabled("finance"):
    app.include_router(finance_web_router, prefix="/finance")
    app.include_router(
        finance_settings_web_router
    )  # Has its own /settings prefix (finance)
    app.include_router(automation_web_router)  # Has its own /automation prefix
    _include_api_router(gl_router, dependencies=[Depends(require_tenant_auth)])
    _include_api_router(ap_router, dependencies=[Depends(require_tenant_auth)])
    _include_api_router(ar_router, dependencies=[Depends(require_tenant_auth)])
    _include_api_router(lease_router, dependencies=[Depends(require_tenant_auth)])
    _include_api_router(tax_router, dependencies=[Depends(require_tenant_auth)])
    _include_api_router(cons_router, dependencies=[Depends(require_tenant_auth)])
    _include_api_router(rpt_router, dependencies=[Depends(require_tenant_auth)])
    _include_api_router(finance_ncc_router, dependencies=[Depends(require_tenant_auth)])
    _include_api_router(banking_router, dependencies=[Depends(require_tenant_auth)])
    _include_api_router(
        import_export_router, dependencies=[Depends(require_tenant_auth)]
    )
    _include_api_router(
        opening_balance_router, dependencies=[Depends(require_tenant_auth)]
    )
    _include_api_router(search_router, dependencies=[Depends(require_tenant_auth)])
    _include_api_router(payments_router, dependencies=[Depends(require_tenant_auth)])
    _include_api_router(fx_router, dependencies=[Depends(require_tenant_auth)])
    _include_api_router(analysis_router, dependencies=[Depends(require_tenant_auth)])
    _include_api_router(payments_webhook_router)
    _include_api_router(mono_webhook_router)
    _include_api_router(dotmac_sub_webhook_router)
    _include_api_router(dotmac_academy_webhook_router)

# ---------------------------------------------------------------------------
# Expense module
# ---------------------------------------------------------------------------
if is_module_enabled("expense"):
    app.include_router(expense_web_router)
    _include_api_router(expense_router, dependencies=[Depends(require_tenant_auth)])
    _include_api_router(
        expense_limits_router, dependencies=[Depends(require_tenant_auth)]
    )

app.include_router(module_settings_web_router)  # /settings/* generic module routes

# ---------------------------------------------------------------------------
# Support/Helpdesk module
# ---------------------------------------------------------------------------
if is_module_enabled("support"):
    app.include_router(
        support_router,
        prefix="/api/v1",
        dependencies=[Depends(require_tenant_auth)],
    )
    app.include_router(support_web_router)

# ---------------------------------------------------------------------------
# Fleet Management module
# ---------------------------------------------------------------------------
if is_module_enabled("fleet"):
    app.include_router(
        fleet_router,
        prefix="/api/v1",
        dependencies=[Depends(require_tenant_auth)],
    )
    app.include_router(fleet_web_router)

# ---------------------------------------------------------------------------
# Fixed Assets module
# ---------------------------------------------------------------------------
if is_module_enabled("fixed_assets"):
    app.include_router(
        fa_api_router,
        prefix="/api/v1",
        dependencies=[Depends(require_tenant_auth)],
    )
    app.include_router(fixed_assets_web_router)

# ---------------------------------------------------------------------------
# Inventory module
# ---------------------------------------------------------------------------
if is_module_enabled("inventory"):
    app.include_router(
        inv_api_router,
        prefix="/api/v1",
        dependencies=[Depends(require_tenant_auth)],
    )
    app.include_router(inventory_web_router)

# ---------------------------------------------------------------------------
# Procurement module
# ---------------------------------------------------------------------------
if is_module_enabled("procurement"):
    app.include_router(
        procurement_router,
        prefix="/api/v1",
        dependencies=[Depends(require_tenant_auth)],
    )
    app.include_router(procurement_web_router)

# ---------------------------------------------------------------------------
# Project Management module
# ---------------------------------------------------------------------------
if is_module_enabled("projects"):
    app.include_router(
        pm_router,
        prefix="/api/v1",
        dependencies=[Depends(require_tenant_auth)],
    )
    app.include_router(projects_web_router)

# ---------------------------------------------------------------------------
# Sub operational observations are authenticated by their own service-principal
# guards and are independent of every optional ERP feature flag.
# ---------------------------------------------------------------------------
_include_api_router(sub_sync_router)

# ---------------------------------------------------------------------------
# Coach/Intelligence module
# ---------------------------------------------------------------------------
if is_module_enabled("coach"):
    app.include_router(
        coach_router,
        prefix="/api/v1",
        dependencies=[Depends(require_tenant_auth)],
    )
    app.include_router(coach_web_router)

# ---------------------------------------------------------------------------
# Public Sector (IPSAS) module
# ---------------------------------------------------------------------------
if is_module_enabled("public_sector"):
    _include_api_router(ipsas_router, dependencies=[Depends(require_tenant_auth)])
    app.include_router(public_sector_web_router)

# Authenticated file downloads (S3-backed)
app.include_router(files_router)  # /files/* (avatars, resumes, attachments, etc.)
app.include_router(files_legacy_router)  # /uploads/* (legacy URL compat)

static_dir = Path(__file__).resolve().parent.parent / "static"
app.mount(
    UI_ASSET_MOUNT,
    StaticFiles(directory=str(UI_ASSET_DIRECTORY)),
    name="dotmac_ui_static",
)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return RedirectResponse(url="/static/favicon.svg")


@app.get("/favicon.svg", include_in_schema=False)
def favicon_svg():
    """Backward-compatible favicon path used by some branding configs."""
    return RedirectResponse(url="/static/favicon.svg")


@app.get("/v2/api-docs", include_in_schema=False)
def legacy_api_docs():
    """Legacy OpenAPI alias used by older tooling."""
    return JSONResponse(content=_get_cached_openapi_schema())


@app.get("/health")
def health_check():
    """Basic health check endpoint (backwards compatibility)."""
    return {"status": "ok"}


@app.get("/health/monitoring")
def monitoring_health(request: Request):
    """Report status of external monitoring integrations (Loki, Sentry/GlitchTip).

    Returns per-integration health with delivery stats so operators can detect
    silent failures (e.g. Loki handler dying after a connection timeout).
    """
    if not _monitoring_authorized(request):
        return Response(status_code=404)

    from app.monitoring import get_monitoring_status
    from app.telemetry import get_otel_status

    status = get_monitoring_status()
    status["tracing"] = get_otel_status()

    # Determine overall health: Loki healthy if enabled and no sustained failures
    loki = status["loki"]
    loki_healthy = not loki["enabled"] or loki["consecutive_failures"] < 5
    sentry = status["sentry"]
    sentry_healthy = not sentry["enabled"] or (
        sentry.get("dsn_configured", False) and sentry.get("transport_healthy", False)
    )
    tracing = status["tracing"]
    tracing_healthy = not tracing["enabled"] or bool(tracing.get("initialized", False))

    overall = (
        "healthy"
        if (loki_healthy and sentry_healthy and tracing_healthy)
        else "degraded"
    )
    code = 200 if overall == "healthy" else 503

    return JSONResponse(
        content={"status": overall, "integrations": status},
        status_code=code,
    )


@app.get("/health/live")
def liveness_probe():
    """Liveness probe - checks if the process is running.

    Kubernetes uses this to determine if the container should be restarted.
    This should always return 200 if the Python process is running.
    """
    return {"status": "alive"}


@app.get("/health/ready")
def readiness_probe():
    """Readiness probe - checks if the app is ready to serve traffic.

    Kubernetes uses this to determine if the pod should receive traffic.
    Checks critical dependencies (database, etc.).

    Returns:
        200: Ready to serve traffic
        503: Not ready (dependency failure)
    """
    from app.dependency_health import collect_dependency_health, readiness_failures

    checks = {
        "database": _check_database(),
        "redis": _check_redis(),
    }
    dependencies = collect_dependency_health()
    required_dependency_failures = readiness_failures(dependencies)

    core_healthy = all(check["healthy"] for check in checks.values())
    all_healthy = core_healthy and not required_dependency_failures
    degraded_dependencies = {
        name: check
        for name, check in dependencies.items()
        if bool(check["configured"])
        and not bool(check["healthy"])
        and not bool(check["required"])
    }

    if all_healthy and degraded_dependencies:
        status = "ready_with_degraded_dependencies"
    elif all_healthy:
        status = "ready"
    else:
        status = "not_ready"

    payload = {
        "status": status,
        "checks": checks,
        "dependencies": dependencies,
    }

    if all_healthy:
        return payload
    else:
        return Response(
            content=JSONResponse(content=payload).body,
            status_code=503,
            media_type="application/json",
        )


def _check_database() -> dict:
    """Check database connectivity."""
    try:
        db = SessionLocal()
        try:
            # Simple query to verify connection
            db.execute(text("SELECT 1"))
            return {"healthy": True, "message": "Connected"}
        finally:
            db.close()
    except Exception as e:
        return {"healthy": False, "message": str(e)[:100]}


def _check_redis() -> dict:
    """Check Redis connectivity (optional dependency)."""
    import os

    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        return {"healthy": True, "message": "Not configured (optional)"}

    try:
        import redis

        client = redis.from_url(redis_url, socket_connect_timeout=2)
        client.ping()
        return {"healthy": True, "message": "Connected"}
    except ImportError:
        return {"healthy": False, "message": "Redis package not installed"}
    except Exception as e:
        return {"healthy": False, "message": str(e)[:100]}


def _bool_env_enabled(*keys: str) -> bool:
    return any(
        os.getenv(key, "").strip().lower() in {"1", "true", "yes", "on"} for key in keys
    )


def _observability_endpoint_authorized(
    request: Request,
    *,
    token_env_keys: tuple[str, ...],
    disable_env_keys: tuple[str, ...] = (),
) -> bool:
    if _bool_env_enabled(*disable_env_keys):
        return True

    for key in token_env_keys:
        token = os.getenv(key, "").strip()
        if token:
            return _token_matches(request, token)

    # If no token configured, only allow local access.
    return bool(request.client and request.client.host in {"127.0.0.1", "::1"})


def _token_matches(request: Request, token: str) -> bool:
    """Accept the fleet-standard bearer header, or the legacy header.

    Fleet standard (matches the observe stack's minio/openbao/academy scrapes,
    and lets Prometheus use `authorization: credentials_file:` rather than a
    custom header): ``Authorization: Bearer <token>``.

    ``x-metrics-token`` stays supported so existing scrapers and runbooks keep
    working; it is deprecated and should be retired once nothing uses it.
    Comparisons are constant-time — these endpoints are reachable publicly.
    """
    authorization = request.headers.get("authorization", "").strip()
    if authorization.lower().startswith("bearer "):
        if secrets.compare_digest(authorization[7:].strip(), token):
            return True
    legacy = request.headers.get("x-metrics-token", "").strip()
    return bool(legacy) and secrets.compare_digest(legacy, token)


def _metrics_authorized(request: Request) -> bool:
    return _observability_endpoint_authorized(
        request,
        token_env_keys=("METRICS_TOKEN",),
        disable_env_keys=("METRICS_AUTH_DISABLED",),
    )


def _monitoring_authorized(request: Request) -> bool:
    return _observability_endpoint_authorized(
        request,
        token_env_keys=("MONITORING_TOKEN", "METRICS_TOKEN"),
        disable_env_keys=("MONITORING_AUTH_DISABLED", "METRICS_AUTH_DISABLED"),
    )


@app.get("/metrics")
def metrics(request: Request):
    if not _metrics_authorized(request):
        return Response(status_code=404)
    data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)
