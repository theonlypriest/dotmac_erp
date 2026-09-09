"""Unit tests for the dotmac_sub integration (client, config, webhook).

Covers the pure-logic surface that does not require a database session:
- decimal/record parsing from API payloads,
- ListResponse pagination envelope handling,
- DotmacSubConfig (is_configured / auth_headers / env fallback),
- inbound webhook HMAC-SHA256 verification,
- webhook payload entity-id extraction,
- invoice status mapping (dotmac_sub → ERP).

The DB-backed sync behaviours (reseller parent/child wiring and the wholesale
GL-suppression rule) are exercised by the integration suite; the rule itself is
a single predicate — ``post_unposted_payments`` posts a payment only when its
customer has ``parent_customer_id IS NULL``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.services.dotmac_sub.client import (
    DotmacSubClient,
    DotmacSubConfig,
    _dec,
)
from app.services.dotmac_sub.sync._bank_mapping import BankMappingMixin


# ---------------------------------------------------------------------------
# Decimal coercion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("123.45", Decimal("123.45")),
        (10, Decimal("10")),
        (None, Decimal("0")),
        ("", Decimal("0")),
        ("not-a-number", Decimal("0")),
    ],
)
def test_dec_coercion(value: object, expected: Decimal) -> None:
    assert _dec(value) == expected


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_config_is_configured() -> None:
    assert not DotmacSubConfig(api_url="", api_token="").is_configured()
    assert not DotmacSubConfig(api_url="https://x", api_token="").is_configured()
    assert DotmacSubConfig(api_url="https://x", api_token="tok").is_configured()


def test_config_auth_headers() -> None:
    # dotmac_sub service auth is a scoped API key via X-Api-Key; its Bearer
    # path only accepts session-bound login JWTs (contract fix, audit A3).
    assert DotmacSubConfig(api_url="x", api_token="abc").auth_headers == {
        "X-Api-Key": "abc"
    }


def test_config_for_org_falls_back_to_env_when_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """for_org returns the env config when the IntegrationConfig lookup raises."""
    env = DotmacSubConfig(api_url="https://env", api_token="env-tok")
    monkeypatch.setattr(DotmacSubConfig, "from_settings", classmethod(lambda cls: env))
    db = MagicMock()
    db.execute.side_effect = RuntimeError("no db")  # forces the except path
    cfg = DotmacSubConfig.for_org(db, "00000000-0000-0000-0000-000000000000")
    assert cfg is env


# ---------------------------------------------------------------------------
# Pagination envelope + record parsing
# ---------------------------------------------------------------------------


def _client_with_responses(responses: list[object]) -> DotmacSubClient:
    client = DotmacSubClient(DotmacSubConfig(api_url="https://x", api_token="t"))
    client._request = MagicMock(side_effect=responses)  # type: ignore[method-assign]
    return client


def test_paginate_listresponse_envelope_stops_on_short_page() -> None:
    client = _client_with_responses(
        [{"items": [{"id": "1"}, {"id": "2"}], "count": 2, "limit": 100, "offset": 0}]
    )
    items = list(client._paginate("/things", page_size=100))
    assert [i["id"] for i in items] == ["1", "2"]
    client._request.assert_called_once()


def test_paginate_handles_bare_list_and_empty() -> None:
    client = _client_with_responses(
        [
            [],
        ]
    )
    assert list(client._paginate("/things")) == []


def test_sync_paginate_paces_full_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client_with_responses(
        [
            {"items": [{"id": str(index)} for index in range(500)]},
            {"items": []},
        ]
    )
    sleeps: list[float] = []
    monkeypatch.setattr(
        "app.services.dotmac_sub.client.time.sleep", lambda delay: sleeps.append(delay)
    )

    items = list(client._sync_paginate("/things/sync"))

    assert len(items) == 500
    assert sleeps == [client._SYNC_PAGE_DELAY_SECONDS]


class _BankMappingHarness(BankMappingMixin):
    pass


def test_payment_channel_mapping_uses_bounded_sync_feed() -> None:
    client = _client_with_responses([{"items": []}])
    harness = _BankMappingHarness()
    harness.client = client
    harness._payment_channel_names = {}

    harness._load_payment_channels()

    assert client._request.call_args.args[:2] == ("GET", "/payment-channels/sync")
    assert client._request.call_args.kwargs["params"]["limit"] == 500


def test_parse_invoice_maps_fields_and_inline_allocations() -> None:
    client = DotmacSubClient(DotmacSubConfig(api_url="x", api_token="t"))
    inv = client._parse_invoice(
        {
            "id": "inv-1",
            "account_id": "acc-1",
            "invoice_number": "INV-100",
            "status": "issued",
            "currency": "NGN",
            "subtotal": "100.00",
            "tax_total": "7.50",
            "total": "107.50",
            "balance_due": "107.50",
            "issued_at": "2026-02-01",
            "lines": [
                {
                    "id": "l1",
                    "description": "Service",
                    "quantity": "1",
                    "unit_price": "100.00",
                    "amount": "100.00",
                }
            ],
            "payment_allocations": [
                {
                    "id": "a1",
                    "payment_id": "p1",
                    "invoice_id": "inv-1",
                    "amount": "50.00",
                }
            ],
        }
    )
    assert inv.id == "inv-1"
    assert inv.total == Decimal("107.50")
    assert len(inv.lines) == 1
    assert inv.lines[0].amount == Decimal("100.00")
    assert len(inv.allocations) == 1
    assert inv.allocations[0].amount == Decimal("50")


def test_parse_payment_effective_account_prefers_billing_account() -> None:
    client = DotmacSubClient(DotmacSubConfig(api_url="x", api_token="t"))
    pay = client._parse_payment(
        {
            "id": "p1",
            "account_id": "acc-a",
            "billing_account_id": "acc-b",
            "amount": "200.00",
            "currency": "NGN",
            "status": "succeeded",
            "allocations": [],
        }
    )
    assert pay.effective_account_id == "acc-b"
    assert pay.amount == Decimal("200.00")


def test_parse_payment_reads_refunded_amount() -> None:
    client = DotmacSubClient(DotmacSubConfig(api_url="x", api_token="t"))
    base = {"id": "p1", "amount": "200.00", "currency": "NGN", "status": "succeeded"}

    pay = client._parse_payment({**base, "refunded_amount": "75.00"})
    assert pay.refunded_amount == Decimal("75.00")
    assert pay.amount - pay.refunded_amount == Decimal("125.00")  # net cash

    # Absent (dotmac_sub not yet deployed) → 0, so ERP posts the full gross.
    assert client._parse_payment(base).refunded_amount == Decimal("0")


def test_parse_payment_reads_proof_backed_wht_contract() -> None:
    client = DotmacSubClient(DotmacSubConfig(api_url="x", api_token="t"))

    payment = client._parse_payment(
        {
            "id": "p-wht",
            "amount": "107.50",
            "gross_amount": "107.50",
            "net_amount": "100.00",
            "wht_amount": "7.50",
            "wht_rate": "7.5",
            "wht_status": "certified",
            "wht_record_id": "wht-1",
            "wht_certificate_reference": "CERT-42",
            "wht_resolved_at": "2026-07-03T09:00:00+00:00",
            "currency": "NGN",
            "status": "succeeded",
        }
    )

    assert payment.gross_amount == Decimal("107.50")
    assert payment.net_amount == Decimal("100.00")
    assert payment.wht_amount == Decimal("7.50")
    assert payment.wht_rate == Decimal("7.5")
    assert payment.wht_status == "certified"
    assert payment.wht_certificate_reference == "CERT-42"
    assert payment.wht_resolved_at == datetime(2026, 7, 3, 9, tzinfo=timezone.utc)


def test_subscriber_full_name_prefers_display_then_company_then_parts() -> None:
    client = DotmacSubClient(DotmacSubConfig(api_url="x", api_token="t"))
    assert (
        client._parse_subscriber({"id": "s", "display_name": "Acme"}).full_name
        == "Acme"
    )
    assert (
        client._parse_subscriber({"id": "s", "company_name": "Beta Ltd"}).full_name
        == "Beta Ltd"
    )
    assert (
        client._parse_subscriber(
            {"id": "s", "first_name": "Ada", "last_name": "Lovelace"}
        ).full_name
        == "Ada Lovelace"
    )


# ---------------------------------------------------------------------------
# Invoice status mapping (dotmac_sub → ERP)
# ---------------------------------------------------------------------------


def test_map_invoice_status() -> None:
    from app.models.finance.ar.invoice import InvoiceStatus
    from app.services.dotmac_sub.sync._base import BaseSyncMixin

    m = BaseSyncMixin._map_invoice_status
    fake = object()  # self is unused by the method
    assert m(fake, "paid", Decimal("0")) == InvoiceStatus.PAID
    assert m(fake, "issued", Decimal("0")) == InvoiceStatus.PAID  # zero balance → paid
    assert m(fake, "issued", Decimal("10")) == InvoiceStatus.POSTED
    assert m(fake, "partially_paid", Decimal("5")) == InvoiceStatus.PARTIALLY_PAID
    assert m(fake, "void", Decimal("10")) == InvoiceStatus.VOID
    # A voided invoice usually carries a zero balance: void must win over the
    # zero-balance → PAID shortcut (regression guard).
    assert m(fake, "void", Decimal("0")) == InvoiceStatus.VOID
    assert m(fake, "overdue", Decimal("10")) == InvoiceStatus.POSTED


# ---------------------------------------------------------------------------
# Webhook HMAC verification + entity-id extraction
# ---------------------------------------------------------------------------


def test_verify_webhook_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    import hashlib
    import hmac

    from app.api import dotmac_sub as api

    secret = "shhh"
    monkeypatch.setattr(
        api.settings, "dotmac_sub_webhook_secret", secret, raising=False
    )
    body = b'{"event_type":"invoice.paid","data":{"id":"inv-1"}}'
    good = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    assert api.verify_dotmac_sub_signature(body, good)
    assert api.verify_dotmac_sub_signature(body, f"sha256={good}")  # prefixed
    assert not api.verify_dotmac_sub_signature(body, "deadbeef")


def test_verify_webhook_signature_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api import dotmac_sub as api

    monkeypatch.setattr(api.settings, "dotmac_sub_webhook_secret", None, raising=False)
    assert not api.verify_dotmac_sub_signature(b"x", "anything")


def test_webhook_entity_id_extraction() -> None:
    from app.services.dotmac_sub.webhook_dispatch import _entity_id

    # Real dotmac_sub envelope: the entity is in ``payload``, its ids echoed in
    # ``context``. Payments carry the id in payload.payment_id (no context id);
    # invoices/subscribers carry it in context.<domain>_id.
    payment_evt = {
        "event_type": "payment.received",
        "payload": {"payment_id": "pay-9", "amount": "100"},
        "context": {"account_id": "acc-1", "invoice_id": "inv-3"},
    }
    assert _entity_id(payment_evt, "payment") == "pay-9"

    invoice_evt = {
        "event_type": "invoice.paid",
        "payload": {"total": "100"},
        "context": {"invoice_id": "inv-3", "account_id": "acc-1"},
    }
    assert _entity_id(invoice_evt, "invoice") == "inv-3"

    subscriber_evt = {
        "event_type": "subscriber.updated",
        "payload": {"id": "sub-7"},
        "context": {"subscriber_id": "sub-7"},
    }
    assert _entity_id(subscriber_evt, "subscriber") == "sub-7"

    # Legacy / flat-body fallback still resolves.
    assert _entity_id({"data": {"id": "abc"}}, "invoice") == "abc"
    assert _entity_id({"id": "xyz"}, "payment") == "xyz"
    assert _entity_id({"event_type": "x"}, "payment") is None


def test_dispatch_webhook_routes_real_payment_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end shape check: a real dotmac_sub payment envelope reaches
    sync_payment_by_id with the id from payload.payment_id (regression for the
    dead-webhook contract mismatch)."""
    import uuid

    from app.services.dotmac_sub import webhook_dispatch as wd
    from app.services.dotmac_sub.sync._types import SyncResult

    calls: list[tuple[str, str]] = []

    class _FakeSyncService:
        def __init__(self, **_kw):
            pass

        def sync_payment_by_id(self, entity_id, _user):
            calls.append(("payment", entity_id))
            return SyncResult(success=True, entity_type="payments", created=1)

        def sync_invoice_by_id(self, entity_id, _user):
            calls.append(("invoice", entity_id))
            return SyncResult(success=True, entity_type="invoices")

        def sync_subscriber_by_id(self, entity_id, _user):
            calls.append(("subscriber", entity_id))
            return SyncResult(success=True, entity_type="subscribers")

        def close(self):
            pass

    monkeypatch.setattr(wd, "DotmacSubSyncService", _FakeSyncService)
    monkeypatch.setattr(
        "app.tasks.dotmac_sub._resolve_ar_control_account",
        lambda *_a, **_k: uuid.uuid4(),
    )
    monkeypatch.setattr(
        "app.tasks.dotmac_sub._resolve_default_revenue_account",
        lambda *_a, **_k: uuid.uuid4(),
    )

    envelope = {
        "event_id": "evt-1",
        "event_type": "payment.received",
        "occurred_at": "2026-07-05T00:00:00+00:00",
        "payload": {"payment_id": "pay-42", "amount": "250.00"},
        "context": {"account_id": "acc-9", "invoice_id": "inv-7"},
    }
    result = wd.dispatch_webhook(None, uuid.uuid4(), "payment.received", envelope)

    assert calls == [("payment", "pay-42")]
    assert result["status"] == "ok"
    assert result["entity_id"] == "pay-42"


