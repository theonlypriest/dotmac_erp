from __future__ import annotations

import csv
import io
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.services.people.payroll.nhf_export import NHFExportService
from app.services.people.payroll.paye_export import PAYEExportService
from app.services.people.payroll.pension_export import PensionExportService
from app.services.people.payroll.employment_type_classification import (
    PayrollEmploymentTypeClassification,
)

_EMPLOYMENT_TYPE_CODES: dict[UUID, str] = {}


@pytest.fixture(autouse=True)
def _authoritative_payroll_classification(monkeypatch):
    def classify(_db, *, organization_id, employment_type_id):
        assert organization_id is not None
        return PayrollEmploymentTypeClassification(
            _EMPLOYMENT_TYPE_CODES[employment_type_id]
        )

    monkeypatch.setattr(
        "app.services.people.payroll.paye_export.classify_payroll_employment_type",
        classify,
    )


def _make_component(code: str) -> SimpleNamespace:
    return SimpleNamespace(component_code=code)


def _make_earning(code: str, amount: str) -> SimpleNamespace:
    return SimpleNamespace(component=_make_component(code), amount=Decimal(amount))


def _make_deduction(code: str, amount: str) -> SimpleNamespace:
    return SimpleNamespace(component=_make_component(code), amount=Decimal(amount))


def _make_employee(
    tax_state: str = "Lagos",
    employment_type_code: str = "permanent",
) -> SimpleNamespace:
    designation = SimpleNamespace(designation_name="Analyst")
    tax_profile = SimpleNamespace(
        tin="TIN-123",
        tax_state=tax_state,
        pfa_code="PFA01",
        rsa_pin="RSA123",
        pfa=None,
        nhf_number="NHF001",
    )
    employment_type_id = uuid4()
    _EMPLOYMENT_TYPE_CODES[employment_type_id] = employment_type_code.upper()
    return SimpleNamespace(
        organization_id=uuid4(),
        employment_type_id=employment_type_id,
        full_name="Jane Doe",
        designation=designation,
        employee_code="EMP001",
        employee_number="EMP001",
        current_tax_profile=tax_profile,
    )


def test_paye_export_uses_deductions_and_earnings():
    service = PAYEExportService(db=None)
    slip = SimpleNamespace(
        slip_number="SLIP-2026-00001",
        employee=_make_employee(),
        gross_pay=Decimal("2200.00"),
        earnings=[
            _make_earning("BASIC", "1000"),
            _make_earning("HOUSING", "500"),
            _make_earning("TRANSPORT", "500"),
            _make_earning("BONUS", "200"),
        ],
        deductions=[
            _make_deduction("PAYE", "100"),
            _make_deduction("NHF", "25"),
            _make_deduction("NHIS", "0"),
            _make_deduction("PENSION", "80"),
        ],
    )

    result = service._generate_lirs_format([slip], 2026, 1)
    content = result.content.decode("utf-8")
    rows = list(csv.reader(io.StringIO(content)))

    # Header + 1 data row
    assert len(rows) == 2
    data = rows[1]

    # Basic, Housing, Transport, Bonus columns
    assert data[5] == "1000.00"
    assert data[6] == "500.00"
    assert data[7] == "500.00"
    assert data[13] == "200.00"

    # NHF, NHIS, Pension, PAYE
    assert data[17] == "25.00"
    assert data[18] == "0.00"
    assert data[19] == "80.00"
    assert data[22] == "100.00"


