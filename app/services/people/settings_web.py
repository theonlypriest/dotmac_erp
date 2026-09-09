"""
People Settings Web Service.

Provides context and update functions for HR/People settings UI pages.
"""

import logging
import mimetypes
import uuid
from inspect import isawaitable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from app.models.finance.core_org import Organization
from app.models.finance.core_org.location import Location
from app.services.file_upload import FileUploadError, get_hr_invite_attachment_upload
from app.services.people.hr.invite_attachment import (
    clear_default_invite_attachment_metadata,
    get_default_invite_attachment_metadata,
    set_default_invite_attachment_metadata,
)
from app.services.people.hr.invite_email import (
    EMPLOYEE_INVITE_NEXT_URL,
    default_employee_invite_email_template,
    get_employee_invite_email_template,
    set_employee_invite_email_template,
)
from app.rls import tenant_context
from app.services.storage import get_storage

logger = logging.getLogger(__name__)

# Payroll frequency options
PAYROLL_FREQUENCIES = [
    ("MONTHLY", "Monthly"),
    ("BIWEEKLY", "Bi-weekly"),
    ("WEEKLY", "Weekly"),
]

# Attendance mode options
ATTENDANCE_MODES = [
    ("MANUAL", "Manual entry"),
    ("BIOMETRIC", "Biometric device"),
    ("GEOFENCED", "Geofenced mobile"),
]

# Employee ID format placeholders
EMPLOYEE_ID_PLACEHOLDERS = [
    ("{PREFIX}", "Configurable prefix (e.g., EMP)"),
    ("{YYYY}", "4-digit year"),
    ("{YY}", "2-digit year"),
    ("{SEQ}", "Sequential number"),
    ("{SEQ:4}", "Sequential number with minimum 4 digits"),
]

# Leave year start month options (same as fiscal year months)
MONTHS = [
    (1, "January"),
    (2, "February"),
    (3, "March"),
    (4, "April"),
    (5, "May"),
    (6, "June"),
    (7, "July"),
    (8, "August"),
    (9, "September"),
    (10, "October"),
    (11, "November"),
    (12, "December"),
]

# Common timezone list (shared with finance)
COMMON_TIMEZONES = [
    ("UTC", "UTC"),
    ("America/New_York", "Eastern Time (US)"),
    ("America/Chicago", "Central Time (US)"),
    ("America/Denver", "Mountain Time (US)"),
    ("America/Los_Angeles", "Pacific Time (US)"),
    ("Europe/London", "London"),
    ("Europe/Paris", "Paris"),
    ("Europe/Berlin", "Berlin"),
    ("Asia/Tokyo", "Tokyo"),
    ("Asia/Shanghai", "Shanghai"),
    ("Asia/Singapore", "Singapore"),
    ("Australia/Sydney", "Sydney"),
    ("Africa/Lagos", "Lagos"),
    ("Africa/Johannesburg", "Johannesburg"),
]


async def _maybe_await(value: Any) -> Any:
    if isawaitable(value):
        return await value
    return value


