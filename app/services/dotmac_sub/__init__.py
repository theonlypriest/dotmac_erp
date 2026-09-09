"""
dotmac_sub integration package.

Syncs the AR ledger (customers, invoices, payments, credit notes) from the
dotmac_sub subscriber-management system (``selfcare.dotmac.io``), replacing the
legacy Splynx feed.
"""

from __future__ import annotations

from app.services.dotmac_sub.client import (
    DotmacSubAuthenticationError,
    DotmacSubClient,
    DotmacSubConfig,
    DotmacSubError,
    DotmacSubNotFoundError,
    DotmacSubPermanentSyncError,
    DotmacSubRateLimitError,
)
from app.services.dotmac_sub.sync import (
    SYSTEM_USER_ID,
    DotmacSubSyncService,
    FullSyncResult,
    SyncResult,
)

__all__ = [
    "SYSTEM_USER_ID",
    "DotmacSubAuthenticationError",
    "DotmacSubClient",
    "DotmacSubConfig",
    "DotmacSubError",
    "DotmacSubNotFoundError",
    "DotmacSubPermanentSyncError",
    "DotmacSubRateLimitError",
    "DotmacSubSyncService",
    "FullSyncResult",
    "SyncResult",
]
