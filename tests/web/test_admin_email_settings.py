from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from starlette.datastructures import FormData

from app.web.admin import _admin_base_context


def test_email_settings_template_keeps_connection_result_banner() -> None:
    template = Path("templates/admin/settings/email.html").read_text(encoding="utf-8")

    assert "{% if smtp_test %}" in template
    assert "{{ smtp_test.message }}" in template


def test_admin_context_replaces_posted_form_data_with_hidden_csrf_input() -> None:
    organization_id = uuid4()
    request = SimpleNamespace(
        state=SimpleNamespace(
            csrf_token="safe-token",
            csrf_form=FormData(
                [
                    ("smtp_host", "smtp.example.com"),
                    ("smtp_password", "secret"),
                    ("csrf_token", "safe-token"),
                ]
            ),
        )
    )
    auth = SimpleNamespace(
        is_authenticated=True,
        organization_id=organization_id,
        user={},
    )
    db = MagicMock()

    with patch("app.web.admin.resolve_brand_context", return_value={}):
        context = _admin_base_context(request, auth, "Email Configuration", db)

    assert context["page_title"] == "Email Configuration"
    assert request.state.csrf_form == (
        '<input type="hidden" name="csrf_token" value="safe-token">'
    )
    assert "FormData" not in request.state.csrf_form
    assert "smtp_host" not in request.state.csrf_form
    assert "smtp_password" not in request.state.csrf_form
