import ipaddress
import uuid
from datetime import datetime, timedelta, timezone

import pytest


try:
    from datetime import UTC  # type: ignore
except ImportError:  # pragma: no cover
    UTC = timezone.utc

from starlette.requests import Request

import app.net as app_net
from app.api import auth_flow as auth_flow_api
from app.models.auth import (
    Session as AuthSession,
)
from app.models.auth import (
    SessionStatus,
    UserCredential,
)
from app.models.person import Person
from app.services import auth_flow as auth_flow_service
from app.services.auth_flow import hash_password
from tests.conftest import DEFAULT_TEST_ORG_ID


@pytest.fixture()
def tenant_catalog(monkeypatch):
    """Expose the test tenant through the production catalog boundary."""

    def _active_organization_ids(*, only=None):
        if only is not None and only != DEFAULT_TEST_ORG_ID:
            return []
        return [DEFAULT_TEST_ORG_ID]

    monkeypatch.setattr(
        "app.services.auth_flow_api.active_organization_ids",
        _active_organization_ids,
    )


def _build_request(
    *,
    client_host: str,
    host: str,
    forwarded_host: str | None = None,
    forwarded_proto: str | None = None,
    scheme: str = "http",
) -> Request:
    headers = [(b"host", host.encode())]
    if forwarded_host is not None:
        headers.append((b"x-forwarded-host", forwarded_host.encode()))
    if forwarded_proto is not None:
        headers.append((b"x-forwarded-proto", forwarded_proto.encode()))

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": "/auth/forgot-password",
        "raw_path": b"/auth/forgot-password",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": (client_host, 12345),
        "server": (host, 80),
        "scheme": scheme,
    }
    return Request(scope)


