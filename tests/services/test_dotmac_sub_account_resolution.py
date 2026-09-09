"""Regression tests for dotmac_sub account-owner resolution throttling.

Invoices/payments/credit-notes resolve their ``account_id`` to an ERP customer
via ``BaseSyncMixin._get_customer_for_account``. Unresolvable accounts (orphaned
in dotmac_sub, or throttled by its rate limiter) previously hit the dotmac_sub
API on *every* reference, every run — server-158 is dotmac_sub's hardcoded
offender IP (10 req/60s), so ~82% of those calls were 429s that the client then
retried, a self-perpetuating storm. These tests pin the two fixes:

1. an unresolvable account is not re-fetched again within the same run, and
2. a rate-limited subscriber lookup does not fall through to the
   billing-account lookup (which would just be throttled too).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from app.services.dotmac_sub.client import (
    DotmacSubNotFoundError,
    DotmacSubRateLimitError,
    SubscriberRecord,
)
from app.services.dotmac_sub.sync._base import BaseSyncMixin


class _AccountResolveHarness(BaseSyncMixin):
    """Minimal harness exercising only the account-resolution path (no DB)."""

    def __init__(self) -> None:
        self._account_cache = {}
        self._unresolvable_accounts = set()
        # ``client`` is a read-only property returning ``self._client``.
        self._client = MagicMock()

    # DB-backed lookups are stubbed to "nothing synced yet" so resolution
    # always falls through to the dotmac_sub client.
    def _get_synced_entity(self, _entity_type, _external_id):
        return None

    def _get_customer_by_dotmac_sub_id(self, _dotmac_sub_id):
        return None

    def _record_sync(self, *_args, **_kwargs):
        return None


def test_unresolvable_account_is_not_refetched_within_a_run() -> None:
    harness = _AccountResolveHarness()
    harness.client.get_subscriber.side_effect = DotmacSubNotFoundError(
        "no such subscriber", status_code=404
    )
    harness.client.get_billing_account.side_effect = DotmacSubNotFoundError(
        "no such billing account", status_code=404
    )

    # First reference resolves to None and both endpoints are tried once.
    assert harness._get_customer_for_account("acct-1") is None
    assert harness.client.get_subscriber.call_count == 1
    assert harness.client.get_billing_account.call_count == 1

    # Every subsequent reference to the same account this run is short-circuited
    # — no further dotmac_sub calls (previously each reference re-fetched).
    assert harness._get_customer_for_account("acct-1") is None
    assert harness._get_customer_for_account("acct-1") is None
    assert harness.client.get_subscriber.call_count == 1
    assert harness.client.get_billing_account.call_count == 1


def test_rate_limited_subscriber_lookup_skips_billing_account_fallback() -> None:
    harness = _AccountResolveHarness()
    harness.client.get_subscriber.side_effect = DotmacSubRateLimitError(
        "rate limited", status_code=429, retry_after=30.0
    )

    # A 429 is transient, not "not found": don't also hit the (equally throttled)
    # billing-account endpoint; leave the account for the next run.
    assert harness._get_customer_for_account("acct-2") is None
    assert harness.client.get_subscriber.call_count == 1
    harness.client.get_billing_account.assert_not_called()


def test_embedded_subscriber_resolves_without_detail_request() -> None:
    harness = _AccountResolveHarness()
    customer = MagicMock(customer_id="customer-1")
    harness._get_customer_by_dotmac_sub_id = MagicMock(side_effect=[None, customer])
    harness._sync_single_subscriber = MagicMock()
    harness._cache_reseller = MagicMock()
    subscriber = SubscriberRecord(
        id="acct-3",
        display_name="Embedded Subscriber",
        reseller_id="reseller-1",
    )

    assert harness._resolve_account_owner("acct-3", subscriber) == "customer-1"

    harness.client.get_subscriber.assert_not_called()
    harness.client.get_billing_account.assert_not_called()
    harness._cache_reseller.assert_called_once_with("reseller-1")
    harness._sync_single_subscriber.assert_called_once()
