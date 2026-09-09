# File Upload Patterns

## Core Service
All durable file uploads go through `app/services/file_upload.py`, which delegates
to the one `app.services.storage.S3StorageService` MinIO/S3 owner. NEVER implement
custom persistent disk I/O in domain services and never add a local-filesystem
fallback when object storage is unavailable. A domain stores only the opaque
object key plus the metadata it owns.

## Adding a New Upload Domain

1. Add a config factory in `file_upload.py`:
```python
def _new_domain_config() -> FileUploadConfig:
    return FileUploadConfig(
        base_dir="/app/uploads/new-domain",  # compatibility/prefix input, not a disk target
        allowed_content_types=frozenset({...}),
        max_size_bytes=10 * 1024 * 1024,
        compute_checksum=True,     # for audit trail
        require_magic_bytes=True,  # for security
    )

def get_new_domain_upload() -> FileUploadService:
    return FileUploadService(_new_domain_config())
```

2. Use in domain service:
```python
from app.services.file_upload import FileUploadError, get_new_domain_upload

svc = get_new_domain_upload()
try:
    result = svc.save(
        file_data=content,
        content_type=content_type,
        subdirs=(str(org_id), str(entity_id)),
        original_filename=filename,
    )
except FileUploadError as e:
    return None, str(e)
```

## Shared Helpers
Import from `app/services/file_upload`:
- `coerce_uuid(value)` — str or UUID → UUID
- `format_file_size(size)` — int → "1.5 MB"
- `compute_checksum(data)` — bytes → SHA-256 hex
- `compute_checksum_from_file(path)` — file path → SHA-256 hex (chunked)
- `resolve_safe_path(base_dir, relative)` — path traversal protection
- `safe_entity_segment(entity_type)` — validate for filesystem paths

## Frontend Component
Use `templates/components/_file_upload.html` macro for upload UI.
See `.claude/rules/templates.md` for usage.

## Security Rules
- Size MUST be validated BEFORE object upload
- Never use user-supplied filenames for storage — always generate UUID-based names
- Validate magic bytes for document formats (PDF, DOC, DOCX, images)
- A provider failure fails the operation; it is never treated as a missing object
  and never falls back to a container path or named volume
