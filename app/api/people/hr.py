"""
HR API Router.

Thin API wrapper for HR Core endpoints. All business logic is in services.
"""

import json
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    status,
)
from sqlalchemy.orm import Session

from app.api.deps import (
    get_db_with_org,
    require_organization_id,
    require_tenant_auth,
    require_tenant_permission,
)
from app.models.finance.core_org.location import LocationType
from app.models.people.hr.checklist_template import ChecklistTemplateType
from app.net import get_request_host, get_request_scheme
from app.schemas.auth import UserCredentialRead
from app.schemas.people.checklist import (
    ChecklistTemplateCreate,
    ChecklistTemplateListResponse,
    ChecklistTemplateRead,
    ChecklistTemplateUpdate,
)
from app.schemas.people.hr import (
    BulkDeleteRequest,
    BulkOperationResponse,
    BulkUpdateRequest,
    # Department
    DepartmentCreate,
    DepartmentListResponse,
    DepartmentRead,
    DepartmentUpdate,
    # Designation
    DesignationCreate,
    DesignationRead,
    DesignationUpdate,
    # Employee
    EmployeeCreate,
    # Employee Grade
    EmployeeGradeCreate,
    EmployeeGradeRead,
    EmployeeGradeUpdate,
    EmployeeListResponse,
    EmployeeRead,
    EmployeeStatsRead,
    EmployeeUpdate,
    EmployeeUserCredentialCreate,
    EmployeeUserLink,
    # Employment Type
    EmploymentTypeCreate,
    EmploymentTypeRead,
    EmploymentTypeUpdate,
    LocationCreate,
    LocationListResponse,
    LocationRead,
    LocationUpdate,
    RehireRequest,
    ResignationRequest,
    TerminationRequest,
)
from app.services.common import PaginationParams
from app.services.people.hr import (
    BulkUpdateData,
    DepartmentCreateData,
    DepartmentFilters,
    DepartmentUpdateData,
    DesignationCreateData,
    DesignationFilters,
    DesignationUpdateData,
    EmployeeCreateData,
    EmployeeFilters,
    EmployeeGradeCreateData,
    EmployeeGradeFilters,
    EmployeeGradeUpdateData,
    EmployeeService,
    EmployeeUpdateData,
    EmploymentTypeCreateData,
    EmploymentTypeFilters,
    EmploymentTypeUpdateData,
    OrganizationService,
    TerminationData,
)
from app.services.people.hr.checklist_templates import ChecklistTemplateService
from app.services.people.hr.employee_filter_engine import (
    parse_employee_filter_payload_json,
)
from app.services.people.hr.employees import send_employee_access_invite_background
from app.services.people.hr.employment_types import EmploymentTypeService

router = APIRouter(
    prefix="/hr",
    tags=["hr"],
    dependencies=[Depends(require_tenant_auth)],
)


@dataclass(frozen=True, slots=True)
class _EmploymentTypeActor:
    id: UUID


def _employment_type_actor(auth: dict) -> _EmploymentTypeActor | None:
    person_id = auth.get("person_id")
    return _EmploymentTypeActor(UUID(str(person_id))) if person_id else None


def _resolve_app_url(request: Request) -> str:
    scheme = get_request_scheme(request)
    host = get_request_host(request) or request.url.netloc
    return f"{scheme}://{host}".rstrip("/")


def parse_enum(value: str | None, enum_type, field_name: str):
    if value is None:
        return None
    try:
        return enum_type(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"Invalid {field_name}: {value}"
        ) from exc


FINAL_PAYROLL_EDITOR_ROLES = {"admin", "hr_director", "hr_manager"}


def _require_final_payroll_editor(auth: dict) -> None:
    roles = {
        str(role).strip().lower() for role in auth.get("roles", []) if str(role).strip()
    }
    if not roles.intersection(FINAL_PAYROLL_EDITOR_ROLES):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin, HR Director, or HR Manager can enable final payroll",
        )


def _payload_updates_final_payroll(payload, *field_names: str) -> bool:
    provided_fields: AbstractSet[str] = getattr(payload, "model_fields_set", set())
    return any(field_name in provided_fields for field_name in field_names)


# =============================================================================
# Departments
# =============================================================================