# ---------------------------------------------------------------------------
# Payment FX conversion (foreign currency → functional)
# ---------------------------------------------------------------------------


class _FakePaymentSvc:
    """Minimal stand-in exposing the attributes _functional_amount reads."""

    def __init__(self) -> None:
        from uuid import uuid4

        self.db = MagicMock()
        self.organization_id = uuid4()


def test_functional_amount_converts_using_inverse_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A USD payment must be converted to functional via currency→functional."""
    from datetime import date

    from app.services.dotmac_sub.sync._base import BaseSyncMixin
    from app.services.finance.platform import fx as fx_module

    # lookup_spot_rate returns inverse_rate = currency_code → functional.
    # 1 USD = 1500 NGN, so functional(NGN) = amount(USD) * 1500.
    monkeypatch.setattr(
        fx_module.FXService,
        "lookup_spot_rate",
        staticmethod(lambda *a, **k: {"rate": "0.000667", "inverse_rate": "1500"}),
    )

    rate, functional = BaseSyncMixin._functional_amount(
        _FakePaymentSvc(), Decimal("74.87"), "USD", date(2026, 6, 1)
    )
    assert rate == Decimal("1500")
    assert functional == Decimal("112305.000000")  # 74.87 * 1500


def test_functional_amount_falls_back_to_one_when_no_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing rate must degrade to 1.0 (no conversion), never raise."""
    from datetime import date

    from app.services.dotmac_sub.sync._base import BaseSyncMixin
    from app.services.finance.platform import fx as fx_module

    monkeypatch.setattr(
        fx_module.FXService,
        "lookup_spot_rate",
        staticmethod(lambda *a, **k: {"rate": None, "message": "no rate"}),
    )

    rate, functional = BaseSyncMixin._functional_amount(
        _FakePaymentSvc(), Decimal("100"), "USD", date(2026, 6, 1)
    )
    assert rate == Decimal("1")
    assert functional == Decimal("100")


