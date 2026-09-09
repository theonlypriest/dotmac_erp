"""Push active customers to Paystack, creating or updating each.

The owner of an operation that lived in
`scripts/sync_customers_to_paystack.py`, which carried the usual hardcoded
`ORG_ID` and raw `SessionLocal()`.

Idempotent by construction: each customer is looked up in Paystack by email
first, then updated or created. Re-running converges rather than duplicating,
which is what makes it safe to schedule at all.

## What it costs

Two network round-trips per customer — a lookup, then a write. That is the
existing behaviour and is preserved deliberately rather than optimised here:
batching would change which customers end up synced when a call fails
midway, and that is a behaviour decision, not a refactor. It is worth knowing
before scheduling this against a large customer base.

## Contact details come from a JSON blob

`Customer.primary_contact` is a JSONB column, so email and phone are dict
lookups that can legitimately be absent. A customer with no email is SKIPPED,
not failed: Paystack keys customers on email, so there is nothing to create.

Opens no session, sets no scope, never commits — the caller owns the
transaction. The Paystack client is opened per call and closed with it.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.finance.ar.customer import Customer

logger = logging.getLogger(__name__)


@dataclass
class PaystackSyncResult:
    total: int = 0
    created: int = 0
    updated: int = 0
    skipped_no_email: int = 0
    errors: list[str] = field(default_factory=list)


def customer_email(customer: Customer) -> str | None:
    """Email from the primary_contact JSON blob, if present."""
    contact = customer.primary_contact
    if isinstance(contact, dict):
        return contact.get("email")
    return None


def customer_phone(customer: Customer) -> str | None:
    contact = customer.primary_contact
    if isinstance(contact, dict):
        return contact.get("phone")
    return None


def split_name(legal_name: str) -> tuple[str, str]:
    """Split a legal name into (first, rest).

    Deliberately naive — one split on the first run of whitespace, everything
    else becomes the surname. Paystack wants two fields and a legal name is
    not reliably two words, so this is a presentation compromise rather than
    an attempt to parse names. An empty or whitespace-only name yields
    ("", ""), which the caller passes through unchanged.
    """
    parts = (legal_name or "").strip().split(maxsplit=1)
    return (parts[0] if parts else "", parts[1] if len(parts) > 1 else "")


def active_customers(db: Session, *, organization_id: uuid.UUID) -> list[Customer]:
    return list(
        db.scalars(
            select(Customer)
            .where(
                Customer.organization_id == organization_id,
                Customer.is_active.is_(True),
            )
            .order_by(Customer.customer_code)
        ).all()
    )


def _metadata(customer: Customer) -> dict[str, str | None]:
    """What we attach to the Paystack record so it can be traced back here."""
    meta: dict[str, str | None] = {
        "customer_id": str(customer.customer_id),
        "customer_code": customer.customer_code,
        "legal_name": customer.legal_name,
        "customer_type": (
            customer.customer_type.value if customer.customer_type else None
        ),
    }
    if customer.trading_name:
        meta["trading_name"] = customer.trading_name
    return meta


def sync_customers(
    db: Session,
    *,
    organization_id: uuid.UUID,
    dry_run: bool = True,
) -> PaystackSyncResult:
    """Create or update every active customer in Paystack.

    Reads Paystack credentials through the settings resolver, so a deployment
    without them configured fails loudly here rather than sending requests
    with an empty key.
    """
    from app.services.finance.payments.paystack_client import (
        PaystackClient,
        PaystackConfig,
    )
    from app.services.settings_spec import SettingDomain, resolve_value

    secret_key = resolve_value(
        db,
        SettingDomain.payments,
        "paystack_secret_key",
        organization_id=organization_id,
    )
    public_key = resolve_value(
        db,
        SettingDomain.payments,
        "paystack_public_key",
        organization_id=organization_id,
    )
    if not secret_key:
        raise RuntimeError(
            "paystack_secret_key is not configured for this organization"
        )

    customers = active_customers(db, organization_id=organization_id)
    result = PaystackSyncResult(total=len(customers))

    config = PaystackConfig(
        secret_key=str(secret_key),
        public_key=str(public_key or ""),
        # Paystack signs webhooks with the secret key itself; this is not a
        # second credential being reused by accident.
        webhook_secret=str(secret_key),
    )

    with PaystackClient(config) as client:
        for customer in customers:
            email = customer_email(customer)
            if not email:
                # Paystack keys customers on email — there is nothing to
                # create, so this is a skip rather than a failure.
                result.skipped_no_email += 1
                continue

            if dry_run:
                result.created += 1
                continue

            first_name, last_name = split_name(customer.legal_name)
            try:
                existing = client.get_customer(email)
                if existing:
                    client.update_customer(
                        customer_code=existing.customer_code,
                        first_name=first_name,
                        last_name=last_name,
                        phone=customer_phone(customer),
                        metadata=_metadata(customer),
                    )
                    result.updated += 1
                else:
                    client.create_customer(
                        email=email,
                        first_name=first_name,
                        last_name=last_name,
                        phone=customer_phone(customer),
                        metadata=_metadata(customer),
                    )
                    result.created += 1
            except Exception as exc:
                result.errors.append(f"{customer.customer_code}: {exc}")
                logger.exception("Paystack sync failed for %s", customer.customer_code)

    return result
