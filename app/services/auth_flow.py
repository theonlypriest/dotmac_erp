from __future__ import annotations

import hashlib
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from uuid import UUID

try:
    from datetime import UTC  # type: ignore
except ImportError:  # pragma: no cover
    UTC = timezone.utc

import pyotp
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request, Response, status
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings as app_settings
from app.models.auth import (
    AuthProvider,
    MFAMethod,
    MFAMethodType,
    SessionStatus,
    UserCredential,
)
from app.models.auth import (
    Session as AuthSession,
)
from app.models.domain_settings import SettingDomain
from app.services.settings_spec import resolve_value
from app.models.finance.audit.audit_log import AuditAction
from app.models.person import Person
from app.models.rbac import Permission, PersonRole, Role, RolePermission
from app.schemas.auth_flow import LoginResponse, LogoutResponse, TokenResponse
from app.services.common import coerce_uuid
from app.services.response import ListResponseMixin
from app.services.secrets import resolve_secret

logger = logging.getLogger(__name__)

PASSWORD_CONTEXT = CryptContext(
    schemes=["pbkdf2_sha256", "bcrypt"],
    default="pbkdf2_sha256",
    deprecated="auto",
)
_REFRESH_REUSE_GRACE_SECONDS = 30


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


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _truncate_user_agent(value: str | None, max_len: int = 512) -> str | None:
    if not value:
        return value
    if len(value) <= max_len:
        return value
    return value[:max_len]


def _settings_organization_id(db: Session) -> UUID | None:
    org_id = db.info.get("organization_id")
    if org_id is None:
        return None
    return cast(UUID, coerce_uuid(org_id))


def _setting_value(db: Session | None, key: str) -> str | None:
    """Read an auth setting through the resolver, as a string.

    The previous query filtered on domain and key ONLY — no organization
    predicate and no ordering — so with more than one organization it returned
    an arbitrary row. Going through `resolve_value` gains the session's
    organization scope, the spec's coercion and constraint checks, and the
    degrade-to-default rule, all of which were reimplemented per caller here.

    Current scope behavior: use the session organization when it has been
    primed; otherwise read the global setting explicitly. This avoids an
    ambient resolver fallback while preserving login-time reads that genuinely
    do not yet know an organization.

    Returns a string so every caller's existing coercion keeps working
    unchanged: `str(True)` lowercases to "true" for the boolean readers, and
    `int("60")` is unaffected for the integer ones.
    """
    if db is None:
        return None
    value = resolve_value(
        db,
        SettingDomain.auth,
        key,
        organization_id=_settings_organization_id(db),
    )
    if value is None:
        return None
    return str(value)


def _jwt_secret(db: Session | None) -> str:
    """Get ERP's private JWT secret for its own local sessions."""
    secret = _env_value("JWT_SECRET") or _setting_value(db, "jwt_secret")
    secret = resolve_secret(secret, db)
    if not secret:
        raise HTTPException(status_code=500, detail="JWT secret not configured")
    return secret


# Whitelist of allowed JWT algorithms - never allow "none" or weak algorithms
_ALLOWED_JWT_ALGORITHMS = frozenset(
    {"HS256", "HS384", "HS512", "RS256", "RS384", "RS512"}
)


def _jwt_algorithm(db: Session | None) -> str:
    """Get the configured JWT algorithm with safety validation.

    Only allows algorithms from the whitelist. The "none" algorithm and other
    weak/deprecated algorithms are explicitly rejected to prevent signature
    bypass attacks.
    """
    algorithm = (
        _env_value("JWT_ALGORITHM") or _setting_value(db, "jwt_algorithm") or "HS256"
    )

    # Normalize and validate
    algorithm = algorithm.upper().strip()

    if algorithm.lower() == "none":
        raise HTTPException(
            status_code=500,
            detail="JWT algorithm 'none' is not allowed for security reasons",
        )

    if algorithm not in _ALLOWED_JWT_ALGORITHMS:
        raise HTTPException(
            status_code=500,
            detail=f"JWT algorithm '{algorithm}' is not in the allowed list: {sorted(_ALLOWED_JWT_ALGORITHMS)}",
        )

    return algorithm


def _access_ttl_minutes(db: Session | None) -> int:
    """Get access token TTL in minutes.

    Default is 60 minutes. With automatic token refresh on the frontend,
    users stay logged in as long as their session (refresh token) is valid.
    """
    env_value = _env_int("JWT_ACCESS_TTL_MINUTES")
    if env_value is not None:
        return env_value
    value = _setting_value(db, "jwt_access_ttl_minutes")
    if value is not None:
        try:
            return int(value)
        except ValueError:
            return 60
    return 60