def test_api_key_required_no_staff_login() -> None:
    """Staff-credential login is retired (audit S1): a client with no api_token
    raises loudly instead of scraping the login page with a staff password."""
    from app.services.dotmac_sub.client import (
        DotmacSubAuthenticationError,
        DotmacSubClient,
        DotmacSubConfig,
    )

    client = DotmacSubClient(DotmacSubConfig(api_url="https://x", api_token=""))
    with pytest.raises(DotmacSubAuthenticationError):
        client._api_key()

    # With a key it returns it directly (no login round-trip).
    ok = DotmacSubClient(DotmacSubConfig(api_url="https://x", api_token="svc-key"))
    assert ok._api_key() == "svc-key"


def test_request_sends_api_key_header_not_bearer() -> None:
    """The wire contract: X-Api-Key carries the key; no Authorization header.
    dotmac_sub's Bearer path only accepts session-bound login JWTs, so sending
    the key as Bearer authenticates nothing (401)."""
    import httpx as _httpx

    from app.services.dotmac_sub.client import DotmacSubClient, DotmacSubConfig

    seen: dict[str, str] = {}

    def handler(request: _httpx.Request) -> _httpx.Response:
        seen["x-api-key"] = request.headers.get("X-Api-Key", "")
        seen["authorization"] = request.headers.get("Authorization", "")
        return _httpx.Response(200, json={"items": [], "count": 0})

    client = DotmacSubClient(DotmacSubConfig(api_url="https://x", api_token="svc-key"))
    client._client = _httpx.Client(
        base_url="https://x/api/v1", transport=_httpx.MockTransport(handler)
    )
    client._request("GET", "/subscribers")

    assert seen["x-api-key"] == "svc-key"
    assert seen["authorization"] == ""


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (
            422,
            "Department is not mapped in Self-Care. Map this ERP department first.",
        ),
        (
            409,
            "ERP employee is already linked to a different Self-Care user.",
        ),
    ],
)
def test_department_sync_permanent_errors_are_clear(
    status_code: int, expected: str
) -> None:
    import httpx as _httpx

    from app.services.dotmac_sub.client import (
        DotmacSubClient,
        DotmacSubConfig,
        DotmacSubPermanentSyncError,
    )

    client = DotmacSubClient(DotmacSubConfig(api_url="https://x", api_token="svc-key"))
    response = _httpx.Response(status_code, json={"detail": "Self-Care detail"})

    with pytest.raises(DotmacSubPermanentSyncError, match=expected):
        client._handle_response(
            response, endpoint="/staff-accounts/acc-9/erp-department"
        )


