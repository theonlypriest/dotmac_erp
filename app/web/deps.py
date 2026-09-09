"""
Web route authentication dependencies.

Provides authentication dependencies for HTML template routes with
proper tenant context handling.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus
from uuid import UUID

try:
    from datetime import UTC
except ImportError:  # pragma: no cover
    UTC = timezone.utc

from fastapi import Cookie, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import settings
from app.db import AsyncSessionLocal, SessionLocal
from app.db.session_context import (
    allow_cross_org,
    prime_session,
    prime_tenant_context,
    tenant_scope_for_session,
)
from app.models.auth import Session as AuthSession
from app.models.auth import SessionStatus
from app.models.person import Person
from app.models.rbac import Permission, PersonRole, Role, RolePermission
from app.observability import actor_id_var
from app.rls import set_current_organization
from app.services.auth_dependencies import is_session_inactive
from app.services.auth_flow import (
    AuthFlow,
    _load_rbac_claims,
    decode_access_token,
    hash_session_token,
)
from app.services.common import coerce_uuid
from app.services.finance.branding import BrandingService, CSSGenerator
from app.services.people.perf.performance_mode_policy import (
    get_policy_profile_for_mode,
    is_pms_enabled_for_org,
    resolve_performance_mode,
)
from app.templates import templates  # noqa: F401 - re-exported for web routes

logger = logging.getLogger(__name__)

_SESSION_TOUCH_INTERVAL = timedelta(seconds=60)


def _set_actor_context(request: Request, actor_id: UUID | str) -> None:
    """Keep request state and observability context in sync for auditing."""
    actor = str(actor_id)
    request.state.actor_id = actor
    actor_id_var.set(actor)


def _set_default_org_context(db: Session) -> UUID | None:
    """Set default org RLS context before loading org-scoped records."""
    if not settings.default_organization_id:
        return None
    organization_id = UUID(str(settings.default_organization_id))
    prime_tenant_context(db, organization_id)
    return organization_id


def _get_person_for_web_session(db: Session, person_id: UUID) -> Person | None:
    """Load a web-session person with the default org context in single-org mode."""
    if _set_default_org_context(db):
        return db.get(Person, person_id)
    with allow_cross_org(db):
        return db.get(Person, person_id)


def get_db():
    """Get database session for web routes."""
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def get_async_db():
    """Get async database session for web routes."""
    async with AsyncSessionLocal() as db:
        try:
            yield db
        finally:
            await db.close()


def _rollback_session_safely(db: Session | None) -> None:
    """Rollback a session if a swallowed exception may have aborted it."""
    if db is None:
        return
    try:
        db.rollback()
    except Exception:
        logger.debug("Failed to rollback session after swallowed exception")


def _brand_mark(name: str) -> str:
    """Generate a 2-letter brand mark from the brand name.

    For multi-word names, uses the first letter of first two words (e.g., "DotMac ERP" → "DB").
    For single-word names, uses first two letters (e.g., "Ledger" → "LE").
    """
    parts = [part for part in name.split() if part]
    if not parts:
        return "DB"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


def brand_context() -> dict:
    """Get standard brand context for templates (system defaults)."""
    # Use configured brand_mark or derive from name
    mark = settings.brand_mark or (
        _brand_mark(settings.brand_name) if settings.brand_name else "DB"
    )
    return {
        "name": settings.brand_name,
        "tagline": settings.brand_tagline,
        "logo_url": settings.brand_logo_url,
        "mark": mark,
    }


def org_brand_context(db: Session, org_id: UUID | None) -> dict:
    """
    Get organization-specific brand context for templates.

    Falls back to system defaults if no org branding exists.
    Uses Redis cache for CSS generation (1 hour TTL).

    Returns dict with:
        - name: Display name
        - tagline: Brand tagline
        - logo_url: Logo URL for light backgrounds
        - logo_dark_url: Logo URL for dark backgrounds
        - favicon_url: Favicon URL
        - mark: 2-letter brand mark
        - css: Generated CSS for brand colors/fonts
        - fonts_url: Google Fonts URL if custom fonts
        - has_custom_branding: Whether org has custom branding
    """
    base = brand_context()

    no_branding = {
        **base,
        "logo_dark_url": None,
        "favicon_url": None,
        "css": "",
        "fonts_url": None,
        "has_custom_branding": False,
    }

    if not org_id:
        return no_branding

    service = BrandingService(db)
    branding = service.get_by_org_id(org_id)

    if not branding:
        return no_branding

    # Try cache for CSS generation (expensive HSL color math)
    from app.services.cache import CacheKeys, CacheService, cache_service

    cache_key = CacheKeys.org_branding_css(org_id)
    cached = cache_service.get(cache_key)

    if cached and isinstance(cached, dict):
        css = cached.get("css", "")
        fonts_url = cached.get("fonts_url")
    else:
        css_gen = CSSGenerator(branding)
        css = css_gen.generate()
        fonts_url = css_gen.get_google_fonts_url()

        # Cache the result
        cache_service.set(
            cache_key,
            {"css": css, "fonts_url": fonts_url},
            ttl_seconds=CacheService.TTL_BRANDING,
        )

    return {
        "name": branding.display_name or base["name"],
        "tagline": branding.tagline or base["tagline"],
        "logo_url": branding.logo_url or base["logo_url"],
        "logo_dark_url": branding.logo_dark_url,
        "favicon_url": branding.favicon_url,
        "mark": branding.brand_mark or base["mark"],
        "css": css,
        "fonts_url": fonts_url,
        "has_custom_branding": True,
        "primary_color": branding.primary_color,
        "accent_color": branding.accent_color,
    }


def resolve_brand_context(
    db: Session | None,
    organization,
    organization_id: UUID | None,
) -> dict:
    """Resolve brand context with consistent fallback order."""
    brand = (
        org_brand_context(db, organization_id)
        if db and organization_id
        else brand_context()
    )
    if organization:
        org_name = organization.trading_name or organization.legal_name
        # Prefer org name/logo when branding isn't explicitly configured.
        if org_name and (
            not brand.get("name") or brand.get("name") == settings.brand_name
        ):
            brand["name"] = org_name
        if organization.logo_url and not brand.get("logo_url"):
            brand["logo_url"] = organization.logo_url
        if not brand.get("mark"):
            brand["mark"] = _brand_mark(brand.get("name") or org_name or "DB")
    return brand


def _merge_dicts(base: dict, override: dict) -> dict:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge_dicts(base[key], value)
        else:
            base[key] = value
    return base


def landing_content() -> dict:
    """Get landing page content for templates."""
    content = {
        "hero": {
            "badge": settings.landing_hero_badge,
            "title": settings.landing_hero_title,
            "subtitle": f"{settings.brand_tagline}. {settings.landing_hero_subtitle}",
            "cta_primary": settings.landing_cta_primary,
            "cta_secondary": settings.landing_cta_secondary,
        },
        "proof_pills": [
            {"key": "multi_entity", "label": "Multi-entity"},
            {"key": "audit_trail", "label": "Audit trail"},
            {"key": "hr_payroll", "label": "HR & Payroll"},
            {"key": "real_time", "label": "Real-time"},
        ],
        "benefits": {
            "title": "Why teams choose Dotmac ERP",
            "subtitle": "Less busywork, faster closes, and a single source of truth.",
            "items": [
                {
                    "title": "One system, fewer handoffs",
                    "description": "Finance, HR, and operational modules stay in sync with shared data and approvals.",
                },
                {
                    "title": "Audit-ready out of the box",
                    "description": "Standard statements, audit trails, and controls built into every workflow.",
                },
                {
                    "title": "Real-time visibility",
                    "description": "Dashboards and reports update as transactions happen, not at month-end.",
                },
                {
                    "title": "Built for growing teams",
                    "description": "Multi-entity support, roles, and approvals that scale with your org.",
                },
            ],
        },
        "core_modules": {
            "title": "Core ERP modules",
            "subtitle": "Start with finance and HR, then add inventory, fleet, support, procurement, and projects.",
            "cards": [
                {
                    "key": "finance",
                    "title": "Finance",
                    "description": "GL, AR/AP, fixed assets, banking, and financial reporting.",
                    "cta_label": "Explore finance",
                    "cta_href": "/finance/dashboard",
                },
                {
                    "key": "people",
                    "title": "People",
                    "description": "HR, payroll, leave, and employee expenses in one place.",
                    "cta_label": "Explore people",
                    "cta_href": "/people/hr/employees",
                },
                {
                    "key": "inventory",
                    "title": "Inventory",
                    "description": "Items, warehouses, stock movements, and valuation.",
                    "cta_label": "Explore inventory",
                    "cta_href": "/inventory/items",
                },
            ],
        },
        "modules": {
            "title": "ERP modules, fully connected",
            "subtitle": "Finance, people, and operational modules working from a single system of record.",
            "featured": {
                "title": "General Ledger",
                "description": (
                    "The foundation of your accounting system. Chart of accounts with flexible hierarchies, "
                    "journal entries with approval workflows, and trial balance with multi-currency support."
                ),
                "chips": [
                    "Chart of Accounts",
                    "Journal Entries",
                    "Trial Balance",
                    "Multi-Currency",
                ],
                "cta_label": "Explore General Ledger",
                "cta_href": "/finance/gl/accounts",
            },
            "cards": [
                {
                    "key": "ar",
                    "title": "Accounts Receivable",
                    "description": "Customer invoices, payments, credit memos, and aging analysis.",
                    "cta_label": "View AR",
                    "cta_href": "/finance/ar/customers",
                },
                {
                    "key": "ap",
                    "title": "Accounts Payable",
                    "description": "Supplier bills, payment scheduling, and expense allocation.",
                    "cta_label": "View AP",
                    "cta_href": "/finance/ap/suppliers",
                },
                {
                    "key": "banking",
                    "title": "Banking",
                    "description": "Bank accounts, reconciliation, and cash flow management.",
                    "cta_label": "View banking",
                    "cta_href": "/finance/banking/accounts",
                },
                {
                    "key": "reports",
                    "title": "Financial Reports",
                    "description": "Trial balance, P&L, balance sheet, and disclosure notes.",
                    "cta_label": "View reports",
                    "cta_href": "/finance/reports",
                },
            ],
        },
        "people": {
            "title": "People & Payroll",
            "subtitle": "Hire, pay, and manage teams with compliant HR workflows.",
            "featured": {
                "title": "Human Resources",
                "description": (
                    "Centralized employee management with departments, designations, and organizational hierarchy. "
                    "Track employee lifecycle from onboarding to offboarding."
                ),
                "chips": [
                    "Employee Database",
                    "Departments",
                    "Designations",
                    "Org Structure",
                ],
                "cta_label": "Explore HR",
                "cta_href": "/people/hr/employees",
            },
            "cards": [
                {
                    "key": "payroll",
                    "title": "Payroll",
                    "description": "Salary structures, components, payslips, and statutory compliance.",
                    "cta_label": "View Payroll",
                    "cta_href": "/people/payroll/slips",
                },
                {
                    "key": "leave",
                    "title": "Leave Management",
                    "description": "Leave types, applications, approvals, and balance tracking.",
                    "cta_label": "View Leave",
                    "cta_href": "/people/leave",
                },
                {
                    "key": "attendance",
                    "title": "Attendance",
                    "description": "Shift management, check-in/out tracking, and attendance reports.",
                    "cta_label": "View Attendance",
                    "cta_href": "/people/attendance",
                },
                {
                    "key": "recruit",
                    "title": "Recruitment",
                    "description": "Job postings, applicant tracking, and hiring workflows.",
                    "cta_label": "View Recruitment",
                    "cta_href": "/people/recruit",
                },
                {
                    "key": "expenses",
                    "title": "Expense Claims",
                    "description": "Employee expenses, cash advances, and corporate cards.",
                    "cta_label": "View Expenses",
                    "cta_href": "/people/expenses",
                },
            ],
        },
        "audit": {
            "badge": "Audit-ready",
            "title": "Every entry traceable.\nEvery report ready.",
            "description": (
                "Built for compliance from day one. Complete audit trail, approval workflows, document "
                "attachments, and row-level security ensure your books are always ready for review."
            ),
            "bullets": [
                "Complete change history with user attribution",
                "Multi-level approval workflows",
                "Document attachments for supporting evidence",
                "Row-level security for data isolation",
            ],
        },
        "reports": {
            "title": "Real-time reporting & insights",
            "subtitle": "Dashboards and reports that update as transactions happen across all modules.",
            "cards": [
                {
                    "title": "Financial Reports",
                    "subtitle": "P&L, balance sheet, cash flow",
                },
                {
                    "title": "HR Analytics",
                    "subtitle": "Headcount, attrition, payroll costs",
                },
                {
                    "title": "Operations Metrics",
                    "subtitle": "Inventory, procurement, fulfillment",
                },
                {
                    "title": "Custom Dashboards",
                    "subtitle": "Build reports for your KPIs",
                },
            ],
        },
        "security": {
            "title": "Enterprise-grade security",
            "subtitle": "Your financial data deserves the highest level of protection.",
            "cards": [
                {
                    "key": "rls",
                    "title": "Row-Level Security",
                    "description": "PostgreSQL RLS ensures data isolation between tenants.",
                },
                {
                    "key": "rbac",
                    "title": "Role-Based Access",
                    "description": "Fine-grained permissions control who can view, edit, and approve.",
                },
                {
                    "key": "encryption",
                    "title": "Encrypted at Rest",
                    "description": "Sensitive data is encrypted with industry-standard algorithms.",
                },
            ],
        },
        "cta": {
            "title": "Ready to run on one ERP?",
            "subtitle": "Unify finance, HR, and operational modules with {brand}.",
            "cta_primary": settings.landing_cta_primary,
            "cta_secondary": settings.landing_cta_secondary,
        },
    }

    if settings.landing_content_json:
        try:
            override = json.loads(settings.landing_content_json)
        except json.JSONDecodeError:
            override = None
        if isinstance(override, dict):
            content = _merge_dicts(content, override)

    return content


def _is_license_grace() -> bool:
    """Return True when the license is in its grace period (expired but still functional)."""
    try:
        from app.licensing.enforcement import is_in_grace_period

        return is_in_grace_period()
    except Exception:
        return False


def _resolve_mode_policy_banner(
    raw_error: str | None,
    *,
    ui_messages: dict[str, str] | None = None,
) -> tuple[str | None, str | None]:
    """Translate mode-policy error codes into user-facing banner messages."""
    if not raw_error:
        return None, None

    normalized = raw_error.strip()
    resolved_messages = ui_messages or {}
    if "MODE_POLICY_BLOCKED:pms_write_requires_government_or_hybrid" in normalized:
        return (
            "pms",
            resolved_messages.get(
                "mode_blocked_pms_write",
                "PMS actions are blocked in the current performance mode.",
            ),
        )
    if "MODE_POLICY_BLOCKED:private_write_requires_private_or_hybrid" in normalized:
        return (
            "private",
            resolved_messages.get(
                "mode_blocked_private_write",
                "Private performance actions are blocked in the current mode.",
            ),
        )
    return None, None


def base_context(
    request: Request,
    auth: "WebAuthContext",
    page_title: str,
    active_module: str = "",
    notifications: list | None = None,
    db: Session | None = None,
) -> dict:
    """
    Get base template context with authentication.

    Args:
        request: FastAPI request
        auth: WebAuthContext from authentication
        page_title: Page title for the template
        active_module: Active navigation module
        notifications: List of notification dicts with keys:
            - type: 'mention' | 'invoice' | 'payment' | 'alert' | 'info'
            - title: Short title text
            - message: Longer description
            - url: Link to navigate when clicked
            - time: Relative time string (e.g., "5 min ago")
            - read: bool indicating if notification was read
        db: Optional database session for loading org branding and notifications

    Returns:
        Dict with common template context values, including request for TemplateResponse.
    """
    effective_db = db
    owns_db = False
    if effective_db is None and auth.organization_id:
        effective_db = SessionLocal()
        owns_db = True
        # Sessions created here are outside FastAPI's get_db dependency,
        # so RLS context hasn't been set yet — tenant-scoped queries below
        # would return None for every row and look like missing data.
        prime_tenant_context(effective_db, auth.organization_id)

    try:
        # Load organization object for template conditionals (e.g. IPSAS sidebar toggle)
        organization = None
        performance_mode = None
        policy_profile = get_policy_profile_for_mode(None)
        if effective_db and auth.organization_id:
            from app.models.finance.core_org.organization import Organization

            organization = effective_db.get(Organization, auth.organization_id)
            if organization is not None:
                performance_mode = resolve_performance_mode(organization).value
                policy_profile = get_policy_profile_for_mode(performance_mode)
                pms_ohcsf_enabled = is_pms_enabled_for_org(organization)
            else:
                # Avoid 500s when a stale session references a missing org row.
                performance_mode = None
                policy_profile = get_policy_profile_for_mode(None)
                pms_ohcsf_enabled = False
        else:
            pms_ohcsf_enabled = False

        # Set per-request formatting preferences from organisation settings
        if organization is not None:
            from app.services.formatting_context import (
                resolve_from_org,
                set_formatting_prefs,
            )

            set_formatting_prefs(resolve_from_org(organization))

        # Get org-specific branding if db session available
        org_branding = None
        if effective_db and auth.organization_id:
            org_branding = org_brand_context(effective_db, auth.organization_id)
            try:
                from app.services.finance.platform.currency_context import (
                    get_currency_context,
                )
            except Exception:
                get_currency_context = None  # type: ignore[assignment]
        else:
            get_currency_context = None  # type: ignore[assignment]

        # Auto-fetch notifications if db session available and user is authenticated
        if (
            notifications is None
            and effective_db
            and auth.is_authenticated
            and auth.person_id
        ):
            try:
                # Import here to avoid circular import
                from datetime import datetime, timedelta

                from app.services.notification import notification_service

                def _format_relative_time(dt: datetime) -> str:
                    now = datetime.utcnow()
                    diff = now - dt
                    if diff < timedelta(minutes=1):
                        return "Just now"
                    elif diff < timedelta(hours=1):
                        return f"{int(diff.total_seconds() / 60)} min ago"
                    elif diff < timedelta(days=1):
                        return f"{int(diff.total_seconds() / 3600)}h ago"
                    elif diff < timedelta(days=7):
                        return f"{diff.days}d ago"
                    else:
                        return dt.strftime("%b %d")

                def _notification_type_to_display(
                    entity_type, notification_type
                ) -> str:
                    from app.models.notification import NotificationType

                    if notification_type in (
                        NotificationType.MENTION,
                        NotificationType.COMMENT,
                        NotificationType.REPLY,
                    ):
                        return "mention"
                    elif notification_type in (
                        NotificationType.APPROVED,
                        NotificationType.COMPLETED,
                        NotificationType.RESOLVED,
                    ):
                        return "payment"
                    elif notification_type in (
                        NotificationType.REJECTED,
                        NotificationType.OVERDUE,
                        NotificationType.ALERT,
                    ):
                        return "alert"
                    else:
                        return "info"

                raw_notifications = notification_service.list_notifications(
                    effective_db,
                    recipient_id=auth.person_id,
                    organization_id=auth.organization_id,
                    limit=5,
                )
                notifications = [
                    {
                        "id": str(n.notification_id),
                        "type": _notification_type_to_display(
                            n.entity_type, n.notification_type
                        ),
                        "title": n.title,
                        "message": n.message,
                        "url": n.action_url or "#",
                        "time": _format_relative_time(n.created_at),
                        "read": n.is_read,
                    }
                    for n in raw_notifications
                ]
            except Exception:
                _rollback_session_safely(effective_db)
                # Don't fail page load if notifications fail
                notifications = []

        can_team_leave = "admin" in auth.roles or auth.has_any_permission(
            [
                "leave:applications:approve:tier1",
                "leave:applications:approve:tier2",
                "leave:applications:approve:tier3",
            ]
        )
        can_team_expenses = "admin" in auth.roles or auth.has_any_permission(
            [
                "expense:claims:approve:tier1",
                "expense:claims:approve:tier2",
                "expense:claims:approve:tier3",
            ]
        )

        settings_url = "/settings"
        if request.url.path.startswith("/people"):
            settings_url = "/people/settings"
        elif active_module in {
            "support",
            "inventory",
            "projects",
            "fleet",
            "procurement",
        }:
            settings_url = f"/settings/{active_module}"

        brand = resolve_brand_context(effective_db, organization, auth.organization_id)

        # Ensure csrf_form contains an HTML hidden input for template rendering.
        # On POST requests, the CSRF middleware caches the parsed FormData object
        # in request.state.csrf_form for handler consumption.  By this point all
        # handlers have already read the form data, so it is safe to replace it
        # with the HTML string that templates expect via {{ request.state.csrf_form | safe }}.
        csrf_token = getattr(request.state, "csrf_token", "")
        csrf_form_val = getattr(request.state, "csrf_form", None)
        if not isinstance(csrf_form_val, str):
            request.state.csrf_form = (
                f'<input type="hidden" name="csrf_token" value="{csrf_token}">'
                if csrf_token
                else ""
            )

        # Extract feedback param from query string for success banner
        _saved = bool(request.query_params.get("saved"))
        _raw_error = request.query_params.get("error")
        mode_banner_variant, mode_banner_message = _resolve_mode_policy_banner(
            _raw_error,
            ui_messages=dict(policy_profile.ui_messages),
        )

        help_module_map = {
            "fixed_assets": "fixed_assets",
            "dashboard": "settings"
            if request.url.path.startswith("/operations")
            else "",
            "training": "people",
            "employees": "people",
            "departments": "people",
            "designations": "people",
            "employment-types": "people",
            "grades": "people",
            "locations": "people",
            "skills": "people",
            "competencies": "people",
            "job-descriptions": "people",
            "onboarding": "people",
            "attendance": "people",
            "leave": "people",
            "payroll": "people",
            "recruitment": "people",
            "support": "support",
            "inventory": "inventory",
            "projects": "projects",
            "fleet": "fleet",
            "procurement": "procurement",
            "settings": "settings",
            "coach": "coach",
            "expense": "expense",
            "finance": "finance",
        }
        contextual_module = help_module_map.get(active_module, "")
        if not contextual_module:
            path = request.url.path
            if path.startswith("/finance"):
                contextual_module = "finance"
            elif path.startswith("/people"):
                contextual_module = "people"
            elif path.startswith("/support"):
                contextual_module = "support"
            elif path.startswith("/inventory"):
                contextual_module = "inventory"
            elif path.startswith("/projects"):
                contextual_module = "projects"
            elif path.startswith("/fleet"):
                contextual_module = "fleet"
            elif path.startswith("/procurement"):
                contextual_module = "procurement"
            elif path.startswith("/expense"):
                contextual_module = "expense"
            elif path.startswith("/settings"):
                contextual_module = "settings"
            elif path.startswith("/coach"):
                contextual_module = "coach"
            elif path.startswith("/fixed-assets"):
                contextual_module = "fixed_assets"

        # Deep route-to-article mapping for contextual help
        route_article_map = {
            "/finance/gl/periods": "finance-period-close-checklist",
            "/finance/gl/journals": "finance-journal-entry",
            "/finance/gl/trial-balance": "finance-trial-balance-not-balanced",
            "/finance/ar/invoices": "finance-invoice-to-cash",
            "/finance/ap/invoices": "finance-ap-invoice-processing",
            "/finance/ap/payments": "finance-payment-run",
            "/finance/banking/reconciliation": "finance-bank-reconciliation",
            "/finance/tax/periods": "finance-tax-period-filing",
            "/finance/gl/chart-of-accounts": "finance-chart-of-accounts-setup",
            "/people/hr/employees": "people-onboarding-workflow",
            "/people/leave": "people-leave-management",
            "/people/attendance": "people-attendance-setup",
            "/people/recruit": "people-recruitment-pipeline",
            "/people/payroll": "people-payroll-processing",
            "/people/training": "people-training-workflow",
            "/inventory/items": "inventory-warehouse-setup",
            "/inventory/material-requests": "inventory-material-request-flow",
            "/procurement/vendors": "procurement-vendor-evaluation",
            "/procurement/purchase-orders": "procurement-po-to-grn",
            "/support/dashboard": "support-team-setup",
            "/projects/tasks": "projects-task-delivery",
            "/expense/claims": "expense-claim-submission",
        }
        path = request.url.path
        contextual_help_url = ""
        contextual_article_slug = ""
        if path.startswith("/help"):
            contextual_help_url = ""
        else:
            # Check deep route mapping first
            for route_prefix, article_slug in route_article_map.items():
                if path.startswith(route_prefix):
                    contextual_help_url = f"/help/articles/{article_slug}"
                    contextual_article_slug = article_slug
                    break
            if not contextual_help_url and contextual_module:
                contextual_help_url = f"/help/module/{contextual_module}"
            elif not contextual_help_url:
                contextual_help_url = f"/help/search?q={quote_plus(page_title)}"

        context = {
            "request": request,
            "title": page_title,
            "page_title": page_title,
            "app_version": settings.app_version,
            "brand": brand,
            "org_branding": org_branding,
            "active_module": active_module,
            "settings_url": settings_url,
            "auth": auth,
            "user": auth.user,
            "organization": organization,
            "performance_mode": performance_mode,
            "pms_ohcsf_enabled": pms_ohcsf_enabled,
            "performance_private_enabled": performance_mode in {"PRIVATE", "HYBRID"},
            "performance_government_enabled": performance_mode
            in {"GOVERNMENT_PMS", "HYBRID"},
            "performance_nav_label": policy_profile.ui_labels.get(
                "performance_nav_label",
                "Performance (Private)",
            ),
            "pms_nav_label": policy_profile.ui_labels.get(
                "pms_nav_label",
                "PMS (Government)",
            ),
            "accessible_modules": auth.accessible_modules,
            "can_team_leave": can_team_leave,
            "can_team_expenses": can_team_expenses,
            "csrf_token": csrf_token,
            "notifications": notifications or [],
            "saved": _saved,
            "mode_banner_message": mode_banner_message,
            "mode_banner_variant": mode_banner_variant,
            # Org formatting settings for JS / template use
            "org_date_format": getattr(organization, "date_format", None)
            if organization
            else None,
            "org_number_format": getattr(organization, "number_format", None)
            if organization
            else None,
            "org_timezone": getattr(organization, "timezone", None)
            if organization
            else None,
            "contextual_help_url": contextual_help_url,
            "contextual_article_slug": contextual_article_slug,
            "contextual_help_search_url": f"/help/search?q={quote_plus(page_title)}",
            "license_grace_period": _is_license_grace(),
        }
        if effective_db and auth.organization_id and get_currency_context is not None:
            try:
                context.update(
                    get_currency_context(effective_db, str(auth.organization_id))
                )
            except Exception:
                _rollback_session_safely(effective_db)
                logger.exception("Ignored exception")
        return context
    finally:
        if owns_db and effective_db:
            effective_db.close()


@dataclass(frozen=True)
class WebPrincipal:
    """Lightweight principal for web routes."""

    id: UUID | None
    user_id: UUID | None
    person_id: UUID | None
    organization_id: UUID | None
    employee_id: UUID | None
    roles: list[str]
    scopes: list[str]


class WebAuthContext:
    """Authentication context for web routes."""

    def __init__(
        self,
        is_authenticated: bool = False,
        person_id: UUID | None = None,
        organization_id: UUID | None = None,
        employee_id: UUID | None = None,
        user_name: str = "Guest",
        user_initials: str = "GU",
        roles: list[str] | None = None,
        scopes: list[str] | None = None,
        leave_write_restricted: bool = False,
        leave_restriction_id: UUID | None = None,
    ):
        self.is_authenticated = is_authenticated
        self.person_id = person_id
        self.organization_id = organization_id
        self.employee_id = employee_id
        self.user_name = user_name
        self.user_initials = user_initials
        self.roles = roles or []
        self.scopes = scopes or []
        self.leave_write_restricted = leave_write_restricted
        self.leave_restriction_id = leave_restriction_id

    @property
    def user(self) -> dict:
        """Get user dict for template context."""
        return {
            "name": self.user_name,
            "initials": self.user_initials,
            "is_authenticated": self.is_authenticated,
            "is_admin": self.is_admin,
        }

    @property
    def user_id(self) -> UUID | None:
        """Alias for person_id for backward compatibility."""
        return self.person_id

    @property
    def principal(self) -> WebPrincipal:
        """Provide a Principal-like object for services that require it."""
        return WebPrincipal(
            id=self.person_id,
            user_id=self.person_id,
            person_id=self.person_id,
            organization_id=self.organization_id,
            employee_id=self.employee_id,
            roles=list(self.roles),
            scopes=list(self.scopes),
        )

    @property
    def is_admin(self) -> bool:
        """Check if user has admin role."""
        return "admin" in self.roles

    @property
    def accessible_modules(self) -> list[str]:
        """Get list of modules the user can access.

        Respects ENABLED_MODULES env var — modules not enabled for this
        deployment are excluded regardless of user permissions.
        """
        from app.main import is_module_enabled

        if not self.is_authenticated or self.organization_id is None:
            return []

        modules = []
        scopes_set = set(self.scopes)
        roles_set = {r.strip().lower() for r in self.roles if r and r.strip()}

        has_fixed_assets_scope = any(
            scope == "fa"
            or scope == "fixed_assets"
            or scope.startswith("fa:")
            or scope.startswith("fixed_assets:")
            for scope in scopes_set
        )

        if self.is_admin or "finance:access" in scopes_set:
            modules.append("finance")
        # HR/People: allow either scope-based access or named HR roles.
        # Role names come from JWT claims (Role.name in DB, e.g. "hr_manager").
        if (
            self.is_admin
            or "hr:access" in scopes_set
            or any(scope.startswith("hr:schedule:") for scope in scopes_set)
            or roles_set.intersection(
                {"hr_manager", "hr_director", "payroll_admin", "payroll_approver"}
            )
        ):
            modules.append("people")
        if self.is_admin or "training:access" in scopes_set:
            modules.append("training")
        if self.is_admin or "inventory:access" in scopes_set:
            modules.append("inventory")
        if self.is_admin or "fleet:access" in scopes_set:
            modules.append("fleet")
        if self.is_admin or "support:access" in scopes_set:
            modules.append("support")
        if self.is_admin or "procurement:access" in scopes_set:
            modules.append("procurement")
        if self.is_admin or "projects:access" in scopes_set:
            modules.append("projects")
        if self.is_admin or "settings:access" in scopes_set:
            modules.append("settings")
        if self.is_admin or "expense:access" in scopes_set:
            modules.append("expense")
        if self.is_admin or has_fixed_assets_scope:
            modules.append("fixed_assets")
        if self.is_admin or "discipline:access" in scopes_set:
            modules.append("discipline")
        if self.is_admin or scopes_set.intersection(
            {
                "coach:insights:read",
                "coach:insights:read_team",
                "coach:insights:read_all",
                "coach:reports:read",
                "coach:reports:read_all",
            }
        ):
            modules.append("coach")
        if self.is_admin or "public_sector:access" in scopes_set:
            modules.append("public_sector")
        if "self:access" in scopes_set:
            modules.append("self_service")

        # Filter by deployment-level enabled modules.
        # "discipline" maps to "people", and "settings" is always on.
        _module_map = {
            "discipline": "people",
            "self_service": "people",
        }
        _always_on = {"settings"}

        def _is_module_enabled(module_name: str) -> bool:
            return is_module_enabled(_module_map.get(module_name, module_name))

        return [m for m in modules if m in _always_on or _is_module_enabled(m)]

    def has_module_access(self, module: str) -> bool:
        """Check if user can access a specific module."""
        alias_map = {
            "hr": "people",
            "people": "people",
            "finance": "finance",
            "inventory": "inventory",
            "fleet": "fleet",
            "support": "support",
            "procurement": "procurement",
            "projects": "projects",
            "settings": "settings",
            "expense": "expense",
            "expenses": "expense",
            "discipline": "discipline",
            "training": "training",
            "coach": "coach",
            "public_sector": "public_sector",
            "public-sector": "public_sector",
            "fixed_assets": "fixed_assets",
            "fa": "fixed_assets",
            "self": "self_service",
            "self-service": "self_service",
            "self_service": "self_service",
        }
        canonical = alias_map.get(module, module)
        return canonical in self.accessible_modules

    def has_permission(self, permission: str) -> bool:
        """Check if user has a specific permission."""
        requested = (permission or "").strip()
        if not requested:
            return False

        if self.leave_write_restricted:
            from app.services.people.hr.staff_access_projection import (
                is_mutating_permission_key,
            )

            if is_mutating_permission_key(requested):
                return False

        if self.is_admin:
            return True

        scopes_set = {scope.strip() for scope in self.scopes}

        for scope in scopes_set:
            if scope == requested:
                return True

            if requested.endswith(":*"):
                requested_root = requested[:-2]
                if scope == requested_root or scope.startswith(f"{requested_root}:"):
                    return True
            elif scope.endswith(":*"):
                scope_root = scope[:-2]
                if requested == scope_root or requested.startswith(f"{scope_root}:"):
                    return True

        return False

    def has_any_permission(self, permissions: list[str]) -> bool:
        """Check if user has any of the specified permissions."""
        if self.is_admin and not self.leave_write_restricted:
            return True
        return any(self.has_permission(permission) for permission in permissions)

    def has_all_permissions(self, permissions: list[str]) -> bool:
        """Check if user has all specified permissions."""
        return all(self.has_permission(permission) for permission in permissions)

    @property
    def default_module(self) -> str | None:
        """Get the user's default module (first accessible module)."""
        modules = self.accessible_modules
        return modules[0] if modules else None

    @property
    def default_redirect(self) -> str:
        """Get the default redirect URL based on accessible modules."""
        if not self.is_authenticated:
            return "/login"
        modules = self.accessible_modules
        if len(modules) == 0:
            return "/no-access"
        if len(modules) == 1:
            module = modules[0]
            if module == "finance":
                return "/finance/dashboard"
            if module == "people":
                return "/people/hr/employees"
            if module == "inventory":
                return "/inventory/items"
            if module == "fleet":
                return "/fleet"
            if module == "support":
                return "/support/dashboard"
            if module == "procurement":
                return "/procurement"
            if module == "projects":
                return "/projects"
            if module == "settings":
                return "/settings"
            if module == "expense":
                return "/expense"
            if module == "discipline":
                return "/people/hr/discipline"
            if module == "coach":
                return "/coach/"
            if module == "self_service":
                return "/people/self/attendance"
            if module == "fixed_assets":
                return "/fixed-assets"
            return f"/{module}/dashboard"
        # Multiple modules - go to module selector
        return "/"


