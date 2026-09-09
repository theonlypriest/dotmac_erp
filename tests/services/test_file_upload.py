"""
Tests for the unified FileUploadService (S3-backed).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.services.file_upload import (
    FileTooLargeError,
    FileStorageError,
    FileUploadConfig,
    FileUploadService,
    InvalidContentTypeError,
    InvalidExtensionError,
    InvalidMagicBytesError,
    UploadResult,
    _derive_s3_prefix,
    prepare_tenant_import_csv,
)
from app.config import settings

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_storage():
    """Mock S3StorageService returned by _get_storage()."""
    return MagicMock()


@pytest.fixture
def image_config():
    """Config for image uploads."""
    return FileUploadConfig(
        base_dir="static/avatars",
        allowed_content_types=frozenset({"image/jpeg", "image/png"}),
        max_size_bytes=1024 * 1024,  # 1MB
        s3_prefix="avatars",
    )


@pytest.fixture
def doc_config():
    """Config for document uploads with extensions + magic bytes."""
    return FileUploadConfig(
        base_dir="uploads/attachments",
        allowed_content_types=frozenset({"application/pdf"}),
        allowed_extensions=frozenset({".pdf", ".doc"}),
        max_size_bytes=5 * 1024 * 1024,
        require_magic_bytes=True,
        compute_checksum=True,
        s3_prefix="attachments",
    )


@pytest.fixture
def image_service(image_config, mock_storage):
    svc = FileUploadService(image_config)
    svc._get_storage = lambda: mock_storage
    return svc


@pytest.fixture
def doc_service(doc_config, mock_storage):
    svc = FileUploadService(doc_config)
    svc._get_storage = lambda: mock_storage
    return svc


# ---------------------------------------------------------------------------
# _derive_s3_prefix
# ---------------------------------------------------------------------------


class TestDeriveS3Prefix:
    def test_absolute_path(self):
        assert _derive_s3_prefix("/app/uploads/support") == "support"

    def test_relative_path(self):
        assert _derive_s3_prefix("uploads/attachments") == "attachments"

    def test_static_path(self):
        assert _derive_s3_prefix("static/avatars") == "avatars"

    def test_nested_path(self):
        assert _derive_s3_prefix("uploads/generated_docs") == "generated_docs"

    def test_empty_path_fallback(self):
        assert _derive_s3_prefix("/") == "uploads"

    def test_explicit_s3_prefix_overrides(self):
        config = FileUploadConfig(
            base_dir="/app/uploads/support",
            allowed_content_types=frozenset(),
            max_size_bytes=1024,
            s3_prefix="custom_prefix",
        )
        assert config.effective_s3_prefix == "custom_prefix"

    def test_empty_s3_prefix_derives_from_base_dir(self):
        config = FileUploadConfig(
            base_dir="/app/uploads/support",
            allowed_content_types=frozenset(),
            max_size_bytes=1024,
        )
        assert config.effective_s3_prefix == "support"


# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------


class TestValidate:
    """Tests for the validate() method."""

    def test_valid_content_type(self, image_service):
        image_service.validate("image/jpeg", "photo.jpg", 100)

    def test_invalid_content_type(self, image_service):
        with pytest.raises(InvalidContentTypeError, match="not allowed"):
            image_service.validate("application/pdf", "doc.pdf", 100)

    def test_valid_extension(self, doc_service):
        doc_service.validate("application/pdf", "report.pdf", 100, b"%PDF-1.4")

    def test_invalid_extension(self, doc_service):
        with pytest.raises(InvalidExtensionError, match="not allowed"):
            doc_service.validate("application/pdf", "file.exe", 100)

    def test_file_too_large(self, image_service):
        with pytest.raises(FileTooLargeError, match="too large"):
            image_service.validate("image/jpeg", "big.jpg", 2 * 1024 * 1024)

    def test_file_at_exact_limit(self, image_service):
        image_service.validate("image/jpeg", "exact.jpg", 1024 * 1024)

    def test_valid_magic_bytes_pdf(self, doc_service):
        doc_service.validate("application/pdf", "doc.pdf", 100, b"%PDF-1.7 ...")

    def test_invalid_magic_bytes(self, doc_service):
        with pytest.raises(InvalidMagicBytesError, match="does not match"):
            doc_service.validate("application/pdf", "fake.pdf", 100, b"NOT_A_PDF")

    def test_skip_magic_bytes_when_not_required(self, image_service):
        image_service.validate("image/jpeg", "photo.jpg", 100, b"WHATEVER")

    def test_none_content_type_skips_check(self, image_service):
        image_service.validate(None, "photo.jpg", 100)

    def test_none_filename_skips_extension_check(self, doc_service):
        doc_service.validate("application/pdf", None, 100)


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------


class TestSave:
    """Tests for the save() method (S3-backed)."""

    def test_save_uploads_to_s3(self, image_service, mock_storage):
        data = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        result = image_service.save(
            file_data=data,
            content_type="image/jpeg",
            original_filename="test.jpg",
        )

        assert isinstance(result, UploadResult)
        assert result.s3_key.startswith("avatars/")
        assert result.filename.endswith(".jpg")
        assert result.file_size == len(data)
        assert result.checksum is None  # Not configured
        mock_storage.upload.assert_called_once()

    def test_save_with_subdirs(self, image_service, mock_storage):
        data = b"\x89PNG" + b"\x00" * 50
        result = image_service.save(
            file_data=data,
            content_type="image/png",
            subdirs=("org123", "photos"),
            original_filename="pic.png",
        )

        assert "org123" in result.s3_key
        assert "photos" in result.s3_key
        assert result.s3_key.startswith("avatars/org123/photos/")

    def test_save_with_prefix(self, image_service, mock_storage):
        data = b"\x89PNG" + b"\x00" * 50
        result = image_service.save(
            file_data=data,
            content_type="image/png",
            prefix="logo",
            original_filename="brand.png",
        )

        assert result.filename.startswith("logo_")

    def test_save_with_checksum(self, doc_service, mock_storage):
        data = b"%PDF-1.4 test content"
        result = doc_service.save(
            file_data=data,
            content_type="application/pdf",
            original_filename="report.pdf",
        )

        assert result.checksum is not None
        assert len(result.checksum) == 64  # SHA-256 hex

    def test_save_rejects_oversized_no_upload(self, image_service, mock_storage):
        data = b"\x00" * (2 * 1024 * 1024)

        with pytest.raises(FileTooLargeError):
            image_service.save(
                file_data=data,
                content_type="image/jpeg",
                original_filename="huge.jpg",
            )

        mock_storage.upload.assert_not_called()

    def test_save_fails_closed_when_object_storage_rejects_upload(
        self, image_service, mock_storage
    ):
        mock_storage.upload.side_effect = RuntimeError("provider unavailable")

        with pytest.raises(FileStorageError, match="Object storage upload failed"):
            image_service.save(
                b"image",
                content_type="image/jpeg",
                original_filename="photo.jpg",
            )

    def test_save_generates_unique_filenames(self, image_service, mock_storage):
        data = b"\x89PNG" + b"\x00" * 50
        r1 = image_service.save(data, "image/png", original_filename="same.png")
        r2 = image_service.save(data, "image/png", original_filename="same.png")

        assert r1.filename != r2.filename
        assert r1.s3_key != r2.s3_key

    def test_save_s3_key_format(self, doc_service, mock_storage):
        """S3 key should be: prefix/subdirs.../filename."""
        data = b"%PDF-1.4 content"
        result = doc_service.save(
            file_data=data,
            content_type="application/pdf",
            subdirs=("org-abc",),
            original_filename="invoice.pdf",
        )

        parts = result.s3_key.split("/")
        assert parts[0] == "attachments"
        assert parts[1] == "org-abc"
        assert parts[2].endswith(".pdf")

    def test_relative_path_excludes_prefix(self, image_service, mock_storage):
        """relative_path should NOT include the S3 prefix."""
        data = b"\x89PNG" + b"\x00" * 50
        result = image_service.save(
            data, "image/png", subdirs=("org1",), original_filename="pic.png"
        )

        assert not result.relative_path.startswith("avatars/")
        assert result.relative_path.startswith("org1/")


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


class TestDelete:
    """Tests for delete() and delete_by_url()."""

    def test_delete_delegates_to_s3(self, image_service, mock_storage):
        deleted = image_service.delete("photo.jpg")
        assert deleted is True
        mock_storage.delete.assert_called_once_with("avatars/photo.jpg")

    def test_delete_full_key_not_double_prefixed(self, image_service, mock_storage):
        """If the path already starts with the prefix, don't double it."""
        deleted = image_service.delete("avatars/photo.jpg")
        assert deleted is True
        mock_storage.delete.assert_called_once_with("avatars/photo.jpg")

    def test_delete_by_url(self, image_service, mock_storage):
        url = "/files/avatars/photo.jpg"
        deleted = image_service.delete_by_url(url, "/files/avatars")
        assert deleted is True
        mock_storage.delete.assert_called_once()

    def test_delete_by_url_wrong_prefix(self, image_service, mock_storage):
        assert (
            image_service.delete_by_url("/other/path/file.png", "/files/avatars")
            is False
        )
        mock_storage.delete.assert_not_called()

    def test_delete_by_url_empty(self, image_service, mock_storage):
        assert image_service.delete_by_url("", "/prefix") is False
        assert image_service.delete_by_url(None, "/prefix") is False
        mock_storage.delete.assert_not_called()