def test_department_sync_forbidden_error_identifies_missing_scope() -> None:
    import httpx as _httpx

    from app.services.dotmac_sub.client import (
        DotmacSubClient,
        DotmacSubConfig,
        DotmacSubPermanentSyncError,
    )

    client = DotmacSubClient(DotmacSubConfig(api_url="https://x", api_token="svc-key"))
    response = _httpx.Response(403, json={"detail": "forbidden"})

    with pytest.raises(
        DotmacSubPermanentSyncError,
        match="operations:service_team:membership",
    ):
        client._handle_response(
            response, endpoint="/staff-accounts/acc-9/erp-department"
        )


def test_staff_role_rejection_points_to_selfcare_role_catalog() -> None:
    import httpx as _httpx

    from app.services.dotmac_sub.client import (
        DotmacSubClient,
        DotmacSubConfig,
        DotmacSubPermanentSyncError,
    )

    client = DotmacSubClient(DotmacSubConfig(api_url="https://x", api_token="svc-key"))
    response = _httpx.Response(
        422,
        json={
            "detail": {
                "code": "auth.staff_provisioning.unknown_roles",
                "message": "One or more requested roles are not active.",
                "details": {"role_names": ["field_technician"]},
            }
        },
    )

    with pytest.raises(
        DotmacSubPermanentSyncError,
        match="dotmac_sub_roles exist and are active",
    ):
        client._handle_response(response, endpoint="/staff-accounts/acc-9/roles")


