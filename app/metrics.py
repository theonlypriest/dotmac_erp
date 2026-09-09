import re

from prometheus_client import Counter, Gauge, Histogram

REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
)
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["method", "path", "status"],
)
REQUEST_ERRORS = Counter(
    "http_request_errors_total",
    "Total HTTP 5xx responses",
    ["method", "path", "status"],
)

JOB_DURATION = Histogram(
    "job_duration_seconds",
    "Background job duration",
    ["task", "status"],
)


JOB_RUNS = Counter(
    "job_runs_total",
    "Background job executions",
    ["task", "status"],
)

INTEGRATION_REQUESTS = Counter(
    "integration_requests_total",
    "Outbound integration requests",
    ["integration", "operation", "status"],
)
INTEGRATION_REQUEST_DURATION = Histogram(
    "integration_request_duration_seconds",
    "Outbound integration request duration",
    ["integration", "operation", "status"],
)

PAYSTACK_SELFCARE_RELAY = Counter(
    "paystack_selfcare_relay_total",
    "Verified Paystack webhook relay outcomes from ERP to Selfcare",
    ["outcome"],
)
PAYSTACK_SELFCARE_RELAY_DURATION = Histogram(
    "paystack_selfcare_relay_duration_seconds",
    "Duration of verified Paystack webhook relay attempts from ERP to Selfcare",
    ["outcome"],
)

# ── Finance event outbox (claim/deliver/settle relay) ──────────────────
# Outcome labels: published, no_consequence, retried, dead, unsupported,
# stale_claim, commit_failed, partial_failure_rolled_back, missing_org.
OUTBOX_EVENTS = Counter(
    "outbox_events_total",
    "Outbox relay delivery outcomes",
    ["outcome"],
)
OUTBOX_REPLAYS = Counter(
    "outbox_replays_total",
    "Authorized replays of dead outbox events",
)
OUTBOX_OLDEST_PENDING_AGE = Gauge(
    "outbox_oldest_pending_age_seconds",
    "Age of the oldest deliverable (PENDING/FAILED) outbox event",
)
OUTBOX_OLDEST_LEASE_AGE = Gauge(
    "outbox_oldest_lease_age_seconds",
    "Age of the oldest still-active claim lease (0 when none held)",
)
OUTBOX_RECONCILIATION = Counter(
    "outbox_reconciliation_total",
    "Outbox-vs-consequence reconciliation results",
    ["result"],  # drift_found | repaired
)


def observe_outbox_outcome(outcome: str, count: int = 1) -> None:
    if count > 0:
        OUTBOX_EVENTS.labels(outcome=normalize_metric_label(outcome)).inc(count)


def observe_outbox_replay() -> None:
    OUTBOX_REPLAYS.inc()


def set_outbox_backlog_ages(
    pending_age_seconds: float, lease_age_seconds: float
) -> None:
    OUTBOX_OLDEST_PENDING_AGE.set(max(0.0, pending_age_seconds))
    OUTBOX_OLDEST_LEASE_AGE.set(max(0.0, lease_age_seconds))


def observe_outbox_reconciliation(result: str, count: int = 1) -> None:
    if count > 0:
        OUTBOX_RECONCILIATION.labels(result=normalize_metric_label(result)).inc(count)


# ── Outbound transfers whose outcome could not be observed ─────────────
# An INDETERMINATE payment intent is not an error rate: it is money whose
# fate nobody knows, and it stays that way until a human or the slow
# reconciler resolves it. The AGE of the oldest one is the number an operator
# should be paged on, not the count (ADR-0007).
TRANSFER_UNRESOLVED = Counter(
    "payment_transfer_unresolved_total",
    "Outbound transfers recorded INDETERMINATE because the outcome was not observed",
    ["stage"],  # initiation | polling | unrecognised_status
)
TRANSFER_UNRESOLVED_OLDEST_AGE = Gauge(
    "payment_transfer_unresolved_oldest_age_seconds",
    "Age of the oldest INDETERMINATE outbound transfer intent (0 when none)",
)


def observe_transfer_unresolved(stage: str) -> None:
    TRANSFER_UNRESOLVED.labels(stage=normalize_metric_label(stage)).inc()


def set_transfer_unresolved_oldest_age(age_seconds: float) -> None:
    TRANSFER_UNRESOLVED_OLDEST_AGE.set(max(0.0, age_seconds))


LOKI_LOGS_SENT = Counter(
    "loki_logs_sent_total",
    "Log records successfully pushed to Loki",
)
LOKI_LOGS_DROPPED = Counter(
    "loki_logs_dropped_total",
    "Log records dropped (Loki unreachable, queue full, or HTTP error)",
)


_ID_TOKEN_RE = re.compile(r"\b(?:[0-9a-f]{8,}|[0-9]{3,})\b", re.IGNORECASE)


def observe_job(task_name: str, status: str, duration: float) -> None:
    normalized_status = normalize_metric_label(status)
    JOB_RUNS.labels(task=task_name, status=normalized_status).inc()
    JOB_DURATION.labels(task=task_name, status=normalized_status).observe(duration)


def observe_integration_request(
    integration: str,
    operation: str,
    status: str,
    duration: float,
) -> None:
    normalized_status = normalize_metric_label(status)
    INTEGRATION_REQUESTS.labels(
        integration=integration,
        operation=operation,
        status=normalized_status,
    ).inc()
    INTEGRATION_REQUEST_DURATION.labels(
        integration=integration,
        operation=operation,
        status=normalized_status,
    ).observe(duration)


def observe_paystack_selfcare_relay(outcome: str, duration: float) -> None:
    normalized_outcome = normalize_metric_label(outcome)
    PAYSTACK_SELFCARE_RELAY.labels(outcome=normalized_outcome).inc()
    PAYSTACK_SELFCARE_RELAY_DURATION.labels(outcome=normalized_outcome).observe(
        max(duration, 0.0)
    )


def categorize_http_status(status_code: int) -> str:
    if status_code in {401, 403}:
        return "auth_error"
    if status_code == 404:
        return "not_found"
    if status_code == 429:
        return "rate_limited"
    if 400 <= status_code < 500:
        return "client_error"
    if status_code >= 500:
        return "server_error"
    return "success"


def normalize_metric_label(value: str) -> str:
    scrubbed = _ID_TOKEN_RE.sub("id", str(value).strip().lower())
    normalized = re.sub(r"[^a-zA-Z0-9_]+", "_", scrubbed)
    normalized = normalized.strip("_")
    return normalized or "unknown"
