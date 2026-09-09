"""
Add Missing Contract Staff - January 2026.

Creates employee records for contract staff without employee codes.
Does NOT clear existing data - just adds missing employees.

Usage:
    poetry run python scripts/add_missing_contract_staff.py --organization-id <organization-uuid>

    # To see what would happen without making changes:
    poetry run python scripts/add_missing_contract_staff.py --organization-id <organization-uuid> --dry-run
"""

import argparse
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import UUID

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.session_context import session_for_org
from app.models.people.hr.department import Department
from app.models.people.hr.employee import Employee, EmployeeStatus
from app.models.people.payroll.salary_assignment import SalaryStructureAssignment
from app.models.people.payroll.salary_structure import SalaryStructure
from app.models.person import Person, PersonStatus
from app.services.people.hr.employment_types import EmploymentTypeService

# Excel file path
EXCEL_PATH = Path("/root/.dotmac/jan paye (2) (1).xlsx")

# Division to Department mapping
DIVISION_MAP = {
    "1. Executive": "Executive",
    "2. Technology & NOC": "Technology & NOC",
    "3. Customer Experience": "Customer Experience",
    "4. Commercial": "Commercial",
    "5. Service Delivery": "Service Delivery",
    "6. Finance": "Finance",
    "7. Corporate Services": "Corporate Services",
}


def get_admin_user_id(db: Session) -> UUID:
    """Get admin user ID."""
    result = db.execute(
        text(
            "SELECT person_id FROM public.user_credentials WHERE username = 'admin' LIMIT 1"
        )
    )
    row = result.fetchone()
    if row:
        return row[0]
    result = db.execute(text("SELECT person_id FROM public.user_credentials LIMIT 1"))
    row = result.fetchone()
    if not row:
        raise ValueError("No users found.")
    return row[0]


def get_next_employee_code(db: Session, org_id: UUID) -> int:
    """Get the next available employee code number."""
    result = db.execute(
        text("""
        SELECT employee_code FROM hr.employee
        WHERE organization_id = :org_id
        AND employee_code ~ '^EMP[0-9]+$'
        ORDER BY employee_code DESC
    """),
        {"org_id": org_id},
    )

    max_code = 0
    for row in result:
        try:
            code_num = int(row[0].replace("EMP", ""))
            if code_num > max_code:
                max_code = code_num
        except (ValueError, AttributeError):
            continue

    return max_code + 1


def get_or_create_department(
    db: Session, org_id: UUID, name: str, user_id: UUID
) -> Department:
    """Get or create a department."""
    dept = (
        db.query(Department)
        .filter(
            Department.organization_id == org_id,
            Department.department_name == name,
        )
        .first()
    )

    if not dept:
        dept = Department(
            organization_id=org_id,
            department_code=name.upper().replace(" ", "-")[:20],
            department_name=name,
            is_active=True,
            created_by_id=user_id,
        )
        db.add(dept)
        db.flush()

    return dept


def get_contract_structure(db: Session, org_id: UUID) -> SalaryStructure:
    """Get the contract staff salary structure."""
    struct = (
        db.query(SalaryStructure)
        .filter(
            SalaryStructure.organization_id == org_id,
            SalaryStructure.structure_code == "CONTRACT-STAFF",
        )
        .first()
    )

    if not struct:
        raise ValueError(
            "Contract staff salary structure not found. Run seed_payroll_from_excel.py first."
        )

    return struct


def parse_missing_staff(excel_path: Path) -> list[dict]:
    """Parse Excel file and find staff without employee codes."""
    print(f"Reading Excel file: {excel_path}")

    # Read contract staff sheet
    df = pd.read_excel(excel_path, sheet_name="Payroll (January Contract)", header=2)

    # Set column names from first data row
    df.columns = df.iloc[0].tolist()
    df = df.iloc[1:]

    # Filter to actual data rows
    df = df[pd.to_numeric(df["S/N"], errors="coerce").notna()]

    # Find rows with missing Employee Code
    missing = df[df["Employee Code"].isna() | (df["Employee Code"] == "")]

    staff = []
    for _, row in missing.iterrows():
        name = str(row["Name"]).strip() if pd.notna(row["Name"]) else ""
        if not name:
            continue

        division = str(row["Division"]).strip() if pd.notna(row["Division"]) else ""
        role = str(row["Role"]).strip() if pd.notna(row["Role"]) else ""

        # Get net pay (salary)
        net_pay = row.get("Net Pay") or row.get("Current Take-Home") or 0
        if pd.isna(net_pay):
            net_pay = 0

        staff.append(
            {
                "name": name,
                "division": division,
                "role": role,
                "net_pay": Decimal(str(net_pay)),
            }
        )

    print(f"  Found {len(staff)} staff without employee codes")
    return staff