def _extract_bearer_token(authorization: str | None) -> str | None:
    """Extract Bearer token from Authorization header."""
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


def _refresh_cookie_name(db: Session | None) -> str:
    try:
        name = AuthFlow.refresh_cookie_settings(db).get("key")
    except Exception:
        _rollback_session_safely(db)
        name = None
    return name or "refresh_token"


def _get_refresh_token_cookie(request: Request, db: Session | None) -> str | None:
    configured_name = _refresh_cookie_name(db)
    cookie_names = [configured_name]
    if configured_name != "refresh_token":
        cookie_names.append("refresh_token")

    for cookie_name in cookie_names:
        cookie_value = request.cookies.get(cookie_name)
        if cookie_value:
            return cookie_value
    return None


def _resolve_session_from_refresh_token(
    db: Session,
    refresh_token: str,
    now: datetime,
) -> tuple[UUID, UUID] | None:
    """Resolve (person_id, session_id) from an ERP refresh token."""
    token_hash = hash_session_token(refresh_token)
    session = db.scalar(
        select(AuthSession).where(
            AuthSession.token_hash == token_hash,
            AuthSession.status == SessionStatus.active,
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > now,
        )
    )
    if not session or is_session_inactive(session, now):
        return None
    if (
        not session.last_seen_at
        or (now - session.last_seen_at) > _SESSION_TOUCH_INTERVAL
    ):
        session.last_seen_at = now
        db.flush()
    return session.person_id, session.id


