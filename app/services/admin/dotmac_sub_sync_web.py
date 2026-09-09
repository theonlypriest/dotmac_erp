"""
dotmac_sub Sync Admin web service.

UI for managing the dotmac_sub integration: credentials (via the encrypted
IntegrationConfig store), connection test, manual sync triggers, and sync-run
history (from SyncHistory).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any
from urllib.parse import quote_plus, urlencode

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.models.sync import (
    IntegrationConfig,
    IntegrationType,
    SyncHistory,
    SyncJobStatus,
)
from app.services.integration_config import IntegrationConfigService
from app.templates import templates
from app.web.deps import WebAuthContext, brand_context, org_brand_context

logger = logging.getLogger(__name__)

_SOURCE = "dotmac_sub"

# Tier marker → friendly label (entity_types[0] recorded by each task).
_TIER_LABELS = {
    "resellers": "Incremental (30 min)",
    "invoices_reconciliation": "Daily reconciliation",
    "full_reconciliation": "Full reconciliation (weekly)",
}


class DotmacSubSyncWebService:
    """Web service for the dotmac_sub integration admin UI."""

    def _base_context(
        self,
        request: Request,
        auth: WebAuthContext | None,
        title: str,
        active_tab: str = "dashboard",
        db: Session | None = None,
    ) -> dict[str, Any]:
        org_branding = None
        if db and auth and auth.organization_id:
            org_branding = org_brand_context(db, auth.organization_id)
        return {
            "request": request,
            "auth": auth,
            "title": title,
            "page_title": title,
            "brand": org_branding or brand_context(),
            "org_branding": org_branding,
            "user": auth.user if auth else {"name": "Admin", "initials": "AD"},
            "csrf_token": getattr(request.state, "csrf_token", ""),
            "active_tab": active_tab,
            "active_page": "sync",
            "module": "admin",
            "sub_module": "dotmac-sub-sync",
        }

    def _require_admin(
        self, request: Request, auth: WebAuthContext | None
    ) -> HTMLResponse | RedirectResponse | None:
        if not auth or not auth.is_authenticated:
            next_path = request.url.path
            if request.url.query:
                next_path = f"{next_path}?{request.url.query}"
            return RedirectResponse(
                url=f"/admin/login?{urlencode({'next': next_path})}",
                status_code=302,
            )
        if not auth.is_admin:
            raise HTTPException(status_code=403, detail="Admin access required")
        return None

    def _get_config(self, db: Session, org_id: uuid.UUID) -> IntegrationConfig | None:
        stmt = select(IntegrationConfig).where(
            IntegrationConfig.organization_id == org_id,
            IntegrationConfig.integration_type == IntegrationType.DOTMAC_SUB,
            IntegrationConfig.is_active.is_(True),
        )
        return db.scalar(stmt)

    def _recent_runs(
        self, db: Session, org_id: uuid.UUID, limit: int = 15
    ) -> list[SyncHistory]:
        stmt = (
            select(SyncHistory)
            .where(
                SyncHistory.organization_id == org_id,
                SyncHistory.source_system == _SOURCE,
            )
            .order_by(desc(SyncHistory.started_at))
            .limit(limit)
        )
        return list(db.scalars(stmt).all())

    def _run_stats(self, db: Session, org_id: uuid.UUID) -> dict[str, Any]:
        total_runs = (
            db.scalar(
                select(func.count(SyncHistory.history_id)).where(
                    SyncHistory.organization_id == org_id,
                    SyncHistory.source_system == _SOURCE,
                )
            )
            or 0
        )
        failed = (
            db.scalar(
                select(func.count(SyncHistory.history_id)).where(
                    SyncHistory.organization_id == org_id,
                    SyncHistory.source_system == _SOURCE,
                    SyncHistory.status == SyncJobStatus.FAILED,
                )
            )
            or 0
        )
        synced = (
            db.scalar(
                select(func.coalesce(func.sum(SyncHistory.synced_count), 0)).where(
                    SyncHistory.organization_id == org_id,
                    SyncHistory.source_system == _SOURCE,
                )
            )
            or 0
        )
        return {"total_runs": total_runs, "failed_runs": failed, "total_synced": synced}

    # ============ Dashboard ============

    def dashboard_response(
        self, request: Request, db: Session, auth: WebAuthContext | None
    ) -> HTMLResponse | RedirectResponse:
        err = self._require_admin(request, auth)
        if err:
            return err
        ctx = self._base_context(request, auth, "dotmac_sub Sync", "dashboard", db)
        org_id = auth.organization_id if auth else None
        if org_id:
            config = self._get_config(db, org_id)
            ctx["config"] = config
            ctx["integration_configured"] = bool(config and config.api_key)
            ctx["stats"] = self._run_stats(db, org_id)
            runs = self._recent_runs(db, org_id)
            ctx["recent_runs"] = runs
            ctx["tier_labels"] = _TIER_LABELS
            ctx["last_run"] = runs[0] if runs else None
        else:
            ctx["integration_configured"] = False
            ctx["stats"] = {"total_runs": 0, "failed_runs": 0, "total_synced": 0}
            ctx["recent_runs"] = []
            ctx["last_run"] = None
        return templates.TemplateResponse(
            request, "admin/sync/dotmac_sub/dashboard.html", ctx
        )

    # ============ Config ============

    def config_response(
        self, request: Request, db: Session, auth: WebAuthContext | None
    ) -> HTMLResponse | RedirectResponse:
        err = self._require_admin(request, auth)
        if err:
            return err
        ctx = self._base_context(
            request, auth, "dotmac_sub Configuration", "config", db
        )
        org_id = auth.organization_id if auth else None
        config = self._get_config(db, org_id) if org_id else None
        ctx["config"] = config
        # Never surface the decrypted token; only whether one is set.
        ctx["has_token"] = bool(config and config.api_key)
        return templates.TemplateResponse(
            request, "admin/sync/dotmac_sub/config.html", ctx
        )

    def config_save_response(
        self,
        request: Request,
        db: Session,
        auth: WebAuthContext | None,
        *,
        base_url: str,
        api_token: str,
        webhook_secret: str,
    ) -> RedirectResponse:
        err = self._require_admin(request, auth)
        if err:
            return err  # type: ignore[return-value]
        org_id = auth.organization_id if auth else None
        if not org_id:
            return RedirectResponse(
                "/admin/sync/dotmac-sub/config?error="
                + quote_plus("No organization context"),
                status_code=302,
            )
        try:
            # api_key is a scoped service key; staff-session credentials are
            # deliberately not accepted by DotmacSubClient.
            svc = IntegrationConfigService(db)
            existing = svc.get_config(org_id, IntegrationType.DOTMAC_SUB)
            if existing:
                svc.update_credentials(
                    org_id,
                    IntegrationType.DOTMAC_SUB,
                    api_key=api_token or None,
                    api_secret=webhook_secret or None,
                    base_url=base_url or None,
                )
            else:
                if not api_token:
                    raise ValueError("A scoped Dotmac Sub service API key is required")
                svc.create_config(
                    organization_id=org_id,
                    integration_type=IntegrationType.DOTMAC_SUB,
                    base_url=base_url,
                    api_key=api_token,
                    api_secret=webhook_secret,
                    company=None,
                    user_id=auth.user_id if auth else None,
                )
            db.commit()
        except Exception as e:  # noqa: BLE001
            logger.exception("Failed to save dotmac_sub config")
            return RedirectResponse(
                "/admin/sync/dotmac-sub/config?error=" + quote_plus(str(e)),
                status_code=302,
            )
        return RedirectResponse(
            "/admin/sync/dotmac-sub/config?success="
            + quote_plus("Configuration saved"),
            status_code=302,
        )

    # ============ Test connection ============

    def test_connection_response(
        self, request: Request, db: Session, auth: WebAuthContext | None
    ) -> RedirectResponse:
        err = self._require_admin(request, auth)
        if err:
            return err  # type: ignore[return-value]
        org_id = auth.organization_id if auth else None
        from app.services.dotmac_sub import DotmacSubClient, DotmacSubConfig

        config = DotmacSubConfig.for_org(db, org_id) if org_id else None
        if not config or not config.is_configured():
            return RedirectResponse(
                "/admin/sync/dotmac-sub?error="
                + quote_plus("Integration is not configured"),
                status_code=302,
            )
        with DotmacSubClient(config) as client:
            ok = client.test_connection()
        if ok and org_id:
            IntegrationConfigService(db).mark_verified(
                org_id, IntegrationType.DOTMAC_SUB
            )
            db.commit()
        msg = "Connection successful" if ok else "Connection failed"
        key = "success" if ok else "error"
        return RedirectResponse(
            f"/admin/sync/dotmac-sub?{key}=" + quote_plus(msg), status_code=302
        )

    # ============ Manual trigger ============

    def trigger_sync_response(
        self,
        request: Request,
        db: Session,
        auth: WebAuthContext | None,
        *,
        tier: str = "incremental",
    ) -> RedirectResponse:
        err = self._require_admin(request, auth)
        if err:
            return err  # type: ignore[return-value]
        org_id = auth.organization_id if auth else None
        from app.tasks.dotmac_sub import (
            run_dotmac_sub_daily_reconciliation,
            run_dotmac_sub_full_reconciliation,
            run_dotmac_sub_incremental_sync,
        )

        task_map = {
            "incremental": run_dotmac_sub_incremental_sync,
            "daily": run_dotmac_sub_daily_reconciliation,
            "full": run_dotmac_sub_full_reconciliation,
        }
        task = task_map.get(tier, run_dotmac_sub_incremental_sync)
        try:
            task.delay(str(org_id) if org_id else None)
            msg = f"{tier.title()} sync queued"
            return RedirectResponse(
                "/admin/sync/dotmac-sub?success=" + quote_plus(msg), status_code=302
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("Failed to queue dotmac_sub sync")
            return RedirectResponse(
                "/admin/sync/dotmac-sub?error=" + quote_plus(str(e)), status_code=302
            )

    # ============ History ============

    def history_response(
        self, request: Request, db: Session, auth: WebAuthContext | None
    ) -> HTMLResponse | RedirectResponse:
        err = self._require_admin(request, auth)
        if err:
            return err
        ctx = self._base_context(
            request, auth, "dotmac_sub Sync History", "history", db
        )
        org_id = auth.organization_id if auth else None
        ctx["runs"] = self._recent_runs(db, org_id, limit=50) if org_id else []
        ctx["tier_labels"] = _TIER_LABELS
        return templates.TemplateResponse(
            request, "admin/sync/dotmac_sub/history.html", ctx
        )


dotmac_sub_sync_web_service = DotmacSubSyncWebService()
