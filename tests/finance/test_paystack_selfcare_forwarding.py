import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException

from app.api.finance.payments import paystack_webhook
from app.services.dotmac_sub.client import (
    DotmacSubClient,
    DotmacSubConfig,
    DotmacSubError,
    DotmacSubRateLimitError,
)
from app.services.finance.payments.paystack_client import PaystackConfig


def _body(reference: str) -> bytes:
    return json.dumps(
        {"event": "charge.success", "data": {"reference": reference}},
        separators=(",", ":"),
    ).encode()


def _signature(body: bytes) -> str:
    return hmac.new(b"webhook-secret", body, hashlib.sha512).hexdigest()


def _request(body: bytes) -> MagicMock:
    request = MagicMock()
    request.body = AsyncMock(return_value=body)
    request.json = AsyncMock(return_value=json.loads(body))
    return request


def _paystack_config() -> PaystackConfig:
    return PaystackConfig("webhook-secret", "public-key", "webhook-secret")


def _sub_config() -> MagicMock:
    config = MagicMock()
    config.is_configured.return_value = True
    return config


@pytest.mark.asyncio
async def test_verified_selfcare_charge_relays_exact_signed_bytes():
    body = _body("DMAC-SELFCARE-1")
    sub_client = MagicMock()

    with (
        patch(
            "app.api.finance.payments.PaymentService.get_intent_by_reference",
            return_value=None,
        ),
        patch("app.api.finance.payments.settings") as settings,
        patch(
            "app.api.finance.payments.get_paystack_config",
            return_value=_paystack_config(),
        ),
        patch("app.api.finance.payments.prime_session"),
        patch("app.api.finance.payments.set_payment_tenant_context"),
        patch(
            "app.services.dotmac_sub.DotmacSubConfig.for_org",
            return_value=_sub_config(),
        ),
        patch("app.services.dotmac_sub.DotmacSubClient", return_value=sub_client),
    ):
        settings.default_organization_id = "89ed80a7-7f7e-4f27-8794-cf260d985542"
        response = await paystack_webhook(
            request=_request(body),
            x_paystack_signature=_signature(body),
            db=MagicMock(),
        )

    assert response.status == "forwarded"
    sub_client.relay_paystack_webhook.assert_called_once_with(
        raw_payload=body,
        signature=_signature(body),
    )


@pytest.mark.asyncio
async def test_invalid_signature_is_rejected_before_relay():
    body = _body("DMAC-SELFCARE-2")
    sub_client = MagicMock()

    with (
        patch(
            "app.api.finance.payments.PaymentService.get_intent_by_reference",
            return_value=None,
        ),
        patch("app.api.finance.payments.settings") as settings,
        patch(
            "app.api.finance.payments.get_paystack_config",
            return_value=_paystack_config(),
        ),
        patch("app.api.finance.payments.prime_session"),
        patch("app.api.finance.payments.set_payment_tenant_context"),
        patch("app.services.dotmac_sub.DotmacSubClient", return_value=sub_client),
        pytest.raises(HTTPException) as exc_info,
    ):
        settings.default_organization_id = "89ed80a7-7f7e-4f27-8794-cf260d985542"
        await paystack_webhook(
            request=_request(body),
            x_paystack_signature="invalid-signature",
            db=MagicMock(),
        )

    assert exc_info.value.status_code == 401
    sub_client.relay_paystack_webhook.assert_not_called()


@pytest.mark.asyncio
async def test_verified_non_selfcare_reference_is_not_relayed():
    body = _body("UNKNOWN-REFERENCE")
    sub_client = MagicMock()

    with (
        patch(
            "app.api.finance.payments.PaymentService.get_intent_by_reference",
            return_value=None,
        ),
        patch("app.api.finance.payments.settings") as settings,
        patch(
            "app.api.finance.payments.get_paystack_config",
            return_value=_paystack_config(),
        ),
        patch("app.api.finance.payments.prime_session"),
        patch("app.api.finance.payments.set_payment_tenant_context"),
        patch("app.services.dotmac_sub.DotmacSubClient", return_value=sub_client),
    ):
        settings.default_organization_id = "89ed80a7-7f7e-4f27-8794-cf260d985542"
        response = await paystack_webhook(
            request=_request(body),
            x_paystack_signature=_signature(body),
            db=MagicMock(),
        )

    assert response.status == "ignored"
    sub_client.relay_paystack_webhook.assert_not_called()


