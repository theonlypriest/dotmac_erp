"""Contract coverage for subscriber identity embedded in invoice sync rows."""

from decimal import Decimal

from app.services.dotmac_sub.client import DotmacSubClient, DotmacSubConfig


def test_parse_invoice_admits_embedded_subscriber_identity() -> None:
    client = DotmacSubClient(DotmacSubConfig(api_url="x", api_token="t"))

    invoice = client._parse_invoice(
        {
            "id": "inv-1",
            "account_id": "acc-1",
            "currency": "NGN",
            "subtotal": "100.00",
            "tax_total": "0.00",
            "total": "100.00",
            "balance_due": "100.00",
            "updated_at": "2026-02-01T12:00:00Z",
            "account": {
                "id": "acc-1",
                "display_name": "Example Subscriber",
                "updated_at": "2026-02-01T12:00:00Z",
            },
        }
    )

    assert invoice.total == Decimal("100.00")
    assert invoice.account is not None
    assert invoice.account.id == "acc-1"
    assert invoice.account.display_name == "Example Subscriber"