@router.get("/departments", response_model=DepartmentListResponse)
def list_departments(
    organization_id: UUID = Depends(require_organization_id),
    search: str | None = None,
    is_active: bool | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db_with_org),
):
    """List departments."""
    svc = OrganizationService(db, organization_id)
    filters = DepartmentFilters(search=search, is_active=is_active)
    result = svc.list_departments(filters, PaginationParams(offset=offset, limit=limit))
    return DepartmentListResponse(
        items=[DepartmentRead.model_validate(d) for d in result.items],
        total=result.total,
        offset=offset,
        limit=limit,
    )


@router.post(
    "/departments", response_model=DepartmentRead, status_code=status.HTTP_201_CREATED
)
def create_department(
    payload: DepartmentCreate,
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
):
    """Create a department."""
    svc = OrganizationService(db, organization_id)
    data = DepartmentCreateData(
        department_code=payload.department_code,
        department_name=payload.department_name,
        description=payload.description,
        parent_department_id=payload.parent_department_id,
        cost_center_id=payload.cost_center_id,
        is_active=payload.is_active,
    )
    dept = svc.create_department(data)
    return DepartmentRead.model_validate(dept)


@router.get("/departments/{department_id}", response_model=DepartmentRead)
def get_department(
    department_id: UUID,
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
):
    """Get a department by ID."""
    svc = OrganizationService(db, organization_id)
    return DepartmentRead.model_validate(svc.get_department(department_id))


@router.patch("/departments/{department_id}", response_model=DepartmentRead)
def update_department(
    department_id: UUID,
    payload: DepartmentUpdate,
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
):
    """Update a department."""
    svc = OrganizationService(db, organization_id)
    data = DepartmentUpdateData(
        department_code=payload.department_code,
        department_name=payload.department_name,
        description=payload.description,
        parent_department_id=payload.parent_department_id,
        cost_center_id=payload.cost_center_id,
        is_active=payload.is_active,
    )
    dept = svc.update_department(department_id, data)
    return DepartmentRead.model_validate(dept)


@router.delete("/departments/{department_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_department(
    department_id: UUID,
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
):
    """Delete a department."""
    svc = OrganizationService(db, organization_id)
    svc.delete_department(department_id)


# =============================================================================
# Designations
# =============================================================================


@router.get("/designations")
def list_designations(
    organization_id: UUID = Depends(require_organization_id),
    search: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db_with_org),
):
    """List designations."""
    svc = OrganizationService(db, organization_id)
    filters = DesignationFilters(search=search)
    result = svc.list_designations(
        filters, PaginationParams(offset=offset, limit=limit)
    )
    return {
        "items": [DesignationRead.model_validate(d) for d in result.items],
        "total": result.total,
        "offset": offset,
        "limit": limit,
    }


# =============================================================================
# Office Locations
# =============================================================================


@router.get("/locations", response_model=LocationListResponse)
def list_locations(
    organization_id: UUID = Depends(require_organization_id),
    search: str | None = None,
    is_active: bool | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db_with_org),
):
    svc = OrganizationService(db, organization_id)
    result = svc.list_locations(
        search=search,
        is_active=is_active,
        pagination=PaginationParams(offset=offset, limit=limit),
    )
    return LocationListResponse(
        items=[LocationRead.model_validate(i) for i in result.items],
        total=result.total,
        offset=offset,
        limit=limit,
    )


@router.post(
    "/locations", response_model=LocationRead, status_code=status.HTTP_201_CREATED
)
def create_location(
    payload: LocationCreate,
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
):
    location_type = parse_enum(payload.location_type, LocationType, "location_type")
    svc = OrganizationService(db, organization_id)
    location = svc.create_location(
        location_code=payload.location_code,
        location_name=payload.location_name,
        location_type=location_type,
        address_line_1=payload.address_line_1,
        address_line_2=payload.address_line_2,
        city=payload.city,
        state_province=payload.state_province,
        postal_code=payload.postal_code,
        country_code=payload.country_code,
        latitude=float(payload.latitude) if payload.latitude is not None else None,
        longitude=float(payload.longitude) if payload.longitude is not None else None,
        geofence_radius_m=payload.geofence_radius_m,
        geofence_enabled=payload.geofence_enabled,
        geofence_polygon=(
            json.dumps(payload.geofence_polygon)
            if payload.geofence_polygon is not None
            else None
        ),
        is_active=payload.is_active,
    )
    return LocationRead.model_validate(location)


