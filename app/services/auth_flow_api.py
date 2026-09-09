"""
Auth flow API service.

Provides API-focused handlers for auth flow routes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone


try:
    from datetime import UTC  # type: ignore
except ImportError:  # pragma: no cover
    UTC = timezone.utc

from fastapi import HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.auth import AuthProvider, SessionStatus, UserCredential
from app.models.auth import Session as AuthSession
from app.models.person import Person
from app.schemas.auth_flow import (
    AvatarUploadResponse,
    ForgotPasswordResponse,
    MeResponse,
    PasswordChangeResponse,
    PasswordResetRequiredRequest,
    PasswordResetRequiredResponse,
    SessionInfoResponse,
    SessionListResponse,
    SessionRevokeResponse,
)
from app.services import avatar as avatar_service
from app.services.auth_flow import (
    hash_password,
    password_reset_organization_hint,
    request_password_reset,
    reset_password,
    revoke_sessions_for_person,
    verify_password,
)
from app.services.common import coerce_uuid
from app.db.session_context import session_for_org
from app.services.email import send_password_reset_email
from app.tenant_catalog import active_organization_ids

logger = logging.getLogger(__name__)


class AuthFlowApiService:
    def get_me(self, auth: dict, db: Session) -> MeResponse:
        person = db.get(Person, coerce_uuid(auth["person_id"]))
        if not person:
            raise HTTPException(status_code=404, detail="User not found")

        return MeResponse(
            id=person.id,
            first_name=person.first_name,
            last_name=person.last_name,
            display_name=person.display_name,
            avatar_url=person.avatar_url,
            email=person.email,
            email_verified=person.email_verified,
            phone=person.phone,
            date_of_birth=person.date_of_birth,
            gender=person.gender.value if person.gender else "unknown",
            preferred_contact_method=person.preferred_contact_method.value
            if person.preferred_contact_method
            else None,
            locale=person.locale,
            timezone=person.timezone,
            roles=auth.get("roles", []),
            scopes=auth.get("scopes", []),
        )

    def update_me(self, auth: dict, payload, db: Session) -> MeResponse:
        person = db.get(Person, coerce_uuid(auth["person_id"]))
        if not person:
            raise HTTPException(status_code=404, detail="User not found")

        update_data = payload.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(person, field, value)

        db.commit()
        db.refresh(person)

        return self.get_me(auth, db)

    async def upload_avatar(
        self, file: UploadFile, auth: dict, db: Session
    ) -> AvatarUploadResponse:
        person = db.get(Person, coerce_uuid(auth["person_id"]))
        if not person:
            raise HTTPException(status_code=404, detail="User not found")

        avatar_service.delete_avatar(person.avatar_url)

        avatar_url = await avatar_service.save_avatar(file, str(person.id))
        person.avatar_url = avatar_url
        db.commit()

        return AvatarUploadResponse(avatar_url=avatar_url)

    def delete_avatar(self, auth: dict, db: Session) -> None:
        person = db.get(Person, coerce_uuid(auth["person_id"]))
        if not person:
            raise HTTPException(status_code=404, detail="User not found")

        avatar_service.delete_avatar(person.avatar_url)
        person.avatar_url = None
        db.commit()

    def list_sessions(self, auth: dict, db: Session) -> SessionListResponse:
        person_id = coerce_uuid(auth["person_id"])
        now = datetime.now(UTC)
        sessions = list(
            db.scalars(
                select(AuthSession)
                .where(
                    AuthSession.person_id == person_id,
                    AuthSession.status == SessionStatus.active,
                    AuthSession.revoked_at.is_(None),
                    AuthSession.expires_at > now,
                )
                .order_by(AuthSession.created_at.desc())
            )
        )

        current_session_id = auth.get("session_id")

        return SessionListResponse(
            sessions=[
                SessionInfoResponse(
                    id=session.id,
                    status=session.status.value,
                    ip_address=session.ip_address,
                    user_agent=session.user_agent,
                    created_at=session.created_at,
                    last_seen_at=session.last_seen_at,
                    expires_at=session.expires_at,
                    is_current=(str(session.id) == current_session_id),
                )
                for session in sessions
            ],
            total=len(sessions),
        )

    def revoke_session(
        self, session_id: str, auth: dict, db: Session
    ) -> SessionRevokeResponse:
        now = datetime.now(UTC)
        session = db.scalar(
            select(AuthSession).where(
                AuthSession.id == coerce_uuid(session_id),
                AuthSession.person_id == coerce_uuid(auth["person_id"]),
                AuthSession.status == SessionStatus.active,
                AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > now,
            )
        )

        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        session.status = SessionStatus.revoked
        session.revoked_at = now
        db.commit()

        return SessionRevokeResponse(revoked_at=now)

    def revoke_all_other_sessions(
        self, auth: dict, db: Session
    ) -> SessionRevokeResponse:
        current_session_id = auth.get("session_id")
        if current_session_id:
            current_session_id = coerce_uuid(current_session_id)

        now = datetime.now(UTC)
        sessions = list(
            db.scalars(
                select(AuthSession).where(
                    AuthSession.person_id == coerce_uuid(auth["person_id"]),
                    AuthSession.status == SessionStatus.active,
                    AuthSession.revoked_at.is_(None),
                    AuthSession.expires_at > now,
                    AuthSession.id != current_session_id,
                )
            )
        )

        for session in sessions:
            session.status = SessionStatus.revoked
            session.revoked_at = now

        db.commit()

        return SessionRevokeResponse(revoked_at=now, revoked_count=len(sessions))

    def change_password(
        self, payload, auth: dict, db: Session
    ) -> PasswordChangeResponse:
        credential = db.scalar(
            select(UserCredential).where(
                UserCredential.person_id == coerce_uuid(auth["person_id"]),
                UserCredential.is_active.is_(True),
            )
        )

        if not credential:
            raise HTTPException(status_code=404, detail="No credentials found")

        if not verify_password(payload.current_password, credential.password_hash):
            raise HTTPException(status_code=401, detail="Current password is incorrect")

        if payload.current_password == payload.new_password:
            raise HTTPException(
                status_code=400, detail="New password must be different"
            )

        now = datetime.now(UTC)
        credential.password_hash = hash_password(payload.new_password)
        credential.password_updated_at = now
        credential.must_change_password = False
        revoke_sessions_for_person(db, auth["person_id"])
        db.commit()

        return PasswordChangeResponse(changed_at=now)

    def reset_password_required(
        self,
        payload: PasswordResetRequiredRequest,
        db: Session,
    ) -> PasswordResetRequiredResponse:
        username = payload.username.strip()
        credential = db.scalar(
            select(UserCredential).where(
                UserCredential.username == username,
                UserCredential.provider == AuthProvider.local,
                UserCredential.is_active.is_(True),
            )
        )
        if not credential:
            raise HTTPException(status_code=401, detail="Invalid credentials")

        now = datetime.now(UTC)
        if credential.locked_until and credential.locked_until > now:
            raise HTTPException(status_code=403, detail="Account locked")

        if not verify_password(payload.current_password, credential.password_hash):
            credential.failed_login_attempts += 1
            if credential.failed_login_attempts >= 5:
                credential.locked_until = now + timedelta(minutes=15)
            db.commit()
            raise HTTPException(status_code=401, detail="Invalid credentials")

        if payload.current_password == payload.new_password:
            raise HTTPException(
                status_code=400, detail="New password must be different"
            )

        credential.password_hash = hash_password(payload.new_password)
        credential.password_updated_at = now
        credential.must_change_password = False
        credential.failed_login_attempts = 0
        credential.locked_until = None
        db.commit()

        return PasswordResetRequiredResponse(changed_at=now)

    def forgot_password(
        self,
        payload,
        db: Session,
        app_url: str | None = None,
    ) -> ForgotPasswordResponse:
        # The public auth dependency can bypass the ORM tenant filter, but it
        # cannot bypass PostgreSQL RLS. Discover tenant ids through the narrow
        # catalogue and do every person lookup and SMTP-profile lookup inside
        # a fully scoped tenant session.
        del db
        for organization_id in active_organization_ids():
            with session_for_org(organization_id) as tenant_db:
                result = request_password_reset(tenant_db, payload.email)
                if not result:
                    continue
                send_password_reset_email(
                    db=tenant_db,
                    to_email=result["email"],
                    reset_token=result["token"],
                    person_name=result["person_name"],
                    app_url=app_url,
                    organization_id=organization_id,
                )
                break

        return ForgotPasswordResponse()

    def reset_password(self, payload, db: Session):
        del db
        organization_hint = password_reset_organization_hint(payload.token)
        organization_ids = (
            active_organization_ids(only=organization_hint)
            if organization_hint is not None
            else active_organization_ids()
        )

        for organization_id in organization_ids:
            try:
                with session_for_org(organization_id) as tenant_db:
                    return reset_password(
                        tenant_db,
                        payload.token,
                        payload.new_password,
                    )
            except HTTPException as exc:
                # A legacy token has no tenant hint. Continue only when it is
                # invalid in this tenant; a valid token's domain error must be
                # returned to the caller unchanged.
                if organization_hint is None and exc.status_code == 401:
                    continue
                raise

        raise HTTPException(status_code=401, detail="Invalid reset token")


auth_flow_api_service = AuthFlowApiService()
