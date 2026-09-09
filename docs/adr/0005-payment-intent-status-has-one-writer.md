# ADR-0005 — `PaymentIntent.status` has one writer

- **Status:** Accepted
- **Date:** 2026-08-24
- **Decider:** Michael
- **Domain:** `payment_execution` (new in
  `app/services/sot_relationships.py` and `docs/SOT_RELATIONSHIP_MAP.md`)
- **Supersedes:** nothing.

## Context

`payments.payment_intent.status` is the record of whether money left the
account. On `origin/main` it had three live writers.

1. **`PaymentService`** (`app/services/finance/payments/payment_service.py`) —
   the legitimate owner. Initiation, immediate success/failure, webhook
   completion, failure, polling and reversal all resolve here, and the
   completion path takes a `FOR UPDATE` lock precisely because the webhook and
   the poller race.

2. **`app/tasks/expense.py::poll_stuck_expense_transfers`** — a Celery beat job
   running every two minutes, with **no tests of any kind**. It selected intents
   under `cross_org_session`, then in a *different* session wrote
   `status = EXPIRED` to every id that select returned; promoted PENDING to
   PROCESSING inline; and, when its own poll-attempt budget ran out, wrote
   `status = FAILED` and a `poll_abandoned` gateway response.

3. **`BatchTransferService`** (`app/services/finance/payments/batch_transfer_service.py`)
   — dead code. Zero callers across `app/`, `tests/`, `scripts/` and `tools/`;
   not exported from its package `__init__.py`; zero tests. It nonetheless held
   a live `client.initiate_transfer` loop and wrote both `PENDING` at intent
   construction and `PROCESSING` after initiating, with no separation of duties
   (`approve_batch` takes an `approved_by_user_id` argument and checks no
   permission, and the same actor could create, submit, approve and process a
   batch of payouts).

The second writer is not a style problem. A scheduled job decides from a read
taken in another session, minutes and one network call earlier:

- the expiry pass wrote `EXPIRED` without looking at the row again, so a
  transfer initiated between the select and the write was stamped expired while
  the money was in flight — and an expired-looking intent is one nobody chases;
- the poll pass re-read intents into a session it then held for the whole
  tenant's batch, so SQLAlchemy served the identity-mapped copy. A row a
  webhook had settled to COMPLETED still read PENDING to the worker, which
  would write PROCESSING back over it and let the completion path re-post the
  reimbursement to the ledger.

## Decision

### 1. `PaymentService` is the sole writer of `PaymentIntent.status`

Every transition, from every trigger. Webhook receiver, HTTP routes and the
scheduled poller are adapters: they validate, authorize and delegate. No other
module assigns the column, and no other module constructs a `PaymentIntent`
with a `status`.

### 2. The poller is an adapter

`poll_stuck_expense_transfers` keeps only what a scheduler owns: enumerating
tenants, one `session_for_org` per tenant, and the aggregation of results into
its return dict. Selection predicates,
credential resolution, the PENDING-to-PROCESSING promotion, attempt counting
and the circuit breaker all moved to `PaymentService`
(`find_stale_pending_transfer_intents`, `find_stuck_transfer_intents`,
`resolve_transfer_polling_config`, `expire_stale_pending_transfer`,
`reconcile_stuck_transfer`).

### 3. A premise established in another session is re-proved before it is written

Both new entry points take the intent **by id**, load it `FOR UPDATE` with
`populate_existing=True`, and re-check the condition the selection was made
under. An intent whose state has moved is skipped and reported, never
overwritten. This is the corruption fix, not a tidiness measure.

### 4. `BatchTransferService` is deleted, its persistence is not

The service file is removed. `app/models/finance/payments/transfer_batch.py`,
the `payments.transfer_batch` / `payments.transfer_batch_item` tables and the
migration that created them **stay**: existing rows are payout history, and
`PaymentService._update_batch_item_status` still maintains them for any intent
that belongs to a batch. Deleting dead service code is not a destructive
migration and this ADR does not authorize one.

If batch payouts are wanted again they are built on `PaymentService` with a
real approval check, not on a second service that initiates transfers and
decides intent statuses itself.

### 5. The rule is enforced, not stated