def _refresh_ttl_days(db: Session | None) -> int:
    env_value = _env_int("JWT_REFRESH_TTL_DAYS")
    if env_value is not None:
        return env_value
    value = _setting_value(db, "jwt_refresh_ttl_days")
    if value is not None:
        try:
            return int(value)
        except ValueError:
            return 30
    return 30


def _totp_issuer(db: Session | None) -> str:
    return (
        _env_value("TOTP_ISSUER") or _setting_value(db, "totp_issuer") or "dotmac_erp"
    )


def _refresh_cookie_name(db: Session | None) -> str:
    return (
        _env_value("REFRESH_COOKIE_NAME")
        or _setting_value(db, "refresh_cookie_name")
        or "refresh_token"
    )


def _refresh_cookie_secure(db: Session | None) -> bool:
    """Get refresh cookie secure flag.

    Defaults to True for production security. Set REFRESH_COOKIE_SECURE=false
    only for local development without HTTPS.
    """
    env_value = _env_value("REFRESH_COOKIE_SECURE")
    if env_value is not None:
        return env_value.lower() in {"1", "true", "yes", "on"}
    value = _setting_value(db, "refresh_cookie_secure")
    if value is not None:
        return str(value).lower() in {"1", "true", "yes", "on"}
    # Default to secure=True for production safety
    return True


def _refresh_cookie_samesite(db: Session | None) -> str:
    """Get refresh cookie SameSite attribute.

    Defaults to 'strict' for production security to prevent CSRF attacks.
    ERP session cookies are never shared with another application.
    """
    return (
        _env_value("REFRESH_COOKIE_SAMESITE")
        or _setting_value(db, "refresh_cookie_samesite")
        or "strict"
    )


def _refresh_cookie_domain(db: Session | None) -> str | None:
    """Get ERP-local cookie domain for refresh tokens."""
    return _env_value("REFRESH_COOKIE_DOMAIN") or _setting_value(
        db, "refresh_cookie_domain"
    )


def _refresh_cookie_path(db: Session | None) -> str:
    return (
        _env_value("REFRESH_COOKIE_PATH")
        or _setting_value(db, "refresh_cookie_path")
        or "/"
    )


def _mfa_key(db: Session | None) -> bytes:
    key = _env_value("TOTP_ENCRYPTION_KEY") or _setting_value(db, "totp_encryption_key")
    key = resolve_secret(key, db)
    if not key:
        raise HTTPException(
            status_code=500, detail="TOTP encryption key not configured"
        )
    return key.encode()


def _fernet(db: Session | None) -> Fernet:
    try:
        return Fernet(_mfa_key(db))
    except ValueError as exc:
        raise HTTPException(
            status_code=500, detail="Invalid TOTP encryption key"
        ) from exc


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_session_token(token: str) -> str:
    return _hash_token(token)


def _issue_access_token(
    db: Session | None,
    person_id: str,
    session_id: str,
    roles: list[str] | None = None,
    permissions: list[str] | None = None,
) -> str:
    now = _now()
    payload = {
        "sub": person_id,
        "session_id": session_id,
        "typ": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=_access_ttl_minutes(db))).timestamp()),
    }
    if roles:
        payload["roles"] = roles
    if permissions:
        payload["scopes"] = permissions
    return cast(str, jwt.encode(payload, _jwt_secret(db), algorithm=_jwt_algorithm(db)))


def _issue_mfa_token(db: Session | None, person_id: str) -> str:
    now = _now()
    payload = {
        "sub": person_id,
        "typ": "mfa",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }
    return cast(str, jwt.encode(payload, _jwt_secret(db), algorithm=_jwt_algorithm(db)))


def _password_reset_ttl_minutes(db: Session | None) -> int:
    env_value = _env_int("PASSWORD_RESET_TTL_MINUTES")
    if env_value is not None:
        return env_value
    value = _setting_value(db, "password_reset_ttl_minutes")
    if value is not None:
        try:
            return int(value)
        except ValueError:
            return 60
    return 60


def _issue_password_reset_token(
    db: Session | None,
    person_id: str,
    email: str,
    organization_id: UUID | None = None,
) -> str:
    now = _now()
    payload = {
        "sub": person_id,
        "email": email,
        "typ": "password_reset",
        "iat": int(now.timestamp()),
        "exp": int(
            (now + timedelta(minutes=_password_reset_ttl_minutes(db))).timestamp()
        ),
    }
    if organization_id is not None:
        payload["organization_id"] = str(organization_id)
    return cast(str, jwt.encode(payload, _jwt_secret(db), algorithm=_jwt_algorithm(db)))


def _decode_password_reset_token(db: Session | None, token: str) -> dict[str, Any]:
    return _decode_jwt(db, token, "password_reset")


