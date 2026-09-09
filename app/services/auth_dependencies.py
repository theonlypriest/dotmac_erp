import logging
import os
from datetime import datetime, timedelta, timezone
from typing import cast
from uuid import UUID

try:
    from datetime import UTC  # type: ignore
except ImportError:  # pragma: no cover
    UTC = timezone.utc

from fastapi import Cookie, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings as app_settings
from app.db import SessionLocal
from app.db.session_context import allow_cross_org, prime_tenant_context
from app.models.auth import ApiKey, SessionStatus
from app.models.auth import Session as AuthSession
from app.models.person import Person
from app.models.rbac import Permission, PersonRole, Role, RolePermission
from app.observability import actor_id_var
from app.services.auth import hash_api_key
from app.services.auth_flow import decode_access_token, hash_session_token
from app.services.cache import cache_service
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)

# Cookie name for web session
WEB_SESSION_COOKIE = "session_token"

# Session activity timeout in days - sessions inactive longer than this are considered expired
# Default: 7 days. Override with SESSION_ACTIVITY_TIMEOUT_DAYS env var.
SESSION_ACTIVITY_TIMEOUT_DAYS = int(os.getenv("SESSION_ACTIVITY_TIMEOUT_DAYS", "7"))

# Session validation cache TTL in seconds (5 minutes default)
SESSION_CACHE_TTL_SECONDS = int(os.getenv("SESSION_CACHE_TTL_SECONDS", "300"))


def _session_cache_key(session_id: UUID) -> str:
    """Generate cache key for session validation."""
    return f"session:{session_id}:valid"


def _session_revoked_key(session_id: UUID) -> str:
    """Generate cache key for session revocation marker."""
    return f"session:{session_id}:revoked"


# Short TTL for revocation markers to handle race conditions (30 seconds)
SESSION_REVOKED_MARKER_TTL_SECONDS = 30


def _set_actor_context(request: Request | None, actor_id: UUID | str) -> None:
    """Keep request state and observability context in sync for auditing."""
    actor = str(actor_id)
    actor_id_var.set(actor)
    if request is not None:
        request.state.actor_id = actor


def _set_default_org_context(db: Session) -> UUID | None:
    """Set default org RLS context before loading org-scoped records."""
    if not app_settings.default_organization_id:
        return None
    organization_id = UUID(str(app_settings.default_organization_id))
    prime_tenant_context(db, organization_id)
    return organization_id


def _get_person_for_session(db: Session, person_id: UUID) -> Person | None:
    """Load a session person with the default org context in single-org mode."""
    _set_default_org_context(db)
    return db.get(Person, person_id)


def _validate_session_cached(
    session_id: UUID,
    person_id: UUID,
    now: datetime,
    db: Session,
) -> AuthSession | None:
    """
    Validate session with Redis caching.

    Checks Redis cache first. On cache miss, queries DB and caches result.
    Returns None if session is invalid or expired.

    Uses a revocation marker to handle race conditions between cache hit
    and logout/revocation operations.
    """
    cache_key = _session_cache_key(session_id)
    revoked_key = _session_revoked_key(session_id)

    # Check cache first (if Redis is available)
    if cache_service.is_available:
        # First check if session was recently revoked (handles race condition)
        if cache_service.get(revoked_key) is not None:
            logger.debug("Session %s has revocation marker, querying DB", session_id)
            # Fall through to DB query to get authoritative state
        else:
            cached = cache_service.get(cache_key)
            if cached is not None:
                # Cache hit - verify cached person_id matches
                if cached.get("person_id") == str(person_id):
                    # Check cached expiration
                    cached_expires = cached.get("expires_at")
                    if cached_expires:
                        try:
                            expires_dt = datetime.fromisoformat(cached_expires)
                            if expires_dt.tzinfo is None:
                                expires_dt = expires_dt.replace(tzinfo=UTC)
                            if expires_dt > now:
                                logger.debug(
                                    "Session %s validated from cache", session_id
                                )
                                # Query DB for full session object (needed for activity tracking)
                                # but skip if we only need validation
                                return _query_session(session_id, person_id, now, db)
                        except ValueError:
                            pass
                # Cache invalid, fall through to DB check
                cache_service.delete(cache_key)

    # Query database
    session = _query_session(session_id, person_id, now, db)

    if session and cache_service.is_available:
        # Cache the valid session
        expires_at = session.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)

        cache_service.set(
            cache_key,
            {
                "person_id": str(person_id),
                "expires_at": expires_at.isoformat(),
            },
            ttl_seconds=SESSION_CACHE_TTL_SECONDS,
        )
        logger.debug("Session %s cached for %ds", session_id, SESSION_CACHE_TTL_SECONDS)

    return session