def test_fctirs_export_uses_expected_template():
    service = PAYEExportService(db=None)
    slip = SimpleNamespace(
        slip_number="SLIP-2026-00004",
        employee=_make_employee(tax_state="Abuja"),
        gross_pay=Decimal("2400.00"),
        earnings=[
            _make_earning("BASIC", "1000"),
            _make_earning("HOUSING", "400"),
            _make_earning("TRANSPORT", "300"),
            _make_earning("UTILITY", "100"),
            _make_earning("MEDICAL", "200"),
            _make_earning("ENTERTAINMENT", "150"),
            _make_earning("BONUS", "250"),
        ],
        deductions=[
            _make_deduction("PAYE", "120"),
            _make_deduction("NHF", "25"),
            _make_deduction("NHIS", "10"),
            _make_deduction("PENSION", "80"),
        ],
    )

    result = service._generate_fctirs_format(
        [slip],
        2026,
        11,
        organization_name="Dotmac Technologies Limited",
    )
    rows = list(csv.reader(io.StringIO(result.content.decode("utf-8"))))

    assert rows[0] == ["DOTMAC TECHNOLOGIES LIMITED"]
    assert rows[2] == ["PAY-AS-YOU-EARN COMPUTATION FOR Nov, 2026"]
    assert rows[3] == [
        "S/N",
        "NAME",
        "TIN ID",
        "BASIC",
        "HOUSING",
        "TRANSPORT",
        "UTILITY",
        "MEDICAL",
        "ENTERTAINMENT",
        "OTHER NON ALLOW",
        "MONTHLY GROSS INCOME",
        "ANNUAL GROSS INCOME",
        "EARNED INCOME",
        "CONSOLDATED ALLOWANCE",
        "NHF",
        "NHIS",
        "LIFE ASSURANCE",
        "PENSION",
        "TOTAL  ALLOWANCE",
        "CHARGEABLE INCOME",
        "ANNUAL TAX DUE",
        "ANNUAL MINIMUM TAX",
        "ANNUAL TAX PAYABLE",
        "MONTHLY TAX PAYABLE",
        "No. of Months Worked",
        "TAX PAID",
        "TAX OVER/(UNDER) DEDUC.",
    ]

    data = rows[4]
    assert data[0] == "1"
    assert data[1] == "Jane Doe"
    assert data[2] == '="TIN-123"'
    assert data[3] == "1000.00"
    assert data[4] == "400.00"
    assert data[5] == "300.00"
    assert data[6] == "100.00"
    assert data[7] == "200.00"
    assert data[8] == "150.00"
    assert data[9] == "250.00"
    assert data[10] == "2400.00"
    assert data[11] == "28800.00"
    assert data[24] == "1.00"
    assert data[25] == "120.00"


def test_generate_export_uses_loaded_tax_profile_for_tin(monkeypatch):
    service = PAYEExportService(db=None)
    employee = _make_employee()
    employee.employee_id = "emp-1"
    employee.current_tax_profile = None
    slip = SimpleNamespace(
        slip_number="SLIP-2026-00005",
        employee=employee,
        gross_pay=Decimal("1000.00"),
        earnings=[_make_earning("BASIC", "1000")],
        deductions=[_make_deduction("PAYE", "50")],
    )
    loaded_profile = SimpleNamespace(
        employee_id="emp-1",
        tin="TIN-DB-999",
        tax_state="Lagos",
    )

    monkeypatch.setattr(service, "_get_slips", lambda *_args: [slip])
    monkeypatch.setattr(
        service,
        "_get_tax_profiles_by_employee",
        lambda *_args: {"emp-1": loaded_profile},
    )

    result = service.generate_export("org-id", 2026, 1, paye_format="lirs")
    rows = list(csv.reader(io.StringIO(result.content.decode("utf-8"))))

    assert rows[1][1] == '="TIN-DB-999"'


def test_paye_export_formats_long_numeric_tin_as_text():
    service = PAYEExportService(db=None)
    employee = _make_employee()
    employee.current_tax_profile.tin = "2512500000000"
    slip = SimpleNamespace(
        slip_number="SLIP-2026-00009",
        employee=employee,
        gross_pay=Decimal("1000.00"),
        earnings=[_make_earning("BASIC", "1000")],
        deductions=[_make_deduction("PAYE", "50")],
    )

    lirs = service._generate_lirs_format([slip], 2026, 1)
    lirs_rows = list(csv.reader(io.StringIO(lirs.content.decode("utf-8"))))
    assert lirs_rows[1][1] == '="2512500000000"'

    employee.current_tax_profile.tax_state = "Abuja"
    fctirs = service._generate_fctirs_format(
        [slip],
        2026,
        1,
        organization_name="Dotmac Technologies Limited",
    )
    fctirs_rows = list(csv.reader(io.StringIO(fctirs.content.decode("utf-8"))))
    assert fctirs_rows[4][2] == '="2512500000000"'