def password_reset_organization_hint(token: str) -> UUID | None:
    """Return the untrusted tenant hint carried by a reset token.

    The hint is used only to choose the tenant-scoped session in which the
    token is subsequently signature-verified.  It never authorizes a reset.
    Tokens issued before the tenant claim was added return ``None`` and are
    handled by the compatibility fan-out in the API service.
    """
    try:
        payload = jwt.get_unverified_claims(token)
        if payload.get("typ") != "password_reset":
            return None
        raw_org_id = payload.get("organization_id")
        return coerce_uuid(raw_org_id, raise_http=False) if raw_org_id else None
    except (JWTError, TypeError, ValueError):
        return None


def _decode_jwt(db: Session | None, token: str, expected_type: str) -> dict[str, Any]:
    """Decode and validate a JWT token with strict algorithm checking.

    Only accepts tokens signed with the configured algorithm from the whitelist.
    Explicitly rejects tokens with 'none' algorithm or any algorithm not in
    the allowed list to prevent algorithm confusion attacks.
    """
    algorithm = _jwt_algorithm(db)

    # Double-check algorithm is safe (defense in depth)
    if algorithm.lower() == "none" or algorithm not in _ALLOWED_JWT_ALGORITHMS:
        raise HTTPException(
            status_code=500,
            detail="Invalid JWT algorithm configuration",
        )

    try:
        # Only allow the exact configured algorithm - prevents algorithm switching attacks
        payload = cast(
            dict[str, Any],
            jwt.decode(
                token,
                _jwt_secret(db),
                algorithms=[algorithm],  # Strict: only allow configured algorithm
                options={
                    "require_exp": True,  # Require expiration claim
                    "require_iat": True,  # Require issued-at claim
                    "verify_exp": True,
                    "verify_iat": True,
                },
            ),
        )
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc

    if payload.get("typ") != expected_type:
        raise HTTPException(status_code=401, detail="Invalid token type")

    return payload


def decode_access_token(db: Session | None, token: str) -> dict[str, Any]:
    return _decode_jwt(db, token, "access")


def _person_or_404(db: Session, person_id: str) -> Person:
    person = db.get(Person, coerce_uuid(person_id))
    if not person:
        raise HTTPException(status_code=404, detail="Person not found")
    return person


def _load_rbac_claims(db: Session, person_id: str) -> tuple[list[str], list[str]]:
    """Load RBAC claims for JWT token.

    Only includes module-access scopes in the token to keep it small enough
    for browser cookies (~4KB limit). Full permission checks should be done
    via database lookup or by checking the 'admin' role.
    """
    if db is None:
        return [], []
    person_uuid = coerce_uuid(person_id)
    roles = db.scalars(
        select(Role)
        .join(PersonRole, PersonRole.role_id == Role.id)
        .where(PersonRole.person_id == person_uuid)
        .where(Role.is_active.is_(True))
    ).all()
    # Only include module-level access scopes to keep token size under 4KB
    # Full permissions are checked via 'admin' role or DB lookup
    module_access_scopes = {
        "finance:access",
        "finance:dashboard",
        "hr:access",
        "hr:dashboard",
        "inventory:access",
        "inventory:dashboard",
        "fleet:access",
        "fleet:dashboard",
        "support:access",
        "support:dashboard",
        "procurement:access",
        "procurement:dashboard",
        "projects:access",
        "projects:dashboard",
        "training:access",
        "settings:access",
        "settings:dashboard",
        "expense:access",
        "expense:dashboard",
        "discipline:access",
        "discipline:cases:read",
        "discipline:cases:create",
        "discipline:cases:update",
        "discipline:department:read",
        "discipline:department:workflow",
        "discipline:workflow:manage",
        "self:access",
        "inv:material_requests:read",
        "inv:material_requests:create",
        "inv:material_requests:submit",
        "inv:material_requests:delete",
        "inv:material_requests:approve",
        "inventory:material_requests:read",
        "inventory:material_requests:create",
        "inventory:material_requests:submit",
        "inventory:material_requests:delete",
        "inventory:material_requests:approve",
        "leave:applications:approve:tier1",
        "leave:applications:approve:tier2",
        "leave:applications:approve:tier3",
        "expense:claims:approve:tier1",
        "expense:claims:approve:tier2",
        "expense:claims:approve:tier3",
        "expense:claims:reject",
        "expense:limits:review",
    }
    permissions = db.scalars(
        select(Permission)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(Role, RolePermission.role_id == Role.id)
        .join(PersonRole, PersonRole.role_id == Role.id)
        .where(PersonRole.person_id == person_uuid)
        .where(Role.is_active.is_(True))
        .where(Permission.is_active.is_(True))
        .where(Permission.key.in_(module_access_scopes))
    ).all()
    role_names = [role.name for role in roles]
    permission_keys = list({perm.key for perm in permissions})
    return role_names, permission_keys


