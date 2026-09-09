from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.services import auth_flow_api
from app.services.auth_flow_api import AuthFlowApiService
from tests._helpers.session_mocks import org_session_context


def test_forgot_password_discovers_and_uses_tenant_session(monkeypatch) -> None:
    org_id = uuid4()
    tenant_db = MagicMock()
    result = {
        "email": "user@example.com",
        "token": "reset-token",
        "person_name": "User",
        "organization_id": org_id,
    }
    captured_org_ids: list[object] = []

    monkeypatch.setattr(
        auth_flow_api,
        "active_organization_ids",
        lambda **kwargs: [org_id],
    )
    monkeypatch.setattr(
        auth_flow_api,
        "session_for_org",
        org_session_context(tenant_db, captured_org_ids),
    )
    request_reset = MagicMock(return_value=result)
    send_email = MagicMock(return_value=True)
    monkeypatch.setattr(auth_flow_api, "request_password_reset", request_reset)
    monkeypatch.setattr(auth_flow_api, "send_password_reset_email", send_email)

    response = AuthFlowApiService().forgot_password(
        SimpleNamespace(email="user@example.com"),
        MagicMock(),
        app_url="https://erp.example.com",
    )

    assert response.message == "If the email exists, a reset link has been sent"
    assert captured_org_ids == [org_id]
    request_reset.assert_called_once_with(tenant_db, "user@example.com")
    assert send_email.call_args.kwargs["db"] is tenant_db
    assert send_email.call_args.kwargs["organization_id"] == org_id


def test_reset_password_uses_signed_token_tenant_hint(monkeypatch) -> None:
    org_id = uuid4()
    tenant_db = MagicMock()
    captured_org_ids: list[object] = []
    payload = SimpleNamespace(token="reset-token", new_password="ValidPass123!")

    hint = MagicMock(return_value=org_id)
    catalog = MagicMock(return_value=[org_id])
    reset = MagicMock(return_value="reset-at")
    monkeypatch.setattr(auth_flow_api, "password_reset_organization_hint", hint)
    monkeypatch.setattr(auth_flow_api, "active_organization_ids", catalog)
    monkeypatch.setattr(
        auth_flow_api,
        "session_for_org",
        org_session_context(tenant_db, captured_org_ids),
    )
    monkeypatch.setattr(auth_flow_api, "reset_password", reset)

    result = AuthFlowApiService().reset_password(payload, MagicMock())

    assert result == "reset-at"
    catalog.assert_called_once_with(only=org_id)
    assert captured_org_ids == [org_id]
    reset.assert_called_once_with(tenant_db, "reset-token", "ValidPass123!")