def _normalize_roles_scopes(
    roles: list[str], scopes: list[str]
) -> tuple[list[str], list[str]]:
    normalized_roles = [
        str(role).strip().lower() for role in roles if str(role).strip()
    ]
    normalized_scopes = [
        str(scope).strip().lower() for scope in scopes if str(scope).strip()
    ]
    return normalized_roles, normalized_scopes


def _ensure_admin_role(db: Session, person_id: UUID, roles: list[str]) -> list[str]:
    if "admin" in roles:
        return roles
    admin_role = db.scalar(select(Role).where(Role.name == "admin"))
    if not admin_role:
        return roles
    has_admin_role = db.scalar(
        select(PersonRole).where(
            PersonRole.person_id == person_id,
            PersonRole.role_id == admin_role.id,
        )
    )
    if has_admin_role:
        roles = [*roles, "admin"]
    return roles


def _load_web_permission_scopes(
    db: Session,
    person_id: UUID,
    existing_scopes: list[str],
) -> list[str]:
    """Merge token scopes with current DB-backed permission scopes for web auth."""
    permission_rows = db.scalars(
        select(Permission.key)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(Role, RolePermission.role_id == Role.id)
        .join(PersonRole, PersonRole.role_id == Role.id)
        .where(PersonRole.person_id == person_id)
        .where(Role.is_active.is_(True))
        .where(Permission.is_active.is_(True))
    ).all()
    return list({*existing_scopes, *(str(key) for key in permission_rows if key)})