def create_employee(
    db: Session,
    org_id: UUID,
    user_id: UUID,
    data: dict,
    employment_type_id: UUID,
    employee_code: str,
    structure: SalaryStructure,
) -> Employee:
    """Create a new employee record."""
    name_parts = data["name"].split()
    first_name = name_parts[0] if name_parts else "Unknown"
    last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else "Staff"

    # Generate placeholder email (required field)
    # Use format: employee_code@internal.dotmac.ng to indicate system-generated
    email = f"{employee_code.lower()}@internal.dotmac.ng"

    # Check if email already exists (shouldn't happen with employee code pattern)
    existing = db.query(Person).filter(Person.email == email).first()
    if existing:
        # Append timestamp to make unique
        import time

        email = f"{employee_code.lower()}.{int(time.time())}@internal.dotmac.ng"

    # Create Person record
    person = Person(
        organization_id=org_id,
        first_name=first_name,
        last_name=last_name,
        email=email,
        email_verified=False,  # Placeholder email
        status=PersonStatus.active,
        is_active=True,
        notes=f"Auto-created for contract staff: {data['role']}",
    )
    db.add(person)
    db.flush()

    # Get department
    dept_name = DIVISION_MAP.get(data["division"], "Corporate Services")
    dept = get_or_create_department(db, org_id, dept_name, user_id)

    # Create Employee record
    emp = Employee(
        organization_id=org_id,
        person_id=person.id,
        employee_code=employee_code,
        status=EmployeeStatus.ACTIVE,
        date_of_joining=date(2024, 1, 1),  # Default
        department_id=dept.department_id,
        employment_type_id=employment_type_id,
        notes=f"Role: {data['role']}",
        created_by_id=user_id,
    )
    db.add(emp)
    db.flush()

    # Create salary assignment
    assignment = SalaryStructureAssignment(
        organization_id=org_id,
        employee_id=emp.employee_id,
        structure_id=structure.structure_id,
        from_date=date(2026, 1, 1),
        base=data["net_pay"],
        created_by_id=user_id,
    )
    db.add(assignment)

    return emp


def main():
    parser = argparse.ArgumentParser(description="Add missing contract staff employees")
    parser.add_argument("--organization-id", type=UUID, required=True)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without making changes",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("Add Missing Contract Staff - January 2026")
    if args.dry_run:
        print("  Mode: DRY RUN (no changes will be made)")
    print("=" * 60)

    if not EXCEL_PATH.exists():
        print(f"ERROR: Excel file not found: {EXCEL_PATH}")
        sys.exit(1)

    org_id = args.organization_id
    with session_for_org(org_id) as db:
        try:
            user_id = get_admin_user_id(db)
            print(f"Organization ID: {org_id}")
            print(f"Admin User ID: {user_id}")

            # Fail before any employee, Person, department, or payroll mutation.
            employment_type = EmploymentTypeService(db, org_id).require_active_by_code(
                "CONTRACT"
            )
            structure = get_contract_structure(db, org_id)
            missing_staff = parse_missing_staff(EXCEL_PATH)
            next_code = get_next_employee_code(db, org_id)
            print(f"Next employee code: EMP{next_code:05d}")
            print(f"Salary Structure: {structure.structure_name}")

            if not missing_staff:
                print("\nNo missing staff found!")
                return

            print("\nCreating employee records...")
            created = []

            for i, data in enumerate(missing_staff):
                employee_code = f"EMP{next_code + i:05d}"

                if args.dry_run:
                    print(
                        f"  [DRY RUN] Would create: {data['name']} -> {employee_code}"
                    )
                    print(
                        f"             Role: {data['role']}, Salary: {data['net_pay']}"
                    )
                else:
                    create_employee(
                        db,
                        org_id,
                        user_id,
                        data,
                        employment_type.employment_type_id,
                        employee_code,
                        structure,
                    )
                    created.append((data["name"], employee_code))
                    print(
                        f"  + Created: {data['name']} -> {employee_code} "
                        f"(Salary: {data['net_pay']})"
                    )

            if args.dry_run:
                print("\n[DRY RUN - No changes committed]")
                db.rollback()
            else:
                db.commit()
                print("\n" + "=" * 60)
                print("SUMMARY")
                print("=" * 60)
                print(f"  Created {len(created)} new employees:")
                for name, code in created:
                    print(f"    - {code}: {name}")
                print("=" * 60)
                print("\nSUCCESS: Employees created successfully!")

        except Exception as exc:
            db.rollback()
            print(f"\nERROR: {exc}")
            import traceback

            traceback.print_exc()
            sys.exit(1)


if __name__ == "__main__":
    main()