@router.get("/locations/{location_id}", response_model=LocationRead)
def get_location(
    location_id: UUID,
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
):
    svc = OrganizationService(db, organization_id)
    return LocationRead.model_validate(svc.get_location(location_id))


@router.patch("/locations/{location_id}", response_model=LocationRead)
def update_location(
    location_id: UUID,
    payload: LocationUpdate,
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
):
    update_data = payload.model_dump(exclude_unset=True)
    if "location_type" in update_data:
        update_data["location_type"] = parse_enum(
            update_data["location_type"],
            LocationType,
            "location_type",
        )
    svc = OrganizationService(db, organization_id)
    location = svc.update_location(location_id, update_data)
    return LocationRead.model_validate(location)


@router.delete("/locations/{location_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_location(
    location_id: UUID,
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
):
    svc = OrganizationService(db, organization_id)
    svc.delete_location(location_id)


# =============================================================================
# Checklist Templates
# =============================================================================


@router.get("/checklist-templates", response_model=ChecklistTemplateListResponse)
def list_checklist_templates(
    organization_id: UUID = Depends(require_organization_id),
    template_type: str | None = None,
    is_active: bool | None = None,
    search: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db_with_org),
):
    svc = ChecklistTemplateService(db)
    template_enum = parse_enum(template_type, ChecklistTemplateType, "template_type")
    result = svc.list_templates(
        org_id=organization_id,
        template_type=template_enum,
        is_active=is_active,
        search=search,
        pagination=PaginationParams(offset=offset, limit=limit),
    )
    return ChecklistTemplateListResponse(
        items=[ChecklistTemplateRead.model_validate(t) for t in result.items],
        total=result.total,
        offset=offset,
        limit=limit,
    )


@router.post(
    "/checklist-templates",
    response_model=ChecklistTemplateRead,
    status_code=status.HTTP_201_CREATED,
)
def create_checklist_template(
    payload: ChecklistTemplateCreate,
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
):
    svc = ChecklistTemplateService(db)
    template = svc.create_template(
        org_id=organization_id,
        template_code=payload.template_code,
        template_name=payload.template_name,
        description=payload.description,
        template_type=payload.template_type,
        is_active=payload.is_active,
        items=[item.model_dump() for item in payload.items],
    )
    return ChecklistTemplateRead.model_validate(template)


@router.get("/checklist-templates/{template_id}", response_model=ChecklistTemplateRead)
def get_checklist_template(
    template_id: UUID,
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
):
    svc = ChecklistTemplateService(db)
    return ChecklistTemplateRead.model_validate(
        svc.get_template(organization_id, template_id)
    )


@router.patch(
    "/checklist-templates/{template_id}", response_model=ChecklistTemplateRead
)
def update_checklist_template(
    template_id: UUID,
    payload: ChecklistTemplateUpdate,
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
):
    svc = ChecklistTemplateService(db)
    update_data = payload.model_dump(exclude_unset=True)
    if "items" in update_data and update_data["items"] is not None:
        update_data["items"] = [item.model_dump() for item in update_data["items"]]
    template = svc.update_template(organization_id, template_id, **update_data)
    return ChecklistTemplateRead.model_validate(template)


@router.delete(
    "/checklist-templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT
)
def delete_checklist_template(
    template_id: UUID,
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
):
    svc = ChecklistTemplateService(db)
    svc.delete_template(organization_id, template_id)


@router.post(
    "/designations", response_model=DesignationRead, status_code=status.HTTP_201_CREATED
)
def create_designation(
    payload: DesignationCreate,
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
):
    """Create a designation."""
    svc = OrganizationService(db, organization_id)
    data = DesignationCreateData(
        designation_code=payload.designation_code,
        designation_name=payload.designation_name,
        description=payload.description,
        is_active=payload.is_active,
    )
    desig = svc.create_designation(data)
    return DesignationRead.model_validate(desig)


@router.get("/designations/{designation_id}", response_model=DesignationRead)
def get_designation(
    designation_id: UUID,
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
):
    """Get a designation by ID."""
    svc = OrganizationService(db, organization_id)
    return DesignationRead.model_validate(svc.get_designation(designation_id))


