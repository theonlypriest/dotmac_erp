# ADR-0007 — An unobserved transfer is not a failed one

- **Status:** Accepted
- **Date:** 2026-08-25
- **Decider:** Michael
- **Domain:** `payment_execution` (`app/services/sot_relationships.py`,
  `docs/SOT_RELATIONSHIP_MAP.md`)
- **Builds on:** ADR-0005 (`PaymentIntent.status` has one writer), which fixed
  who writes this column and deliberately left what it should say open.
- **Adopts:** `dotmac_starter_mt` ADR-0032, *Unobserved is UNKNOWN, never
  ABSENT* — the fleet-level rule. This ADR is ERP applying it to money
  movement; it does not restate or re-derive the principle.

## Context

ADR-0005 gave `payments.payment_intent.status` a single writer and stopped a
scheduled job deciding from a read taken in another session. It closed with an
open question, in `_record_transfer_poll_failure`'s own docstring:

> "we could not reach Paystack ten times" is not the same claim as "Paystack
> told us this failed". Whether an unreachable transfer should be settled as
> FAILED at all is a real question, and not one to answer inside a
> single-writer fix.

This is that answer.

### The vocabulary had no word for "we do not know"

`PaymentIntentStatus` had seven members and every one of them asserted a fact
about the money: it moved (COMPLETED), it did not move (FAILED, EXPIRED,
ABANDONED), it moved and came back (REVERSED), it is moving (PENDING,
PROCESSING). There was no member for the one thing that actually happens most
often when a payment provider goes quiet — the outcome is unknown.

With no word for it, the system used the nearest available one. FAILED.

### Four places where "we could not look" became "it did not happen"

1. **The poll circuit breaker.** After ten failed attempts,
   `_record_transfer_poll_failure` wrote `status = FAILED` and
   `gateway_response["poll_abandoned"] = True`. Ten consecutive 30-second
   connect timeouts produced the identical row a Paystack `{"status":
   "failed"}` verdict produces.

2. **The client collapsed transport into verdict.** Every one of fifteen call
   sites in `paystack_client.py` ended `except httpx.RequestError as e: raise
   PaystackError(f"Request failed: {str(e)}")`. A DNS failure, a TLS error and
   a provider refusal were the same exception type, so no caller downstream
   *could* tell them apart even if it wanted to.

3. **An unrecognised provider status read as "still in flight".**
   `poll_transfer_status` handled `success`, `failed` and `reversed` and had no
   `else` — anything else logged "still pending". A transfer Paystack had
   already settled under a word we do not parse would sit in PROCESSING for
   twenty minutes and then be stamped FAILED by (1), a verdict manufactured out
   of a word we did not read.

4. **An ambiguous initiation left no record at all.**
   `_recover_transfer_initiation` string-matched two cases (`"Request timed
   out"`, `"duplicate_transfer_reference"`); everything else returned `None`,
   the original error re-raised, and the intent stayed PENDING with no
   transfer_code while the API returned `502 Transfer failed`. The commonest
   ambiguous initiation there is — an actual connect timeout — did not match
   either string, because that text was a Paystack *body* message and never
   what the client raised. So the row sat PENDING until the expiry pass stamped
   it EXPIRED, and an expired-looking intent is one nobody chases.

### Why this is not a labelling problem

FAILED is not a shade of grey. Every reader in the codebase acts on it, and
each action is wrong when the money may in fact have moved:

- `reset_expense_payment_intent` treats FAILED as retryable and lets an
  operator create a fresh payout — the claim gets paid twice;
- `reimburse_expense_context` shows the Reimburse button again;
- `mark_transfer_failed` reverts a PAID claim to APPROVED;
- the transfers list paints it rose and files it under "Failed", where nobody
  looks for money that might be missing;
- the API tells the user "Transfer failed. Please check the error and try
  again," which is an instruction to do the one thing that is unsafe.

The cost of the two errors is not symmetric. Recording FAILED when the truth is
unknown risks a double payout. Recording INDETERMINATE when the truth was
FAILED costs an operator one look. The system should fall into the second.

## Decision

### 1. `INDETERMINATE` is added, and it asserts nothing about the money

A new `PaymentIntentStatus` member, plus an `unresolved_since` timestamp beside
`poll_count`/`last_poll_error`. It means: *the outcome was not observed*. It is
not a weaker FAILED and not a longer PROCESSING. It is the only member of the
vocabulary that makes no claim.

`unresolved_since` is written once, when the intent becomes unresolved, and
never re-stamped on a failed re-check — otherwise the clock the operator alert
is measured against resets and an unaccounted-for payout never ages.

