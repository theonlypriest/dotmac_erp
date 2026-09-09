"""Repair ERP's derived Employment Type compatibility projection explicitly."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.session_context import for_each_organization
from app.services.people.hr.employment_types import EmploymentTypeService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--organization-id", type=UUID)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run every check and repair, then roll back the transaction",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    processed = 0
    for organization_id, db in for_each_organization(
        include_inactive=True,
        only=args.organization_id,
    ):
        try:
            repaired = EmploymentTypeService(
                db,
                organization_id,
            ).repair_compatibility_projection()
            if args.dry_run:
                db.rollback()
            else:
                db.commit()
        except BaseException:
            db.rollback()
            raise
        processed += 1
        print(
            json.dumps(
                {
                    "committed": not args.dry_run,
                    "mode": "repair",
                    "organization_id": str(organization_id),
                    "repaired": repaired,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )

    if args.organization_id is not None and processed == 0:
        raise SystemExit("requested organization is absent from the tenant catalog")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