def _query_session(
    session_id: UUID,
    person_id: UUID,
    now: datetime,
    db: Session,
) -> AuthSession | None:
    """Query an ERP-owned session from ERP's local database."""
    return db.scalar(
        select(AuthSession)
        .where(AuthSession.id == session_id)
        .where(AuthSession.person_id == person_id)
        .where(AuthSession.status == SessionStatus.active)
        .where(AuthSession.revoked_at.is_(None))
        .where(AuthSession.expires_at > now)
    )


def invalidate_session_cache(session_id: UUID) -> bool:
    """
    Invalidate session cache on logout/revocation.

    Should be called when:
    - User logs out
    - Session is revoked
    - Password is changed

    Sets a short-lived revocation marker to handle race conditions where
    a concurrent request might have already passed the cache check.
    """
    cache_key = _session_cache_key(session_id)
    revoked_key = _session_revoked_key(session_id)

    # Set revocation marker first (short TTL to handle race conditions)
    cache_service.set(
        revoked_key,
        {"revoked": True},
        ttl_seconds=SESSION_REVOKED_MARKER_TTL_SECONDS,
    )

    # Then delete the session cache
    return cache_service.delete(cache_key)


def is_session_inactive(session: AuthSession, now: datetime) -> bool:
    """Check if a session has been inactive for too long.

    A session is considered inactive if last_seen_at is older than
    SESSION_ACTIVITY_TIMEOUT_DAYS. This provides an additional security
    layer on top of absolute token expiration.
    """
    if SESSION_ACTIVITY_TIMEOUT_DAYS <= 0:
        # Activity timeout disabled
        return False

    if session.last_seen_at is None:
        # No activity recorded yet, use created_at
        last_activity = _make_aware(session.created_at)
    else:
        last_activity = _make_aware(session.last_seen_at)

    if last_activity is None:
        return False

    timeout = timedelta(days=SESSION_ACTIVITY_TIMEOUT_DAYS)
    return now - last_activity > timeout


