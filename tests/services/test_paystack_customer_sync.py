"""Paystack customer sync — idempotency, skips, and the naive name split."""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests._helpers.source_introspection import (
    mentions_in_code,
    module_level_assignments,
)

from app.services.finance.payments.paystack_customer_sync import (
    PaystackSyncResult,
    customer_email,
    customer_phone,
    split_name,
    sync_customers,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "sync_customers_to_paystack.py"
ORG = uuid.uuid4()


def _customer(email="a@b.com", phone="0800", name="Acme Trading Limited"):
    c = MagicMock()
    c.customer_id = uuid.uuid4()
    c.customer_code = "CUST-001"
    c.legal_name = name
    c.trading_name = None
    c.customer_type = None
    c.primary_contact = {"email": email, "phone": phone} if (email or phone) else None
    return c


def _db(customers):
    db = MagicMock()
    db.scalars.return_value.all.return_value = customers
    return db


def _patched(client):
    """Patch the settings resolver and the Paystack client together."""

    def _resolve_value(db, domain, key, *, organization_id):
        assert organization_id == ORG
        return "sk_test"

    return (
        patch(
            "app.services.settings_spec.resolve_value",
            side_effect=_resolve_value,
        ),
        patch(
            "app.services.finance.payments.paystack_client.PaystackClient",
            return_value=client,
        ),
    )


def _client():
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    return client


# --------------------------------------------------------------------------
# The pure helpers
# --------------------------------------------------------------------------


def test_contact_details_come_from_the_json_blob():
    c = _customer(email="x@y.com", phone="0801")
    assert customer_email(c) == "x@y.com"
    assert customer_phone(c) == "0801"


def test_a_missing_contact_blob_is_not_an_error():
    c = _customer(email=None, phone=None)
    assert customer_email(c) is None
    assert customer_phone(c) is None


def test_a_non_dict_contact_is_treated_as_absent():
    """The column is JSONB; anything could be in it."""
    c = _customer()
    c.primary_contact = "not-a-dict"
    assert customer_email(c) is None


def test_split_name_takes_the_first_word_as_the_forename():
    assert split_name("Acme Trading Limited") == ("Acme", "Trading Limited")
    assert split_name("Madonna") == ("Madonna", "")


def test_split_name_survives_empty_and_whitespace():
    assert split_name("") == ("", "")
    assert split_name("   ") == ("", "")
    assert split_name(None) == ("", "")


# --------------------------------------------------------------------------
# Sync behaviour
# --------------------------------------------------------------------------


def test_a_customer_with_no_email_is_skipped_not_failed():
    """Paystack keys customers on email, so there is nothing to create."""
    client = _client()
    resolver, paystack = _patched(client)
    with resolver, paystack:
        result = sync_customers(
            _db([_customer(email=None)]), organization_id=ORG, dry_run=False
        )
    assert result.skipped_no_email == 1
    assert result.errors == []
    client.create_customer.assert_not_called()


def test_an_existing_paystack_customer_is_updated_not_duplicated():
    """The idempotency that makes this safe to schedule."""
    client = _client()
    client.get_customer.return_value = MagicMock(customer_code="CUS_x")
    resolver, paystack = _patched(client)
    with resolver, paystack:
        result = sync_customers(_db([_customer()]), organization_id=ORG, dry_run=False)
    assert (result.updated, result.created) == (1, 0)
    client.create_customer.assert_not_called()


def test_an_unknown_customer_is_created():
    client = _client()
    client.get_customer.return_value = None
    resolver, paystack = _patched(client)
    with resolver, paystack:
        result = sync_customers(_db([_customer()]), organization_id=ORG, dry_run=False)
    assert (result.created, result.updated) == (1, 0)


def test_dry_run_makes_no_write_calls():
    client = _client()
    resolver, paystack = _patched(client)
    with resolver, paystack:
        result = sync_customers(_db([_customer()]), organization_id=ORG, dry_run=True)
    client.get_customer.assert_not_called()
    client.create_customer.assert_not_called()
    assert result.total == 1


def test_one_failure_does_not_abort_the_rest():
    client = _client()
    client.get_customer.side_effect = [RuntimeError("429"), None]
    resolver, paystack = _patched(client)
    with resolver, paystack:
        result = sync_customers(
            _db([_customer(), _customer()]), organization_id=ORG, dry_run=False
        )
    assert len(result.errors) == 1
    assert result.created == 1


def test_missing_credentials_fail_loudly_rather_than_sending_empty_keys():
    with patch(
        "app.services.settings_spec.resolve_value",
        side_effect=lambda db, domain, key, *, organization_id: None,
    ):
        try:
            sync_customers(_db([]), organization_id=ORG, dry_run=False)
        except RuntimeError as exc:
            assert "paystack_secret_key" in str(exc)
        else:
            raise AssertionError("expected a RuntimeError")


def test_the_query_is_scoped_to_the_organization():
    client = _client()
    resolver, paystack = _patched(client)
    db = _db([])
    with resolver, paystack:
        sync_customers(db, organization_id=ORG, dry_run=True)
    assert "organization_id" in str(db.scalars.call_args[0][0])


def test_result_defaults_are_zero():
    r = PaystackSyncResult()
    assert (r.total, r.created, r.updated, r.skipped_no_email) == (0, 0, 0, 0)


# --------------------------------------------------------------------------
# Regression guards on the script
# --------------------------------------------------------------------------


def test_the_script_no_longer_hardcodes_an_organization():
    assert "ORG_ID" not in module_level_assignments(SCRIPT)


def test_the_script_uses_a_scoped_session_and_records_the_run():
    assert mentions_in_code(SCRIPT, "SessionLocal") == []
    source = SCRIPT.read_text(encoding="utf-8")
    assert "session_for_org" in source and "batch_operation(" in source
