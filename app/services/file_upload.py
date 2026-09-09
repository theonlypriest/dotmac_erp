"""
Unified File Upload Service.

Central service for file validation, storage, and deletion.
All upload services (avatar, branding, resume, finance attachments,
support attachments, PM attachments, discipline attachments) delegate
to this core service.

Storage backend: S3 (MinIO).  Validation still runs in-process before
the upload is sent to the object store.

Key features:
- Size validated BEFORE uploading to S3
- Magic byte validation (opt-in)
- SHA-256 checksum (opt-in)
- Shared helpers: coerce_uuid, format_file_size, compute_checksum_*
"""

from __future__ import annotations

import hashlib
import logging
import re
import tempfile
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Protocol, Union

from dotmac_files import FilePolicy, PreparedFile, StorageProvider, prepare_upload

from app.config import settings
from app.tenancy import OrganizationTenantContext

if TYPE_CHECKING:
    from app.services.storage import S3StorageService


class AsyncUpload(Protocol):
    """The small UploadFile surface needed by the durable import boundary."""

    filename: str | None
    content_type: str | None

    async def read(self, size: int = -1) -> bytes: ...


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Magic bytes for file-type validation (extension → list of valid signatures)
# ---------------------------------------------------------------------------
MAGIC_BYTES: dict[str, list[bytes]] = {
    # Documents
    ".pdf": [b"%PDF"],
    ".doc": [b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"],
    ".docx": [b"PK\x03\x04"],
    ".xls": [b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"],
    ".xlsx": [b"PK\x03\x04"],
    # Images
    ".jpg": [b"\xff\xd8\xff"],
    ".jpeg": [b"\xff\xd8\xff"],
    ".png": [b"\x89PNG\r\n\x1a\n"],
    ".gif": [b"GIF87a", b"GIF89a"],
    ".webp": [b"RIFF"],
    # Archives
    ".zip": [b"PK\x03\x04"],
}

# Content-type → extension mapping (superset across all services)
CONTENT_TYPE_EXTENSIONS: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "image/svg+xml": ".svg",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "application/zip": ".zip",
}

# Safe entity-type pattern (used by finance/PM attachment paths)
SAFE_ENTITY_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


# ---------------------------------------------------------------------------
# Shared helpers (used by multiple domain-specific attachment services)
# ---------------------------------------------------------------------------


def coerce_uuid(value: Union[str, uuid.UUID]) -> uuid.UUID:
    """Convert string to UUID if needed."""
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def format_file_size(size: int) -> str:
    """Format file size for human-readable display."""
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    else:
        return f"{size / (1024 * 1024):.1f} MB"


def compute_checksum(data: bytes) -> str:
    """Compute SHA-256 checksum of in-memory bytes."""
    return hashlib.sha256(data).hexdigest()


def compute_checksum_from_file(file_path: str) -> str:
    """Compute SHA-256 checksum of a file on disk (chunked)."""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def resolve_safe_path(base_dir: Path, relative_path: str) -> Path:
    """
    Resolve a relative path safely within a base directory.

    Raises ValueError if the resolved path would escape base_dir.
    """
    base = base_dir.resolve()
    full_path = (base / relative_path).resolve()
    if base != full_path and base not in full_path.parents:
        raise ValueError("Invalid file path: outside upload directory")
    return full_path


def safe_entity_segment(entity_type: str) -> str:
    """Validate an entity-type string for use in filesystem paths."""
    if not entity_type:
        raise ValueError("Entity type is required")
    if not SAFE_ENTITY_PATTERN.match(entity_type):
        raise ValueError("Invalid entity type")
    if Path(entity_type).name != entity_type:
        raise ValueError("Invalid entity type")
    return entity_type


def _derive_s3_prefix(base_dir: str) -> str:
    """
    Derive a clean S3 key prefix from a legacy base_dir path.

    Examples:
        "/app/uploads/support"   → "support"
        "uploads/attachments"    → "attachments"
        "static/avatars"         → "avatars"
        "uploads/generated_docs" → "generated_docs"
    """
    # Take the last meaningful path component
    parts = PurePosixPath(base_dir.rstrip("/")).parts
    # Skip common prefixes like /app, uploads, static
    skip = {"", "/", "app", "uploads", "static"}
    meaningful = [p for p in parts if p not in skip]
    return "/".join(meaningful) if meaningful else "uploads"


@dataclass(frozen=True)
class FileUploadConfig:
    """Policy object defining upload constraints for a specific domain."""

    base_dir: str
    allowed_content_types: frozenset[str]
    max_size_bytes: int
    allowed_extensions: frozenset[str] = field(default_factory=frozenset)
    require_magic_bytes: bool = False
    compute_checksum: bool = False
    s3_prefix: str = ""  # Explicit S3 prefix; derived from base_dir if empty

    @property
    def effective_s3_prefix(self) -> str:
        """Return the S3 key prefix to use."""
        return self.s3_prefix or _derive_s3_prefix(self.base_dir)


@dataclass
class UploadResult:
    """Result of a successful file upload."""

    s3_key: str
    relative_path: str
    filename: str
    file_size: int
    checksum: str | None = None

    @property
    def file_path(self) -> Path:
        """Backward-compat: return the S3 key as a Path-like object."""
        return Path(self.s3_key)


class FileUploadError(Exception):
    """Base exception for upload errors."""

    pass


class FileStorageError(FileUploadError):
    """The configured object store could not durably accept the file."""

    pass


class InvalidContentTypeError(FileUploadError):
    """Content type is not allowed."""

    pass


class InvalidExtensionError(FileUploadError):
    """File extension is not allowed."""

    pass


class FileTooLargeError(FileUploadError):
    """File exceeds size limit."""

    pass


class InvalidMagicBytesError(FileUploadError):
    """File content does not match its claimed format."""

    pass


class PathTraversalError(FileUploadError):
    """Attempted path traversal detected."""

    pass


async def prepare_tenant_import_csv(
    upload: AsyncUpload,
    *,
    tenant_id: uuid.UUID,
    provider: StorageProvider | None = None,
) -> PreparedFile:
    """Stream one admitted CSV into the shared immutable file owner.

    The request body is spooled to disk above one MiB and the configured limit
    is enforced while reading, before ``dotmac-files`` performs its independent
    signature, checksum and policy checks.  No session is accepted here.
    """
    if provider is None:
        from app.services.storage import get_dotmac_files_provider

        provider = get_dotmac_files_provider()

    filename = upload.filename or ""
    content_type = (upload.content_type or "").split(";", 1)[0].strip().lower()
    policy = FilePolicy(
        max_bytes=settings.import_max_file_size_bytes,
        allowed_extensions=frozenset({".csv"}),
        allowed_media_types=frozenset({"text/csv"}),
    )
    with tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b") as spool:
        total = 0
        while chunk := await upload.read(1024 * 1024):
            total += len(chunk)
            if total > settings.import_max_file_size_bytes:
                raise FileTooLargeError(
                    "Import file exceeds the configured upload limit"
                )
            spool.write(chunk)
        spool.seek(0)

        def chunks() -> Iterator[bytes]:
            while value := spool.read(1024 * 1024):
                yield value

        return prepare_upload(
            provider,
            scope=OrganizationTenantContext.for_organization(tenant_id).tenant_scope,
            policy=policy,
            original_filename=filename,
            declared_media_type=content_type,
            chunks=chunks(),
        )


class FileUploadService:
    """
    Core file upload service.

    Handles validation and delegates storage to S3StorageService.
    """

    def __init__(self, config: FileUploadConfig) -> None:
        self.config = config

    @property
    def base_path(self) -> Path:
        """Legacy compat: resolved base directory."""
        return Path(self.config.base_dir).resolve()

    def _get_storage(self) -> S3StorageService:
        """Lazy import to avoid circular deps and allow test patching."""
        from app.services.storage import get_storage

        svc: S3StorageService = get_storage()
        return svc

    def validate(
        self,
        content_type: str | None,
        filename: str | None,
        file_size: int,
        file_data: bytes | None = None,
    ) -> None:
        """
        Run all validation checks BEFORE uploading.

        Raises FileUploadError subclass on failure.
        """
        # Content type check
        if content_type and self.config.allowed_content_types:
            if content_type not in self.config.allowed_content_types:
                allowed = ", ".join(sorted(self.config.allowed_content_types))
                raise InvalidContentTypeError(
                    f"Content type '{content_type}' not allowed. Allowed: {allowed}"
                )

        # Extension check
        if filename and self.config.allowed_extensions:
            ext = Path(filename).suffix.lower()
            if ext not in self.config.allowed_extensions:
                allowed = ", ".join(sorted(self.config.allowed_extensions))
                raise InvalidExtensionError(
                    f"File extension '{ext}' not allowed. Allowed: {allowed}"
                )

        # Size check
        if file_size > self.config.max_size_bytes:
            max_mb = self.config.max_size_bytes / (1024 * 1024)
            raise FileTooLargeError(
                f"File too large ({file_size} bytes). Maximum size: {max_mb:.0f}MB"
            )

        # Magic bytes check
        if self.config.require_magic_bytes and file_data and filename:
            ext = Path(filename).suffix.lower()
            if ext in MAGIC_BYTES:
                valid = any(
                    file_data[: len(magic)] == magic for magic in MAGIC_BYTES[ext]
                )
                if not valid:
                    raise InvalidMagicBytesError(
                        "File content does not match the expected format"
                    )
            elif content_type in {"text/plain", "text/csv"}:
                try:
                    file_data.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise InvalidMagicBytesError(
                        "Text file contains invalid UTF-8 content"
                    ) from exc

    def _generate_filename(
        self,
        content_type: str | None,
        original_filename: str | None,
        prefix: str | None = None,
    ) -> str:
        """Generate a unique filename preserving the original extension."""
        ext = ""
        if original_filename:
            ext = Path(original_filename).suffix.lower()
        if not ext and content_type:
            ext = CONTENT_TYPE_EXTENSIONS.get(content_type, "")

        unique_id = uuid.uuid4().hex[:12]
        if prefix:
            return f"{prefix}_{unique_id}{ext}"
        return f"{unique_id}{ext}"

    def _build_s3_key(
        self,
        filename: str,
        subdirs: Sequence[str] | None = None,
    ) -> str:
        """Build the full S3 object key."""
        parts: list[str] = [self.config.effective_s3_prefix]
        if subdirs:
            parts.extend(subdirs)
        parts.append(filename)
        return "/".join(parts)

    def save(
        self,
        file_data: bytes,
        content_type: str | None = None,
        subdirs: Sequence[str] | None = None,
        prefix: str | None = None,
        original_filename: str | None = None,
    ) -> UploadResult:
        """
        Validate and upload a file to S3.

        Args:
            file_data: Raw file bytes.
            content_type: MIME type.
            subdirs: Optional key segments (e.g. [org_id]).
            prefix: Optional filename prefix (e.g. "logo", "favicon").
            original_filename: Original filename for extension preservation.

        Returns:
            UploadResult with S3 key and metadata.

        Raises:
            FileUploadError subclass on validation failure.
        """
        # Validate BEFORE uploading
        self.validate(content_type, original_filename, len(file_data), file_data)

        # Generate unique filename and S3 key
        filename = self._generate_filename(content_type, original_filename, prefix)
        s3_key = self._build_s3_key(filename, subdirs)

        # Relative path (without the domain prefix)
        rel_parts: list[str] = []
        if subdirs:
            rel_parts.extend(subdirs)
        rel_parts.append(filename)
        relative_path = "/".join(rel_parts)

        # Upload to S3
        try:
            storage = self._get_storage()
            storage.upload(s3_key, file_data, content_type)
        except Exception as exc:
            logger.error("Object storage upload failed for key %s", s3_key)
            raise FileStorageError("Object storage upload failed") from exc

        # Optional checksum
        checksum: str | None = None
        if self.config.compute_checksum:
            checksum = hashlib.sha256(file_data).hexdigest()

        logger.info(
            "File uploaded to S3: %s (%d bytes, type=%s)",
            s3_key,
            len(file_data),
            content_type,
        )

        return UploadResult(
            s3_key=s3_key,
            relative_path=relative_path,
            filename=filename,
            file_size=len(file_data),
            checksum=checksum,
        )

    def delete(self, relative_path: str) -> bool:
        """
        Delete a file by its relative path (or full S3 key).

        Returns True after issuing the delete (S3 delete is idempotent).
        """
        # Build the full S3 key if only a relative path was given
        if relative_path.startswith(self.config.effective_s3_prefix + "/"):
            s3_key = relative_path
        else:
            s3_key = f"{self.config.effective_s3_prefix}/{relative_path}"

        storage = self._get_storage()
        storage.delete(s3_key)
        logger.info("File deleted from S3: %s", s3_key)
        return True

    def delete_by_url(self, url: str, url_prefix: str) -> bool:
        """
        Delete a file identified by its URL.

        Strips url_prefix to derive the relative path, then delegates
        to delete(). Used by avatar and branding services.
        """
        if not url or not url.startswith(url_prefix):
            return False

        relative = url[len(url_prefix) :].lstrip("/")
        if not relative:
            return False

        return self.delete(relative)


# ---------------------------------------------------------------------------
# Pre-configured instances for each upload domain
# ---------------------------------------------------------------------------


def _avatar_config() -> FileUploadConfig:
    return FileUploadConfig(
        base_dir=settings.avatar_upload_dir,
        allowed_content_types=frozenset(settings.avatar_allowed_types.split(",")),
        max_size_bytes=settings.avatar_max_size_bytes,
        s3_prefix="avatars",
    )


def _branding_config() -> FileUploadConfig:
    return FileUploadConfig(
        base_dir=settings.branding_upload_dir,
        allowed_content_types=frozenset(settings.branding_allowed_types.split(",")),
        max_size_bytes=settings.branding_max_size_bytes,
        s3_prefix="branding",
    )


def _resume_config() -> FileUploadConfig:
    allowed_doc_extensions = {".pdf", ".doc", ".docx"}
    configured_extensions = {
        ext.strip().lower()
        for ext in settings.resume_allowed_extensions.split(",")
        if ext.strip()
    }
    # Resume uploads are intentionally restricted to Word and PDF documents.
    extensions = frozenset(configured_extensions & allowed_doc_extensions)
    if not extensions:
        extensions = frozenset(allowed_doc_extensions)
    # Derive content types from extensions
    ext_to_ct: dict[str, str] = {
        ".pdf": "application/pdf",
        ".doc": "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    content_types = frozenset(ct for ext, ct in ext_to_ct.items() if ext in extensions)
    return FileUploadConfig(
        base_dir=settings.resume_upload_dir,
        allowed_content_types=content_types,
        allowed_extensions=extensions,
        max_size_bytes=settings.resume_max_size_bytes,
        require_magic_bytes=True,
        s3_prefix="resumes",
    )


def _finance_attachment_config() -> FileUploadConfig:
    import os

    max_size = int(os.getenv("MAX_ATTACHMENT_SIZE", str(10 * 1024 * 1024)))
    return FileUploadConfig(
        base_dir="uploads/attachments",
        allowed_content_types=frozenset(
            {
                "application/pdf",
                "image/jpeg",
                "image/png",
                "image/gif",
                "image/webp",
                "application/msword",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/vnd.ms-excel",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "text/plain",
                "text/csv",
            }
        ),
        max_size_bytes=max_size,
        compute_checksum=True,
        s3_prefix="attachments",
    )


def _support_attachment_config() -> FileUploadConfig:
    return FileUploadConfig(
        base_dir="/app/uploads/support",
        allowed_content_types=frozenset(
            {
                "image/jpeg",
                "image/png",
                "image/gif",
                "image/webp",
                "application/pdf",
                "application/msword",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/vnd.ms-excel",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "text/csv",
                "text/plain",
                "application/zip",
            }
        ),
        allowed_extensions=frozenset(
            {
                ".jpg",
                ".jpeg",
                ".png",
                ".gif",
                ".webp",
                ".pdf",
                ".doc",
                ".docx",
                ".xls",
                ".xlsx",
                ".csv",
                ".txt",
                ".zip",
            }
        ),
        max_size_bytes=10 * 1024 * 1024,
        compute_checksum=True,
        s3_prefix="support",
    )


def _form_attachment_config() -> FileUploadConfig:
    return FileUploadConfig(
        base_dir="/app/uploads/form_attachments",
        allowed_content_types=frozenset(
            {
                "image/jpeg",
                "image/png",
                "image/gif",
                "image/webp",
                "application/pdf",
                "application/msword",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "text/plain",
                "text/csv",
            }
        ),
        allowed_extensions=frozenset(
            {
                ".jpg",
                ".jpeg",
                ".png",
                ".gif",
                ".webp",
                ".pdf",
                ".doc",
                ".docx",
                ".txt",
                ".csv",
            }
        ),
        max_size_bytes=10 * 1024 * 1024,
        compute_checksum=True,
        require_magic_bytes=True,
        s3_prefix="form_attachments",
    )


def _employee_document_config() -> FileUploadConfig:
    return FileUploadConfig(
        base_dir="/app/uploads/employee_documents",
        allowed_content_types=frozenset(
            {
                "application/pdf",
                "application/msword",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "image/jpeg",
                "image/png",
            }
        ),
        allowed_extensions=frozenset(
            {
                ".pdf",
                ".doc",
                ".docx",
                ".jpg",
                ".jpeg",
                ".png",
            }
        ),
        max_size_bytes=10 * 1024 * 1024,
        compute_checksum=True,
        require_magic_bytes=True,
        s3_prefix="employee_documents",
    )


def get_avatar_upload() -> FileUploadService:
    """Get avatar upload service (lazily configured from settings)."""
    return FileUploadService(_avatar_config())


def get_branding_upload() -> FileUploadService:
    """Get branding upload service (lazily configured from settings)."""
    return FileUploadService(_branding_config())


def get_resume_upload() -> FileUploadService:
    """Get resume upload service (lazily configured from settings)."""
    return FileUploadService(_resume_config())


def get_finance_attachment_upload() -> FileUploadService:
    """Get finance attachment upload service."""
    return FileUploadService(_finance_attachment_config())


def get_support_attachment_upload() -> FileUploadService:
    """Get support attachment upload service."""
    return FileUploadService(_support_attachment_config())


def get_form_attachment_upload() -> FileUploadService:
    """Get generic form attachment upload service."""
    return FileUploadService(_form_attachment_config())


def get_employee_document_upload() -> FileUploadService:
    """Get employee document upload service."""
    return FileUploadService(_employee_document_config())


def _sla_policy_document_config() -> FileUploadConfig:
    return FileUploadConfig(
        base_dir="/app/uploads/sla_policies",
        allowed_content_types=frozenset(
            {
                "application/pdf",
                "image/jpeg",
                "image/png",
            }
        ),
        allowed_extensions=frozenset({".pdf", ".jpg", ".jpeg", ".png"}),
        max_size_bytes=10 * 1024 * 1024,
        compute_checksum=True,
        require_magic_bytes=True,
        s3_prefix="sla_policies",
    )


def get_sla_policy_document_upload() -> FileUploadService:
    """Get the SLA policy document upload service."""
    return FileUploadService(_sla_policy_document_config())


def _pm_attachment_config() -> FileUploadConfig:
    return FileUploadConfig(
        base_dir="/app/uploads/projects",
        allowed_content_types=frozenset(
            {
                "image/jpeg",
                "image/png",
                "image/gif",
                "image/webp",
                "application/pdf",
                "application/msword",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/vnd.ms-excel",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "text/csv",
                "text/plain",
                "application/zip",
            }
        ),
        allowed_extensions=frozenset(
            {
                ".jpg",
                ".jpeg",
                ".png",
                ".gif",
                ".webp",
                ".pdf",
                ".doc",
                ".docx",
                ".xls",
                ".xlsx",
                ".csv",
                ".txt",
                ".zip",
            }
        ),
        max_size_bytes=10 * 1024 * 1024,
        compute_checksum=True,
        s3_prefix="projects",
    )


def _discipline_attachment_config() -> FileUploadConfig:
    import os

    max_size = int(os.getenv("MAX_DISCIPLINE_FILE_SIZE", str(10 * 1024 * 1024)))
    return FileUploadConfig(
        base_dir="uploads/discipline",
        allowed_content_types=frozenset(
            {
                "application/pdf",
                "image/jpeg",
                "image/png",
                "image/gif",
                "image/webp",
                "application/msword",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/vnd.ms-excel",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "text/plain",
                "text/csv",
            }
        ),
        max_size_bytes=max_size,
        require_magic_bytes=True,
        compute_checksum=True,
        s3_prefix="discipline",
    )


def get_pm_attachment_upload() -> FileUploadService:
    """Get PM/project attachment upload service."""
    return FileUploadService(_pm_attachment_config())


def get_discipline_attachment_upload() -> FileUploadService:
    """Get discipline attachment upload service."""
    return FileUploadService(_discipline_attachment_config())


def _hr_invite_attachment_config() -> FileUploadConfig:
    return FileUploadConfig(
        base_dir="/app/uploads/hr_invites",
        allowed_content_types=frozenset(
            {
                "application/pdf",
                "application/msword",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "text/plain",
            }
        ),
        allowed_extensions=frozenset({".pdf", ".doc", ".docx", ".txt"}),
        max_size_bytes=5 * 1024 * 1024,
        compute_checksum=True,
        require_magic_bytes=True,
        s3_prefix="hr_invites",
    )


def get_hr_invite_attachment_upload() -> FileUploadService:
    """Get HR default invite attachment upload service."""
    return FileUploadService(_hr_invite_attachment_config())


def _expense_receipt_config() -> FileUploadConfig:
    return FileUploadConfig(
        base_dir="/app/uploads/expense_receipts",
        allowed_content_types=frozenset(
            {
                "image/jpeg",
                "image/png",
                "image/gif",
                "image/webp",
                "application/pdf",
            }
        ),
        allowed_extensions=frozenset(
            {
                ".jpg",
                ".jpeg",
                ".png",
                ".gif",
                ".webp",
                ".pdf",
            }
        ),
        max_size_bytes=10 * 1024 * 1024,
        compute_checksum=True,
        require_magic_bytes=True,
        s3_prefix="expense_receipts",
    )


def _fleet_fuel_receipt_config() -> FileUploadConfig:
    import os

    max_size = int(os.getenv("MAX_FLEET_FUEL_RECEIPT_SIZE", str(20 * 1024 * 1024)))
    return FileUploadConfig(
        base_dir="uploads/attachments",
        allowed_content_types=frozenset(
            {
                "image/jpeg",
                "image/png",
                "image/gif",
                "image/webp",
                "image/heic",
                "image/heif",
                "application/pdf",
            }
        ),
        allowed_extensions=frozenset(
            {
                ".jpg",
                ".jpeg",
                ".png",
                ".gif",
                ".webp",
                ".heic",
                ".heif",
                ".pdf",
            }
        ),
        max_size_bytes=max_size,
        compute_checksum=True,
        require_magic_bytes=True,
        s3_prefix="attachments",
    )


def _fleet_incident_attachment_config() -> FileUploadConfig:
    import os

    max_size = int(
        os.getenv("MAX_FLEET_INCIDENT_ATTACHMENT_SIZE", str(20 * 1024 * 1024))
    )
    return FileUploadConfig(
        base_dir="uploads/attachments",
        allowed_content_types=frozenset(
            {
                "image/jpeg",
                "image/png",
                "image/gif",
                "image/webp",
                "image/heic",
                "image/heif",
                "application/pdf",
            }
        ),
        allowed_extensions=frozenset(
            {
                ".jpg",
                ".jpeg",
                ".png",
                ".gif",
                ".webp",
                ".heic",
                ".heif",
                ".pdf",
            }
        ),
        max_size_bytes=max_size,
        compute_checksum=True,
        require_magic_bytes=True,
        s3_prefix="attachments",
    )


def _fleet_maintenance_attachment_config() -> FileUploadConfig:
    import os

    max_size = int(
        os.getenv("MAX_FLEET_MAINTENANCE_ATTACHMENT_SIZE", str(20 * 1024 * 1024))
    )
    return FileUploadConfig(
        base_dir="uploads/attachments",
        allowed_content_types=frozenset(
            {
                "image/jpeg",
                "image/png",
                "image/gif",
                "image/webp",
                "image/heic",
                "image/heif",
                "application/pdf",
            }
        ),
        allowed_extensions=frozenset(
            {
                ".jpg",
                ".jpeg",
                ".png",
                ".gif",
                ".webp",
                ".heic",
                ".heif",
                ".pdf",
            }
        ),
        max_size_bytes=max_size,
        compute_checksum=True,
        require_magic_bytes=True,
        s3_prefix="attachments",
    )


def _generated_docs_config() -> FileUploadConfig:
    return FileUploadConfig(
        base_dir=settings.generated_docs_dir,
        allowed_content_types=frozenset({"application/pdf"}),
        max_size_bytes=50 * 1024 * 1024,  # 50MB for large reports
        s3_prefix="generated_docs",
    )


def _hr_handbook_config() -> FileUploadConfig:
    return FileUploadConfig(
        base_dir="uploads/hr_documents",
        # The legacy handbook contract admits these extensions without making
        # a browser-supplied content type authoritative.
        allowed_content_types=frozenset(),
        allowed_extensions=frozenset({".pdf", ".doc", ".docx", ".txt", ".rtf"}),
        max_size_bytes=10 * 1024 * 1024,
        compute_checksum=True,
        s3_prefix="hr_documents",
    )


def _generated_report_config() -> FileUploadConfig:
    return FileUploadConfig(
        base_dir="reports_output",
        allowed_content_types=frozenset({"application/json"}),
        allowed_extensions=frozenset({".json"}),
        max_size_bytes=50 * 1024 * 1024,
        compute_checksum=True,
        s3_prefix="generated_reports",
    )


def get_expense_receipt_upload() -> FileUploadService:
    """Get expense receipt upload service."""
    return FileUploadService(_expense_receipt_config())


def get_fleet_fuel_receipt_upload() -> FileUploadService:
    """Get Fleet fuel receipt upload service."""
    return FileUploadService(_fleet_fuel_receipt_config())


def get_fleet_incident_attachment_upload() -> FileUploadService:
    """Get Fleet incident attachment upload service."""
    return FileUploadService(_fleet_incident_attachment_config())


def get_fleet_maintenance_attachment_upload() -> FileUploadService:
    """Get Fleet maintenance attachment upload service."""
    return FileUploadService(_fleet_maintenance_attachment_config())


def get_generated_docs_upload() -> FileUploadService:
    """Get generated documents upload service."""
    return FileUploadService(_generated_docs_config())


def get_hr_handbook_upload() -> FileUploadService:
    """Get the durable HR handbook object upload service."""
    return FileUploadService(_hr_handbook_config())


def get_generated_report_upload() -> FileUploadService:
    """Get the durable generated-report object upload service."""
    return FileUploadService(_generated_report_config())
