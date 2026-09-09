"""Payroll classifications derived from People-owned Employment Type facts."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.services.people.hr.employment_types import EmploymentTypeService

_CONTRACT_CODES = frozenset({"CONTRACT"})
_PERMANENT_CODES = frozenset({"PERMANENT", "FULL_TIME", "FULL-TIME", "FULLTIME"})


@dataclass(frozen=True, slots=True)
class PayrollEmploymentTypeClassification:
    """Payroll consequences derived from one canonical People code."""

    code: str | None

    @property
    def is_contract(self) -> bool:
        return self.code in _CONTRACT_CODES

    @property
    def is_permanent(self) -> bool:
        return self.code in _PERMANENT_CODES

    def is_contract_staff(self, *, structure_name: str | None = None) -> bool:
        """Preserve the payroll-structure override for contract-only structures."""
        return self.is_contract or (structure_name or "").strip().casefold() == (
            "contract staff"
        )


def classify_payroll_employment_type(
    db: Session,
    *,
    organization_id: UUID,
    employment_type_id: UUID | None,
) -> PayrollEmploymentTypeClassification:
    """Read the People owner once and derive payroll's local consequences."""
    if employment_type_id is None:
        return PayrollEmploymentTypeClassification(code=None)
    employment_type = EmploymentTypeService(db, organization_id).get_employment_type(
        employment_type_id
    )
    return PayrollEmploymentTypeClassification(
        code=employment_type.type_code.strip().upper()
    )


__all__ = [
    "PayrollEmploymentTypeClassification",
    "classify_payroll_employment_type",
]