@router.patch("/designations/{designation_id}", response_model=DesignationRead)
def update_designation(
    designation_id: UUID,
    payload: DesignationUpdate,
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
):
    """Update a designation."""
    svc = OrganizationService(db, organization_id)
    data = DesignationUpdateData(
        designation_code=payload.designation_code,
        designation_name=payload.designation_name,
        description=payload.description,
        is_active=payload.is_active,
    )
    desig = svc.update_designation(designation_id, data)
    return DesignationRead.model_validate(desig)


@router.delete("/designations/{designation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_designation(
    designation_id: UUID,
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
):
    """Delete a designation."""
    svc = OrganizationService(db, organization_id)
    svc.delete_designation(designation_id)


# =============================================================================
# Employment Types
# =============================================================================


@router.get("/employment-types")
def list_employment_types(
    organization_id: UUID = Depends(require_organization_id),
    search: str | None = None,
    is_active: bool | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    _auth: dict = Depends(require_tenant_permission("hr:employment_types:read")),
    db: Session = Depends(get_db_with_org),
):
    """List employment types."""
    svc = EmploymentTypeService(db, organization_id, _employment_type_actor(_auth))
    filters = EmploymentTypeFilters(search=search, is_active=is_active)
    result = svc.list_employment_types(
        filters, PaginationParams(offset=offset, limit=limit)
    )
    return {
        "items": [EmploymentTypeRead.model_validate(t) for t in result.items],
        "total": result.total,
        "offset": offset,
        "limit": limit,
    }


@router.post(
    "/employment-types",
    response_model=EmploymentTypeRead,
    status_code=status.HTTP_201_CREATED,
)
def create_employment_type(
    payload: EmploymentTypeCreate,
    organization_id: UUID = Depends(require_organization_id),
    _auth: dict = Depends(require_tenant_permission("hr:employment_types:manage")),
    db: Session = Depends(get_db_with_org),
):
    """Create an employment type."""
    svc = EmploymentTypeService(db, organization_id, _employment_type_actor(_auth))
    data = EmploymentTypeCreateData(
        type_code=payload.type_code,
        type_name=payload.type_name,
        description=payload.description,
        is_active=payload.is_active,
    )
    emp_type = svc.create_employment_type(data)
    return EmploymentTypeRead.model_validate(emp_type)


@router.get("/employment-types/{employment_type_id}", response_model=EmploymentTypeRead)
def get_employment_type(
    employment_type_id: UUID,
    organization_id: UUID = Depends(require_organization_id),
    _auth: dict = Depends(require_tenant_permission("hr:employment_types:read")),
    db: Session = Depends(get_db_with_org),
):
    """Get an employment type by ID."""
    svc = EmploymentTypeService(db, organization_id, _employment_type_actor(_auth))
    return EmploymentTypeRead.model_validate(
        svc.get_employment_type(employment_type_id)
    )


@router.patch(
    "/employment-types/{employment_type_id}", response_model=EmploymentTypeRead
)
def update_employment_type(
    employment_type_id: UUID,
    payload: EmploymentTypeUpdate,
    organization_id: UUID = Depends(require_organization_id),
    _auth: dict = Depends(require_tenant_permission("hr:employment_types:manage")),
    db: Session = Depends(get_db_with_org),
):
    """Update an employment type."""
    svc = EmploymentTypeService(db, organization_id, _employment_type_actor(_auth))
    data = EmploymentTypeUpdateData(
        type_code=payload.type_code,
        type_name=payload.type_name,
        description=payload.description,
        description_is_set="description" in payload.model_fields_set,
        is_active=payload.is_active,
    )
    emp_type = svc.update_employment_type(employment_type_id, data)
    return EmploymentTypeRead.model_validate(emp_type)


@router.delete(
    "/employment-types/{employment_type_id}", status_code=status.HTTP_204_NO_CONTENT
)
def delete_employment_type(
    employment_type_id: UUID,
    organization_id: UUID = Depends(require_organization_id),
    _auth: dict = Depends(require_tenant_permission("hr:employment_types:manage")),
    db: Session = Depends(get_db_with_org),
):
    """Deactivate an employment type."""
    svc = EmploymentTypeService(db, organization_id, _employment_type_actor(_auth))
    svc.deactivate_employment_type(employment_type_id)