def test_lock_dotmac_sub_customer_issues_advisory_lock_on_postgres() -> None:
    """RC1/I-5: the customer upsert serializes on (org, dotmac_sub_id) so the
    batch sync and the on-demand resolve can't both create a customer."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from app.services.dotmac_sub.sync._base import BaseSyncMixin

    db = MagicMock()
    db.get_bind.return_value.dialect.name = "postgresql"
    fake = SimpleNamespace(db=db, organization_id="org-1")

    db.execute.return_value.scalar.return_value = True

    BaseSyncMixin._lock_dotmac_sub_customer(fake, "sub-9")

    assert db.execute.call_count == 1
    sql, params = db.execute.call_args.args
    assert "pg_try_advisory_xact_lock" in str(sql)
    assert params == {"key": "erp_customer:org-1:sub-9"}


def test_lock_dotmac_sub_customer_noop_off_postgres_or_without_id() -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from app.services.dotmac_sub.sync._base import BaseSyncMixin

    sqlite_db = MagicMock()
    sqlite_db.get_bind.return_value.dialect.name = "sqlite"
    BaseSyncMixin._lock_dotmac_sub_customer(
        SimpleNamespace(db=sqlite_db, organization_id="o"), "sub-9"
    )
    sqlite_db.execute.assert_not_called()

    pg_db = MagicMock()
    pg_db.get_bind.return_value.dialect.name = "postgresql"
    BaseSyncMixin._lock_dotmac_sub_customer(
        SimpleNamespace(db=pg_db, organization_id="o"), ""
    )
    pg_db.execute.assert_not_called()


def test_lock_dotmac_sub_customer_never_blocks_and_defers_on_contention(
    monkeypatch,
) -> None:
    """Deadlock fix (2026-07-18): batch transactions hold every acquired xact
    lock until the outer commit, so invoice sync and subscriber sync
    accumulating the same per-subscriber locks in different orders formed a
    wait cycle. The acquire must poll pg_try_advisory_xact_lock (never a
    blocking pg_advisory_xact_lock) and give up after the bounded budget so
    only the contended entity is deferred."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import pytest

    from app.services.dotmac_sub.sync import _base
    from app.services.dotmac_sub.sync._base import (
        BaseSyncMixin,
        CustomerLockContentionError,
    )

    sleeps: list[float] = []
    monkeypatch.setattr(_base.time, "sleep", sleeps.append)

    db = MagicMock()
    db.get_bind.return_value.dialect.name = "postgresql"
    db.execute.return_value.scalar.return_value = False
    fake = SimpleNamespace(db=db, organization_id="org-1")

    with pytest.raises(CustomerLockContentionError):
        BaseSyncMixin._lock_dotmac_sub_customer(fake, "sub-9")

    assert db.execute.call_count == _base._CUSTOMER_LOCK_ATTEMPTS
    assert len(sleeps) == _base._CUSTOMER_LOCK_ATTEMPTS - 1
    for call in db.execute.call_args_list:
        assert "pg_try_advisory_xact_lock" in str(call.args[0])
        assert "pg_advisory_xact_lock(" not in str(call.args[0]).replace(
            "pg_try_advisory_xact_lock(", ""
        )


