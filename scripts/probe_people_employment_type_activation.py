"""Resolve an ambiguous Employment Type activation migration outcome.

The deploy script calls this only after the Alembic container reports failure.
It never treats an unavailable or unfamiliar database state as permission to
restart legacy writers.  Exit status 3 is the sole rollback-safe answer.

The probe reads the CURRENT module-owned surface, not the retired assembly
path.  Two facts are read, both of which the activation revision creates or
consumes inside the one transaction PostgreSQL commits or rolls back
atomically with the ``alembic_version`` row:

``mod_people.employment_types``
    The module-owned authority relation, and the probe's positive control.
    It is composed by the People module lineage and is therefore present
    both before and after activation.  If it cannot be seen, the probe is not
    looking at a composed People database and says so rather than guessing.

``hr.enforce_employment_type_projection()``
    The compatibility projection fence the activation revision installs on the
    retained ``hr.employment_type`` relation.  It is the activation's own
    artifact: absent beforehand, present afterward, and never partially
    applied, because the revision row and this function share one transaction.

A rollback is authorized only when the authority relation is visible AND the
activation revision is absent AND the activation's projection fence is absent.
Any other combination is a torn or unrecognized state and stops the deploy.
"""

from __future__ import annotations

import os
from enum import IntEnum

from sqlalchemy import create_engine, text

ACTIVATION_REVISION = "20260828_people_et_activation"
MIGRATION_DATABASE_URL = "MIGRATION_DATABASE_URL"


class ProbeExit(IntEnum):
    ACTIVATED = 0
    DEFINITELY_PRE_ACTIVATION = 3
    AMBIGUOUS = 4


def classify(
    *,
    revision_recorded: bool,
    module_authority_present: bool,
    projection_fence_present: bool,
) -> ProbeExit:
    """Return the only rollback-safe classification for the observed state."""
    if not module_authority_present:
        # No positive control: the probe cannot prove it read the composed
        # People database at all, so an absent revision proves nothing.
        return ProbeExit.AMBIGUOUS
    if revision_recorded:
        return ProbeExit.ACTIVATED
    if projection_fence_present:
        # The activation's own artifact exists without its revision row.
        return ProbeExit.AMBIGUOUS
    return ProbeExit.DEFINITELY_PRE_ACTIVATION


_STATE_QUERY = text(
    "SELECT "
    "EXISTS ("
    "SELECT 1 FROM public.alembic_version WHERE version_num = :revision"
    ") AS revision_recorded, "
    "to_regclass('mod_people.employment_types') IS NOT NULL "
    "AS module_authority_present, "
    "to_regprocedure('hr.enforce_employment_type_projection()') IS NOT NULL "
    "AS projection_fence_present"
)


def main() -> int:
    database_url = os.environ.get(MIGRATION_DATABASE_URL)
    if not database_url:
        print("ambiguous: migration database material is unavailable")
        return ProbeExit.AMBIGUOUS

    try:
        engine = create_engine(database_url, pool_pre_ping=True)
        with engine.connect() as connection:
            row = connection.execute(
                _STATE_QUERY,
                {"revision": ACTIVATION_REVISION},
            ).one()
    except Exception:
        # A probe error is deliberately opaque: connection details and database
        # exceptions can contain secret material. The deploy remains stopped.
        print("ambiguous: activation outcome could not be read")
        return ProbeExit.AMBIGUOUS

    outcome = classify(
        revision_recorded=bool(row.revision_recorded),
        module_authority_present=bool(row.module_authority_present),
        projection_fence_present=bool(row.projection_fence_present),
    )
    if outcome is ProbeExit.ACTIVATED:
        print("activated: activation revision is committed")
    elif outcome is ProbeExit.DEFINITELY_PRE_ACTIVATION:
        print(
            "pre-activation: the module-owned authority is present and neither "
            "the activation revision nor its projection fence exists"
        )
    elif not row.module_authority_present:
        print("ambiguous: the module-owned authority relation is not visible")
    else:
        print("ambiguous: the projection fence exists without its revision row")
    return outcome


if __name__ == "__main__":
    raise SystemExit(main())
