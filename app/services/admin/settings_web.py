"""
Admin Settings Web Service.

Provides context and update functions for Admin settings UI pages.
Handles org-wide settings: Organization profile, Branding, Email, Features, Payments.
"""

import logging
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain, SettingValueType
from app.models.finance.core_org import Organization, PerformanceMode
from app.schemas.settings import DomainSettingUpdate
from app.services.domain_settings import DomainSettings
from app.services.formatting_context import (
    COMMON_TIMEZONES,
)
from app.services.people.perf.performance_mode_policy import (
    is_pms_enabled_for_org,
    resolve_performance_mode,
)
from app.services.formatting_context import (
    DATE_FORMAT_CHOICES as DATE_FORMATS,
)
from app.services.formatting_context import (
    NUMBER_FORMAT_CHOICES as NUMBER_FORMATS,
)
from app.services.settings_cache import get_cached_setting
from app.services.settings_spec import (
    resolve_value,
)
from app.services.tenant_projection import reconcile_organization_tenant

logger = logging.getLogger(__name__)


# ── Font presets (available in the app's self-hosted stylesheet) ──
FONT_PRESETS: dict[str, list[dict[str, str]]] = {
    "display": [
        {"value": "", "label": "Default (Fraunces)"},
        {"value": "Fraunces, Georgia, serif", "label": "Fraunces"},
        {"value": "DM Sans, system-ui, sans-serif", "label": "DM Sans"},
        {"value": "Georgia, Cambria, serif", "label": "Georgia"},
        {"value": "Palatino Linotype, Book Antiqua, serif", "label": "Palatino"},
        {"value": "system-ui, -apple-system, sans-serif", "label": "System UI"},
    ],
    "body": [
        {"value": "", "label": "Default (DM Sans)"},
        {"value": "DM Sans, system-ui, sans-serif", "label": "DM Sans"},
        {"value": "Inter, system-ui, sans-serif", "label": "Inter"},
        {"value": "system-ui, -apple-system, sans-serif", "label": "System UI"},
        {"value": "Segoe UI, Roboto, sans-serif", "label": "Segoe UI"},
        {"value": "Helvetica Neue, Arial, sans-serif", "label": "Helvetica"},
    ],
    "mono": [
        {"value": "", "label": "Default (JetBrains Mono)"},
        {"value": "JetBrains Mono, monospace", "label": "JetBrains Mono"},
        {"value": "Fira Code, monospace", "label": "Fira Code"},
        {"value": "Source Code Pro, monospace", "label": "Source Code Pro"},
        {"value": "Menlo, Monaco, monospace", "label": "Menlo / Monaco"},
        {"value": "Consolas, monospace", "label": "Consolas"},
    ],
}


# Hub sections configuration
ADMIN_SETTINGS_SECTIONS = [
    {
        "title": "Organization",
        "description": "Company profile, legal details, and contact information",
        "url": "/admin/settings/organization",
        "icon": "building-office",
    },
    {
        "title": "Branding",
        "description": "Logo, colors, and visual identity",
        "url": "/admin/settings/branding",
        "icon": "swatch",
    },
    {
        "title": "Email",
        "description": "SMTP configuration and email profiles",
        "url": "/admin/settings/email",
        "icon": "envelope",
    },
    {
        "title": "Features",
        "description": "Enable or disable system features",
        "url": "/admin/settings/features",
        "icon": "flag",
    },
    {
        "title": "Integrations",
        "description": "Inbound providers, service connections, and outbound hooks",
        "url": "/admin/settings/integrations",
        "icon": "link",
    },
    {
        "title": "Coach / AI",
        "description": "Configure LLM backends (DeepSeek, Llama) for the AI Coach module.",
        "url": "/admin/settings/coach",
        "icon": "lightning-bolt",
    },
    {
        "title": "Advanced",
        "description": "Raw system settings (for administrators)",
        "url": "/admin/settings/advanced",
        "icon": "cog",
    },
]