### 2. The client distinguishes "did not answer" from "answered no"

`PaystackUnreachable(PaystackError)` is raised at every `httpx.RequestError`
site and on any 5xx `HTTPStatusError`. Plain `PaystackError` keeps its original
meaning and narrows to it: Paystack answered, and the answer was a refusal — a
parsed `status: false` body or a 4xx.

It subclasses `PaystackError` so existing `except PaystackError` handlers keep
working unchanged. What the subclass buys is that a handler about to write a
verdict can ask whether it has one.

### 3. The safe answer is the default, not the remembered case

`is_unobserved(error)` is deliberately inverted. It does not ask "is this a
non-observation?" — it asks "did Paystack actually answer?", and only one
exception type can say yes. A timeout, a 5xx, a bug in our own posting code, or
an exception type that does not exist yet are all unobserved.

This matters more than the classification itself. A rule where the dangerous
answer is the default and the safe one has to be remembered will be got wrong
eventually, by someone adding a case years from now who never read this file.

### 4. FAILED on the give-up path requires that Paystack answered

Spending the poll budget ends the fast loop and nothing else. What the intent
becomes then depends on the last attempt:

- Paystack answered and refused → FAILED, unchanged, and the claim becomes
  payable again;
- nothing was learned → INDETERMINATE, `unresolved_since` set.

An affirmative `{"status": "failed"}` never reaches this path at all — it is
handled by `mark_transfer_failed` inside `poll_transfer_status` on the first
attempt, which is why one attempt is enough to record a real failure and ten
are not enough to invent one.

### 5. An unrecognised provider status is INDETERMINATE

`poll_transfer_status` gains the missing `else`. `_PAYSTACK_IN_FLIGHT_STATUSES`
enumerates the words that genuinely mean "still moving"; anything in neither
the terminal set nor that set is a word this system has never seen, and the
honest response is to admit we do not know what it means rather than guess the
reassuring interpretation.

### 6. An unobserved initiation is recorded, and the API says "do not retry"

`_recover_transfer_initiation` now keys on the exception *type* rather than two
hard-coded strings, so a genuine timeout finally reaches the recovery path. It
has three outcomes: a verified transfer, `None` (Paystack answered that nothing
started — retryable, the intent stays PENDING), or `TransferOutcomeUnknown`
raised with the intent already INDETERMINATE and committed.

The route maps that to **409 Conflict**, not 502, and the choice is load-bearing:

- 502 is in every default retry set there is — proxies, HTTP client libraries,
  queue backoff, and the operator's own instinct. Repeating *this* request is
  precisely how the employee gets reimbursed twice.
- 409 says the resource is in a state that conflicts with the request, which is
  exactly true: the intent is INDETERMINATE and no further initiation is
  permitted from it. It is not retried automatically by anything, and it does
  not read as "we failed".
- The body carries `retryable: false` and says the money may have moved.

### 7. One reconciler owns the way out

`find_indeterminate_transfer_intents` / `resolve_indeterminate_transfer` on
`PaymentService`, driven hourly by
`app.tasks.expense.reconcile_unresolved_expense_transfers` — the slow lane
behind the two-minute poller, which these intents have already exhausted.

`resolve_indeterminate_transfer` is the only writer permitted to move an intent
out of INDETERMINATE, and only to a status Paystack itself justified, by
delegating to the same `process_successful_transfer` /
`mark_transfer_failed` / `process_transfer_reversal` the webhook uses. It keeps
ADR-0005's discipline exactly: taken by id, locked `FOR UPDATE` with
`populate_existing=True`, premise re-proved before anything is written.

**There is no give-up branch and no attempt cap.** A budget here would rebuild
the original defect one level up, manufacturing a verdict out of repeated
silence. An unresolved payout stays unresolved until somebody learns what
happened to it.

The operator signal is the AGE of the oldest unresolved payout, not the count:
the count is noisy during a provider incident and falls to zero on its own,
while the age only grows until a human acts. Past
`paystack_transfer_unresolved_alert_hours` (default 6, config per the
everything-by-config rule) the reconciler logs at ERROR with the reference,
amount and elapsed time; `payment_transfer_unresolved_oldest_age_seconds` is
published every run.

### 8. Every reader is explicit, and INDETERMINATE is never resettable

A new enum member that some reader silently falls through is worse than the bug
it fixes. Every reader was inspected; the money-critical ones:

