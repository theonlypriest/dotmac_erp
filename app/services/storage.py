"""
S3-compatible object storage service (MinIO).

Wraps the ``minio`` Python SDK to provide upload, download, delete,
and streaming for file objects.  The client is lazily created as a
module-level singleton so every caller shares one connection pool.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterable, Iterator
from datetime import datetime
from typing import IO, TYPE_CHECKING, Any, BinaryIO, Protocol, cast
from urllib.parse import urlparse

import urllib3

from app.config import settings

if TYPE_CHECKING:
    from minio import Minio

    from dotmac_files import ObjectInfo

logger = logging.getLogger(__name__)

_MISSING_OBJECT_CODES = frozenset({"NoSuchKey", "NoSuchObject", "NotFound"})


def _is_missing_object_error(exc: Exception) -> bool:
    """Return whether an object-store error is an authoritative absence."""
    return getattr(exc, "code", None) in _MISSING_OBJECT_CODES


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
_client: Minio | None = None
_bucket_ensured: bool = False


class _FallbackS3Error(Exception):
    """Fallback used when the optional `minio` dependency is not installed."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args)


def _get_client() -> Minio:
    """Return the shared Minio client, creating it on first call."""
    global _client
    if _client is None:
        from minio import Minio

        parsed = urlparse(settings.s3_endpoint_url)
        endpoint = parsed.netloc or parsed.path  # host:port
        secure = parsed.scheme == "https"

        http_client = urllib3.PoolManager(
            timeout=urllib3.Timeout(
                connect=settings.s3_connect_timeout_s,
                read=settings.s3_read_timeout_s,
            ),
            retries=False,
        )

        _client = Minio(
            endpoint,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            region=settings.s3_region,
            secure=secure,
            http_client=http_client,
        )
        logger.info(
            "MinIO client created (endpoint=%s, bucket=%s, secure=%s)",
            endpoint,
            settings.s3_bucket_name,
            secure,
        )
    return _client


def _ensure_bucket() -> None:
    """Create the bucket if it does not already exist (idempotent)."""
    global _bucket_ensured
    if _bucket_ensured:
        return

    client = _get_client()
    bucket = settings.s3_bucket_name
    try:
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
            logger.info("Created MinIO bucket '%s'", bucket)
        else:
            logger.debug("MinIO bucket '%s' already exists", bucket)
    except Exception:
        # TOCTOU: another process may have created the bucket between
        # bucket_exists() and make_bucket(). Re-check rather than fail.
        if not client.bucket_exists(bucket):
            raise
        logger.debug("MinIO bucket '%s' created concurrently", bucket)
    _bucket_ensured = True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class S3StorageService:
    """
    Thin wrapper around the ``minio`` Python SDK.

    All methods are synchronous.  For async contexts the caller should
    run them in a thread pool if needed.
    """

    def __init__(self) -> None:
        _ensure_bucket()

    @property
    def _client(self) -> Minio:
        return _get_client()

    @property
    def _bucket(self) -> str:
        return settings.s3_bucket_name

    # -- Upload -------------------------------------------------------------

    def upload(
        self,
        key: str,
        data: bytes,
        content_type: str | None = None,
    ) -> None:
        """Upload bytes to MinIO under *key*."""
        self._client.put_object(
            self._bucket,
            key,
            io.BytesIO(data),
            length=len(data),
            content_type=content_type or "application/octet-stream",
        )
        logger.info("S3 upload: %s (%d bytes)", key, len(data))

    # -- Download -----------------------------------------------------------

    def download(self, key: str) -> bytes:
        """Download an object and return its bytes."""
        response = None
        try:
            response = self._client.get_object(self._bucket, key)
            data: bytes = response.read()
            return data
        finally:
            if response is not None:
                response.close()
                response.release_conn()

    # -- Stream -------------------------------------------------------------

    def stream(
        self,
        key: str,
        chunk_size: int = 64 * 1024,
    ) -> tuple[Iterator[bytes], str | None, int | None]:
        """
        Return a streaming iterator, content-type, and content-length.

        Usage with FastAPI::

            chunks, ct, cl = storage.stream("attachments/abc.pdf")
            return StreamingResponse(chunks, media_type=ct,
                                     headers={"Content-Length": str(cl)})
        """
        # stat to get metadata without downloading body
        stat = self._client.stat_object(self._bucket, key)
        content_type = stat.content_type
        content_length = stat.size

        response = self._client.get_object(self._bucket, key)

        def _iter() -> Iterator[bytes]:
            try:
                yield from response.stream(chunk_size)
            finally:
                response.close()
                response.release_conn()

        return _iter(), content_type, content_length

    # -- Delete -------------------------------------------------------------

    def delete(self, key: str) -> None:
        """Delete an object.  No error if the key does not exist."""
        self._client.remove_object(self._bucket, key)
        logger.info("S3 delete: %s", key)

    # -- Exists -------------------------------------------------------------

    def exists(self, key: str) -> bool:
        """Check whether an object exists."""
        try:
            self._client.stat_object(self._bucket, key)
        except self._s3_error as exc:
            if _is_missing_object_error(exc):
                return False
            raise
        return True

    @property
    def _s3_error(self) -> type[Exception]:
        """Lazy import of minio.error.S3Error for exception handling."""
        try:
            from minio.error import S3Error

            return cast(type[Exception], S3Error)
        except ModuleNotFoundError:  # pragma: no cover
            # Allow unit tests to run without the optional `minio` dependency.
            return _FallbackS3Error


