"""Shared incremental-sync watermark progress tracker.

ONE owner for the park-vs-freeze cursor decision that was previously
copy-pasted across the invoice/payment/credit-note sync mixins (which is
exactly how per-path gaps crept in). Contract:

- rows are pulled in ascending ``updated_at, id`` order;
- a successful row raises ``max_ok``;
- a POSITIONED failure (usable ``updated_at``) parks the cursor at the
  earliest failed row (``min_error``) — the inclusive ``>=`` watermark
  filter re-pulls and retries it next cycle;
- an UNPOSITIONED failure (missing/malformed/non-string ``updated_at``)
  FREEZES the cursor at its pre-run position: the watermark must not
  advance at all for the run, otherwise a later good row would advance it
  past the failed row and skip it permanently. Good rows still sync.

Positions are ``datetime``s throughout: the client parses ``updated_at`` at
admission, so neither this tracker nor its callers re-parse wire text.

The tracker also builds the ``on_parse_error`` collector handed to the
client feeds, so parse-level rejections and savepoint-level row failures
flow through the same accounting.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from ._base import SyncWatermarkPosition, _aware_utc, next_watermark

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.services.dotmac_sub.client import DotmacSubParseError

    from ._types import SyncResult

logger = logging.getLogger(__name__)


class WatermarkProgress:
    """Progress accounting + the advance/park/freeze decision for one run."""

    def __init__(self, watermark: datetime | None, *, label: str) -> None:
        self.watermark = watermark  # pre-run cursor position
        self.label = label
        self.max_ok: datetime | None = None
        self.min_error: datetime | None = None
        self.frozen = False

    def record_success(self, row_updated_at: datetime | None) -> None:
        """A row synced cleanly; it may raise the advance candidate."""
        if row_updated_at is not None:
            self.max_ok = (
                row_updated_at
                if self.max_ok is None
                else max(self.max_ok, row_updated_at)
            )

    def record_failure(self, row_updated_at: datetime | None) -> None:
        """A row failed (parse rejection or savepoint rollback).

        Positioned -> park the cursor at the earliest failure; unpositioned
        -> freeze the cursor for the whole run.
        """
        if row_updated_at is None:
            self.frozen = True
            return
        self.min_error = (
            row_updated_at
            if self.min_error is None
            else min(self.min_error, row_updated_at)
        )

    def parse_error_collector(
        self, result: SyncResult
    ) -> Callable[[DotmacSubParseError], None]:
        """The ``on_parse_error`` callback for the client feed: record the
        rejected row as THIS row's failure (run continues).

        No ``parse_datetime`` hook: since the records the boundary admits
        carry real ``datetime``s, the parser positions the rejection itself
        and ``DotmacSubParseError.updated_at`` is already the parsed instant
        (``None`` = unpositioned = freeze).
        """

        def _collect(exc: DotmacSubParseError) -> None:
            result.errors.append(str(exc))
            logger.error("Rejected dotmac_sub %s row at parse: %s", self.label, exc)
            self.record_failure(exc.updated_at)

        return _collect

    def conclude(self, advance: Callable[[datetime | None], None]) -> None:
        """Apply the run's cursor decision: freeze (skip advancing entirely)
        when any failure was unpositioned, else advance-or-park via
        ``next_watermark``."""
        if self.frozen:
            logger.warning(
                "%s sync watermark frozen: a failed row had no usable "
                "updated_at, so the cursor cannot be parked at it; holding "
                "the pre-run position",
                self.label,
            )
            return
        advance(next_watermark(self.watermark, self.max_ok, self.min_error))


class WatermarkPositionProgress:
    """Progress tracker for feeds with ``updated_at, id`` continuation."""

    def __init__(self, position: SyncWatermarkPosition, *, label: str) -> None:
        self.position = position
        self.label = label
        self.last_success = position
        self.first_failure: SyncWatermarkPosition | None = None
        self.frozen = False

    def record_success(
        self, row_updated_at: datetime | None, row_external_id: str | None
    ) -> None:
        if (
            row_updated_at is None
            or row_external_id is None
            or self.first_failure is not None
        ):
            return
        candidate = SyncWatermarkPosition(row_updated_at, str(row_external_id))
        if _position_after(candidate, self.last_success):
            self.last_success = candidate

    def record_failure(
        self, row_updated_at: datetime | None, row_external_id: str | None
    ) -> None:
        if row_updated_at is None or row_external_id is None:
            self.frozen = True
            return
        candidate = SyncWatermarkPosition(row_updated_at, str(row_external_id))
        if self.first_failure is None or _position_after(self.first_failure, candidate):
            self.first_failure = candidate

    def parse_error_collector(
        self, result: SyncResult
    ) -> Callable[[DotmacSubParseError], None]:
        def _collect(exc: DotmacSubParseError) -> None:
            result.errors.append(str(exc))
            logger.error("Rejected dotmac_sub %s row at parse: %s", self.label, exc)
            # Parse errors do not expose a structured source id, so advancing
            # would risk skipping the rejected row within an identical-timestamp
            # group. Freeze and retry from the pre-run cursor.
            self.record_failure(exc.updated_at, None)

        return _collect

    def conclude(self, advance: Callable[[SyncWatermarkPosition | None], None]) -> None:
        if self.frozen:
            logger.warning(
                "%s sync compound watermark frozen: a failed row had no usable "
                "updated_at/id position; holding the pre-run cursor",
                self.label,
            )
            return
        advance(self.last_success)


def _position_after(left: SyncWatermarkPosition, right: SyncWatermarkPosition) -> bool:
    if left.watermark_at is None:
        return False
    if right.watermark_at is None:
        return True
    left_at = _aware_utc(left.watermark_at)
    right_at = _aware_utc(right.watermark_at)
    if left_at != right_at:
        return left_at > right_at
    if left.external_id is None:
        return False
    if right.external_id is None:
        return True
    return left.external_id > right.external_id