def test_lock_dotmac_sub_customer_acquires_after_transient_contention(
    monkeypatch,
) -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from app.services.dotmac_sub.sync import _base
    from app.services.dotmac_sub.sync._base import BaseSyncMixin

    monkeypatch.setattr(_base.time, "sleep", lambda _s: None)

    db = MagicMock()
    db.get_bind.return_value.dialect.name = "postgresql"
    db.execute.return_value.scalar.side_effect = [False, False, True]
    fake = SimpleNamespace(db=db, organization_id="org-1")

    BaseSyncMixin._lock_dotmac_sub_customer(fake, "sub-9")

    assert db.execute.call_count == 3


def test_customer_code_fits_column_limit() -> None:
    """ar.customer.customer_code is VARCHAR(30): an untruncated
    "DSUB-R-<uuid>" (43 chars) failed every reseller INSERT on the first
    prod sync. UUID refs compact to dash-less hex before truncating;
    short account numbers pass through untouched."""
    from app.services.dotmac_sub.sync._base import BaseSyncMixin

    mixin = BaseSyncMixin.__new__(BaseSyncMixin)
    uuid_ref = "f81e6646-3a7a-41b3-b600-0934abf17330"

    reseller_code = mixin._customer_code("R", uuid_ref)
    assert reseller_code == "DSUB-R-f81e66463a7a41b3b600093"
    assert len(reseller_code) <= 30

    # Distinct UUIDs stay distinct after compaction+truncation.
    other = mixin._customer_code("R", "f840450e-7e2a-4eb4-af21-e1cb3353f5e8")
    assert other != reseller_code

    # Short human account numbers are preserved verbatim.
    assert mixin._customer_code("", "ACC-10042") == "DSUB-ACC-10042"

    # Subscriber UUID fallback also fits.
    assert len(mixin._customer_code("", uuid_ref)) <= 30