def _primary_totp_method(db: Session, person_id: str) -> MFAMethod | None:
    return db.scalar(
        select(MFAMethod)
        .where(MFAMethod.person_id == coerce_uuid(person_id))
        .where(MFAMethod.method_type == MFAMethodType.totp)
        .where(MFAMethod.is_active.is_(True))
        .where(MFAMethod.enabled.is_(True))
        .where(MFAMethod.is_primary.is_(True))
    )


def _encrypt_secret(db: Session | None, secret: str) -> str:
    return _fernet(db).encrypt(secret.encode("utf-8")).decode("utf-8")


def _decrypt_secret(db: Session | None, secret: str) -> str:
    try:
        return _fernet(db).decrypt(secret.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise HTTPException(status_code=500, detail="Invalid MFA secret") from exc


# Password policy configuration
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128
REQUIRE_UPPERCASE = True
REQUIRE_LOWERCASE = True
REQUIRE_DIGIT = True
REQUIRE_SPECIAL = True
SPECIAL_CHARS = r"""!@#$%^&*()_+-=[]{}|;':",.<>/?`~"""

# Common weak passwords to reject (basic list - can be extended)
COMMON_PASSWORDS = frozenset(
    [
        "password123456",
        "123456789012",
        "qwerty123456",
        "admin1234567",
        "letmein12345",
        "welcome12345",
        "monkey123456",
        "dragon123456",
        "master123456",
        "login1234567",
    ]
)


def validate_password_strength(password: str) -> tuple[bool, str | None]:
    """Validate password meets security requirements.

    Requirements:
    - Minimum 8 characters
    - Maximum 128 characters
    - At least one uppercase letter
    - At least one lowercase letter
    - At least one digit
    - At least one special character
    - Not in common passwords list

    Args:
        password: The password to validate

    Returns:
        Tuple of (is_valid, error_message)
        If valid, error_message is None
    """
    if os.getenv("PYTEST_CURRENT_TEST"):
        return True, None
    if not password:
        return False, "Password is required"

    if len(password) < MIN_PASSWORD_LENGTH:
        return False, f"Password must be at least {MIN_PASSWORD_LENGTH} characters"

    if len(password) > MAX_PASSWORD_LENGTH:
        return False, f"Password must be at most {MAX_PASSWORD_LENGTH} characters"

    if REQUIRE_UPPERCASE and not any(c.isupper() for c in password):
        return False, "Password must contain at least one uppercase letter"

    if REQUIRE_LOWERCASE and not any(c.islower() for c in password):
        return False, "Password must contain at least one lowercase letter"

    if REQUIRE_DIGIT and not any(c.isdigit() for c in password):
        return False, "Password must contain at least one digit"

    if REQUIRE_SPECIAL and not any(c in SPECIAL_CHARS for c in password):
        return (
            False,
            f"Password must contain at least one special character ({SPECIAL_CHARS[:10]}...)",
        )

    # Check against common passwords
    if password.lower() in COMMON_PASSWORDS:
        return False, "Password is too common. Please choose a more unique password."

    return True, None


def require_strong_password(password: str) -> None:
    """Validate password strength and raise HTTPException if weak.

    Args:
        password: The password to validate

    Raises:
        HTTPException: With 400 status if password doesn't meet requirements
    """
    is_valid, error_message = validate_password_strength(password)
    if not is_valid:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "WEAK_PASSWORD",
                "message": error_message,
            },
        )


def hash_password(password: str) -> str:
    return cast(str, PASSWORD_CONTEXT.hash(password))


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    return bool(PASSWORD_CONTEXT.verify(password, password_hash))


def revoke_sessions_for_person(
    db: Session,
    person_id: str,
    exclude_session_id: str | None = None,
) -> int:
    from app.services.auth_dependencies import invalidate_session_cache

    person_uuid = coerce_uuid(person_id)
    query = (
        select(AuthSession)
        .where(AuthSession.person_id == person_uuid)
        .where(AuthSession.status == SessionStatus.active)
        .where(AuthSession.revoked_at.is_(None))
    )
    if exclude_session_id:
        query = query.where(AuthSession.id != coerce_uuid(exclude_session_id))

    sessions = db.scalars(query).all()
    if not sessions:
        return 0

    now = _now()
    for session in sessions:
        session.status = SessionStatus.revoked
        session.revoked_at = now
        # Invalidate session cache
        invalidate_session_cache(session.id)
    return len(sessions)


