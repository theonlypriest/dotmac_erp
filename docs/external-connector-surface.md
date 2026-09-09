# ERP direct external-connector surface

ERP adopts the Governance-owned schema-9 ratchet from accepted ADR 0011 at
immutable canonical-main commit
`a19259b10568d29dc0a9617347498fea7f1e7a97`.

The ratchet freezes measured legacy connector surface while providers move
behind Dotmac Integrator. It is transitional defence in depth, not runtime
isolation. The permanent boundary is Integrator-only connector packages and
provider secrets, default-deny product egress, provider ingress terminating at
Integrator, and versioned inbox/outbox contracts between independent apps.

ERP declares no measurement roots and copies no detector. The Governance
engine derives scope from Git-tracked Python, proves test-only reachability
centrally, and reports every untracked Python source as an error.

## Accepted baseline

Remeasured on 2026-08-25 in the direct-CRM retirement change with the pinned
schema-9 engine. The exact file inventory below and the profile baselines are
lowered together; nine conserved findings remain.

Reviewed again on 2026-08-29 for PR #416 after connector failure coverage
changed the source files containing three test-only conserved symbols. Their
fingerprints are re-declared below; no baseline or conserved finding was added
or removed.

| Category | Baseline |
| --- | ---: |
| `outbound_transport` | 20 |
| `webhook_surface` | 6 |
| `provider_credential` | 6 |
| `connector_task` | 11 |
| `sync_checkpoint` | 17 |
| `delivery_retry` | 4 |

### `outbound_transport` — 20 files

`app/dependency_health.py`, `app/monitoring.py`,
`app/services/careers/captcha.py`, `app/services/dotmac_sub/client.py`,
`app/services/email.py`,
`app/services/finance/automation/workflow.py`,
`app/services/finance/banking/mono_client.py`,
`app/services/finance/payments/paystack_client.py`,
`app/services/finance/platform/ecb_rate_fetcher.py`,
`app/services/hooks/registry.py`, `app/services/mailcow/cleanup_queue.py`,
`app/services/mailcow/client.py`, `app/services/nextcloud/client.py`,
`app/services/push.py`, `app/services/remita/client.py`,
`app/services/secrets.py`, `app/services/storage.py`, `app/tasks/email.py`,
`app/tasks/hooks.py`, and `tests/e2e/conftest.py`.

### `webhook_surface` — 6 files

`app/api/dotmac_academy.py`, `app/api/dotmac_sub.py`,
`app/api/finance/banking.py`, `app/api/finance/payments.py`,
`app/services/finance/banking/mono_client.py`, and
`app/services/finance/payments/paystack_client.py`.

### `provider_credential` — 6 files

`app/config.py`, `app/dependency_health.py`, `app/models/email_profile.py`,
`app/services/finance/settings_web.py`, `app/services/storage.py`, and
`tests/conftest.py`.

### `connector_task` — 11 files

`app/api/dotmac_sub.py`, `app/services/finance/banking/mono_sync.py`,
`app/services/people/hr/employees.py`, `app/tasks/dotmac_sub.py`,
`app/tasks/exchange_rates.py`,
`app/tasks/expense.py`, `app/tasks/finance.py`, `app/tasks/hr.py`,
`app/tasks/payments_sync.py`, `app/tasks/performance.py`, and
`app/tasks/staff_sync.py`.

### `sync_checkpoint` — 17 files

`app/models/finance/ar/customer_payment.py`,
`app/models/finance/ar/invoice.py`,
`app/models/finance/platform/event_handler_checkpoint.py`,
`app/models/inventory/material_request.py`, `app/models/mixins.py`,
`app/models/people/base.py`, `app/models/people/training/academy.py`,
`app/models/pm/time_entry.py`, `app/schemas/support.py`,
`app/services/dotmac_sub/sync/_credit_notes.py`,
`app/services/dotmac_sub/sync/_invoices.py`,
`app/services/dotmac_sub/sync/_payments.py`,
`app/services/dotmac_sub/sync/_progress.py`,
`app/services/dotmac_sub/sync/_resellers.py`,
`app/services/dotmac_sub/sync/_subscribers.py`,
`app/services/people/training/academy.py`, and
`app/services/sync/sub/expenses.py`.

### `delivery_retry` — 4 files

`app/dependency_health.py`, `app/services/dotmac_sub/client.py`,
`app/tasks/email.py`, and
`app/tasks/hooks.py`.

## Conserved findings

These are connector-shaped symbols removed by the central test-only
reachability proof. Recording them suppresses nothing and claims no file is
safe; it makes every subtraction from the measured universe reviewable.
`InsightEngine` is application code reached only by its excluded test at this
revision, so its two entries also preserve that stronger dead-code signal.

| Path | Symbol | Category | Fingerprint |
| --- | --- | --- | --- |
| `app/services/coach/insight_engine.py` | `InsightEngine` | `delivery_retry` | `f7f150d9fa6c5e2d3675f1d041c8b2bcc65068b1924504f577f6205207108ed0` |
| `app/services/coach/insight_engine.py` | `InsightEngine` | `outbound_transport` | `f7f150d9fa6c5e2d3675f1d041c8b2bcc65068b1924504f577f6205207108ed0` |
| `tests/services/test_dotmac_sub_incremental_sync.py` | `test_customer_feeds_forward_their_watermarks` | `sync_checkpoint` | `ced5e1214b2e8731e79f877ec620f2d3333f80244fc07284dabdf7aa91b99ef3` |
| `tests/services/test_dotmac_sub_sync.py` | `test_verify_webhook_signature` | `webhook_surface` | `18b2cccff0f9d19c688adbfe3714dfbcfb494ccf55fb00fea58047b2c0babd3d` |
| `tests/services/test_dotmac_sub_sync.py` | `test_verify_webhook_signature_unconfigured` | `webhook_surface` | `18b2cccff0f9d19c688adbfe3714dfbcfb494ccf55fb00fea58047b2c0babd3d` |
| `tests/services/test_hook_registry.py` | `TestHookRegistry` | `webhook_surface` | `eec0ca322ca2be69c1b0860550a85bf268ffef1ef7783869e0af2b9adabdc8f7` |
| `tests/services/test_mono_sync.py` | `test_verify_webhook_rejects_empty_secrets` | `webhook_surface` | `3db300e89674052e47d62060f10e90da27fa084ee070ba39df0a9a577d6bf628` |
| `tests/tasks/test_hooks_tasks.py` | `TestExecuteAsyncHook` | `delivery_retry` | `ced43bd99de9a2af6ac3d9d4be8e3bb3a254f17d4b7435bb8eedb1c29a399ff6` |
| `tests/test_email_services.py` | `TestSendEmail` | `outbound_transport` | `5691ef206ab4756c077e82cda323498492adcb4195916005f4eef6fe185868a8` |

## Review rule

A count rising fails. A count falling also fails until the profile and this
record are lowered in the same change. Every reduction must show deletion or a
cutover to a named connector distribution behind Dotmac Integrator.

The ratchet reaches its sunset only when all baselines and conserved findings
are zero and ADR 0011's runtime package, secret, egress, ingress, and contract
conditions hold simultaneously.