@pytest.mark.asyncio
async def test_selfcare_delivery_failure_returns_retryable_status():
    body = _body("DMAC-SELFCARE-3")
    sub_client = MagicMock()
    sub_client.relay_paystack_webhook.side_effect = DotmacSubError("unavailable")

    with (
        patch(
            "app.api.finance.payments.PaymentService.get_intent_by_reference",
            return_value=None,
        ),
        patch("app.api.finance.payments.settings") as settings,
        patch(
            "app.api.finance.payments.get_paystack_config",
            return_value=_paystack_config(),
        ),
        patch("app.api.finance.payments.prime_session"),
        patch("app.api.finance.payments.set_payment_tenant_context"),
        patch(
            "app.services.dotmac_sub.DotmacSubConfig.for_org",
            return_value=_sub_config(),
        ),
        patch("app.services.dotmac_sub.DotmacSubClient", return_value=sub_client),
        pytest.raises(HTTPException) as exc_info,
    ):
        settings.default_organization_id = "89ed80a7-7f7e-4f27-8794-cf260d985542"
        await paystack_webhook(
            request=_request(body),
            x_paystack_signature=_signature(body),
            db=MagicMock(),
        )

    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_selfcare_rate_limit_propagates_retry_after():
    body = _body("DMAC-SELFCARE-RATE")
    sub_client = MagicMock()
    sub_client.relay_paystack_webhook.side_effect = DotmacSubRateLimitError(
        "limited", status_code=429, retry_after=17
    )

    with (
        patch(
            "app.api.finance.payments.PaymentService.get_intent_by_reference",
            return_value=None,
        ),
        patch("app.api.finance.payments.settings") as settings,
        patch(
            "app.api.finance.payments.get_paystack_config",
            return_value=_paystack_config(),
        ),
        patch("app.api.finance.payments.prime_session"),
        patch("app.api.finance.payments.set_payment_tenant_context"),
        patch(
            "app.services.dotmac_sub.DotmacSubConfig.for_org",
            return_value=_sub_config(),
        ),
        patch("app.services.dotmac_sub.DotmacSubClient", return_value=sub_client),
        pytest.raises(HTTPException) as exc_info,
    ):
        settings.default_organization_id = "89ed80a7-7f7e-4f27-8794-cf260d985542"
        await paystack_webhook(
            request=_request(body),
            x_paystack_signature=_signature(body),
            db=MagicMock(),
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.headers == {"Retry-After": "17"}


def test_relay_client_preserves_body_and_signature():
    body = _body("DMAC-SELFCARE-4")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.content == body
        assert request.headers["X-Paystack-Signature"] == _signature(body)
        return httpx.Response(200, json={"status": "processed"})

    client = DotmacSubClient(
        DotmacSubConfig(api_url="https://selfcare.example", api_token="key")
    )
    client._client = httpx.Client(
        base_url="https://selfcare.example/api/v1",
        transport=httpx.MockTransport(handler),
    )

    assert client.relay_paystack_webhook(
        raw_payload=body,
        signature=_signature(body),
    ) == {"status": "processed"}


def test_relay_client_classifies_rate_limit_and_caps_retry_after():
    body = _body("DMAC-SELFCARE-5")

    client = DotmacSubClient(
        DotmacSubConfig(api_url="https://selfcare.example", api_token="key")
    )
    client._client = httpx.Client(
        base_url="https://selfcare.example/api/v1",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                429, headers={"Retry-After": "3600"}, json={"detail": "limited"}
            )
        ),
    )

    with pytest.raises(DotmacSubRateLimitError) as exc_info:
        client.relay_paystack_webhook(
            raw_payload=body,
            signature=_signature(body),
        )

    assert exc_info.value.retry_after == 60.0