class AdminSettingsWebService:
    """Service for Admin Settings UI."""

    # ========== Hub ==========

    def get_hub_context(self, organization_id: uuid.UUID) -> dict[str, Any]:
        """Get context for settings hub page."""
        return {
            "settings_sections": ADMIN_SETTINGS_SECTIONS,
        }

    # ========== Organization Profile ==========

    def get_organization_context(
        self, db: Session, organization_id: uuid.UUID
    ) -> dict[str, Any]:
        """Get organization profile for editing."""
        org = db.get(Organization, organization_id)
        if not org:
            return {"organization": None, "error": "Organization not found"}

        return {
            "organization": org,
            "timezones": COMMON_TIMEZONES,
            "date_formats": DATE_FORMATS,
            "number_formats": NUMBER_FORMATS,
            "performance_modes": [
                {"value": PerformanceMode.PRIVATE.value, "label": "Private"},
                {
                    "value": PerformanceMode.GOVERNMENT_PMS.value,
                    "label": "Government PMS",
                },
                {"value": PerformanceMode.HYBRID.value, "label": "Hybrid"},
            ],
        }

    def update_organization(
        self,
        db: Session,
        organization_id: uuid.UUID,
        data: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """Update organization profile."""
        org = db.get(Organization, organization_id)
        if not org:
            return False, "Organization not found"

        pms_enabled_before = bool(org.pms_ohcsf_enabled)
        mode_updated = False

        # Update allowed fields
        allowed_fields = [
            "legal_name",
            "trading_name",
            "registration_number",
            "tax_identification_number",
            "functional_currency_code",
            "presentation_currency_code",
            "fiscal_year_end_month",
            "fiscal_year_end_day",
            "timezone",
            "date_format",
            "number_format",
            "contact_email",
            "hr_weekly_report_email",
            "contact_phone",
            "address_line1",
            "address_line2",
            "city",
            "state",
            "postal_code",
            "country",
            "logo_url",
            "website_url",
            "performance_mode",
        ]

        for field in allowed_fields:
            if field in data:
                value = data[field]
                # Handle empty strings as None for optional fields
                if value == "" and field not in [
                    "legal_name",
                    "functional_currency_code",
                    "presentation_currency_code",
                ]:
                    value = None
                setattr(org, field, value)

        if "performance_mode" in data:
            raw_mode = (data.get("performance_mode") or "").strip().upper()
            if raw_mode:
                try:
                    org.performance_mode = PerformanceMode(raw_mode)
                    mode_updated = True
                except ValueError:
                    return False, "Invalid performance mode"

        # Transition policy: when explicit mode is set, derive legacy flag from mode.
        if mode_updated:
            org.pms_ohcsf_enabled = is_pms_enabled_for_org(org)
        elif "pms_ohcsf_enabled" in data:
            org.pms_ohcsf_enabled = str(data["pms_ohcsf_enabled"]).lower() == "true"
            current_mode = resolve_performance_mode(org)
            # Transition compatibility: keep mode aligned when using legacy toggle.
            if org.pms_ohcsf_enabled and current_mode == PerformanceMode.PRIVATE:
                org.performance_mode = PerformanceMode.GOVERNMENT_PMS
            elif (
                not org.pms_ohcsf_enabled
                and current_mode == PerformanceMode.GOVERNMENT_PMS
            ):
                org.performance_mode = PerformanceMode.PRIVATE

        if org.pms_ohcsf_enabled and not pms_enabled_before:
            from app.services.people.perf.pms_config_service import PMSConfigService

            PMSConfigService(db).activate_ohcsf_pms(organization_id)

        reconcile_organization_tenant(db, org)
        db.commit()
        return True, None

    # ========== Branding ==========

    def get_branding_context(
        self, db: Session, organization_id: uuid.UUID
    ) -> dict[str, Any]:
        """Get branding settings for the form."""
        org = db.get(Organization, organization_id)
        if not org:
            return {"organization": None, "error": "Organization not found"}

        # Use get_or_create so branding is always non-None
        branding = None
        try:
            from app.services.finance.branding import BrandingService

            branding = BrandingService(db).get_or_create(organization_id)
        except Exception:
            # Fall back to raw query if BrandingService unavailable
            try:
                from app.models.finance.core_org.organization_branding import (
                    OrganizationBranding,
                )

                branding = db.execute(
                    select(OrganizationBranding).where(
                        OrganizationBranding.organization_id == organization_id
                    )
                ).scalar_one_or_none()
            except Exception:
                logger.exception("Ignored exception")

        email_logo_url = get_cached_setting(
            db,
            SettingDomain.email,
            "email_logo_url",
            "",
            organization_id=organization_id,
        )
        report_logo_url = get_cached_setting(
            db,
            SettingDomain.reporting,
            "report_logo_url",
            "",
            organization_id=organization_id,
        )

        # Import enums for UI controls
        from app.models.finance.core_org.organization_branding import (
            BorderRadiusStyle,
            ButtonStyle,
            SidebarStyle,
        )

        # Build Alpine.js-friendly config dict with safe defaults
        branding_config = {
            "display_name": (branding.display_name or "") if branding else "",
            "tagline": (branding.tagline or "") if branding else "",
            "brand_mark": (branding.brand_mark or "") if branding else "",
            "primary_color": (branding.primary_color or "#0D9488")
            if branding
            else "#0D9488",
            "accent_color": (branding.accent_color or "#D97706")
            if branding
            else "#D97706",
            "font_family_display": (branding.font_family_display or "")
            if branding
            else "",
            "font_family_body": (branding.font_family_body or "") if branding else "",
            "font_family_mono": (branding.font_family_mono or "") if branding else "",
            "border_radius": (
                branding.border_radius.value
                if branding and branding.border_radius
                else "rounded"
            ),
            "button_style": (
                branding.button_style.value
                if branding and branding.button_style
                else "gradient"
            ),
            "sidebar_style": (
                branding.sidebar_style.value
                if branding and branding.sidebar_style
                else "dark"
            ),
        }

        return {
            "organization": org,
            "branding": branding,
            "branding_config": branding_config,
            "email_logo_url": email_logo_url or "",
            "report_logo_url": report_logo_url or "",
            "font_presets": FONT_PRESETS,
            "border_radius_choices": [
                {"value": e.value, "label": e.name.replace("_", " ").title()}
                for e in BorderRadiusStyle
            ],
            "button_style_choices": [
                {"value": e.value, "label": e.name.replace("_", " ").title()}
                for e in ButtonStyle
            ],
            "sidebar_style_choices": [
                {"value": e.value, "label": e.name.replace("_", " ").title()}
                for e in SidebarStyle
            ],
        }

    def update_branding(
        self,
        db: Session,
        organization_id: uuid.UUID,
        data: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """Update branding settings."""
        # Raw CSS is retired fleet-wide (ADR-0006 D8). This form path bypasses
        # the BrandingCreate/BrandingUpdate schemas — their `extra="forbid"`
        # never sees it — so it must reject the field itself. Rejecting, rather
        # than dropping the key, is the point: a silent ignore plus a success
        # redirect tells the operator their CSS was saved when it was not.
        if str(data.get("custom_css") or "").strip():
            return False, (
                "Custom CSS is no longer accepted. Raw CSS can hide or rewrite "
                "legal text, overlay controls, and leak field contents, so "
                "branding is now set through the colour, typography and logo "
                "fields. Remove the custom CSS and save again."
            )

        org = db.get(Organization, organization_id)
        if not org:
            return False, "Organization not found"

        # Update logo_url on organization if provided
        if "logo_url" in data:
            org.logo_url = data["logo_url"] if data["logo_url"] else None

        # Try to update OrganizationBranding if model exists
        try:
            from app.models.finance.core_org.organization_branding import (
                OrganizationBranding,
            )

            branding = db.execute(
                select(OrganizationBranding).where(
                    OrganizationBranding.organization_id == organization_id
                )
            ).scalar_one_or_none()

            branding_fields = [
                "display_name",
                "tagline",
                "brand_mark",
                "logo_url",
                "logo_dark_url",
                "favicon_url",
                "primary_color",
                "primary_light",
                "primary_dark",
                "accent_color",
                "accent_light",
                "accent_dark",
                "success_color",
                "warning_color",
                "danger_color",
                "font_family_display",
                "font_family_body",
                "font_family_mono",
                "border_radius",
                "button_style",
                "sidebar_style",
            ]

            if branding:
                for field in branding_fields:
                    if field in data:
                        setattr(branding, field, data[field] if data[field] else None)
            else:
                # Create new branding record if any branding data provided
                has_branding_data = any(data.get(f) for f in branding_fields)
                if has_branding_data:
                    branding = OrganizationBranding(
                        organization_id=organization_id,
                        **{f: data.get(f) for f in branding_fields if f in data},
                    )
                    db.add(branding)
        except Exception as e:
            logger.debug("OrganizationBranding model unavailable: %s", e)

        email_logo_url = (data.get("email_logo_url") or "").strip()
        report_logo_url = (data.get("report_logo_url") or "").strip()

        email_settings = DomainSettings(SettingDomain.email)
        reporting_settings = DomainSettings(SettingDomain.reporting)

        email_settings.upsert_by_key(
            db,
            "email_logo_url",
            DomainSettingUpdate(
                value_type=SettingValueType.string,
                value_text=email_logo_url or None,
                is_active=bool(email_logo_url),
            ),
        )
        reporting_settings.upsert_by_key(
            db,
            "report_logo_url",
            DomainSettingUpdate(
                value_type=SettingValueType.string,
                value_text=report_logo_url or None,
                is_active=bool(report_logo_url),
            ),
        )

        db.commit()
        return True, None

    # ========== Email Settings ==========

    def get_email_context(
        self, db: Session, organization_id: uuid.UUID
    ) -> dict[str, Any]:
        """Get email settings for the form."""
        # Delegate to finance settings service for email context
        from app.services.finance.settings_web import settings_web_service

        return settings_web_service.get_email_settings_context(db, organization_id)

    def update_email(
        self,
        db: Session,
        organization_id: uuid.UUID,
        data: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """Update email settings."""
        # Delegate to finance settings service for email updates
        from app.services.finance.settings_web import settings_web_service

        return settings_web_service.update_email_settings(db, organization_id, data)

    def test_email(
        self,
        db: Session,
        organization_id: uuid.UUID,
        data: dict[str, Any],
        target: str,
    ) -> tuple[bool, str]:
        """Test email settings without saving form changes."""
        from app.services.finance.settings_web import settings_web_service

        return settings_web_service.test_email_settings(
            db, organization_id, data, target
        )

    # ========== Feature Flags ==========

    def get_features_context(
        self, db: Session, organization_id: uuid.UUID
    ) -> dict[str, Any]:
        """Get feature flags for the admin UI, grouped by category."""
        from app.services.feature_flag_service import FeatureFlagService

        service = FeatureFlagService(db)
        flags = service.get_all_flags(organization_id)
        org = db.get(Organization, organization_id)

        # Group by category for the template
        categories: dict[str, list[dict[str, Any]]] = {}
        for flag in flags:
            cat_label = flag.category.value.replace("_", " ").title()
            cat_list = categories.setdefault(cat_label, [])
            cat_list.append(
                {
                    "key": flag.flag_key,
                    "label": flag.label,
                    "description": flag.description,
                    "enabled": flag.enabled,
                    "default_enabled": flag.default_enabled,
                    "is_org_override": flag.is_org_override,
                    "status": flag.status.value,
                    "owner": flag.owner,
                    "expires_at": flag.expires_at,
                    "category": flag.category.value,
                }
            )

        if org is not None:
            module_flags = categories.setdefault("Module", [])
            module_flags.append(
                {
                    "key": "pms_ohcsf_enabled",
                    "label": "PMS (OHCSF)",
                    "description": (
                        "Enable the OHCSF Performance Management System for "
                        "this organization and show PMS in the People sidebar."
                    ),
                    "enabled": is_pms_enabled_for_org(org),
                    "default_enabled": False,
                    "is_org_override": True,
                    "status": "ACTIVE",
                    "owner": "People",
                    "expires_at": None,
                    "category": "MODULE",
                }
            )

        # Flat list for backward compatibility
        all_features = [item for group in categories.values() for item in group]

        return {
            "features": all_features,
            "categories": categories,
        }

    def toggle_feature(
        self,
        db: Session,
        organization_id: uuid.UUID,
        key: str,
        enabled: bool,
        *,
        changed_by_id: uuid.UUID | None = None,
    ) -> tuple[bool, str | None]:
        """Toggle a feature flag for an organization."""
        from app.services.feature_flag_service import FeatureFlagService

        if key == "pms_ohcsf_enabled":
            org = db.get(Organization, organization_id)
            if org is None:
                return False, "Organization not found"

            pms_enabled_before = bool(org.pms_ohcsf_enabled)
            org.pms_ohcsf_enabled = enabled
            current_mode = resolve_performance_mode(org)
            if enabled and current_mode == PerformanceMode.PRIVATE:
                org.performance_mode = PerformanceMode.GOVERNMENT_PMS
            elif not enabled and current_mode == PerformanceMode.GOVERNMENT_PMS:
                org.performance_mode = PerformanceMode.PRIVATE

            if enabled and not pms_enabled_before:
                from app.services.people.perf.pms_config_service import PMSConfigService

                PMSConfigService(db).activate_ohcsf_pms(organization_id)

            db.commit()
            return True, None

        service = FeatureFlagService(db)
        try:
            service.toggle(organization_id, key, enabled, changed_by_id=changed_by_id)
            db.commit()
            return True, None
        except ValueError as e:
            return False, str(e)

    # ========== Payments Settings ==========

    def get_payments_hub_context(
        self, db: Session, organization_id: uuid.UUID
    ) -> dict[str, Any]:
        """Get payments hub context with available providers."""
        # Check which payment providers are configured
        paystack_enabled = resolve_value(db, SettingDomain.payments, "paystack_enabled")

        mono_enabled = resolve_value(db, SettingDomain.banking, "mono_enabled")

        providers = [
            {
                "name": "Paystack",
                "slug": "paystack",
                "description": "Accept payments via Paystack (cards, bank transfers)",
                "configured": bool(paystack_enabled),
                "url": "/admin/settings/payments/paystack",
                "icon": "credit-card",
            },
            {
                "name": "Mono Connect",
                "slug": "mono",
                "description": "Connect bank accounts for automatic statement retrieval",
                "configured": bool(mono_enabled),
                "url": "/admin/settings/banking/mono",
                "icon": "building",
            },
        ]

        return {"providers": providers}

    def get_integrations_context(
        self, db: Session, organization_id: uuid.UUID
    ) -> dict[str, Any]:
        """Build one control-plane hub without surfacing stored credentials."""
        from app.models.finance.platform.service_hook import ServiceHook
        from app.models.sync import IntegrationConfig, IntegrationType

        paystack_enabled = bool(
            resolve_value(db, SettingDomain.payments, "paystack_enabled")
        )
        sub_configured = bool(
            db.scalar(
                select(IntegrationConfig.config_id).where(
                    IntegrationConfig.organization_id == organization_id,
                    IntegrationConfig.integration_type == IntegrationType.DOTMAC_SUB,
                    IntegrationConfig.is_active.is_(True),
                    IntegrationConfig.api_key.is_not(None),
                    IntegrationConfig.api_key != "",
                )
            )
        )
        active_hooks = int(
            db.scalar(
                select(func.count(ServiceHook.hook_id)).where(
                    ServiceHook.organization_id == organization_id,
                    ServiceHook.is_active.is_(True),
                )
            )
            or 0
        )
        return {
            "integrations": [
                {
                    "name": "Paystack inbound payments",
                    "description": "Credentials, ERP ingress URL, and the verified relay to Dotmac Sub.",
                    "url": "/admin/settings/payments/paystack",
                    "configured": paystack_enabled,
                    "direction": "Inbound → ERP → Dotmac Sub",
                    "icon": "credit-card",
                },
                {
                    "name": "Dotmac Sub",
                    "description": "Scoped service API key, sync health, and connection verification.",
                    "url": "/admin/sync/dotmac-sub/config",
                    "configured": sub_configured,
                    "direction": "Two-way service integration",
                    "icon": "refresh",
                },
                {
                    "name": "Outbound service hooks",
                    "description": "ERP-owned subscriptions that deliver domain events to external URLs.",
                    "url": "/admin/settings/service-hooks",
                    "configured": active_hooks > 0,
                    "direction": f"Outbound only · {active_hooks} active",
                    "icon": "link",
                },
            ]
        }

    def get_paystack_context(
        self, db: Session, organization_id: uuid.UUID
    ) -> dict[str, Any]:
        """Get Paystack settings for the form."""
        # Delegate to finance settings service
        from app.services.finance.settings_web import settings_web_service

        return settings_web_service.get_payments_settings_context(db, organization_id)

    def update_paystack(
        self,
        db: Session,
        organization_id: uuid.UUID,
        data: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """Update Paystack settings."""
        # Delegate to finance settings service
        from app.services.finance.settings_web import settings_web_service

        return settings_web_service.update_payments_settings(db, organization_id, data)

    # ========== Mono Connect Settings ==========

    def get_mono_context(
        self, db: Session, organization_id: uuid.UUID
    ) -> dict[str, Any]:
        """Get Mono Connect settings for the form.

        Secret keys are reported as ``has_value`` only — never their value. The
        template already renders them as write-only password fields, but the
        plaintext was still being placed in the render context, one
        ``{{ settings | tojson }}`` or debug dump away from disclosure. Don't
        carry it there at all.
        """
        secret_keys = {"mono_secret_key", "mono_webhook_secret"}
        mono_keys = [
            "mono_enabled",
            "mono_public_key",
            "mono_secret_key",
            "mono_webhook_secret",
        ]
        settings_map: dict[str, Any] = {}
        for key in mono_keys:
            value = resolve_value(db, SettingDomain.banking, key)
            has_value = value is not None and str(value).strip() != ""
            settings_map[key] = {
                "value": None if key in secret_keys else value,
                "has_value": has_value,
            }
        return {"settings": settings_map}

    def update_mono(
        self,
        db: Session,
        organization_id: uuid.UUID,
        data: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """Update Mono Connect settings."""
        try:
            banking_svc = DomainSettings(SettingDomain.banking)

            secret_keys = {"mono_secret_key", "mono_webhook_secret"}
            mono_keys = [
                "mono_enabled",
                "mono_public_key",
                "mono_secret_key",
                "mono_webhook_secret",
            ]

            # Ensure unchecked checkbox persists as false
            data.setdefault("mono_enabled", "false")

            for key in mono_keys:
                value = data.get(key, "")

                # Skip empty secret fields (keep existing value)
                if key in secret_keys and not str(value).strip():
                    continue

                # Coerce boolean
                value_text: str | None
                if key == "mono_enabled":
                    value = str(value).lower() in ("true", "1", "on", "yes")
                    value_text = str(value)
                else:
                    value_text = str(value).strip() if value else None

                banking_svc.upsert_by_key(
                    db,
                    key,
                    DomainSettingUpdate(
                        value_type=(
                            SettingValueType.boolean
                            if key == "mono_enabled"
                            else SettingValueType.string
                        ),
                        value_text=value_text,
                        is_secret=key in secret_keys,
                    ),
                )

            db.flush()
            return True, None
        except Exception as e:
            logger.exception("Failed to update Mono settings")
            return False, str(e)

    # ========== Coach / AI Settings ==========

    def get_coach_context(
        self, db: Session, organization_id: uuid.UUID
    ) -> dict[str, Any]:
        """Get Coach / AI settings for the form."""
        from app.services.finance.settings_web import settings_web_service

        return settings_web_service.get_coach_settings_context(db, organization_id)

    def update_coach(
        self,
        db: Session,
        organization_id: uuid.UUID,
        data: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """Update Coach / AI settings."""
        from app.services.finance.settings_web import settings_web_service

        return settings_web_service.update_coach_settings(db, organization_id, data)


# Singleton instance
admin_settings_web_service = AdminSettingsWebService()