def _active_leave_restriction_id(
    db: Session,
    organization_id: UUID | None,
    person_id: UUID,
) -> UUID | None:
    if organization_id is None:
        return None
    try:
        from app.services.people.hr.staff_access_projection import (
            StaffAccessProjectionService,
        )

        restriction = StaffAccessProjectionService(db).active_restriction_for_person(
            organization_id,
            person_id,
        )
        return restriction.restriction_id if restriction else None
    except OperationalError:
        return None


def require_web_auth(
    request: Request,
    authorization: str | None = Header(default=None),
    access_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
) -> WebAuthContext:
    """
    Require authentication for web routes and set tenant context.

    Every session reaching this dependency was issued by ERP itself; ERP
    accepts no externally minted session or identity assertion.

    Checks for JWT in:
    1. Authorization header (Bearer token)
    2. access_token cookie

    Returns WebAuthContext with user info for templates.
    Sets RLS context for the user's organization.

    Usage:
        @router.get("/dashboard")
        def dashboard(
            request: Request,
            auth: WebAuthContext = Depends(require_web_auth),
            db: Session = Depends(get_db),
        ):
            # auth.organization_id is available
            # RLS context is set
            return templates.TemplateResponse(request, "dashboard.html", {
                "user": auth.user,
            })
    """
    # Try to get token from header or cookie
    token = _extract_bearer_token(authorization) or access_token
    payload = None
    if token:
        try:
            payload = decode_access_token(db, token)
        except HTTPException:
            payload = None

    now = datetime.now(UTC)

    if payload:
        person_id = payload.get("sub")
        session_id = payload.get("session_id")

        if not person_id or not session_id:
            raise HTTPException(status_code=401, detail="Invalid token")

        person_uuid = coerce_uuid(person_id)
        session_uuid = coerce_uuid(session_id)

        session = db.scalar(
            select(AuthSession).where(
                AuthSession.id == session_uuid,
                AuthSession.person_id == person_uuid,
                AuthSession.status == SessionStatus.active,
                AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > now,
            )
        )
        if not session:
            raise HTTPException(status_code=401, detail="Session expired or invalid")
        if is_session_inactive(session, now):
            raise HTTPException(
                status_code=401, detail="Session expired due to inactivity"
            )
        if (
            not session.last_seen_at
            or (now - session.last_seen_at) > _SESSION_TOUCH_INTERVAL
        ):
            session.last_seen_at = now
            db.flush()

        # Web sessions must reflect admin RBAC edits without waiting for the
        # existing access token to expire. Reload DB roles/scopes after the
        # token's session has been validated, while preserving token-scoped
        # module gates that may not have a granular DB permission in tests.
        scopes_value = payload.get("scopes")
        token_scopes = (
            [str(scope) for scope in scopes_value]
            if isinstance(scopes_value, list)
            else []
        )
        roles, db_scopes = _load_rbac_claims(db, str(person_uuid))
        scopes = [*token_scopes, *db_scopes]
        roles, scopes = _normalize_roles_scopes(roles, scopes)
        roles = _ensure_admin_role(db, person_uuid, roles)
        scopes = _load_web_permission_scopes(db, person_uuid, scopes)
    else:
        refresh_token = _get_refresh_token_cookie(request, db)
        if not refresh_token:
            raise HTTPException(
                status_code=401,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        resolved = _resolve_session_from_refresh_token(db, refresh_token, now)
        if not resolved:
            raise HTTPException(status_code=401, detail="Invalid or expired session")
        person_uuid, _ = resolved
        roles, scopes = _load_rbac_claims(db, str(person_uuid))
        roles, scopes = _normalize_roles_scopes(roles, scopes)
        roles = _ensure_admin_role(db, person_uuid, roles)
        scopes = _load_web_permission_scopes(db, person_uuid, scopes)

    # Get person details
    person = _get_person_for_web_session(db, person_uuid)
    if not person:
        raise HTTPException(status_code=401, detail="User not found")

    organization_id = person.organization_id

    # Set RLS context
    if organization_id:
        prime_tenant_context(db, organization_id)
        request.state.organization_id = str(organization_id)

    _set_actor_context(request, person_uuid)

    # Build user display info
    def _clean_name(value: str | None) -> str:
        cleaned = (value or "").strip()
        return "" if cleaned.lower() in {"none", "null"} else cleaned

    display_name = _clean_name(person.display_name)
    first_name = _clean_name(person.first_name)
    last_name = _clean_name(person.last_name)
    base_name = f"{first_name} {last_name}".strip()
    user_name = display_name or base_name or _clean_name(person.email) or "User"
    initials = (
        "".join(word[0].upper() for word in user_name.split()[:2])
        if user_name
        else "US"
    )

    # Look up employee_id for the person (may be None if person is not an employee)
    from app.models.people.hr.employee import Employee

    try:
        employee = db.scalar(select(Employee).where(Employee.person_id == person_uuid))
    except OperationalError:
        employee = None
    employee_id = employee.employee_id if employee else None
    leave_restriction_id = _active_leave_restriction_id(
        db,
        organization_id,
        person_uuid,
    )

    return WebAuthContext(
        is_authenticated=True,
        person_id=person_uuid,
        organization_id=organization_id,
        employee_id=employee_id,
        user_name=user_name,
        user_initials=initials,
        roles=roles,
        scopes=scopes,
        leave_write_restricted=leave_restriction_id is not None,
        leave_restriction_id=leave_restriction_id,
    )


# Auth-aware DB dependency variant — Phase 1 of the multi-tenant session
# listener. See docs/superpowers/specs/2026-05-10-multi-org-listener-design.md.
# Routes that act on org-scoped data should depend on this in place of
# ``get_db`` so the session is primed with ``auth.organization_id`` before
# yielding. ``get_db`` itself is left unchanged for routes that legitimately
# have no per-request org context (login, healthcheck, public pages).
def get_db_for_org(
    auth: WebAuthContext = Depends(require_web_auth),
):
    """DB session dependency that primes the session with the request's
    organization_id before yielding.

    Use this dependency in routes that act on org-scoped data::

        @router.get("/things")
        def list_things(
            auth: WebAuthContext = Depends(require_web_auth),
            db: Session = Depends(get_db_for_org),
        ):
            ...

    The plain ``get_db`` remains for routes that legitimately don't have
    a per-request organization context (login, healthcheck, public pages).
    """
    # A session primed with ``organization_id=None`` half-honours the
    # contract this dep exists to make: ``session.info`` carries a None
    # marker but the PostgreSQL GUC is never set, so RLS-protected
    # queries silently return empty rows. Fail loudly at the dep
    # boundary instead — wiring an org-scoped route to this dep without
    # an authenticated org is a programming bug, not a runtime state.
    # Mirrors ``require_organization_id`` in ``app/api/deps.py``.
    if auth.organization_id is None:
        raise HTTPException(
            status_code=403,
            detail="Organization context required",
        )
    db = SessionLocal()
    try:
        with tenant_scope_for_session(db, auth.organization_id):
            yield db
            # Mirror ``get_db_with_org`` (API dep): auto-commit on successful
            # yield, rollback on exception. Without this, web routes that
            # follow the documented "services flush, routes commit" rule lose
            # data silently.
            db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def get_async_db_for_org(
    auth: WebAuthContext = Depends(require_web_auth),
):
    """Async sibling of ``get_db_for_org`` for routes that use AsyncSession.

    Primes both layers (Python session.info for the ORM listener, PG GUC
    for in-database RLS policies) before yielding the session. Raises 403
    if the caller has no org context, mirroring ``require_organization_id``
    in ``app/api/deps.py`` — silently downgrading to an unprimed session
    is the original Bug A pattern and would let RLS-protected async
    queries return empty rows.

    Use in async routes::

        @router.get("/numbering")
        async def list_numbering(
            auth: WebAuthContext = Depends(require_web_auth),
            db: AsyncSession = Depends(get_async_db_for_org),
        ):
            ...

    The plain ``get_async_db`` remains for async routes that legitimately
    don't have a per-request org context.
    """
    if auth.organization_id is None:
        raise HTTPException(
            status_code=403,
            detail="Organization context required",
        )
    async with AsyncSessionLocal() as db:
        # ``prime_session`` is typed as Session but writes only to
        # ``.info``, which AsyncSession exposes via its underlying
        # ``sync_session``. The ORM listener attaches to sync Session
        # events, so writing the marker on ``sync_session.info`` is
        # what the listener actually reads at flush time.
        prime_session(db.sync_session, auth.organization_id)
        await set_current_organization(db, auth.organization_id)
        try:
            yield db
            # Mirror sync dep: auto-commit on successful yield, rollback on
            # exception. Without this, async web routes lose data the same way
            # sync routes did before the fix to ``get_db_for_org``.
            await db.commit()
        except Exception:
            await db.rollback()
            raise


def optional_web_auth(
    request: Request,
    authorization: str | None = Header(default=None),
    access_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
) -> WebAuthContext:
    """
    Optional authentication for web routes using ERP-owned sessions.

    Similar to require_web_auth but returns a guest context
    if no valid authentication is provided.

    Use this for pages that can be viewed by unauthenticated users
    but show different content for authenticated users.
    """
    token = _extract_bearer_token(authorization) or access_token
    payload = None
    if token:
        try:
            payload = decode_access_token(db, token)
        except HTTPException:
            payload = None

    now = datetime.now(UTC)

    if payload:
        person_id = payload.get("sub")
        session_id = payload.get("session_id")

        if not person_id or not session_id:
            return WebAuthContext(is_authenticated=False)

        person_uuid = coerce_uuid(person_id)
        session_uuid = coerce_uuid(session_id)

        session = db.scalar(
            select(AuthSession).where(
                AuthSession.id == session_uuid,
                AuthSession.person_id == person_uuid,
                AuthSession.status == SessionStatus.active,
                AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > now,
            )
        )
        if not session or is_session_inactive(session, now):
            return WebAuthContext(is_authenticated=False)
        if (
            not session.last_seen_at
            or (now - session.last_seen_at) > _SESSION_TOUCH_INTERVAL
        ):
            session.last_seen_at = now
            db.flush()

        # Web sessions must reflect admin RBAC edits without waiting for the
        # existing access token to expire. Reload DB roles/scopes after the
        # token's session has been validated, while preserving token-scoped
        # module gates that may not have a granular DB permission in tests.
        scopes_value = payload.get("scopes")
        token_scopes = (
            [str(scope) for scope in scopes_value]
            if isinstance(scopes_value, list)
            else []
        )
        roles, db_scopes = _load_rbac_claims(db, str(person_uuid))
        scopes = [*token_scopes, *db_scopes]
        roles, scopes = _normalize_roles_scopes(roles, scopes)
        roles = _ensure_admin_role(db, person_uuid, roles)
        scopes = _load_web_permission_scopes(db, person_uuid, scopes)
    else:
        refresh_token = _get_refresh_token_cookie(request, db)
        if not refresh_token:
            return WebAuthContext(is_authenticated=False)
        resolved = _resolve_session_from_refresh_token(db, refresh_token, now)
        if not resolved:
            return WebAuthContext(is_authenticated=False)
        person_uuid, _ = resolved
        roles, scopes = _load_rbac_claims(db, str(person_uuid))
        roles, scopes = _normalize_roles_scopes(roles, scopes)
        roles = _ensure_admin_role(db, person_uuid, roles)
        scopes = _load_web_permission_scopes(db, person_uuid, scopes)

    # Get person details
    person = _get_person_for_web_session(db, person_uuid)
    if not person:
        return WebAuthContext(is_authenticated=False)

    organization_id = person.organization_id

    # Set RLS context
    if organization_id:
        prime_tenant_context(db, organization_id)
        request.state.organization_id = str(organization_id)

    _set_actor_context(request, person_uuid)

    # Build user display info
    def _clean_name(value: str | None) -> str:
        cleaned = (value or "").strip()
        return "" if cleaned.lower() in {"none", "null"} else cleaned

    display_name = _clean_name(person.display_name)
    first_name = _clean_name(person.first_name)
    last_name = _clean_name(person.last_name)
    base_name = f"{first_name} {last_name}".strip()
    user_name = display_name or base_name or _clean_name(person.email) or "User"
    initials = (
        "".join(word[0].upper() for word in user_name.split()[:2])
        if user_name
        else "US"
    )

    # Look up employee_id for the person (may be None if person is not an employee)
    from app.models.people.hr.employee import Employee

    employee = db.scalar(select(Employee).where(Employee.person_id == person_uuid))
    employee_id = employee.employee_id if employee else None
    leave_restriction_id = _active_leave_restriction_id(
        db,
        organization_id,
        person_uuid,
    )

    return WebAuthContext(
        is_authenticated=True,
        person_id=person_uuid,
        organization_id=organization_id,
        employee_id=employee_id,
        user_name=user_name,
        user_initials=initials,
        roles=roles,
        scopes=scopes,
        leave_write_restricted=leave_restriction_id is not None,
        leave_restriction_id=leave_restriction_id,
    )


# =============================================================================
# Module Access Dependencies
# =============================================================================


def require_finance_access(
    auth: WebAuthContext = Depends(require_web_auth),
) -> WebAuthContext:
    """
    Require access to the finance module.

    Use this dependency for all finance/accounting web routes.

    Usage:
        @router.get("/finance/dashboard")
        def finance_dashboard(
            request: Request,
            auth: WebAuthContext = Depends(require_finance_access),
        ):
            ...
    """
    if not auth.has_module_access("finance"):
        raise HTTPException(
            status_code=403,
            detail="Finance module access required",
        )
    return auth


def require_admin_access(
    auth: WebAuthContext = Depends(require_web_auth),
) -> WebAuthContext:
    """
    Require administrator access to the /admin web surface.

    Applied as a router-level dependency on the admin web router. Every route
    under ``/admin`` previously depended only on ``optional_web_auth``, which —
    as the name says — does not require anything: the sole check was
    ``if auth and auth.organization_id``. Any authenticated user of any role
    could therefore open the admin settings pages and POST to them, including
    the payment-provider credential forms.

    Use this dependency for every route under ``/admin``.
    """
    if not auth.is_admin:
        raise HTTPException(
            status_code=403,
            detail="Administrator access required",
        )
    return auth


def require_finance_admin(
    auth: WebAuthContext = Depends(require_finance_access),
) -> WebAuthContext:
    """
    Require finance admin access.

    Use this dependency for sensitive finance admin routes like opening balance.
    """
    if not auth.is_admin:
        raise HTTPException(
            status_code=403,
            detail="Finance admin access required",
        )
    return auth


def require_fixed_assets_access(
    auth: WebAuthContext = Depends(require_web_auth),
) -> WebAuthContext:
    """
    Require access to the Fixed Assets module.

    """
    if not auth.has_any_permission(["fa:access", "fa:*", "fixed_assets:*"]):
        raise HTTPException(
            status_code=403,
            detail="Fixed Assets module access required",
        )
    return auth


def require_hr_access(
    auth: WebAuthContext = Depends(require_web_auth),
) -> WebAuthContext:
    """
    Require access to the HR module.

    Use this dependency for all HR/people web routes.

    Usage:
        @router.get("/hr/dashboard")
        def hr_dashboard(
            request: Request,
            auth: WebAuthContext = Depends(require_hr_access),
        ):
            ...
    """
    if not auth.has_module_access("people"):
        raise HTTPException(
            status_code=403,
            detail="HR module access required",
        )
    return auth


def require_scheduler_access(
    auth: WebAuthContext = Depends(require_web_auth),
) -> WebAuthContext:
    """Require People access or explicit shift scheduling permissions."""
    if not (
        auth.has_module_access("people")
        or auth.has_any_permission(
            [
                "hr:schedule:*",
                "hr:schedule:read",
                "hr:schedule:create",
                "hr:schedule:update",
                "hr:schedule:submit",
                "hr:schedule:approve",
                "hr:schedule:publish",
            ]
        )
    ):
        raise HTTPException(status_code=403, detail="Shift scheduling access required")
    return auth


def require_private_performance_mode(
    request: Request,
    auth: WebAuthContext = Depends(require_hr_access),
    db: Session = Depends(get_db),
) -> WebAuthContext:
    """Require org performance mode that can access private performance routes."""
    # PMS endpoints under /people/perf/pms are guarded separately.
    if request.url.path.startswith("/people/perf/pms"):
        return auth

    if not auth.organization_id:
        raise HTTPException(status_code=403, detail="Organization context required")

    from app.models.finance.core_org.organization import Organization, PerformanceMode
    from app.services.people.perf.performance_mode_policy import (
        resolve_performance_mode,
    )

    organization = db.get(Organization, auth.organization_id)
    if organization is None:
        raise HTTPException(status_code=403, detail="Organization not found")
    mode = resolve_performance_mode(organization)
    if mode not in {PerformanceMode.PRIVATE, PerformanceMode.HYBRID}:
        raise HTTPException(
            status_code=403,
            detail="Private performance mode required",
        )
    return auth


def require_government_pms_mode(
    request: Request = None,
    auth: WebAuthContext = Depends(require_hr_access),
    db: Session = Depends(get_db),
) -> WebAuthContext:
    """Require org performance mode that can access government PMS routes."""
    if not auth.organization_id:
        raise HTTPException(status_code=403, detail="Organization context required")

    from app.models.finance.core_org.organization import Organization, PerformanceMode
    from app.services.people.perf.performance_mode_policy import (
        resolve_performance_mode,
    )

    organization = db.get(Organization, auth.organization_id)
    if organization is None:
        raise HTTPException(status_code=403, detail="Organization not found")
    mode = resolve_performance_mode(organization)
    # PIPs are also used by the private appraisal underperformance gate.
    # Keep the rest of /people/perf/pms government-only, but allow private
    # HR users to resolve linked PIPs so private appraisals can be completed.
    if (
        request
        and request.url.path.startswith("/people/perf/pms/pips")
        and mode
        in {
            PerformanceMode.PRIVATE,
            PerformanceMode.HYBRID,
        }
    ):
        return auth
    if mode not in {PerformanceMode.GOVERNMENT_PMS, PerformanceMode.HYBRID}:
        raise HTTPException(
            status_code=403,
            detail="Government PMS mode required",
        )
    return auth


def require_inventory_access(
    auth: WebAuthContext = Depends(require_web_auth),
) -> WebAuthContext:
    """Require access to the Inventory module."""
    if not auth.has_module_access("inventory"):
        raise HTTPException(
            status_code=403,
            detail="Inventory module access required",
        )
    return auth


def require_fleet_access(
    auth: WebAuthContext = Depends(require_web_auth),
) -> WebAuthContext:
    """Require access to the Fleet module."""
    if not auth.has_module_access("fleet"):
        raise HTTPException(
            status_code=403,
            detail="Fleet module access required",
        )
    return auth


def require_public_sector_access(
    auth: WebAuthContext = Depends(require_web_auth),
) -> WebAuthContext:
    """Require access to the Public Sector (IPSAS) module."""
    if not auth.has_module_access("public_sector"):
        raise HTTPException(
            status_code=403,
            detail="Public Sector module access required",
        )
    return auth


def require_support_access(
    auth: WebAuthContext = Depends(require_web_auth),
) -> WebAuthContext:
    """Require access to the Support module."""
    if not auth.has_module_access("support"):
        raise HTTPException(
            status_code=403,
            detail="Support module access required",
        )
    return auth


def require_procurement_access(
    auth: WebAuthContext = Depends(require_web_auth),
) -> WebAuthContext:
    """Require access to the Procurement module."""
    if not auth.has_module_access("procurement"):
        raise HTTPException(
            status_code=403,
            detail="Procurement module access required",
        )
    return auth


def require_projects_access(
    auth: WebAuthContext = Depends(require_web_auth),
) -> WebAuthContext:
    """Require access to the Projects module."""
    if not auth.has_module_access("projects"):
        raise HTTPException(
            status_code=403,
            detail="Projects module access required",
        )
    return auth


def require_settings_access(
    auth: WebAuthContext = Depends(require_web_auth),
) -> WebAuthContext:
    """Require access to the Settings module."""
    if not auth.has_module_access("settings"):
        raise HTTPException(
            status_code=403,
            detail="Settings module access required",
        )
    return auth


def require_expense_access(
    auth: WebAuthContext = Depends(require_web_auth),
) -> WebAuthContext:
    """
    Require access to the Expense module.

    Use this dependency for all expense management web routes.
    Also allows access for users with finance:access scope since
    expense claims integrate with the GL.

    Usage:
        @router.get("/expense/claims")
        def expense_claims(
            request: Request,
            auth: WebAuthContext = Depends(require_expense_access),
        ):
            ...
    """
    # Allow both expense:access and finance:access since they're related
    if not auth.has_module_access("expense") and not auth.has_module_access("finance"):
        raise HTTPException(
            status_code=403,
            detail="Expense module access required",
        )
    return auth


def require_self_service_access(
    auth: WebAuthContext = Depends(require_web_auth),
) -> WebAuthContext:
    """
    Require access to employee self-service pages.

    Allows users with self-service access or full HR module access.
    """
    if not auth.has_module_access("self_service") and not auth.has_module_access(
        "people"
    ):
        raise HTTPException(
            status_code=403,
            detail="Self-service access required",
        )
    return auth


def require_training_access(
    auth: WebAuthContext = Depends(require_web_auth),
) -> WebAuthContext:
    """Require access to Training/Learning routes."""
    if auth.is_admin or auth.has_permission("training:access"):
        return auth
    if auth.has_module_access("people"):
        return auth
    raise HTTPException(
        status_code=403,
        detail="Training access required",
    )


def require_discipline_access(
    auth: WebAuthContext = Depends(require_web_auth),
) -> WebAuthContext:
    """
    Require access to discipline management routes.

    Allows users with explicit discipline permission, HR module access, or admin.
    """
    if auth.is_admin or auth.has_permission("discipline:access"):
        return auth
    if auth.has_module_access("people"):
        return auth
    raise HTTPException(
        status_code=403,
        detail="Discipline access required",
    )


def require_discipline_cases_read(
    auth: WebAuthContext = Depends(require_discipline_access),
) -> WebAuthContext:
    """Require permission to view discipline cases."""
    if auth.is_admin or auth.has_module_access("people"):
        return auth
    if auth.has_permission("discipline:cases:read"):
        return auth
    raise HTTPException(
        status_code=403,
        detail="Discipline case read permission required",
    )


def require_discipline_cases_create(
    auth: WebAuthContext = Depends(require_discipline_access),
) -> WebAuthContext:
    """Require permission to create discipline cases."""
    if auth.is_admin or auth.has_module_access("people"):
        return auth
    if auth.has_permission("discipline:cases:create"):
        return auth
    raise HTTPException(
        status_code=403,
        detail="Discipline case create permission required",
    )


def require_discipline_cases_update(
    auth: WebAuthContext = Depends(require_discipline_access),
) -> WebAuthContext:
    """Require permission to update discipline case records."""
    if auth.is_admin or auth.has_module_access("people"):
        return auth
    if auth.has_permission("discipline:cases:update"):
        return auth
    raise HTTPException(
        status_code=403,
        detail="Discipline case update permission required",
    )


def require_discipline_workflow_manage(
    auth: WebAuthContext = Depends(require_discipline_access),
) -> WebAuthContext:
    """Require permission to execute discipline workflow actions."""
    if auth.is_admin or auth.has_module_access("people"):
        return auth
    if auth.has_permission("discipline:workflow:manage"):
        return auth
    raise HTTPException(
        status_code=403,
        detail="Discipline workflow permission required",
    )


def require_self_service_leave_approver(
    auth: WebAuthContext = Depends(require_self_service_access),
) -> WebAuthContext:
    """Require self-service access plus leave approval permission."""
    normalized_roles = {
        str(role).strip().lower().replace(" ", "_") for role in (auth.roles or [])
    }
    if "admin" in normalized_roles or "leave_approver" in normalized_roles:
        return auth
    permissions = [
        "leave:applications:approve:tier1",
        "leave:applications:approve:tier2",
        "leave:applications:approve:tier3",
    ]
    if not auth.has_any_permission(permissions):
        raise HTTPException(
            status_code=403,
            detail="Leave approval permission required",
        )
    return auth


def require_self_service_discipline_manager(
    auth: WebAuthContext = Depends(require_self_service_access),
    db: Session = Depends(get_db_for_org),
) -> WebAuthContext:
    """Require self-service access plus team discipline authority.

    Full discipline permissions still grant access. Managers without People/HR
    access are allowed through only when the HR position tree shows current
    direct reports. Department-scoped discipline access is allowed only when
    the user has an employee profile with a department; the self-service
    discipline service then scopes every case operation to the permitted team.
    """
    if auth.is_admin:
        return auth
    if auth.has_any_permission(
        [
            "discipline:access",
            "discipline:cases:read",
            "discipline:cases:create",
            "discipline:cases:update",
            "discipline:workflow:manage",
        ]
    ):
        return auth
    has_department_scope = auth.has_permission("discipline:department:read")
    if auth.organization_id is None or auth.person_id is None:
        raise HTTPException(
            status_code=403,
            detail="Team discipline permission required",
        )

    from app.models.people.hr.employee import Employee
    from app.services.people.hr.org_resolver import OrgResolver

    employee_id = auth.employee_id
    if employee_id is None:
        employee_id = db.scalar(
            select(Employee.employee_id).where(
                Employee.organization_id == auth.organization_id,
                Employee.person_id == auth.person_id,
            )
        )

    if employee_id is not None:
        if has_department_scope:
            department_id = db.scalar(
                select(Employee.department_id).where(
                    Employee.organization_id == auth.organization_id,
                    Employee.employee_id == employee_id,
                )
            )
            if department_id is not None:
                return auth

        if OrgResolver(db).get_direct_reports(
            employee_id,
            auth.organization_id,
        ):
            return auth

    raise HTTPException(
        status_code=403,
        detail="Team discipline permission required",
    )


def require_self_service_expense_approver(
    auth: WebAuthContext = Depends(require_self_service_access),
) -> WebAuthContext:
    """Require self-service access plus expense approval permission."""
    normalized_roles = {
        str(role).strip().lower().replace(" ", "_") for role in (auth.roles or [])
    }
    if "admin" in normalized_roles or "expense_approver" in normalized_roles:
        return auth
    permissions = [
        "expense:claims:approve:tier1",
        "expense:claims:approve:tier2",
        "expense:claims:approve:tier3",
    ]
    if not auth.has_any_permission(permissions):
        raise HTTPException(
            status_code=403,
            detail="Expense approval permission required",
        )
    return auth


def require_module_access(module: str):
    """
    Factory for creating module access dependencies.

    Usage:
        @router.get("/custom/dashboard")
        def custom_dashboard(
            request: Request,
            auth: WebAuthContext = Depends(require_module_access("custom")),
        ):
            ...
    """

    def _require_module_access(
        auth: WebAuthContext = Depends(require_web_auth),
    ) -> WebAuthContext:
        if not auth.has_module_access(module):
            raise HTTPException(
                status_code=403,
                detail=f"{module.title()} module access required",
            )
        return auth

    return _require_module_access


def require_web_permission(permission: str):
    """
    Factory for creating permission-based web route dependencies.

    Usage:
        @router.get("/gl/journals")
        def list_journals(
            request: Request,
            auth: WebAuthContext = Depends(require_web_permission("gl:read")),
        ):
            ...
    """

    def _require_permission(
        auth: WebAuthContext = Depends(require_web_auth),
    ) -> WebAuthContext:
        if not auth.has_permission(permission):
            raise HTTPException(
                status_code=403,
                detail=f"Permission '{permission}' required",
            )
        return auth

    return _require_permission


def require_any_web_permission(permissions: list[str]):
    """
    Factory for requiring any of the specified permissions.

    Usage:
        @router.get("/reports/overview")
        def reports_overview(
            auth: WebAuthContext = Depends(require_any_web_permission(["reports:read", "gl:read"])),
        ):
            ...
    """

    def _require_any_permission(
        auth: WebAuthContext = Depends(require_web_auth),
    ) -> WebAuthContext:
        if not auth.has_any_permission(permissions):
            raise HTTPException(
                status_code=403,
                detail=f"One of these permissions required: {', '.join(permissions)}",
            )
        return auth

    return _require_any_permission
