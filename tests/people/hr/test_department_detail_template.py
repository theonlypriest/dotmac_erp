from datetime import date
from types import SimpleNamespace
from uuid import uuid4

from app.templates import templates
from app.services.people.hr.web.employee_web import _can_manage_departments
from app.web.deps import WebAuthContext


def test_department_detail_renders_employee_table_rows() -> None:
    employment_type_id = uuid4()
    employee = SimpleNamespace(
        employee_id=uuid4(),
        employee_code="EMP-001",
        full_name="Ada Lovelace",
        person=SimpleNamespace(email="ada@example.com"),
        designation=SimpleNamespace(designation_name="Engineer"),
        employment_type_id=employment_type_id,
        date_of_joining=date(2026, 1, 2),
        status=SimpleNamespace(value="ACTIVE"),
    )
    department = SimpleNamespace(
        department_id=uuid4(),
        department_name="Engineering",
        department_code="ENG",
        description="",
        is_active=True,
        head=None,
    )
    headcount = SimpleNamespace(
        total_employees=1,
        active_employees=1,
        on_leave=0,
        terminated=0,
    )

    html = templates.env.get_template("people/hr/department_detail.html").render(
        request=SimpleNamespace(state=SimpleNamespace()),
        department=department,
        headcount=headcount,
        employees=[employee],
        employment_types_by_id={
            employment_type_id: SimpleNamespace(type_name="Full Time")
        },
        page=1,
        total_pages=1,
        total=1,
        has_prev=False,
        has_next=False,
        active_module="departments",
        accessible_modules=["people"],
        user=SimpleNamespace(),
        current_user=SimpleNamespace(),
        organization=SimpleNamespace(),
        performance_private_enabled=False,
        performance_government_enabled=False,
    )

    assert "Employee" in html
    assert "Ada Lovelace" in html
    assert "EMP-001" in html
    assert "Engineer" in html
    assert "Full Time" in html


def test_department_detail_renders_delete_button_for_department_managers() -> None:
    department = SimpleNamespace(
        department_id=uuid4(),
        department_name="Engineering",
        department_code="ENG",
        description="",
        is_active=True,
        head=None,
    )
    headcount = SimpleNamespace(
        total_employees=0,
        active_employees=0,
        on_leave=0,
        terminated=0,
    )

    html = templates.env.get_template("people/hr/department_detail.html").render(
        request=SimpleNamespace(
            state=SimpleNamespace(csrf_form='<input name="csrf_token">')
        ),
        department=department,
        headcount=headcount,
        employees=[],
        employment_types_by_id={},
        success=None,
        error=None,
        can_manage_departments=True,
        page=1,
        total_pages=1,
        total=0,
        has_prev=False,
        has_next=False,
        active_module="departments",
        accessible_modules=["people"],
        user=SimpleNamespace(),
        current_user=SimpleNamespace(),
        organization=SimpleNamespace(),
        performance_private_enabled=False,
        performance_government_enabled=False,
    )

    assert "Delete Department" in html
    assert f"/people/hr/departments/{department.department_id}/delete" in html


def test_department_manage_check_accepts_admin_role_case_insensitively() -> None:
    auth = WebAuthContext(is_authenticated=True, roles=["Admin"], scopes=[])

    assert _can_manage_departments(auth)