class TestLoginAPI:
    """Tests for the /auth/login endpoint."""

    def test_login_success(self, client, db_session, person):
        """Test successful login."""
        # Create user credential
        credential = UserCredential(
            person_id=person.id,
            username=f"loginuser_{uuid.uuid4().hex[:8]}",
            password_hash=hash_password("password123"),
            is_active=True,
        )
        db_session.add(credential)
        db_session.commit()

        payload = {"username": credential.username, "password": "password123"}
        response = client.post("/auth/login", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data or "mfa_required" in data

    def test_login_invalid_credentials(self, client):
        """Test login with invalid credentials."""
        payload = {"username": "nonexistent", "password": "wrongpassword"}
        response = client.post("/auth/login", json=payload)
        assert response.status_code in [401, 404]
        if response.status_code == 401:
            assert response.json()["message"] == "Wrong username"

    def test_login_wrong_password(self, client, db_session, person):
        """Test login with wrong password."""
        credential = UserCredential(
            person_id=person.id,
            username=f"wrongpwd_{uuid.uuid4().hex[:8]}",
            password_hash=hash_password("correctpassword"),
            is_active=True,
        )
        db_session.add(credential)
        db_session.commit()

        payload = {"username": credential.username, "password": "wrongpassword"}
        response = client.post("/auth/login", json=payload)
        assert response.status_code == 401
        assert response.json()["message"] == "Wrong password"

    def test_login_third_wrong_password_attempt_warning(
        self, client, db_session, person
    ):
        """Test login warning after the third wrong password attempt."""
        credential = UserCredential(
            person_id=person.id,
            username=f"thirdattempt_{uuid.uuid4().hex[:8]}",
            password_hash=hash_password("correctpassword"),
            failed_login_attempts=2,
            is_active=True,
        )
        db_session.add(credential)
        db_session.commit()

        payload = {"username": credential.username, "password": "wrongpassword"}
        response = client.post("/auth/login", json=payload)

        assert response.status_code == 401
        assert response.json()["message"] == (
            "Wrong password. This is your third failed attempt. "
            "You have 2 more attempts before your account is locked."
        )

    def test_login_inactive_credential(self, client, db_session, person):
        """Test login with inactive credential."""
        credential = UserCredential(
            person_id=person.id,
            username=f"inactive_{uuid.uuid4().hex[:8]}",
            password_hash=hash_password("password123"),
            is_active=False,
        )
        db_session.add(credential)
        db_session.commit()

        payload = {"username": credential.username, "password": "password123"}
        response = client.post("/auth/login", json=payload)
        assert response.status_code in [401, 404]

    def test_login_password_reset_required(self, client, db_session, person):
        """Test login when password reset is required."""
        credential = UserCredential(
            person_id=person.id,
            username=f"resetreq_{uuid.uuid4().hex[:8]}",
            password_hash=hash_password("password123"),
            is_active=True,
            must_change_password=True,
        )
        db_session.add(credential)
        db_session.commit()

        payload = {"username": credential.username, "password": "password123"}
        response = client.post("/auth/login", json=payload)
        assert response.status_code == 428
        data = response.json()
        # Error handler transforms response to {"code": ..., "message": ..., "details": ...}
        assert data["code"] == "PASSWORD_RESET_REQUIRED"


class TestMeAPI:
    """Tests for the /auth/me endpoints."""

    def test_get_me(self, client, auth_headers, person):
        """Test getting current user profile."""
        response = client.get("/auth/me", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["first_name"] == person.first_name
        assert data["email"] == person.email

    def test_get_me_unauthorized(self, client):
        """Test getting profile without auth."""
        response = client.get("/auth/me", follow_redirects=False)
        # Returns 302 redirect to login when unauthorized
        assert response.status_code in [401, 302]

    def test_update_me(self, client, auth_headers, person):
        """Test updating current user profile."""
        payload = {"first_name": "UpdatedName"}
        response = client.patch("/auth/me", json=payload, headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["first_name"] == "UpdatedName"

    def test_update_me_multiple_fields(self, client, auth_headers):
        """Test updating multiple profile fields."""
        payload = {
            "first_name": "NewFirst",
            "last_name": "NewLast",
            "phone": "+1111111111",
        }
        response = client.patch("/auth/me", json=payload, headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["first_name"] == "NewFirst"
        assert data["last_name"] == "NewLast"


class TestSessionsAPI:
    """Tests for the /auth/me/sessions endpoints."""

    def test_list_sessions(self, client, auth_headers, auth_session):
        """Test listing user sessions."""
        response = client.get("/auth/me/sessions", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert "sessions" in data
        assert "total" in data
        assert isinstance(data["sessions"], list)

    def test_list_sessions_unauthorized(self, client):
        """Test listing sessions without auth."""
        response = client.get("/auth/me/sessions", follow_redirects=False)
        # Returns 302 redirect to login when unauthorized
        assert response.status_code in [401, 302]

    def test_revoke_session(self, client, auth_headers, db_session, person):
        """Test revoking a specific session."""
        # Create another session to revoke
        other_session = AuthSession(
            person_id=person.id,
            token_hash="other-token-hash",
            status=SessionStatus.active,
            ip_address="192.168.1.1",
            user_agent="other-client",
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
        db_session.add(other_session)
        db_session.commit()
        db_session.refresh(other_session)

        response = client.delete(
            f"/auth/me/sessions/{other_session.id}", headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()
        assert "revoked_at" in data

    def test_revoke_session_not_found(self, client, auth_headers):
        """Test revoking a non-existent session."""
        fake_id = str(uuid.uuid4())
        response = client.delete(f"/auth/me/sessions/{fake_id}", headers=auth_headers)
        assert response.status_code == 404

    def test_revoke_all_other_sessions(self, client, auth_headers, db_session, person):
        """Test revoking all other sessions."""
        # Create additional sessions
        for i in range(3):
            session = AuthSession(
                person_id=person.id,
                token_hash=f"session-{i}-hash",
                status=SessionStatus.active,
                ip_address=f"192.168.1.{i}",
                user_agent=f"client-{i}",
                expires_at=datetime.now(UTC) + timedelta(days=30),
            )
            db_session.add(session)
        db_session.commit()

        response = client.delete("/auth/me/sessions", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert "revoked_at" in data


class TestPasswordAPI:
    """Tests for password-related endpoints."""

    def test_change_password(self, client, auth_headers, db_session, person):
        """Test changing password."""
        # Create credential for the authenticated user
        credential = UserCredential(
            person_id=person.id,
            username=f"changepwd_{uuid.uuid4().hex[:8]}",
            password_hash=hash_password("oldpassword123"),
            is_active=True,
        )
        db_session.add(credential)
        db_session.commit()

        payload = {
            "current_password": "oldpassword123",
            "new_password": "newpassword456",
        }
        response = client.post("/auth/me/password", json=payload, headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert "changed_at" in data

    def test_change_password_wrong_current(
        self, client, auth_headers, db_session, person
    ):
        """Test changing password with wrong current password."""
        credential = UserCredential(
            person_id=person.id,
            username=f"wrongcurrent_{uuid.uuid4().hex[:8]}",
            password_hash=hash_password("correctpassword"),
            is_active=True,
        )
        db_session.add(credential)
        db_session.commit()

        payload = {
            "current_password": "wrongpassword",
            "new_password": "newpassword456",
        }
        response = client.post("/auth/me/password", json=payload, headers=auth_headers)
        assert response.status_code == 401

    def test_change_password_same_password(
        self, client, auth_headers, db_session, person
    ):
        """Test changing password to the same password."""
        credential = UserCredential(
            person_id=person.id,
            username=f"samepwd_{uuid.uuid4().hex[:8]}",
            password_hash=hash_password("samepassword"),
            is_active=True,
        )
        db_session.add(credential)
        db_session.commit()

        payload = {
            "current_password": "samepassword",
            "new_password": "samepassword",
        }
        response = client.post("/auth/me/password", json=payload, headers=auth_headers)
        assert response.status_code == 400

    def test_change_password_revokes_sessions(
        self, client, auth_headers, db_session, person, auth_session
    ):
        """Test changing password revokes active sessions."""
        credential = UserCredential(
            person_id=person.id,
            username=f"revokepwd_{uuid.uuid4().hex[:8]}",
            password_hash=hash_password("oldpassword123"),
            is_active=True,
        )
        db_session.add(credential)
        db_session.commit()

        other_session = AuthSession(
            person_id=person.id,
            token_hash="other-session-hash",
            status=SessionStatus.active,
            ip_address="192.168.1.100",
            user_agent="other-client",
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
        db_session.add(other_session)
        db_session.commit()

        payload = {
            "current_password": "oldpassword123",
            "new_password": "newpassword456",
        }
        response = client.post("/auth/me/password", json=payload, headers=auth_headers)
        assert response.status_code == 200

        sessions = (
            db_session.query(AuthSession)
            .filter(AuthSession.person_id == person.id)
            .all()
        )
        assert sessions
        for session in sessions:
            assert session.status == SessionStatus.revoked
            assert session.revoked_at is not None

    def test_forgot_password(self, client, db_session, person, tenant_catalog):
        """Test forgot password request."""
        payload = {"email": person.email}
        response = client.post("/auth/forgot-password", json=payload)
        # Always returns success to prevent email enumeration
        assert response.status_code == 200

    def test_forgot_password_nonexistent_email(self, client, tenant_catalog):
        """Test forgot password with non-existent email."""
        payload = {"email": "nonexistent@example.com"}
        response = client.post("/auth/forgot-password", json=payload)
        # Should still return success to prevent email enumeration
        assert response.status_code == 200

    def test_resolve_app_url_ignores_untrusted_forwarded_host(self, monkeypatch):
        """X-Forwarded-* headers should be ignored for untrusted clients."""
        monkeypatch.setattr(
            app_net,
            "_TRUSTED_PROXY_NETWORKS",
            [ipaddress.ip_network("10.0.0.0/8")],
        )
        request = _build_request(
            client_host="198.51.100.10",
            host="internal.local",
            forwarded_host="attacker.example",
            forwarded_proto="https",
        )

        app_url = auth_flow_api._resolve_app_url(request)

        assert app_url == "http://internal.local"

    def test_resolve_app_url_uses_trusted_forwarded_host(self, monkeypatch):
        """X-Forwarded-* headers should be honored for trusted proxies."""
        monkeypatch.setattr(
            app_net,
            "_TRUSTED_PROXY_NETWORKS",
            [ipaddress.ip_network("10.0.0.0/8")],
        )
        request = _build_request(
            client_host="10.1.2.3",
            host="internal.local",
            forwarded_host="erp.example.com,attacker.example",
            forwarded_proto="https",
        )

        app_url = auth_flow_api._resolve_app_url(request)

        assert app_url == "https://erp.example.com"

    def test_reset_password_revokes_sessions(
        self, client, db_session, person, tenant_catalog
    ):
        """Test reset password revokes active sessions."""
        credential = UserCredential(
            person_id=person.id,
            username=f"resetpwd_{uuid.uuid4().hex[:8]}",
            password_hash=hash_password("oldpassword123"),
            is_active=True,
        )
        db_session.add(credential)
        db_session.commit()

        session_one = AuthSession(
            person_id=person.id,
            token_hash="session-one-hash",
            status=SessionStatus.active,
            ip_address="192.168.1.200",
            user_agent="client-one",
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
        session_two = AuthSession(
            person_id=person.id,
            token_hash="session-two-hash",
            status=SessionStatus.active,
            ip_address="192.168.1.201",
            user_agent="client-two",
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
        db_session.add_all([session_one, session_two])
        db_session.commit()

        reset = auth_flow_service.request_password_reset(db_session, person.email)
        assert reset is not None
        assert (
            auth_flow_service.password_reset_organization_hint(reset["token"])
            == person.organization_id
        )

        payload = {"token": reset["token"], "new_password": "newpassword456"}
        response = client.post("/auth/reset-password", json=payload)
        assert response.status_code == 200

        # The public endpoint now performs the mutation in its own fully
        # tenant-scoped session, so discard this fixture session's cache.
        db_session.expire_all()
        sessions = (
            db_session.query(AuthSession)
            .filter(AuthSession.person_id == person.id)
            .all()
        )
        assert sessions
        for session in sessions:
            assert session.status == SessionStatus.revoked
            assert session.revoked_at is not None


class TestRefreshAPI:
    """Tests for token refresh endpoint."""

    def test_refresh_invalid_token(self, client):
        """Test refresh with invalid token."""
        payload = {"refresh_token": "invalid-refresh-token"}
        response = client.post("/auth/refresh", json=payload)
        assert response.status_code == 401

    def test_refresh_missing_token(self, client):
        """Test refresh without token or cookie."""
        response = client.post("/auth/refresh", json={})
        assert response.status_code == 401
        data = response.json()
        # Error handler transforms response to {"code": ..., "message": ..., "details": ...}
        assert (
            "missing" in data["message"].lower() or "refresh" in data["message"].lower()
        )

    def test_refresh_v1_with_cookie(self, client, db_session, person):
        """Test refresh using cookie on v1 endpoint."""
        credential = UserCredential(
            person_id=person.id,
            username=f"cookieuser_{uuid.uuid4().hex[:8]}",
            password_hash=hash_password("password123"),
            is_active=True,
        )
        db_session.add(credential)
        db_session.commit()

        login_payload = {"username": credential.username, "password": "password123"}
        login_response = client.post("/auth/login", json=login_payload)
        assert login_response.status_code == 200

        # Get the refresh token cookie and explicitly pass it for v1 endpoint
        cookie_name = auth_flow_service.AuthFlow.refresh_cookie_settings()["key"]
        refresh_token = client.cookies.get(cookie_name)
        assert refresh_token

        # Use the refresh token in body since cookie may not be auto-passed to different path
        response = client.post(
            "/api/v1/auth/refresh", json={"refresh_token": refresh_token}
        )
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data

    def test_refresh_reuse_within_grace_period(self, client, db_session, person):
        """Test that concurrent refresh (within grace period) succeeds instead of revoking."""
        credential = UserCredential(
            person_id=person.id,
            username=f"reuseuser_{uuid.uuid4().hex[:8]}",
            password_hash=hash_password("password123"),
            is_active=True,
        )
        db_session.add(credential)
        db_session.commit()

        login_payload = {"username": credential.username, "password": "password123"}
        login_response = client.post("/auth/login", json=login_payload)
        assert login_response.status_code == 200

        cookie_name = auth_flow_service.AuthFlow.refresh_cookie_settings()["key"]
        old_refresh = client.cookies.get(cookie_name)
        assert old_refresh

        # First refresh rotates the token
        refresh_response = client.post("/auth/refresh", json={})
        assert refresh_response.status_code == 200

        # Immediate reuse of old token — within 30s grace period, should succeed
        reuse_response = client.post(
            "/auth/refresh", json={"refresh_token": old_refresh}
        )
        assert reuse_response.status_code == 200

        # Session should still be active (not revoked)
        session = (
            db_session.query(AuthSession)
            .filter(AuthSession.person_id == person.id)
            .order_by(AuthSession.created_at.desc())
            .first()
        )
        assert session is not None
        assert session.status == SessionStatus.active
        assert session.revoked_at is None

    def test_refresh_reuse_after_grace_period(self, client, db_session, person):
        """Test that refresh token reuse after grace period revokes session."""
        credential = UserCredential(
            person_id=person.id,
            username=f"reuseuser2_{uuid.uuid4().hex[:8]}",
            password_hash=hash_password("password123"),
            is_active=True,
        )
        db_session.add(credential)
        db_session.commit()

        login_payload = {"username": credential.username, "password": "password123"}
        login_response = client.post("/auth/login", json=login_payload)
        assert login_response.status_code == 200

        cookie_name = auth_flow_service.AuthFlow.refresh_cookie_settings()["key"]
        old_refresh = client.cookies.get(cookie_name)
        assert old_refresh

        # First refresh rotates the token
        refresh_response = client.post("/auth/refresh", json={})
        assert refresh_response.status_code == 200

        # Simulate the grace period having passed by backdating token_rotated_at
        session = (
            db_session.query(AuthSession)
            .filter(AuthSession.person_id == person.id)
            .order_by(AuthSession.created_at.desc())
            .first()
        )
        assert session is not None
        session.token_rotated_at = datetime.now(UTC) - timedelta(seconds=60)
        db_session.commit()

        # Now reuse old token — outside grace period, should be revoked
        reuse_response = client.post(
            "/auth/refresh", json={"refresh_token": old_refresh}
        )
        assert reuse_response.status_code == 401
        data = reuse_response.json()
        assert "reuse" in data["message"].lower()

        db_session.expire_all()
        session = (
            db_session.query(AuthSession)
            .filter(AuthSession.person_id == person.id)
            .order_by(AuthSession.created_at.desc())
            .first()
        )
        assert session is not None
        assert session.status == SessionStatus.revoked
        assert session.revoked_at is not None


class TestLogoutAPI:
    """Tests for logout endpoint."""

    def test_logout_invalid_token(self, client):
        """Test logout with invalid token."""
        payload = {"refresh_token": "invalid-refresh-token"}
        response = client.post("/auth/logout", json=payload)
        assert response.status_code in [401, 404]


class TestMFAAPI:
    """Tests for MFA-related endpoints."""

    def test_mfa_setup(self, client, db_session, person, auth_headers):
        """Test MFA setup."""
        payload = {"person_id": str(person.id), "label": "Test Device"}
        response = client.post("/auth/mfa/setup", json=payload, headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert "secret" in data or "provisioning_uri" in data or "method_id" in data

    def test_mfa_setup_forbidden(self, client, db_session, person, auth_headers):
        """Test MFA setup for a different user."""
        other_person = Person(
            first_name="Other",
            last_name="User",
            email=f"other_{uuid.uuid4().hex[:8]}@example.com",
            organization_id=DEFAULT_TEST_ORG_ID,
        )
        db_session.add(other_person)
        db_session.commit()

        payload = {"person_id": str(other_person.id), "label": "Other Device"}
        response = client.post("/auth/mfa/setup", json=payload, headers=auth_headers)
        assert response.status_code == 403

    def test_mfa_confirm_invalid(self, client, auth_headers):
        """Test MFA confirm with invalid method."""
        payload = {"method_id": str(uuid.uuid4()), "code": "123456"}
        response = client.post("/auth/mfa/confirm", json=payload, headers=auth_headers)
        assert response.status_code in [400, 404]

    def test_mfa_confirm_wrong_user(self, client, db_session, person, auth_headers):
        """Test MFA confirm with method owned by a different user."""
        other_person = Person(
            first_name="Other",
            last_name="User",
            email=f"other_{uuid.uuid4().hex[:8]}@example.com",
            organization_id=DEFAULT_TEST_ORG_ID,
        )
        db_session.add(other_person)
        db_session.commit()
        db_session.refresh(other_person)

        setup = auth_flow_service.auth_flow.mfa_setup(
            db_session, str(other_person.id), label="Other Device"
        )
        payload = {"method_id": str(setup["method_id"]), "code": "123456"}
        response = client.post("/auth/mfa/confirm", json=payload, headers=auth_headers)
        assert response.status_code == 404

    def test_mfa_verify_invalid_token(self, client):
        """Test MFA verify with invalid token."""
        payload = {"mfa_token": "invalid-mfa-token", "code": "123456"}
        response = client.post("/auth/mfa/verify", json=payload)
        assert response.status_code in [401, 404]


class TestAuthFlowAPIV1:
    """Tests for the /api/v1/auth endpoints."""

    def test_login_v1(self, client, db_session, person):
        """Test login via v1 API."""
        credential = UserCredential(
            person_id=person.id,
            username=f"v1login_{uuid.uuid4().hex[:8]}",
            password_hash=hash_password("password123"),
            is_active=True,
        )
        db_session.add(credential)
        db_session.commit()

        payload = {"username": credential.username, "password": "password123"}
        response = client.post("/api/v1/auth/login", json=payload)
        assert response.status_code == 200

    def test_get_me_v1(self, client, auth_headers):
        """Test get me via v1 API."""
        response = client.get("/api/v1/auth/me", headers=auth_headers)
        assert response.status_code == 200

    def test_forgot_password_v1(self, client, tenant_catalog):
        """Test forgot password via v1 API."""
        payload = {"email": "test@example.com"}
        response = client.post("/api/v1/auth/forgot-password", json=payload)
        assert response.status_code == 200
