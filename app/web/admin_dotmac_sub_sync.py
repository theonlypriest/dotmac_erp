"""
dotmac_sub Sync Admin web routes.

Thin wrappers around DotmacSubSyncWebService — UI for managing the dotmac_sub
integration (config, connection test, manual triggers, run history).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.services.admin.dotmac_sub_sync_web import dotmac_sub_sync_web_service
from app.web.deps import WebAuthContext, get_db, optional_web_auth

router = APIRouter(prefix="/admin/sync/dotmac-sub", tags=["admin-dotmac-sub-sync-web"])


def _normalize_form(form: Any) -> dict[str, str]:
    if form is None:
        return {}
    return {key: value if isinstance(value, str) else "" for key, value in form.items()}


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    return dotmac_sub_sync_web_service.dashboard_response(request, db, auth)


@router.get("/config", response_class=HTMLResponse)
def config(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    return dotmac_sub_sync_web_service.config_response(request, db, auth)


@router.post("/config", response_class=HTMLResponse)
async def config_save(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()
    form = _normalize_form(form)
    return dotmac_sub_sync_web_service.config_save_response(
        request,
        db,
        auth,
        base_url=form.get("base_url", "").strip(),
        api_token=form.get("api_token", "").strip(),
        webhook_secret=form.get("webhook_secret", "").strip(),
    )


@router.post("/test", response_class=HTMLResponse)
def test_connection(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    return dotmac_sub_sync_web_service.test_connection_response(request, db, auth)


@router.post("/trigger", response_class=HTMLResponse)
async def trigger_sync(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    form = getattr(request.state, "csrf_form", None)
    if form is None:
        form = await request.form()
    form = _normalize_form(form)
    return dotmac_sub_sync_web_service.trigger_sync_response(
        request, db, auth, tier=form.get("tier", "incremental")
    )


@router.get("/history", response_class=HTMLResponse)
def history(
    request: Request,
    db: Session = Depends(get_db),
    auth: WebAuthContext = Depends(optional_web_auth),
):
    return dotmac_sub_sync_web_service.history_response(request, db, auth)