def get_current_user_id(
    authorization: str | None = Header(default=None),
    db: Session = Depends(lambda: SessionLocal()),
) -> UUID:
    """
    Dependency to get the current authenticated user's ID from JWT token.

    For API routes only. Web routes should use require_web_auth instead.
    Raises 401 if not authenticated.

    Sessions are always validated against ERP's local database.
    """
    token = _extract_bearer_token(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized")

    payload = decode_access_token(db, token)
    person_id = payload.get("sub")
    session_id = payload.get("session_id")
    if not person_id or not session_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    now = datetime.now(UTC)
    person_uuid = cast(UUID, coerce_uuid(person_id))
    session_uuid = coerce_uuid(session_id)

    session = _validate_session_cached(session_uuid, person_uuid, now, db)
    if not session:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if is_session_inactive(session, now):
        raise HTTPException(status_code=401, detail="Session expired due to inactivity")

    return person_uuid


def get_current_org_id(
    authorization: str | None = Header(default=None),
    db: Session = Depends(lambda: SessionLocal()),
) -> UUID:
    """
    Dependency to get the current authenticated user's organization ID.

    For API routes only. Web routes should use require_web_auth instead.
    Raises 401 if not authenticated, 400 if user has no organization.

    Sessions are always validated against ERP's local database.
    """
    token = _extract_bearer_token(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized")

    payload = decode_access_token(db, token)
    person_id = payload.get("sub")
    session_id = payload.get("session_id")
    if not person_id or not session_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    now = datetime.now(UTC)
    person_uuid = coerce_uuid(person_id)
    session_uuid = coerce_uuid(session_id)

    session = _validate_session_cached(session_uuid, person_uuid, now, db)
    if not session:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if is_session_inactive(session, now):
        raise HTTPException(status_code=401, detail="Session expired due to inactivity")

    person = db.get(Person, person_uuid)
    if not person or not person.organization_id:
        raise HTTPException(status_code=400, detail="User has no organization")

    return person.organization_id


def _make_aware(dt: datetime) -> datetime:
    """Ensure datetime is timezone-aware (UTC). SQLite doesn't preserve tz info."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


def _is_jwt(token: str) -> bool:
    return token.count(".") == 2


def _has_audit_scope(payload: dict) -> bool:
    scopes: set[str] = set()
    scope_value = payload.get("scope")
    if isinstance(scope_value, str):
        scopes.update(scope_value.split())
    scopes_value = payload.get("scopes")
    if isinstance(scopes_value, list):
        scopes.update(str(item) for item in scopes_value)
    role_value = payload.get("role")
    roles_value = payload.get("roles")
    roles: set[str] = set()
    if isinstance(role_value, str):
        roles.add(role_value)
    if isinstance(roles_value, list):
        roles.update(str(item) for item in roles_value)
    return (
        "audit:read" in scopes
        or "audit:*" in scopes
        or "admin" in roles
        or "auditor" in roles
    )


def require_audit_auth(
    authorization: str | None = Header(default=None),
    x_session_token: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    request: Request = None,
    db: Session = Depends(_get_db),
):
    """Authenticate for audit access using ERP-owned credentials.

    Supports JWT tokens, session tokens, and API keys.
    """
    token = _extract_bearer_token(authorization) or x_session_token
    now = datetime.now(UTC)
    if token:
        if _is_jwt(token):
            payload = decode_access_token(db, token)
            if not _has_audit_scope(payload):
                raise HTTPException(status_code=403, detail="Insufficient scope")
            session_id = payload.get("session_id")
            person_id = payload.get("sub")
            if session_id and person_id:
                session_uuid = coerce_uuid(session_id)
                person_uuid = coerce_uuid(person_id)
                session = db.scalar(
                    select(AuthSession).where(
                        AuthSession.id == session_uuid,
                        AuthSession.person_id == person_uuid,
                    )
                )
                if not session:
                    raise HTTPException(status_code=401, detail="Invalid session")
                if session.status != SessionStatus.active or session.revoked_at:
                    raise HTTPException(status_code=401, detail="Invalid session")
                if _make_aware(session.expires_at) <= now:
                    raise HTTPException(status_code=401, detail="Session expired")

            actor_id = str(person_id)
            _set_actor_context(request, actor_id)
            return {"actor_type": "user", "actor_id": actor_id}

        session = db.scalar(
            select(AuthSession)
            .where(AuthSession.token_hash == hash_session_token(token))
            .where(AuthSession.status == SessionStatus.active)
            .where(AuthSession.revoked_at.is_(None))
            .where(AuthSession.expires_at > now)
        )
        if session:
            _set_actor_context(request, session.person_id)
            return {"actor_type": "user", "actor_id": str(session.person_id)}

    if x_api_key:
        api_key = db.scalar(
            select(ApiKey)
            .where(ApiKey.key_hash == hash_api_key(x_api_key))
            .where(ApiKey.is_active.is_(True))
            .where(ApiKey.revoked_at.is_(None))
            .where((ApiKey.expires_at.is_(None)) | (ApiKey.expires_at > now))
        )
        if api_key:
            _set_actor_context(request, api_key.id)
            return {"actor_type": "api_key", "actor_id": str(api_key.id)}
    raise HTTPException(status_code=401, detail="Unauthorized")


def require_user_auth(
    authorization: str | None = Header(default=None),
    request: Request = None,
    db: Session = Depends(_get_db),
):
    """Authenticate a user from an ERP-issued JWT and local session."""
    # Try Authorization header first, then fall back to cookie
    token = _extract_bearer_token(authorization)
    if not token and request is not None:
        token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized")

    payload = decode_access_token(db, token)
    person_id = payload.get("sub")
    session_id = payload.get("session_id")
    if not person_id or not session_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    now = datetime.now(UTC)
    person_uuid = coerce_uuid(person_id)
    session_uuid = coerce_uuid(session_id)

    with allow_cross_org(db):
        session = db.scalar(
            select(AuthSession)
            .where(AuthSession.id == session_uuid)
            .where(AuthSession.person_id == person_uuid)
            .where(AuthSession.status == SessionStatus.active)
            .where(AuthSession.revoked_at.is_(None))
            .where(AuthSession.expires_at > now)
        )
    if not session:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if is_session_inactive(session, now):
        raise HTTPException(status_code=401, detail="Session expired due to inactivity")
    session.last_seen_at = now
    db.flush()

    roles_value = payload.get("roles")
    scopes_value = payload.get("scopes")
    roles = [str(role) for role in roles_value] if isinstance(roles_value, list) else []
    scopes = (
        [str(scope) for scope in scopes_value] if isinstance(scopes_value, list) else []
    )
    actor_id = str(person_id)
    _set_actor_context(request, actor_id)
    return {
        "person_id": str(person_id),
        "session_id": str(session_id),
        "roles": roles,
        "scopes": scopes,
    }


def require_role(role_name: str):
    def _require_role(
        auth=Depends(require_user_auth),
        db: Session = Depends(_get_db),
    ):
        person_id = coerce_uuid(auth["person_id"])
        roles = set(auth.get("roles") or [])
        if role_name in roles:
            return auth
        role = db.scalar(
            select(Role).where(Role.name == role_name).where(Role.is_active.is_(True))
        )
        if not role:
            raise HTTPException(status_code=403, detail="Role not found")
        link = db.scalar(
            select(PersonRole)
            .where(PersonRole.person_id == person_id)
            .where(PersonRole.role_id == role.id)
        )
        if not link:
            raise HTTPException(status_code=403, detail="Forbidden")
        return auth

    return _require_role


def _enforce_staff_leave_write_restriction(
    db: Session,
    auth: dict,
    permission_key: str,
) -> None:
    """Deny mutating permissions while the authenticated employee is on leave."""
    organization_id = auth.get("organization_id")
    person_id = auth.get("person_id")
    if not organization_id or not person_id:
        return

    from app.services.people.hr.staff_access_projection import (
        StaffAccessProjectionService,
        is_mutating_permission_key,
    )

    if not is_mutating_permission_key(permission_key):
        return
    restriction = StaffAccessProjectionService(db).active_restriction_for_person(
        coerce_uuid(organization_id),
        coerce_uuid(person_id),
    )
    if restriction is not None:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "staff_on_leave_read_only",
                "message": "Write access is temporarily paused during approved leave.",
                "restriction_id": str(restriction.restriction_id),
            },
        )


def require_permission(permission_key: str):
    def _require_permission(
        auth=Depends(require_user_auth),
        db: Session = Depends(_get_db),
    ):
        person_id = coerce_uuid(auth["person_id"])
        roles = set(auth.get("roles") or [])
        scopes = set(auth.get("scopes") or [])
        if "admin" in roles or permission_key in scopes:
            _enforce_staff_leave_write_restriction(db, auth, permission_key)
            return auth
        permission = db.scalar(
            select(Permission)
            .where(Permission.key == permission_key)
            .where(Permission.is_active.is_(True))
        )
        if not permission:
            raise HTTPException(status_code=403, detail="Permission not found")
        has_permission = db.scalar(
            select(RolePermission)
            .join(Role, RolePermission.role_id == Role.id)
            .join(PersonRole, PersonRole.role_id == Role.id)
            .where(PersonRole.person_id == person_id)
            .where(RolePermission.permission_id == permission.id)
            .where(Role.is_active.is_(True))
            .limit(1)
        )
        if not has_permission:
            raise HTTPException(status_code=403, detail="Forbidden")
        _enforce_staff_leave_write_restriction(db, auth, permission_key)
        return auth

    return _require_permission


def require_tenant_auth(
    authorization: str | None = Header(default=None),
    access_token: str | None = Cookie(default=None),
    request: Request = None,
    db: Session = Depends(_get_db),
):
    """
    Authenticate an ERP session and set RLS tenant context.

    This dependency:
    1. Validates the ERP-issued JWT token
    2. Validates the ERP-owned local session
    3. Looks up the user's organization_id
    4. Sets the PostgreSQL session variable for RLS
    5. Returns auth dict with organization_id included

    Usage:
        @app.get("/items")
        def list_items(auth=Depends(require_tenant_auth), db: Session = Depends(get_db)):
            # All queries in this request are automatically scoped to the user's org
            return db.scalars(select(Item)).all()
    """
    token = _extract_bearer_token(authorization) or access_token
    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized")

    payload = decode_access_token(db, token)
    person_id = payload.get("sub")
    session_id = payload.get("session_id")
    if not person_id or not session_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    now = datetime.now(UTC)
    person_uuid = coerce_uuid(person_id)
    session_uuid = coerce_uuid(session_id)

    session = db.scalar(
        select(AuthSession)
        .where(AuthSession.id == session_uuid)
        .where(AuthSession.person_id == person_uuid)
        .where(AuthSession.status == SessionStatus.active)
        .where(AuthSession.revoked_at.is_(None))
        .where(AuthSession.expires_at > now)
    )
    if not session:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if is_session_inactive(session, now):
        raise HTTPException(status_code=401, detail="Session expired due to inactivity")
    session.last_seen_at = now
    db.flush()

    # Look up the user's organization (or use default if single-org mode)
    with allow_cross_org(db):
        person = db.get(Person, person_uuid)
    organization_id = person.organization_id if person else None

    # Single-org mode: use default org if configured
    if not organization_id and app_settings.default_organization_id:
        organization_id = coerce_uuid(app_settings.default_organization_id)

    if not organization_id:
        raise HTTPException(status_code=403, detail="Organization access required")

    # Set RLS context if user has an organization
    if organization_id:
        prime_tenant_context(db, organization_id)
        if request is not None:
            request.state.organization_id = str(organization_id)

    roles_value = payload.get("roles")
    scopes_value = payload.get("scopes")
    roles = [str(role) for role in roles_value] if isinstance(roles_value, list) else []
    scopes = (
        [str(scope) for scope in scopes_value] if isinstance(scopes_value, list) else []
    )
    actor_id = str(person_id)
    _set_actor_context(request, actor_id)
    return {
        "person_id": str(person_id),
        "session_id": str(session_id),
        "organization_id": str(organization_id) if organization_id else None,
        "roles": roles,
        "scopes": scopes,
    }


def require_tenant_role(role_name: str):
    """
    Require a specific role with tenant context set.

    Combines require_tenant_auth with role checking.
    """

    def _require_tenant_role(
        auth=Depends(require_tenant_auth),
        db: Session = Depends(_get_db),
    ):
        person_id = coerce_uuid(auth["person_id"])
        roles = set(auth.get("roles") or [])
        if role_name in roles:
            return auth
        role = db.scalar(
            select(Role).where(Role.name == role_name).where(Role.is_active.is_(True))
        )
        if not role:
            raise HTTPException(status_code=403, detail="Role not found")
        link = db.scalar(
            select(PersonRole)
            .where(PersonRole.person_id == person_id)
            .where(PersonRole.role_id == role.id)
        )
        if not link:
            raise HTTPException(status_code=403, detail="Forbidden")
        return auth

    return _require_tenant_role


def require_tenant_permission(permission_key: str):
    """
    Require a specific permission with tenant context set.

    Combines require_tenant_auth with permission checking.
    """

    def _require_tenant_permission(
        auth=Depends(require_tenant_auth),
        db: Session = Depends(_get_db),
    ):
        person_id = coerce_uuid(auth["person_id"])
        roles = set(auth.get("roles") or [])
        scopes = set(auth.get("scopes") or [])
        if "admin" in roles or permission_key in scopes:
            _enforce_staff_leave_write_restriction(db, auth, permission_key)
            return auth
        permission = db.scalar(
            select(Permission)
            .where(Permission.key == permission_key)
            .where(Permission.is_active.is_(True))
        )
        if not permission:
            raise HTTPException(status_code=403, detail="Permission not found")
        has_permission = db.scalar(
            select(RolePermission)
            .join(Role, RolePermission.role_id == Role.id)
            .join(PersonRole, PersonRole.role_id == Role.id)
            .where(PersonRole.person_id == person_id)
            .where(RolePermission.permission_id == permission.id)
            .where(Role.is_active.is_(True))
            .limit(1)
        )
        if not has_permission:
            raise HTTPException(status_code=403, detail="Forbidden")
        _enforce_staff_leave_write_restriction(db, auth, permission_key)
        return auth

    return _require_tenant_permission


def has_live_admin_grant(db: Session, person_id: UUID | str | None) -> bool:
    """Does this person hold the ``admin`` role RIGHT NOW?

    THE admin-authority question, asked of the grant tables rather than of a
    token. A JWT's ``roles`` claim and a web session's ``roles`` list are both
    login-time SNAPSHOTS: revoking someone's admin role leaves every already
    issued token asserting it until that token expires, which for a
    platform-level control is a window in which a removed administrator still
    rewrites the deployment's ceiling.

    Extracted from :func:`require_admin_bypass`, which already asked exactly
    this and states the standard in its own comment. It is a FUNCTION and not a
    third dependency because its other two callers are not FastAPI dependencies
    at all — one is a predicate over a history row
    (``app.api.settings._can_restore_history_entry``), the other a method on the
    admin web service (``AdminWebService._require_admin_web_auth``) — and three
    hand-written copies of one security question is how two of them drift.

    Deliberately NOT cached: the whole point is that it is re-asked per request.
    It is one indexed join on the primary key of ``person_roles``.

    Returns ``False`` for a missing or unparseable ``person_id`` — an actor
    whose identity cannot be resolved does not hold a grant.
    """
    if person_id is None:
        return False
    try:
        person_uuid = coerce_uuid(person_id, raise_http=False)
    except (TypeError, ValueError):
        return False
    if person_uuid is None:
        return False
    return (
        db.scalar(
            select(PersonRole)
            .join(Role, PersonRole.role_id == Role.id)
            .where(PersonRole.person_id == person_uuid)
            .where(Role.name == "admin")
            .where(Role.is_active.is_(True))
            .limit(1)
        )
        is not None
    )


def require_admin_bypass(
    authorization: str | None = Header(default=None),
    request: Request = None,
    db: Session = Depends(_get_db),
):
    """
    Authorize an admin-only cross-organization route.

    This dependency proves live admin authority; it does not modify its
    database session and cannot bypass PostgreSQL RLS. Pair it with the route's
    existing ``get_db_admin_bypass`` dependency, whose historical name refers
    only to the ORM listener's ``allow_cross_org`` marker.

    Use for system administration endpoints that need application-layer access
    across organizations. Requires the live ``admin`` role assignment.

    Usage:
        @app.get("/admin/all-organizations")
        def list_all_orgs(auth=Depends(require_admin_bypass), db: Session = Depends(get_db)):
            # The route's DB dependency owns any approved cross-org context.
            return db.scalars(select(Organization)).all()
    """
    token = _extract_bearer_token(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized")
    payload = decode_access_token(db, token)
    person_id = payload.get("sub")
    session_id = payload.get("session_id")
    if not person_id or not session_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    now = datetime.now(UTC)
    person_uuid = coerce_uuid(person_id)
    session_uuid = coerce_uuid(session_id)

    session = db.scalar(
        select(AuthSession)
        .where(AuthSession.id == session_uuid)
        .where(AuthSession.person_id == person_uuid)
        .where(AuthSession.status == SessionStatus.active)
        .where(AuthSession.revoked_at.is_(None))
        .where(AuthSession.expires_at > now)
    )
    if not session:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if is_session_inactive(session, now):
        raise HTTPException(status_code=401, detail="Session expired due to inactivity")
    session.last_seen_at = now
    db.flush()

    # JWT role claims are a login-time snapshot. Cross-tenant authority must be
    # checked against the live assignment on every request so removing the
    # admin role takes effect immediately, even while the session and access
    # token remain otherwise valid. `has_live_admin_grant` above is that check,
    # and is shared with the two other doors onto platform-row writes.
    roles_value = payload.get("roles")
    roles = [str(role) for role in roles_value] if isinstance(roles_value, list) else []
    if not has_live_admin_grant(db, person_uuid):
        raise HTTPException(status_code=403, detail="Admin access required")

    scopes_value = payload.get("scopes")
    scopes = (
        [str(scope) for scope in scopes_value] if isinstance(scopes_value, list) else []
    )
    actor_id = str(person_id)
    _set_actor_context(request, actor_id)
    if request is not None:
        request.state.is_admin_bypass = True
    return {
        "person_id": str(person_id),
        "session_id": str(session_id),
        "organization_id": None,  # Not scoped to an org
        "roles": roles,
        "scopes": scopes,
        "is_admin_bypass": True,
    }


def _resolve_web_session_from_access_token(
    db: Session,
    access_token: str,
    now: datetime,
) -> tuple[AuthSession, Person] | None:
    """Resolve an ERP-owned web session from an ERP-issued access token."""
    try:
        payload = decode_access_token(db, access_token)
    except HTTPException:
        return None
    person_id = payload.get("sub")
    session_id = payload.get("session_id")
    if not person_id or not session_id:
        return None

    person_uuid = coerce_uuid(person_id)
    session_uuid = coerce_uuid(session_id)

    session = db.scalar(
        select(AuthSession)
        .where(AuthSession.id == session_uuid)
        .where(AuthSession.person_id == person_uuid)
        .where(AuthSession.status == SessionStatus.active)
        .where(AuthSession.revoked_at.is_(None))
        .where(AuthSession.expires_at > now)
    )
    if not session or is_session_inactive(session, now):
        return None

    person = _get_person_for_session(db, person_uuid)
    if not person:
        return None

    return session, person


def require_web_session(
    request: Request,
    session_token: str | None = Cookie(default=None, alias=WEB_SESSION_COOKIE),
    access_token: str | None = Cookie(default=None),
    db: Session = Depends(_get_db),
):
    """
    Web session authentication for HTML routes.

    This dependency:
    1. Reads the session token from a cookie
    2. Validates the session against ERP's database
    3. Looks up the user's organization_id
    4. Sets the PostgreSQL session variable for RLS
    5. Returns auth dict with user and organization info

    If authentication fails, redirects to login page instead of returning 401.

    Usage:
        @app.get("/dashboard", response_class=HTMLResponse)
        def dashboard(request: Request, auth=Depends(require_web_session)):
            # User is authenticated, org context is set
            return templates.TemplateResponse(request, "dashboard.html", {"user": auth})
    """
    if not session_token and not access_token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    now = datetime.now(UTC)
    session = None
    person = None

    if session_token:
        session = db.scalar(
            select(AuthSession)
            .where(AuthSession.token_hash == hash_session_token(session_token))
            .where(AuthSession.status == SessionStatus.active)
            .where(AuthSession.revoked_at.is_(None))
            .where(AuthSession.expires_at > now)
        )
        if not session or is_session_inactive(session, now):
            session = None
        else:
            person = _get_person_for_session(db, session.person_id)

    if not session and access_token:
        resolved = _resolve_web_session_from_access_token(db, access_token, now)
        if resolved:
            session, person = resolved

    if not session or not person:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    # Get organization from person or use default (single-org mode)
    from app.config import settings

    organization_id = person.organization_id
    if not organization_id and settings.default_organization_id:
        organization_id = coerce_uuid(settings.default_organization_id)

    # Set RLS context if user has an organization
    if organization_id:
        prime_tenant_context(db, organization_id)
        request.state.organization_id = str(organization_id)

    _set_actor_context(request, person.id)

    def _clean_name(value: str | None) -> str:
        cleaned = (value or "").strip()
        return "" if cleaned.lower() in {"none", "null"} else cleaned

    display_name = _clean_name(person.display_name)
    first_name = _clean_name(person.first_name)
    last_name = _clean_name(person.last_name)
    base_name = f"{first_name} {last_name}".strip()
    user_name = display_name or base_name or _clean_name(person.email) or "User"

    return {
        "person_id": str(person.id),
        "session_id": str(session.id),
        "organization_id": str(organization_id) if organization_id else None,
        "user_name": user_name,
        "user_initials": _get_initials(person),
    }


def _get_initials(person: Person) -> str:
    """Get user initials from person record."""

    def _clean_name(value: str | None) -> str:
        cleaned = (value or "").strip()
        return "" if cleaned.lower() in {"none", "null"} else cleaned

    first_name = _clean_name(person.first_name)
    last_name = _clean_name(person.last_name)
    display_name = _clean_name(person.display_name)

    if first_name and last_name:
        return f"{first_name[0]}{last_name[0]}".upper()
    if display_name:
        parts = display_name.split()
        if len(parts) >= 2:
            return f"{parts[0][0]}{parts[-1][0]}".upper()
        return display_name[:2].upper()
    return "??"


def optional_web_session(
    request: Request,
    session_token: str | None = Cookie(default=None, alias=WEB_SESSION_COOKIE),
    access_token: str | None = Cookie(default=None),
    db: Session = Depends(_get_db),
):
    """
    Optional ERP-local web session authentication.

    Like require_web_session but returns None instead of raising an exception
    when not authenticated. Useful for pages that work with or without auth.

    Usage:
        @app.get("/public-page", response_class=HTMLResponse)
        def public_page(request: Request, auth=Depends(optional_web_session)):
            if auth:
                # User is logged in
                ...
            else:
                # Anonymous user
                ...
    """
    if not session_token and not access_token:
        return None

    now = datetime.now(UTC)
    session = None
    person = None

    if session_token:
        session = db.scalar(
            select(AuthSession)
            .where(AuthSession.token_hash == hash_session_token(session_token))
            .where(AuthSession.status == SessionStatus.active)
            .where(AuthSession.revoked_at.is_(None))
            .where(AuthSession.expires_at > now)
        )
        if not session or is_session_inactive(session, now):
            session = None
        else:
            person = _get_person_for_session(db, session.person_id)

    if not session and access_token:
        resolved = _resolve_web_session_from_access_token(db, access_token, now)
        if resolved:
            session, person = resolved

    if not session or not person:
        return None

    # Get organization from person or use default (single-org mode)
    from app.config import settings

    organization_id = person.organization_id
    if not organization_id and settings.default_organization_id:
        organization_id = coerce_uuid(settings.default_organization_id)

    if organization_id:
        prime_tenant_context(db, organization_id)
        request.state.organization_id = str(organization_id)

    _set_actor_context(request, person.id)

    def _clean_name(value: str | None) -> str:
        cleaned = (value or "").strip()
        return "" if cleaned.lower() in {"none", "null"} else cleaned

    display_name = _clean_name(person.display_name)
    first_name = _clean_name(person.first_name)
    last_name = _clean_name(person.last_name)
    base_name = f"{first_name} {last_name}".strip()
    user_name = display_name or base_name or _clean_name(person.email) or "User"

    return {
        "person_id": str(person.id),
        "session_id": str(session.id),
        "organization_id": str(organization_id) if organization_id else None,
        "user_name": user_name,
        "user_initials": _get_initials(person),
    }
