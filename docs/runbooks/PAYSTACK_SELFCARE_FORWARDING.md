# Paystack notification forwarding to Selfcare

ERP receives and validates the Paystack webhook because the merchant account's
webhook destination points to ERP. For a successful charge whose reference is
not an ERP payment intent and begins with Selfcare's canonical `DMAC-` prefix,
ERP relays the exact raw body and original Paystack signature to Selfcare's
existing `POST /api/v1/payment-events/paystack` endpoint.

Selfcare verifies Paystack's signature over the unchanged bytes, records the
verified receipt idempotently, and its canonical financial owners alone decide
payment creation, allocation, account credit, subscription, and access.

Requirements:

- ERP's Paystack configuration must use the same live merchant account.
- Selfcare and ERP must resolve the same live Paystack signing credential from
  their approved secret stores.
- Delivery failures return HTTP 503 so Paystack retries the original event.
- A Selfcare 429 is translated to 503 while preserving a bounded
  `Retry-After` value (maximum 60 seconds).
- Repeated delivery is safe because Selfcare's existing provider inbox is
  idempotent by Paystack event identity.
- The signed relay bypasses ERP's bulk-sync admission pool. Normal Dotmac Sub
  API traffic is capped per ERP worker by `DOTMAC_SUB_MAX_INFLIGHT_REQUESTS`
  (default `4`, bounded to `1..32`).
- Alert on `paystack_selfcare_relay_total` by outcome and use
  `paystack_selfcare_relay_duration_seconds` for latency. Structured relay logs
  include `event_type`, `payment_reference`, `relay_outcome`, and, for a 429,
  `retry_after_seconds`.

Selfcare's signed endpoint remains the financial owner. Its dedicated webhook
ingress policy must be deployed with this ERP change so generic API-sync abuse
controls cannot throttle the provider relay.