class _MinioResponse(Protocol):
    def read(self, size: int = -1) -> bytes: ...

    def close(self) -> None: ...

    def release_conn(self) -> None: ...


class _S3Readable(io.RawIOBase):
    """Context-managed response that always releases the MinIO connection."""

    def __init__(self, response: _MinioResponse) -> None:
        self._response = response

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        value = self._response.read(len(buffer))
        length = len(value)
        buffer[:length] = value
        return length

    def close(self) -> None:
        if self.closed:
            return
        try:
            self._response.close()
            self._response.release_conn()
        finally:
            super().close()


class DotmacFilesS3Provider:
    """ERP's MinIO adapter for the provider-neutral ``dotmac-files`` seam.

    Provider calls contain no session and no domain decision.  Immutable replay
    is proved from size plus the checksum metadata written with the object;
    existing bytes are never overwritten speculatively.
    """

    code = "erp_s3"

    def __init__(self, storage: S3StorageService | None = None) -> None:
        self._storage = storage or get_storage()

    @staticmethod
    def _checksum(metadata: object) -> str | None:
        getter = getattr(metadata, "get", None)
        if not callable(getter):
            return None
        return cast(
            str | None,
            getter("x-amz-meta-checksum-sha256") or getter("checksum-sha256"),
        )

    @staticmethod
    def _is_missing(exc: Exception) -> bool:
        return _is_missing_object_error(exc)

    def put(
        self,
        key: str,
        content: IO[bytes],
        *,
        content_type: str,
        size_bytes: int,
        checksum_sha256: str,
    ) -> None:
        from dotmac_files import StorageConflict, StorageUnavailable

        try:
            current = self._storage._client.stat_object(self._storage._bucket, key)
        except self._storage._s3_error as exc:
            if not self._is_missing(exc):
                raise StorageUnavailable("object metadata is unavailable") from None
        else:
            current_size = current.size
            if (
                current_size is not None
                and int(current_size) == size_bytes
                and self._checksum(current.metadata) == checksum_sha256
            ):
                return
            raise StorageConflict("immutable object key already holds other bytes")

        try:
            self._storage._client.put_object(
                self._storage._bucket,
                key,
                cast(BinaryIO, content),
                length=size_bytes,
                content_type=content_type,
                metadata={"checksum-sha256": checksum_sha256},
            )
        except self._storage._s3_error:
            raise StorageUnavailable("object upload failed") from None

    def open(self, key: str) -> BinaryIO:
        from dotmac_files import ObjectMissing, StorageUnavailable

        try:
            response = self._storage._client.get_object(self._storage._bucket, key)
        except self._storage._s3_error as exc:
            if self._is_missing(exc):
                raise ObjectMissing("stored object is missing") from None
            raise StorageUnavailable("stored object is unavailable") from None
        return io.BufferedReader(_S3Readable(cast(_MinioResponse, response)))

    def exists(self, key: str) -> bool:
        from dotmac_files import StorageUnavailable

        try:
            self._storage._client.stat_object(self._storage._bucket, key)
        except self._storage._s3_error as exc:
            if self._is_missing(exc):
                return False
            raise StorageUnavailable("object metadata is unavailable") from None
        return True

    def delete(self, key: str) -> None:
        from dotmac_files import StorageUnavailable

        try:
            self._storage._client.remove_object(self._storage._bucket, key)
        except self._storage._s3_error:
            raise StorageUnavailable("object deletion failed") from None

    def list(self, prefix: str) -> Iterable[ObjectInfo]:
        from dotmac_files import ObjectInfo, StorageUnavailable

        try:
            objects = tuple(
                self._storage._client.list_objects(
                    self._storage._bucket,
                    prefix=prefix,
                    recursive=True,
                )
            )
        except self._storage._s3_error:
            raise StorageUnavailable("object listing failed") from None
        result: list[ObjectInfo] = []
        for item in objects:
            modified = cast(datetime | None, item.last_modified)
            if modified is None:
                raise StorageUnavailable("object listing omitted modification time")
            result.append(
                ObjectInfo(
                    key=str(item.object_name),
                    size_bytes=int(item.size),
                    last_modified=modified,
                )
            )
        return tuple(result)


def get_storage() -> S3StorageService:
    """Factory — returns a ready-to-use storage service."""
    return S3StorageService()


def get_dotmac_files_provider() -> DotmacFilesS3Provider:
    """Return the sole dotmac-files provider used by ERP."""
    return DotmacFilesS3Provider()