# ---------------------------------------------------------------------------
# UploadResult backward compat
# ---------------------------------------------------------------------------


class TestUploadResult:
    def test_file_path_property(self):
        """file_path property should return the S3 key as a Path."""
        result = UploadResult(
            s3_key="avatars/abc123.jpg",
            relative_path="abc123.jpg",
            filename="abc123.jpg",
            file_size=100,
        )
        from pathlib import Path

        assert result.file_path == Path("avatars/abc123.jpg")


class _AsyncUpload:
    filename = "customers.csv"
    content_type = "text/csv; charset=utf-8"

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = iter(chunks)

    async def read(self, size: int = -1) -> bytes:
        del size
        return next(self._chunks, b"")


class _CaptureProvider:
    code = "test_store"

    def __init__(self) -> None:
        self.put_calls: list[tuple[str, bytes, int, str]] = []

    def put(
        self,
        key,
        content,
        *,
        content_type,
        size_bytes,
        checksum_sha256,
    ) -> None:
        self.put_calls.append((key, content.read(), size_bytes, checksum_sha256))


@pytest.mark.asyncio
async def test_tenant_import_upload_is_bounded_and_scoped(monkeypatch) -> None:
    tenant_id = uuid4()
    provider = _CaptureProvider()
    monkeypatch.setattr(
        settings,
        "import_max_file_size_bytes",
        64,
        raising=False,
    )

    with patch(
        "app.services.storage.get_dotmac_files_provider",
        return_value=provider,
    ):
        prepared = await prepare_tenant_import_csv(
            _AsyncUpload([b"Display Name,Company Name\n", b"Acme,Acme Ltd\n"]),
            tenant_id=tenant_id,
        )

    assert prepared.scope.tenant_id == tenant_id
    assert provider.put_calls[0][1] == (b"Display Name,Company Name\nAcme,Acme Ltd\n")
    assert prepared.size_bytes == len(provider.put_calls[0][1])


@pytest.mark.asyncio
async def test_tenant_import_upload_refuses_before_storage_at_the_limit(
    monkeypatch,
) -> None:
    tenant_id = uuid4()
    provider = _CaptureProvider()
    monkeypatch.setattr(
        settings,
        "import_max_file_size_bytes",
        4,
        raising=False,
    )

    with (
        patch(
            "app.services.storage.get_dotmac_files_provider",
            return_value=provider,
        ),
        pytest.raises(FileTooLargeError),
    ):
        await prepare_tenant_import_csv(
            _AsyncUpload([b"123", b"45"]),
            tenant_id=tenant_id,
        )

    assert provider.put_calls == []