def test_paye_exports_route_permanent_staff_by_tax_state():
    service = PAYEExportService(db=None)
    lagos_slip = SimpleNamespace(
        slip_number="SLIP-2026-00006",
        employee=_make_employee(tax_state="Lagos"),
        gross_pay=Decimal("1000.00"),
        earnings=[_make_earning("BASIC", "1000")],
        deductions=[_make_deduction("PAYE", "50")],
    )
    abuja_slip = SimpleNamespace(
        slip_number="SLIP-2026-00007",
        employee=_make_employee(tax_state="Abuja"),
        gross_pay=Decimal("1200.00"),
        earnings=[_make_earning("BASIC", "1200")],
        deductions=[_make_deduction("PAYE", "60")],
    )
    contract_slip = SimpleNamespace(
        slip_number="SLIP-2026-00008",
        employee=_make_employee(
            tax_state="Lagos",
            employment_type_code="contract",
        ),
        gross_pay=Decimal("900.00"),
        earnings=[_make_earning("BASIC", "900")],
        deductions=[_make_deduction("PAYE", "45")],
    )

    lirs = service._generate_lirs_format(
        [lagos_slip, abuja_slip, contract_slip], 2026, 1
    )
    lirs_rows = list(csv.reader(io.StringIO(lirs.content.decode("utf-8"))))
    assert len(lirs_rows) == 2
    assert lirs_rows[1][0] == "Jane Doe"
    assert lirs.employee_count == 1

    fctirs = service._generate_fctirs_format(
        [lagos_slip, abuja_slip, contract_slip],
        2026,
        1,
        organization_name="Dotmac Technologies Limited",
    )
    fctirs_rows = list(csv.reader(io.StringIO(fctirs.content.decode("utf-8"))))
    assert len(fctirs_rows) == 5
    assert fctirs_rows[4][1] == "Jane Doe"
    assert fctirs.employee_count == 1


def test_fctirs_serial_numbers_are_sequential_after_skips():
    service = PAYEExportService(db=None)
    lagos_slip = SimpleNamespace(
        slip_number="SLIP-2026-00010",
        employee=_make_employee(tax_state="Lagos"),
        gross_pay=Decimal("1000.00"),
        earnings=[_make_earning("BASIC", "1000")],
        deductions=[_make_deduction("PAYE", "50")],
    )
    abuja_slip_one = SimpleNamespace(
        slip_number="SLIP-2026-00011",
        employee=_make_employee(tax_state="Abuja"),
        gross_pay=Decimal("1200.00"),
        earnings=[_make_earning("BASIC", "1200")],
        deductions=[_make_deduction("PAYE", "60")],
    )
    abuja_slip_two = SimpleNamespace(
        slip_number="SLIP-2026-00012",
        employee=_make_employee(tax_state="FCT"),
        gross_pay=Decimal("1300.00"),
        earnings=[_make_earning("BASIC", "1300")],
        deductions=[_make_deduction("PAYE", "65")],
    )

    result = service._generate_fctirs_format(
        [abuja_slip_one, lagos_slip, abuja_slip_two],
        2026,
        1,
        organization_name="Dotmac Technologies Limited",
    )
    rows = list(csv.reader(io.StringIO(result.content.decode("utf-8"))))

    assert rows[4][0] == "1"
    assert rows[5][0] == "2"


def test_pension_export_uses_deductions_and_earnings():
    service = PensionExportService(db=None)
    slip = SimpleNamespace(
        slip_number="SLIP-2026-00002",
        employee=_make_employee(),
        earnings=[
            _make_earning("BASIC", "1000"),
            _make_earning("HOUSING", "500"),
            _make_earning("TRANSPORT", "500"),
        ],
        deductions=[
            _make_deduction("PENSION", "80"),
            _make_deduction("PENSION_EMPLOYER", "100"),
        ],
    )

    result = service._generate_generic_format([slip], 2026, 1)
    content = result.content.decode("utf-8")
    rows = list(csv.reader(io.StringIO(content)))

    assert len(rows) == 2
    data = rows[1]

    # Basic, Housing, Transport, BHT
    assert data[6] == "1000.00"
    assert data[7] == "500.00"
    assert data[8] == "500.00"
    assert data[9] == "2000.00"

    # Employee/Employer pension
    assert data[10] == "80.00"
    assert data[11] == "100.00"


def test_nhf_export_uses_deductions():
    service = NHFExportService(db=None)
    slip = SimpleNamespace(
        slip_number="SLIP-2026-00003",
        employee=_make_employee(),
        deductions=[_make_deduction("NHF", "25")],
    )

    result = service._generate_fmbn_format([slip], 2026, 1)
    content = result.content.decode("utf-8")
    rows = list(csv.reader(io.StringIO(content)))

    assert len(rows) == 2
    data = rows[1]
    assert data[4] == "25.00"