# =============================================================================
# Employee Grades
# =============================================================================


@router.get("/grades")
def list_employee_grades(
    organization_id: UUID = Depends(require_organization_id),
    search: str | None = None,
    is_active: bool | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db_with_org),
):
    """List employee grades."""
    svc = OrganizationService(db, organization_id)
    filters = EmployeeGradeFilters(search=search, is_active=is_active)
    result = svc.list_employee_grades(
        filters, PaginationParams(offset=offset, limit=limit)
    )
    return {
        "items": [EmployeeGradeRead.model_validate(g) for g in result.items],
        "total": result.total,
        "offset": offset,
        "limit": limit,
    }


@router.post(
    "/grades", response_model=EmployeeGradeRead, status_code=status.HTTP_201_CREATED
)
def create_employee_grade(
    payload: EmployeeGradeCreate,
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
):
    """Create an employee grade."""
    svc = OrganizationService(db, organization_id)
    data = EmployeeGradeCreateData(
        grade_code=payload.grade_code,
        grade_name=payload.grade_name,
        description=payload.description,
        rank=payload.rank,
        min_salary=payload.min_salary,
        max_salary=payload.max_salary,
        is_active=payload.is_active,
    )
    grade = svc.create_employee_grade(data)
    return EmployeeGradeRead.model_validate(grade)


@router.get("/grades/{grade_id}", response_model=EmployeeGradeRead)
def get_employee_grade(
    grade_id: UUID,
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
):
    """Get an employee grade by ID."""
    svc = OrganizationService(db, organization_id)
    return EmployeeGradeRead.model_validate(svc.get_employee_grade(grade_id))


@router.patch("/grades/{grade_id}", response_model=EmployeeGradeRead)
def update_employee_grade(
    grade_id: UUID,
    payload: EmployeeGradeUpdate,
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
):
    """Update an employee grade."""
    svc = OrganizationService(db, organization_id)
    data = EmployeeGradeUpdateData(
        grade_code=payload.grade_code,
        grade_name=payload.grade_name,
        description=payload.description,
        rank=payload.rank,
        min_salary=payload.min_salary,
        max_salary=payload.max_salary,
        is_active=payload.is_active,
    )
    grade = svc.update_employee_grade(grade_id, data)
    return EmployeeGradeRead.model_validate(grade)


@router.delete("/grades/{grade_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_employee_grade(
    grade_id: UUID,
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
):
    """Delete an employee grade."""
    svc = OrganizationService(db, organization_id)
    svc.delete_employee_grade(grade_id)


# =============================================================================
# Employees
# =============================================================================


@router.get("/employees", response_model=EmployeeListResponse)
def list_employees(
    organization_id: UUID = Depends(require_organization_id),
    search: str | None = None,
    status: str | None = None,
    department_id: UUID | None = None,
    designation_id: UUID | None = None,
    reports_to_id: UUID | None = None,
    expense_approver_id: UUID | None = None,
    include_deleted: bool = False,
    filters: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db_with_org),
):
    """List employees."""
    svc = EmployeeService(db, organization_id)
    employee_filters = EmployeeFilters(
        search=search,
        status=status,
        department_id=department_id,
        designation_id=designation_id,
        reports_to_id=reports_to_id,
        expense_approver_id=expense_approver_id,
        include_deleted=include_deleted,
    )
    try:
        advanced_expression = parse_employee_filter_payload_json(filters)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    result = svc.list_employees(
        employee_filters,
        PaginationParams(offset=offset, limit=limit),
        advanced_filter_expression=advanced_expression,
    )
    return EmployeeListResponse(
        items=[EmployeeRead.model_validate(e) for e in result.items],
        total=result.total,
        offset=offset,
        limit=limit,
    )


@router.get("/employees/stats", response_model=EmployeeStatsRead)
def get_employee_stats(
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
):
    """Get employee statistics."""
    svc = EmployeeService(db, organization_id)
    return svc.get_employee_stats()


