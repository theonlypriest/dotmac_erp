"""
People (HR) Import API Endpoints.

CSV import endpoints for HR master data.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import (
    get_db_with_org,
    require_organization_id,
    require_tenant_auth,
)
from app.services.auth_dependencies import (
    get_current_user_id,
    require_tenant_permission,
)
from app.services.finance.import_export.base import (
    ImportConfig,
    ImportResult,
    PreviewResult,
)
from app.services.finance.import_export.import_service import ImportService
from app.services.people.hr.import_export import (
    DepartmentImporter,
    DesignationImporter,
    EmployeeImporter,
    EmploymentTypeImporter,
)
from app.services.upload_utils import get_env_max_bytes, write_upload_to_temp

router = APIRouter(
    prefix="/import",
    tags=["people-import"],
    dependencies=[Depends(require_tenant_auth)],
)


class EntityType(str, Enum):
    DEPARTMENTS = "departments"
    DESIGNATIONS = "designations"
    EMPLOYMENT_TYPES = "employment_types"
    EMPLOYEES = "employees"


class ImportOptions(BaseModel):
    skip_duplicates: bool = Field(default=True)
    dry_run: bool = Field(default=False)
    batch_size: int = Field(default=100, ge=1, le=1000)


class ImportResultResponse(BaseModel):
    entity_type: str
    status: str
    total_rows: int
    imported_count: int
    skipped_count: int
    duplicate_count: int
    error_count: int
    success_rate: str
    duration_seconds: float
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @classmethod
    def from_import_result(cls, result: ImportResult) -> ImportResultResponse:
        return cls(
            entity_type=result.entity_type,
            status=result.status.value,
            total_rows=result.total_rows,
            imported_count=result.imported_count,
            skipped_count=result.skipped_count,
            duplicate_count=result.duplicate_count,
            error_count=result.error_count,
            success_rate=f"{result.success_rate:.1f}%",
            duration_seconds=round(result.duration_seconds, 2),
            errors=[str(e) for e in result.errors[:50]],
            warnings=[str(w) for w in result.warnings[:50]],
        )


class ColumnMappingResponse(BaseModel):
    source: str
    target: str
    confidence: float
    samples: list[str] = Field(default_factory=list)


class ImportPreviewResponse(BaseModel):
    entity_type: str
    total_rows: int
    detected_columns: list[str]
    required_columns: list[str]
    optional_columns: list[str]
    missing_required: list[str]
    column_mappings: list[ColumnMappingResponse]
    sample_data: list[dict[str, Any]]
    validation_errors: list[str]
    detected_format: str
    is_valid: bool

    @classmethod
    def from_preview_result(cls, result: PreviewResult) -> ImportPreviewResponse:
        return cls(
            entity_type=result.entity_type,
            total_rows=result.total_rows,
            detected_columns=result.detected_columns,
            required_columns=result.required_columns,
            optional_columns=result.optional_columns,
            missing_required=result.missing_required,
            column_mappings=[
                ColumnMappingResponse(
                    source=m.source_column,
                    target=m.target_field,
                    confidence=m.confidence,
                    samples=m.sample_values[:3],
                )
                for m in result.column_mappings
            ],
            sample_data=result.sample_data,
            validation_errors=result.validation_errors,
            detected_format=result.detected_format,
            is_valid=result.is_valid,
        )


@router.get("/supported-types")
async def get_supported_types(
    auth: dict = Depends(require_tenant_permission("import:read")),
) -> dict[str, Any]:
    return {
        "entity_types": [
            {
                "type": "departments",
                "name": "Departments",
                "description": "Import departments",
                "required_columns": ["Department Code", "Department Name"],
                "optional_columns": ["Parent Department Code", "Cost Center Code"],
                "import_order": 1,
            },
            {
                "type": "designations",
                "name": "Designations",
                "description": "Import designations/titles",
                "required_columns": ["Designation Code", "Designation Name"],
                "optional_columns": ["Description"],
                "import_order": 2,
            },
            {
                "type": "employment_types",
                "name": "Employment Types",
                "description": "Import employment types",
                "required_columns": ["Employment Type Code", "Employment Type Name"],
                "optional_columns": ["Description"],
                "import_order": 3,
            },
            {
                "type": "employees",
                "name": "Employees",
                "description": "Import employees",
                "required_columns": [
                    "Employee Code",
                    "First Name",
                    "Last Name",
                    "Work Email",
                    "Date of Joining",
                ],
                "optional_columns": [
                    "Department Code",
                    "Designation Code",
                    "Employment Type Code",
                ],
                "import_order": 4,
                "prerequisites": ["departments", "designations", "employment_types"],
            },
        ],
        "recommended_order": [
            "departments",
            "designations",
            "employment_types",
            "employees",
        ],
    }


@router.post("/preview/{entity_type}")
async def preview_import(
    entity_type: EntityType,
    file: UploadFile = File(...),
    db: Session = Depends(get_db_with_org),
    org_id: UUID = Depends(require_organization_id),
    user_id: UUID = Depends(get_current_user_id),
    auth: dict = Depends(require_tenant_permission("import:preview")),
) -> ImportPreviewResponse:
    if not file.filename or not file.filename.endswith(".csv"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only CSV files are supported",
        )

    max_bytes = get_env_max_bytes("MAX_IMPORT_FILE_SIZE", 50 * 1024 * 1024)
    tmp_path = await write_upload_to_temp(
        file,
        suffix=".csv",
        max_bytes=max_bytes,
        error_detail=f"File too large. Maximum size: {max_bytes // 1024 // 1024}MB",
    )

    try:
        config = ImportConfig(
            organization_id=org_id,
            user_id=user_id,
            skip_duplicates=True,
            dry_run=True,
        )
        importer = _get_importer(entity_type, db, config)
        preview_result = importer.preview_file(tmp_path, max_rows=10)
        return ImportPreviewResponse.from_preview_result(preview_result)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Preview failed: {str(exc)}",
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)


@router.post("/{entity_type}")
async def import_data(
    entity_type: EntityType,
    file: UploadFile = File(...),
    skip_duplicates: bool = Form(default=True),
    dry_run: bool = Form(default=False),
    batch_size: int = Form(default=100),
    db: Session = Depends(get_db_with_org),
    org_id: UUID = Depends(require_organization_id),
    user_id: UUID = Depends(get_current_user_id),
    auth: dict = Depends(require_tenant_permission("import:execute")),
) -> ImportResultResponse:
    if not file.filename or not file.filename.endswith(".csv"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only CSV files are supported",
        )

    max_bytes = get_env_max_bytes("MAX_IMPORT_FILE_SIZE", 50 * 1024 * 1024)
    tmp_path = await write_upload_to_temp(
        file,
        suffix=".csv",
        max_bytes=max_bytes,
        error_detail=f"File too large. Maximum size: {max_bytes // 1024 // 1024}MB",
    )

    try:
        config = ImportConfig(
            organization_id=org_id,
            user_id=user_id,
            skip_duplicates=skip_duplicates,
            dry_run=dry_run,
            batch_size=batch_size,
        )
        importer = _get_importer(entity_type, db, config)
        result = ImportService.run_import(importer, tmp_path)
        return ImportResultResponse.from_import_result(result)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Import failed: {str(exc)}",
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _get_importer(entity_type: EntityType, db: Session, config: ImportConfig):
    if entity_type == EntityType.DEPARTMENTS:
        return DepartmentImporter(db, config)
    if entity_type == EntityType.DESIGNATIONS:
        return DesignationImporter(db, config)
    if entity_type == EntityType.EMPLOYMENT_TYPES:
        return EmploymentTypeImporter(db, config)
    if entity_type == EntityType.EMPLOYEES:
        return EmployeeImporter(db, config)
    raise ValueError(f"Unsupported entity type: {entity_type}")