- `reset_expense_payment_intent` refuses INDETERMINATE with 409, **and `force`
  does not override it**. Resetting is how an operator earns permission to pay
  the claim again; doing that while the first payout is unaccounted for is the
  double-payment path. FAILED/EXPIRED/ABANDONED stay resettable because each
  asserts the money did not move.
- `create_expense_payment_intent` refuses outright, before its expiry-and-
  replace branch — that branch would otherwise stamp EXPIRED over an intent
  whose money may have left.
- `reimburse_expense_context` counts INDETERMINATE as active, so the Reimburse
  button does not reappear, and leaves it out of `resettable_statuses`.
- `has_payment_in_flight` counts it, so an approval cannot be withdrawn out
  from under a payout that may have gone.
- `process_successful_transfer` and `process_transfer_reversal` accept it as an
  input state — resolving one is the point.
- `_hold_batch_item_unresolved` records the reason on a batch item but leaves
  its status alone. `TransferBatchItemStatus` has no "unknown" member, and
  writing FAILED would repeat the conflation one level up *and* finalize the
  parent batch on a non-verdict.

The expense claim stays APPROVED and is not re-reimbursable while indeterminate.
No GL journal is posted: posting happens only in `process_successful_transfer`,
which INDETERMINATE never reaches on its own.

### 9. The rule is enforced

`tests/architecture/test_unobserved_is_not_a_verdict.py` fails the build on any
`except` block handling `PaystackUnreachable` or an httpx transport error that
assigns FAILED or EXPIRED, with the two-sided sensitivity proof this repo's
single-owner guards use: a planted violation must be detected, and the
legitimate FAILED write in `poll_transfer_status` must not be flagged.

## Consequences

- Some transfers will sit in a state that answers nobody's question. That is
  the intended cost, and it is the point: the alternative was answering the
  question wrongly, in the direction that lets work proceed.
- The two-minute poller's report gains an `indeterminate` key kept separate
  from `abandoned`, so the job's own summary does not repeat the conflation the
  column no longer makes.
- A tenant with no Paystack keys now shows up as a permanent contributor to the
  unresolved-age gauge rather than silently accumulating unreconcilable rows.
  That is a correct signal, not a regression.
- `INDETERMINATE_RECHECK_INTERVAL` is documentation of the beat cadence, not a
  row predicate: the selector is uncapped and unfiltered by age, and the
  schedule is what paces it.
- Nothing about production enablement is claimed. `paystack_transfers_enabled`
  is runtime data on a deployment nobody has named. **Implemented and tested;
  production enablement unconfirmed.**

## Alternatives rejected

- **Keep FAILED and add a `poll_abandoned` flag to distinguish it.** That flag
  already existed and changed nothing, because no reader consulted it. A
  distinction that lives in a JSONB key while the column every query filters on
  says FAILED is not a distinction; it is a comment.

- **Return 502 from the initiate route and let the caller decide.** 502 is
  retried by default by proxies, client libraries and people. The status code
  IS the instruction, and the instruction has to be "stop".

- **Return 202 Accepted.** It is a success code. A client that treats
  "accepted" as "it worked" would mark the claim paid in its own UI on the
  strength of an outcome nobody observed — the same conflation, moved
  downstream and out of our sight.

- **Give the reconciler an attempt budget so intents eventually settle.** This
  is the original defect wearing a longer timescale. Ten failures over twenty
  minutes and a hundred over a week are the same quantity of information about
  where the money went: none.

- **Treat every 4xx as unobserved too, so a rotated key cannot produce FAILED.**
  Tempting, and narrowly true — a 401 does leave the transfer unknown. But it
  would park every mis-keyed deployment's payouts in INDETERMINATE, and a
  configuration fault has its own loud failure mode. The money-critical
  direction is defended separately and more strongly: on the give-up path,
  anything that is not an affirmative provider verdict is unobserved
  regardless of status code.

- **Add an `INDETERMINATE` member to `TransferBatchItemStatus` for symmetry.**
  Nothing creates batches any more (ADR-0005 §4 deleted the only writer); the
  tables survive as history. Adding a member to a dormant vocabulary invents a
  contract for a writer that does not exist, and needs a migration to do it.

- **Notify an operator rather than logging.** A `Notification` needs a named
  recipient, and there is no owner-of-treasury-exceptions concept in this repo.
  Guessing one would either spam every admin or silently reach nobody. The
  escalation is a structured ERROR log plus a gauge, which is what the fleet's
  alerting actually consumes.

- **Unify the polling path's Paystack config with the API layer's.** Still a
  signature-verification decision, still not this change. ADR-0005 left it
  alone for the same reason.