@router.post(
    "/employees", response_model=EmployeeRead, status_code=status.HTTP_201_CREATED
)
def create_employee(
    payload: EmployeeCreate,
    request: Request,
    background_tasks: BackgroundTasks,
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
):
    """Create an employee."""
    svc = EmployeeService(db, organization_id)
    data = EmployeeCreateData(
        employee_number=payload.employee_code,
        department_id=payload.department_id,
        designation_id=payload.designation_id,
        employment_type_id=payload.employment_type_id,
        grade_id=payload.grade_id,
        reports_to_id=payload.reports_to_id,
        expense_approver_id=payload.expense_approver_id,
        assigned_location_id=payload.assigned_location_id,
        default_shift_type_id=payload.default_shift_type_id,
        date_of_joining=payload.date_of_joining,
        status=payload.status,
        cost_center_id=payload.cost_center_id,
        ctc=payload.ctc,
        salary_mode=payload.salary_mode,
        bank_name=payload.bank_name,
        bank_account_number=payload.bank_account_number,
        bank_sort_code=payload.bank_branch_code,
        bank_account_name=payload.bank_account_name,
        notes=payload.notes,
    )
    emp = svc.create_employee(payload.person_id, data)
    employee_id = emp.employee_id
    response = EmployeeRead.model_validate(emp)
    app_url = _resolve_app_url(request)
    db.commit()
    background_tasks.add_task(
        send_employee_access_invite_background,
        organization_id,
        employee_id,
        app_url,
    )
    return response


@router.get("/employees/{employee_id}", response_model=EmployeeRead)
def get_employee(
    employee_id: UUID,
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
):
    """Get an employee by ID."""
    svc = EmployeeService(db, organization_id)
    return EmployeeRead.model_validate(svc.get_employee(employee_id))


@router.patch("/employees/{employee_id}", response_model=EmployeeRead)
def update_employee(
    employee_id: UUID,
    payload: EmployeeUpdate,
    auth: dict = Depends(require_tenant_auth),
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
):
    """Update an employee."""
    if _payload_updates_final_payroll(
        payload,
        "eligible_for_final_payroll",
        "final_payroll_cutoff_date",
    ):
        _require_final_payroll_editor(auth)
    svc = EmployeeService(db, organization_id)
    data = EmployeeUpdateData(
        employee_number=payload.employee_code,
        department_id=payload.department_id,
        designation_id=payload.designation_id,
        employment_type_id=payload.employment_type_id,
        grade_id=payload.grade_id,
        reports_to_id=payload.reports_to_id,
        expense_approver_id=payload.expense_approver_id,
        assigned_location_id=payload.assigned_location_id,
        default_shift_type_id=payload.default_shift_type_id,
        date_of_joining=payload.date_of_joining,
        date_of_leaving=payload.date_of_leaving,
        final_payroll_cutoff_date=payload.final_payroll_cutoff_date,
        status=payload.status,
        cost_center_id=payload.cost_center_id,
        ctc=payload.ctc,
        salary_mode=payload.salary_mode,
        bank_name=payload.bank_name,
        bank_account_number=payload.bank_account_number,
        bank_sort_code=payload.bank_branch_code,
        bank_account_name=payload.bank_account_name,
        notes=payload.notes,
        eligible_for_final_payroll=payload.eligible_for_final_payroll,
    )
    emp = svc.update_employee(employee_id, data)
    return EmployeeRead.model_validate(emp)


@router.post(
    "/employees/{employee_id}/user-credentials",
    response_model=UserCredentialRead,
    status_code=status.HTTP_201_CREATED,
)
def create_employee_user_credentials(
    employee_id: UUID,
    payload: EmployeeUserCredentialCreate,
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
):
    """Create user credentials for an employee's linked Person."""
    svc = EmployeeService(db, organization_id)
    credential = svc.create_user_credentials_for_employee(
        employee_id,
        username=payload.username,
        password=payload.password,
        provider=payload.provider,
        must_change_password=payload.must_change_password,
    )
    return UserCredentialRead.model_validate(credential)


@router.patch("/employees/{employee_id}/link-user", response_model=EmployeeRead)
def link_employee_user(
    employee_id: UUID,
    payload: EmployeeUserLink,
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
):
    """Link an employee to an existing user (Person)."""
    svc = EmployeeService(db, organization_id)
    emp = svc.link_employee_to_person(employee_id, payload.person_id)
    return EmployeeRead.model_validate(emp)