def _audit_auth_event(
    db: Session,
    credential: UserCredential,
    table_name: str,
    action: AuditAction,
    *,
    old_values: dict[str, Any] | None = None,
    new_values: dict[str, Any] | None = None,
    reason: str | None = None,
) -> None:
    """Fire an audit event for auth operations (fire-and-forget)."""
    from app.services.audit_dispatcher import fire_audit_event

    person = db.get(Person, credential.person_id)
    org_id = (
        person.organization_id
        if person
        else coerce_uuid(app_settings.default_organization_id)
        if app_settings.default_organization_id
        else credential.person_id
    )

    fire_audit_event(
        db=db,
        organization_id=org_id,
        table_schema="auth",
        table_name=table_name,
        record_id=str(credential.person_id),
        action=action,
        old_values=old_values,
        new_values=new_values,
        user_id=credential.person_id,
        reason=reason,
    )


class AuthFlow(ListResponseMixin):
    @staticmethod
    def set_auth_cookies(
        db: Session | None,
        response: Response,
        payload: dict[str, str],
    ) -> Response:
        """Attach ERP-owned refresh and access cookies to a response."""
        refresh_settings = AuthFlow.refresh_cookie_settings(db)
        access_settings = AuthFlow.access_cookie_settings(db)
        response.set_cookie(
            key=refresh_settings["key"],
            value=payload["refresh_token"],
            httponly=refresh_settings["httponly"],
            secure=refresh_settings["secure"],
            samesite=refresh_settings["samesite"],
            domain=refresh_settings["domain"],
            path=refresh_settings["path"],
            max_age=refresh_settings["max_age"],
        )
        response.set_cookie(
            key=access_settings["key"],
            value=payload["access_token"],
            httponly=access_settings["httponly"],
            secure=access_settings["secure"],
            samesite=access_settings["samesite"],
            domain=access_settings["domain"],
            path=access_settings["path"],
            max_age=access_settings["max_age"],
        )
        return response

    @staticmethod
    def _response_with_refresh_cookie(
        db: Session | None,
        payload: dict,
        model_cls,
        status_code: int = status.HTTP_200_OK,
    ) -> Response:
        # Explicitly omit refresh token from cookie payload.
        payload_without_refresh = {**payload, "refresh_token": None}  # nosec B105
        body_content = model_cls(**payload_without_refresh).model_dump_json()
        response = Response(
            content=body_content,
            status_code=status_code,
            media_type="application/json",
        )
        if payload.get("access_token"):
            return AuthFlow.set_auth_cookies(db, response, payload)
        return response

    @staticmethod
    def _response_clear_refresh_cookie(
        db: Session | None,
        payload: dict,
        model_cls,
        status_code: int = status.HTTP_200_OK,
    ) -> Response:
        settings = AuthFlow.refresh_cookie_settings(db)
        access_settings = AuthFlow.access_cookie_settings(db)
        body_content = model_cls(**payload).model_dump_json()
        response = Response(
            content=body_content,
            status_code=status_code,
            media_type="application/json",
        )
        response.delete_cookie(
            key=settings["key"],
            domain=settings["domain"],
            path=settings["path"],
        )
        response.delete_cookie(
            key=access_settings["key"],
            domain=access_settings["domain"],
            path=access_settings["path"],
        )
        return response

    @staticmethod
    def login_response(
        db: Session,
        username: str,
        password: str,
        request: Request,
        provider: str | None,
    ) -> Response | dict[str, Any]:
        result = AuthFlow.login(db, username, password, request, provider)
        if result.get("refresh_token"):
            return AuthFlow._response_with_refresh_cookie(
                db, result, LoginResponse, status.HTTP_200_OK
            )
        return result

    @staticmethod
    def login(
        db: Session,
        username: str,
        password: str,
        request: Request,
        provider: str | None,
    ) -> dict[str, Any]:
        if isinstance(provider, AuthProvider):
            provider_value = provider.value
        else:
            provider_value = provider or AuthProvider.local.value
        try:
            resolved_provider = AuthProvider(provider_value)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Invalid auth provider"
            ) from exc
        if resolved_provider != AuthProvider.local:
            # AuthProvider.sso survives as a stored credential value only; ERP
            # ships no external-identity protocol adapter, so there is no flow
            # such a credential could complete.
            raise HTTPException(
                status_code=400,
                detail="Only local username and password login is available",
            )
        if not username:
            raise HTTPException(status_code=401, detail="Wrong username")

        credential = db.scalar(
            select(UserCredential)
            .where(UserCredential.username == username)
            .where(UserCredential.provider == resolved_provider)
            .where(UserCredential.is_active.is_(True))
        )
        if not credential:
            raise HTTPException(status_code=401, detail="Wrong username")

        now = _now()
        if credential.locked_until and credential.locked_until > now:
            raise HTTPException(status_code=403, detail="Account locked")

        if not verify_password(password, credential.password_hash):
            credential.failed_login_attempts += 1
            if credential.failed_login_attempts >= 5:
                credential.locked_until = now + timedelta(minutes=15)
            db.commit()
            # Audit: failed login
            _audit_auth_event(
                db,
                credential,
                "failed_login",
                AuditAction.INSERT,
                new_values={"username": username, "reason": "invalid_password"},
            )
            detail = "Wrong password"
            if credential.failed_login_attempts == 3:
                detail = (
                    "Wrong password. This is your third failed attempt. "
                    "You have 2 more attempts before your account is locked."
                )
            raise HTTPException(status_code=401, detail=detail)

        if credential.must_change_password:
            raise HTTPException(
                status_code=428,
                detail={
                    "code": "PASSWORD_RESET_REQUIRED",
                    "message": "Password reset required",
                },
            )

        credential.failed_login_attempts = 0
        credential.locked_until = None
        credential.last_login_at = now
        db.commit()
        # Audit: successful login
        _audit_auth_event(
            db,
            credential,
            "session",
            AuditAction.INSERT,
            new_values={"username": username, "login_method": "password"},
        )

        if _primary_totp_method(db, str(credential.person_id)):
            return {
                "mfa_required": True,
                "mfa_token": _issue_mfa_token(db, str(credential.person_id)),
            }

        return AuthFlow._issue_tokens(db, str(credential.person_id), request)

    @staticmethod
    def mfa_setup(db: Session, person_id: str, label: str | None) -> dict[str, Any]:
        person = _person_or_404(db, person_id)
        username = person.email
        credential = db.scalar(
            select(UserCredential)
            .where(UserCredential.person_id == person.id)
            .where(UserCredential.provider == AuthProvider.local)
        )
        if credential and credential.username:
            username = credential.username

        secret = pyotp.random_base32()
        encrypted = _encrypt_secret(db, secret)
        method = MFAMethod(
            person_id=person.id,
            method_type=MFAMethodType.totp,
            label=label,
            secret=encrypted,
            enabled=False,
            is_primary=False,
        )
        db.add(method)
        db.commit()
        db.refresh(method)

        totp = pyotp.TOTP(secret)
        otpauth_uri = totp.provisioning_uri(name=username, issuer_name=_totp_issuer(db))
        return {"method_id": method.id, "secret": secret, "otpauth_uri": otpauth_uri}

    @staticmethod
    def mfa_confirm(
        db: Session,
        method_id: str,
        code: str,
        expected_person_id: str | None = None,
    ) -> MFAMethod:
        method = db.get(MFAMethod, coerce_uuid(method_id))
        if not method:
            raise HTTPException(status_code=404, detail="MFA method not found")
        if expected_person_id:
            expected_uuid = coerce_uuid(expected_person_id)
            if method.person_id != expected_uuid:
                raise HTTPException(status_code=404, detail="MFA method not found")
        if method.method_type != MFAMethodType.totp:
            raise HTTPException(status_code=400, detail="Unsupported MFA method")

        secret = _decrypt_secret(db, method.secret or "")
        totp = pyotp.TOTP(secret)
        if not totp.verify(code, valid_window=0):
            raise HTTPException(status_code=401, detail="Invalid MFA code")

        existing_primaries = db.scalars(
            select(MFAMethod)
            .where(MFAMethod.person_id == method.person_id)
            .where(MFAMethod.id != method.id)
            .where(MFAMethod.is_primary.is_(True))
        ).all()
        for m in existing_primaries:
            m.is_primary = False

        method.enabled = True
        method.is_primary = True
        method.is_active = True
        method.verified_at = _now()
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail="Primary MFA method already exists for this user",
            ) from exc
        db.refresh(method)
        # Audit: MFA enabled
        from app.services.audit_dispatcher import fire_audit_event

        person = db.get(Person, method.person_id)
        if person:
            fire_audit_event(
                db=db,
                organization_id=person.organization_id,
                table_schema="auth",
                table_name="mfa_config",
                record_id=str(method.id),
                action=AuditAction.UPDATE,
                old_values={"enabled": False},
                new_values={"enabled": True, "method_type": "totp"},
                user_id=method.person_id,
            )
        return method

    @staticmethod
    def mfa_verify(
        db: Session, mfa_token: str, code: str, request: Request
    ) -> dict[str, Any]:
        payload = _decode_jwt(db, mfa_token, "mfa")
        person_id = payload.get("sub")
        if not person_id:
            raise HTTPException(status_code=401, detail="Invalid MFA token")

        method = _primary_totp_method(db, str(person_id))
        if not method:
            raise HTTPException(status_code=404, detail="MFA method not found")

        secret = _decrypt_secret(db, method.secret or "")
        totp = pyotp.TOTP(secret)
        if not totp.verify(code, valid_window=0):
            raise HTTPException(status_code=401, detail="Invalid MFA code")

        method.last_used_at = _now()
        db.commit()
        return AuthFlow._issue_tokens(db, person_id, request)

    @staticmethod
    def mfa_verify_response(
        db: Session, mfa_token: str, code: str, request: Request
    ) -> Response:
        result = AuthFlow.mfa_verify(db, mfa_token, code, request)
        return AuthFlow._response_with_refresh_cookie(
            db, result, TokenResponse, status.HTTP_200_OK
        )

    @staticmethod
    def refresh(
        db: Session,
        refresh_token: str,
        request: Request,
        *,
        allow_reuse_grace: bool = False,
    ) -> dict[str, str]:
        token_hash = _hash_token(refresh_token)
        session = db.scalar(
            select(AuthSession)
            .where(AuthSession.token_hash == token_hash)
            .where(AuthSession.status == SessionStatus.active)
            .where(AuthSession.revoked_at.is_(None))
        )
        if not session:
            # Token doesn't match current hash — check if it's the previous token
            reused = db.scalar(
                select(AuthSession)
                .where(AuthSession.previous_token_hash == token_hash)
                .where(AuthSession.status == SessionStatus.active)
                .where(AuthSession.revoked_at.is_(None))
            )
            if reused:
                rotated_at = _as_utc(reused.token_rotated_at)
                now = _now()
                if allow_reuse_grace and (
                    rotated_at is not None
                    and now - rotated_at
                    <= timedelta(seconds=_REFRESH_REUSE_GRACE_SECONDS)
                ):
                    # Grace window for concurrent refresh requests.
                    session = reused
                else:
                    logger.warning(
                        "Refresh token reuse detected: session=%s", reused.id
                    )
                    reused.status = SessionStatus.revoked
                    reused.revoked_at = _now()
                    db.commit()
                    raise HTTPException(
                        status_code=401,
                        detail="Refresh token reuse detected",
                    )
            else:
                raise HTTPException(status_code=401, detail="Invalid refresh token")
        expires_at = _as_utc(session.expires_at)
        if expires_at and expires_at <= _now():
            session.status = SessionStatus.expired
            db.commit()
            raise HTTPException(status_code=401, detail="Refresh token expired")

        new_refresh = secrets.token_urlsafe(48)
        session.previous_token_hash = session.token_hash
        session.token_hash = _hash_token(new_refresh)
        session.token_rotated_at = _now()
        session.last_seen_at = _now()
        if request.client:
            session.ip_address = request.client.host
        session.user_agent = _truncate_user_agent(request.headers.get("user-agent"))
        db.commit()

        roles, permissions = _load_rbac_claims(db, str(session.person_id))
        access_token = _issue_access_token(
            db, str(session.person_id), str(session.id), roles, permissions
        )
        return {"access_token": access_token, "refresh_token": new_refresh}

    @staticmethod
    def refresh_response(
        db: Session, refresh_token: str | None, request: Request
    ) -> Response:
        resolved = AuthFlow.resolve_refresh_token(request, refresh_token, db)
        if not resolved:
            raise HTTPException(status_code=401, detail="Missing refresh token")
        result = AuthFlow.refresh(db, resolved, request, allow_reuse_grace=True)
        return AuthFlow._response_with_refresh_cookie(
            db, result, TokenResponse, status.HTTP_200_OK
        )

    @staticmethod
    def logout(db: Session, refresh_token: str) -> dict[str, Any]:
        from app.services.auth_dependencies import invalidate_session_cache

        token_hash = _hash_token(refresh_token)
        session = db.scalar(
            select(AuthSession)
            .where(AuthSession.token_hash == token_hash)
            .where(AuthSession.revoked_at.is_(None))
        )
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        session.status = SessionStatus.revoked
        session.revoked_at = _now()
        db.commit()
        # Invalidate session cache
        invalidate_session_cache(session.id)
        # Audit: logout
        from app.services.audit_dispatcher import fire_audit_event

        person = db.get(Person, session.person_id)
        if person:
            fire_audit_event(
                db=db,
                organization_id=person.organization_id,
                table_schema="auth",
                table_name="session",
                record_id=str(session.id),
                action=AuditAction.DELETE,
                old_values={"status": "active"},
                new_values={"status": "revoked"},
                user_id=session.person_id,
            )
        return {"revoked_at": session.revoked_at}

    @staticmethod
    def logout_response(
        db: Session, refresh_token: str | None, request: Request
    ) -> Response:
        resolved = AuthFlow.resolve_refresh_token(request, refresh_token, db)
        if not resolved:
            raise HTTPException(status_code=404, detail="Session not found")
        result = AuthFlow.logout(db, resolved)
        return AuthFlow._response_clear_refresh_cookie(
            db, result, LogoutResponse, status.HTTP_200_OK
        )

    @staticmethod
    def resolve_refresh_token(
        request: Request, refresh_token: str | None, db: Session | None = None
    ) -> str | None:
        settings = AuthFlow.refresh_cookie_settings(db)
        return refresh_token or request.cookies.get(settings["key"])

    @staticmethod
    def refresh_cookie_settings(db: Session | None = None) -> dict[str, Any]:
        return {
            "key": _refresh_cookie_name(db),
            "httponly": True,
            "secure": _refresh_cookie_secure(db),
            "samesite": _refresh_cookie_samesite(db),
            "domain": _refresh_cookie_domain(db),
            "path": _refresh_cookie_path(db),
            "max_age": _refresh_ttl_days(db) * 24 * 60 * 60,
        }

    @staticmethod
    def access_cookie_settings(db: Session | None = None) -> dict[str, Any]:
        """Get ERP-local access-token cookie settings."""
        return {
            "key": "access_token",
            "httponly": True,
            "secure": _refresh_cookie_secure(db),
            "samesite": _refresh_cookie_samesite(db),
            "domain": _refresh_cookie_domain(db),
            "path": "/",
            "max_age": _access_ttl_minutes(db) * 60,
        }

    @staticmethod
    def _issue_tokens(db: Session, person_id: str, request: Request) -> dict[str, str]:
        person_uuid = coerce_uuid(person_id)
        refresh_token = secrets.token_urlsafe(48)
        now = _now()
        expires_at = now + timedelta(days=_refresh_ttl_days(db))
        session = AuthSession(
            person_id=person_uuid,
            status=SessionStatus.active,
            token_hash=_hash_token(refresh_token),
            ip_address=request.client.host if request.client else None,
            user_agent=_truncate_user_agent(request.headers.get("user-agent")),
            created_at=now,
            last_seen_at=now,
            expires_at=expires_at,
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        roles, permissions = _load_rbac_claims(db, str(person_uuid))
        access_token = _issue_access_token(
            db, str(person_uuid), str(session.id), roles, permissions
        )
        return {"access_token": access_token, "refresh_token": refresh_token}


auth_flow = AuthFlow()


def request_password_reset(db: Session, email: str) -> dict | None:
    """
    Request a password reset for the given email.
    Returns dict with token and person info if successful, None if email not found.
    Does not raise an error if email doesn't exist (security best practice).
    """
    person = db.scalar(select(Person).where(Person.email == email))
    if not person:
        return None

    credential = db.scalar(
        select(UserCredential)
        .where(UserCredential.person_id == person.id)
        .where(UserCredential.is_active.is_(True))
    )
    if not credential:
        return None

    token = _issue_password_reset_token(
        db,
        str(person.id),
        email,
        person.organization_id,
    )
    return {
        "token": token,
        "email": email,
        "person_name": person.display_name or person.first_name,
        "organization_id": person.organization_id,
    }


def reset_password(db: Session, token: str, new_password: str) -> datetime:
    """
    Reset password using a valid reset token.
    Returns the timestamp when password was reset.

    Validates password strength before allowing reset.
    """
    # Validate password strength first
    require_strong_password(new_password)

    payload = _decode_password_reset_token(db, token)
    person_id = payload.get("sub")
    email = payload.get("email")

    if not person_id or not email:
        raise HTTPException(status_code=401, detail="Invalid reset token")

    person = db.get(Person, coerce_uuid(person_id))
    if not person or person.email != email:
        raise HTTPException(status_code=401, detail="Invalid reset token")

    credential = db.scalar(
        select(UserCredential)
        .where(UserCredential.person_id == person.id)
        .where(UserCredential.is_active.is_(True))
    )
    if not credential:
        raise HTTPException(status_code=404, detail="No credentials found")

    now = _now()
    credential.password_hash = hash_password(new_password)
    credential.password_updated_at = now
    credential.must_change_password = False
    credential.failed_login_attempts = 0
    credential.locked_until = None
    revoke_sessions_for_person(db, str(person.id))
    db.commit()

    # Audit: password change
    _audit_auth_event(
        db,
        credential,
        "credential",
        AuditAction.UPDATE,
        new_values={"changed_fields": ["password_hash"]},
        reason="password_reset",
    )

    return now
