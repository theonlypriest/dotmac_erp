"""Shared service-principal authentication and tenant session dependencies."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings as app_settings
from app.db import SessionLocal
from app.db.session_context import allow_cross_org, tenant_scope_for_session
from app.models.auth import ApiKey
from app.models.person import Person
from app.rls import set_current_organization_sync
from app.services.auth import hash_api_key
from app.services.common import coerce_uuid

try:
    from datetime import UTC  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    UTC = timezone.utc

logger = logging.getLogger(__name__)

_LAST_USED_THROTTLE = timedelta(minutes=5)


def _last_used_is_stale(last_used: datetime | None, now: datetime) -> bool:
    if last_used is None:
        return True
    try:
        return (now - last_used) >= _LAST_USED_THROTTLE
    except TypeError:
        return True


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_service_auth(
    x_api_key: str = Header(..., description="Service API key"),
    db: Session = Depends(_get_db),
) -> dict:
    """Authenticate an API-key service principal and derive its tenant."""
    now = datetime.now(UTC)
    api_key = db.scalar(
        select(ApiKey).where(
            ApiKey.key_hash == hash_api_key(x_api_key),
            ApiKey.is_active.is_(True),
            ApiKey.revoked_at.is_(None),
        )
    )
    if not api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    if api_key.expires_at and api_key.expires_at <= now:
        raise HTTPException(status_code=401, detail="API key expired")
    if not api_key.person_id:
        raise HTTPException(
            status_code=403, detail="API key not associated with a user"
        )

    if app_settings.default_organization_id:
        set_current_organization_sync(
            db, coerce_uuid(app_settings.default_organization_id)
        )
    with allow_cross_org(db):
        person = db.get(Person, api_key.person_id)
    if not person or not person.organization_id:
        raise HTTPException(status_code=403, detail="User has no organization access")

    person_org_id = person.organization_id
    person_id = person.id
    api_key_id = api_key.id
    service_label = api_key.label
    api_key_scopes = list(api_key.scopes) if api_key.scopes else []

    set_current_organization_sync(db, person_org_id)
    if _last_used_is_stale(api_key.last_used_at, now):
        api_key.last_used_at = now
        db.commit()

    logger.info(
        "Service principal authenticated: org=%s, key=%s",
        person_org_id,
        service_label or api_key_id,
    )
    return {
        "organization_id": person_org_id,
        "person_id": person_id,
        "api_key_id": api_key_id,
        "service_label": service_label,
        "scopes": api_key_scopes,
    }


def require_service_scope(scope: str):
    """Require one explicitly granted service-principal scope."""

    def _dep(auth: dict = Depends(require_service_auth)) -> dict:
        scopes = auth.get("scopes") or []
        if scope not in scopes:
            raise HTTPException(
                status_code=403,
                detail=f"API key missing required scope: {scope}",
            )
        return auth

    return _dep


def require_explicit_service_scope(scope: str):
    """Require an explicitly granted scope; empty legacy scopes fail closed."""

    def _dep(auth: dict = Depends(require_service_auth)) -> dict:
        if scope not in (auth.get("scopes") or []):
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "authorization_failed",
                    "message": f"Service credential lacks required scope: {scope}",
                },
            )
        return auth

    return _dep


def require_any_service_scope(*required: str):
    """Require at least one explicitly granted service-principal scope."""

    def _dep(auth: dict = Depends(require_service_auth)) -> dict:
        scopes = auth.get("scopes") or []
        if not any(scope in scopes for scope in required):
            raise HTTPException(
                status_code=403,
                detail=f"API key missing required scope: one of {', '.join(required)}",
            )
        return auth

    return _dep


def get_db_with_service_org(auth: dict = Depends(require_service_auth)):
    """Yield a service session whose tenant scope survives intermediate commits."""
    organization_id = auth["organization_id"]
    if not isinstance(organization_id, UUID):
        organization_id = UUID(str(organization_id))
    db = SessionLocal()
    try:
        with tenant_scope_for_session(db, organization_id):
            yield db
            db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