class PeopleSettingsWebService:
    """Service for People/HR Settings UI."""

    def get_employee_invite_email_context(
        self,
        db: Session,
        organization_id: uuid.UUID,
    ) -> dict[str, Any]:
        """Return editable employee access invite email copy and delivery rules."""
        sample_name = "New Employee"
        sample_link = (
            "https://your-erp-domain/reset-password?token=<secure-token>"
            f"&next={EMPLOYEE_INVITE_NEXT_URL}"
        )
        template = get_employee_invite_email_template(db, organization_id)
        defaults = default_employee_invite_email_template()
        return {
            **template,
            "default_subject": defaults["subject"],
            "default_body_html": defaults["body_html"],
            "default_body_text": defaults["body_text"],
            "sample_name": sample_name,
            "sample_link": sample_link,
            "link_pattern": (
                "{app_url}/reset-password?token=<secure-token>"
                f"&next={EMPLOYEE_INVITE_NEXT_URL}"
            ),
            "app_url_source": (
                "APP_URL environment value when configured, otherwise the current "
                "request scheme and host."
            ),
            "next_url": EMPLOYEE_INVITE_NEXT_URL,
            "recipients": (
                "Work email first. Personal email is also sent when it exists and "
                "differs from the work email."
            ),
            "email_module": "ADMIN",
            "attachment": (
                "The configured welcome pack below is attached automatically. "
                "If the file cannot be loaded, the invite still sends without it."
            ),
        }

    # ========== HR Settings ==========

    async def get_hr_settings_context(
        self, db: AsyncSession, organization_id: uuid.UUID
    ) -> dict[str, Any]:
        """Get HR settings for editing."""
        async with tenant_context(db, organization_id):
            result = await _maybe_await(
                db.execute(
                    select(Organization).where(
                        Organization.organization_id == organization_id
                    )
                )
            )
            org = result.scalar_one_or_none()
        if not org:
            return {"organization": None, "error": "Organization not found"}

        # Get locations with geofence status for geofencing configuration
        async with tenant_context(db, organization_id):
            locations_result = await _maybe_await(
                db.execute(
                    select(Location)
                    .where(Location.organization_id == organization_id)
                    .where(Location.is_active == True)
                    .order_by(Location.location_name)
                )
            )
            locations = locations_result.scalars().all()

        # Build geofence summary
        geofence_summary = {
            "total_locations": len(locations),
            "geofence_enabled": sum(1 for loc in locations if loc.geofence_enabled),
            "polygon_configured": sum(1 for loc in locations if loc.geofence_polygon),
            "locations": [
                {
                    "location_id": str(loc.location_id),
                    "location_name": loc.location_name,
                    "location_code": loc.location_code,
                    "geofence_enabled": loc.geofence_enabled,
                    "has_coordinates": loc.latitude is not None
                    and loc.longitude is not None,
                    "has_polygon": loc.geofence_polygon is not None,
                    "geofence_radius_m": loc.geofence_radius_m,
                }
                for loc in locations
            ],
        }

        return {
            "organization": org,
            "payroll_frequencies": PAYROLL_FREQUENCIES,
            "attendance_modes": ATTENDANCE_MODES,
            "months": MONTHS,
            "timezones": COMMON_TIMEZONES,
            "employee_id_placeholders": EMPLOYEE_ID_PLACEHOLDERS,
            "geofence_summary": geofence_summary,
        }

    def get_default_invite_attachment_context(
        self,
        db: Session,
        organization_id: uuid.UUID,
    ) -> dict[str, Any] | None:
        return get_default_invite_attachment_metadata(db, organization_id)

    def update_employee_invite_email_template(
        self,
        db: Session,
        organization_id: uuid.UUID,
        data: dict[str, Any],
    ) -> tuple[bool, str | None]:
        set_employee_invite_email_template(
            db,
            organization_id,
            subject=str(data.get("employee_invite_subject") or ""),
            body_html=str(data.get("employee_invite_body_html") or ""),
            body_text=str(data.get("employee_invite_body_text") or ""),
        )
        db.commit()
        return True, None

    async def update_hr_settings(
        self,
        db: AsyncSession,
        organization_id: uuid.UUID,
        data: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """Update HR settings."""
        # Update allowed HR fields
        allowed_fields = [
            "hr_employee_id_format",
            "hr_employee_id_prefix",
            "hr_payroll_frequency",
            "hr_leave_year_start_month",
            "hr_probation_days",
            "hr_attendance_mode",
            "timezone",  # Shared with finance but editable from HR
        ]

        async with tenant_context(db, organization_id):
            result = await _maybe_await(
                db.execute(
                    select(Organization).where(
                        Organization.organization_id == organization_id
                    )
                )
            )
            org = result.scalar_one_or_none()
            if not org:
                return False, "Organization not found"

            for field in allowed_fields:
                if field in data:
                    value = data[field]
                    # Handle empty strings as None for optional fields
                    if value == "":
                        value = None
                    # Handle integer conversion for specific fields
                    if (
                        field in ["hr_leave_year_start_month", "hr_probation_days"]
                        and value
                    ):
                        try:
                            value = int(value)
                        except (ValueError, TypeError):
                            value = None
                    setattr(org, field, value)

            await _maybe_await(db.commit())
        return True, None

    async def update_default_invite_attachment(
        self,
        db: Session,
        organization_id: uuid.UUID,
        *,
        file: UploadFile | None,
        remove_existing: bool = False,
    ) -> tuple[bool, str | None]:
        if not isinstance(file, UploadFile):
            file = None
        has_upload = bool(file and file.filename)
        if not has_upload and not remove_existing:
            return True, None

        previous = get_default_invite_attachment_metadata(db, organization_id)
        if remove_existing and not has_upload:
            removed = clear_default_invite_attachment_metadata(db, organization_id)
            db.commit()
            if removed and removed.get("s3_key"):
                get_storage().delete(str(removed["s3_key"]))
            return True, None

        if not file or not file.filename:
            return True, None

        content_type = (
            file.content_type
            or mimetypes.guess_type(file.filename or "")[0]
            or "application/octet-stream"
        )
        data = await file.read()
        if not data:
            return False, "Choose a file to upload."

        try:
            result = get_hr_invite_attachment_upload().save(
                data,
                content_type=content_type,
                subdirs=[str(organization_id)],
                prefix="welcome_pack",
                original_filename=file.filename,
            )
        except FileUploadError as exc:
            return False, str(exc)

        set_default_invite_attachment_metadata(
            db,
            organization_id,
            {
                "s3_key": result.s3_key,
                "filename": file.filename,
                "content_type": content_type,
                "file_size": result.file_size,
            },
        )
        db.commit()

        if previous and previous.get("s3_key") and previous["s3_key"] != result.s3_key:
            get_storage().delete(str(previous["s3_key"]))

        return True, None

    # ========== Organization Profile (read-only for HR) ==========

    async def get_organization_context(
        self, db: AsyncSession, organization_id: uuid.UUID
    ) -> dict[str, Any]:
        """Get organization profile (read-only view for HR users)."""
        async with tenant_context(db, organization_id):
            result = await _maybe_await(
                db.execute(
                    select(Organization).where(
                        Organization.organization_id == organization_id
                    )
                )
            )
            org = result.scalar_one_or_none()
        if not org:
            return {"organization": None, "error": "Organization not found"}

        return {
            "organization": org,
        }


# Singleton instance
people_settings_web_service = PeopleSettingsWebService()