`tests/architecture/test_payment_intent_status_single_owner.py` fails the build
on any assignment of `PaymentIntentStatus.<MEMBER>` to a `.status` attribute,
or any `PaymentIntent(status=...)` construction, anywhere under `app/`,
`scripts/` or `tools/` outside the owner. It carries a two-sided sensitivity
proof: a planted violation must be detected, and the owner's own writes must
remain visible to the detector, so a renamed enum or a mis-globbed root fails
the build instead of passing over nothing.

## Consequences

- The poller gained tests for the first time
  (`tests/tasks/test_expense_transfer_polling.py`) and the owner gained direct
  coverage of the two new entry points.
- Two behaviours changed on purpose: an intent whose state moved is skipped
  rather than written, and a settled intent no longer burns a poll attempt. The
  job's result dict gains a `skipped` key, reported only when non-zero.
- The `FOR UPDATE` lock is taken per intent inside a session the worker commits
  once per tenant, so the row is locked for the remainder of that tenant's
  batch. That is not new contention: the job already wrote `poll_count` to
  every row it touched, and a write lock is held to commit either way.
- Nothing about production enablement is claimed here.
  `paystack_transfers_enabled` is runtime data on a deployment nobody has
  named. **Implemented and tested; production enablement unconfirmed.**

## Alternatives rejected

- **Leave the worker writing, and just add tests.** Tests around a second
  writer pin the second writer. The defect is that two places decide, and one
  of them decides from a stale read it never re-checks.
- **Keep `BatchTransferService` "in case batch payouts come back".** Dead code
  that initiates live transfers with no permission check is a standing
  liability, and its status writes would have to be exempted from the ratchet —
  an exemption whose premise ("nobody calls it") nothing enforces.
- **Route the poller's give-up path through `mark_transfer_failed`.** That
  method also reverts a PAID claim to APPROVED and replaces `gateway_response`.
  "We could not reach Paystack ten times" is not the same claim as "Paystack
  told us this failed", and conflating them in live money code inside a
  single-writer fix would change failure semantics under cover of a refactor.
  The give-up path keeps its own field writes, in the owner. Whether an
  unreachable transfer should be settled FAILED at all is left open, deliberately.
- **Unify the polling path's Paystack config with the API layer's.** The poller
  reads `paystack_webhook_secret` and tolerates its absence; the API builder
  substitutes the secret key. Reconciling those is a signature-verification
  decision and is not this change.
- **Have the worker fan out over the tenant catalogue instead of
  `cross_org_session`.** That is a real defect — a cross-tenant scan returns
  zero rows once the runtime stops being `postgres` — but it is already
  dispositioned as `retire_with_domain_cutover` in
  `docs/inventories/rls-cross-org-callers.tsv`, and changing the discovery
  shape here would mix two decisions in one change. *(Superseded by the
  amendment below, which is that separate change.)*

## Amendment — 2026-08-28: the selections run inside the tenant session

`find_stale_pending_transfer_intents` and `find_stuck_transfer_intents` were
written to run under `cross_org_session()` and return organization -> intent
ids, so the poller learnt which tenants had work before opening any tenant
session. `cross_org_session` lifts only the SQLAlchemy listener and never
PostgreSQL RLS: under `app_user` both selections return zero rows, no stale
payout expires, no stuck transfer is polled, and the job reports a clean run
every two minutes.

The poller now enumerates tenants through `for_each_organization` (the narrow
`tenant_catalog` definer) and runs each selection inside that tenant's own
session. Nothing about §1 or §3 changes: the selections are still
`PaymentService`'s, it is still the sole writer of the column, and each intent
is still taken by id and re-proved `FOR UPDATE` before it is written. Only the
session the selection runs on moved — the same predicate, evaluated where the
database can scope it.

The grouping is kept rather than flattened: inside a tenant session the mapping
has at most one key, and reading it back by the organization whose session the
worker holds is the isolation assertion.

`reconcile_unresolved_expense_transfers`, the hourly slow lane added by
ADR-0007, needed no conversion: it already discovers tenant identifiers through
`app.tenant_catalog.organization_ids` and reads every payout row inside
`session_for_org`, which is the same catalogue `for_each_organization` calls. It
keeps the two-pass spelling because it publishes the backlog-age gauge from the
first pass before any provider I/O, and collapsing that into one loop would make
an unconfigured tenant vanish from the metric it exists to raise.