@router.delete("/employees/{employee_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_employee(
    employee_id: UUID,
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
):
    """Delete an employee (soft delete)."""
    svc = EmployeeService(db, organization_id)
    svc.delete_employee(employee_id)


# =============================================================================
# Employee Status Actions
# =============================================================================


@router.post("/employees/{employee_id}/activate", response_model=EmployeeRead)
def activate_employee(
    employee_id: UUID,
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
):
    """Activate an employee."""
    svc = EmployeeService(db, organization_id)
    emp = svc.activate_employee(employee_id)
    return EmployeeRead.model_validate(emp)


@router.post("/employees/{employee_id}/suspend", response_model=EmployeeRead)
def suspend_employee(
    employee_id: UUID,
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
):
    """Suspend an employee."""
    svc = EmployeeService(db, organization_id)
    emp = svc.suspend_employee(employee_id)
    return EmployeeRead.model_validate(emp)


@router.post("/employees/{employee_id}/terminate", response_model=EmployeeRead)
def terminate_employee(
    employee_id: UUID,
    payload: TerminationRequest,
    auth: dict = Depends(require_tenant_auth),
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
):
    """Terminate an employee."""
    if _payload_updates_final_payroll(
        payload,
        "eligible_for_final_payroll",
        "final_payroll_cutoff_date",
    ):
        _require_final_payroll_editor(auth)
    svc = EmployeeService(db, organization_id)
    data = TerminationData(
        date_of_leaving=payload.date_of_leaving,
        reason=payload.reason,
        exit_interview_notes=payload.exit_interview_notes,
        eligible_for_final_payroll=payload.eligible_for_final_payroll,
        final_payroll_cutoff_date=payload.final_payroll_cutoff_date,
    )
    emp = svc.terminate_employee(employee_id, data)
    return EmployeeRead.model_validate(emp)


@router.post("/employees/{employee_id}/resign", response_model=EmployeeRead)
def resign_employee(
    employee_id: UUID,
    payload: ResignationRequest,
    auth: dict = Depends(require_tenant_auth),
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
):
    """Record employee resignation."""
    if _payload_updates_final_payroll(
        payload,
        "eligible_for_final_payroll",
        "final_payroll_cutoff_date",
    ):
        _require_final_payroll_editor(auth)
    svc = EmployeeService(db, organization_id)
    emp = svc.resign_employee(
        employee_id,
        payload.date_of_leaving,
        eligible_for_final_payroll=payload.eligible_for_final_payroll,
        final_payroll_cutoff_date=payload.final_payroll_cutoff_date,
    )
    return EmployeeRead.model_validate(emp)


@router.post("/employees/{employee_id}/rehire", response_model=EmployeeRead)
def rehire_employee(
    employee_id: UUID,
    payload: RehireRequest,
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
):
    """Rehire a previously separated employee."""
    svc = EmployeeService(db, organization_id)
    emp = svc.rehire_employee(
        employee_id,
        payload.date_of_rejoining,
        notes=payload.notes,
    )
    return EmployeeRead.model_validate(emp)


# =============================================================================
# Bulk Operations
# =============================================================================


@router.post("/employees/bulk-update", response_model=BulkOperationResponse)
def bulk_update_employees(
    payload: BulkUpdateRequest,
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
):
    """Bulk update employees."""
    svc = EmployeeService(db, organization_id)
    data = BulkUpdateData(
        ids=payload.ids,
        department_id=payload.department_id,
        designation_id=payload.designation_id,
        status=payload.status,
        reports_to_id=payload.reports_to_id,
    )
    result = svc.bulk_update(data)
    return BulkOperationResponse(
        updated_count=result.updated_count,
        failed_ids=result.failed_ids,
        errors=result.errors,
    )


@router.post("/employees/bulk-delete", response_model=BulkOperationResponse)
def bulk_delete_employees(
    payload: BulkDeleteRequest,
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
):
    """Bulk delete employees."""
    svc = EmployeeService(db, organization_id)
    result = svc.bulk_delete(payload.ids)
    return BulkOperationResponse(
        deleted_count=result.deleted_count,
        failed_ids=result.failed_ids,
        errors=result.errors,
    )
