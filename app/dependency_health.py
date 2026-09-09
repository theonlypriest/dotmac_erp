from __future__ import annotations

import os

import httpx
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models.domain_settings import SettingDomain
from app.services.email import _get_smtp_config, validate_smtp_config
from app.services.finance.payments.paystack_client import (
    PaystackClient,
    PaystackConfig,
    PaystackError,
)
from app.services.nextcloud.client import (
    NextcloudConfig,
    NextcloudTalkClient,
    _ROOM_API,
    is_configured,
)
from app.services.remita.client import REMITA_DEMO_URL, REMITA_LIVE_URL, RemitaClient
from app.services.secrets import _openbao_allow_insecure, _openbao_config
from app.services.settings_spec import (
    DOMAIN_SETTINGS_SERVICE,
    extract_db_value,
    get_spec,
    resolve_value,
)
from app.services.dotmac_sub import DotmacSubClient, DotmacSubConfig
from app.services.storage import _get_client as _get_storage_client

DEFAULT_REQUIRED_DEPENDENCIES = frozenset({"storage", "openbao"})


def collect_dependency_health() -> dict[str, dict[str, object]]:
    db = SessionLocal()
    try:
        checks = {
            "smtp": _check_smtp(db),
            "storage": _check_storage(),
            "openbao": _check_openbao(db),
            "paystack": _check_paystack(db),
            "nextcloud": _check_nextcloud(db),
            "dotmac_sub": _check_dotmac_sub(),
            "remita": _check_remita(),
        }
    finally:
        db.close()

    for name, check in checks.items():
        check["required"] = _dependency_required(name, bool(check["configured"]))

    return checks


def readiness_failures(
    dependencies: dict[str, dict[str, object]],
) -> dict[str, dict[str, object]]:
    return {
        name: check
        for name, check in dependencies.items()
        if bool(check["required"]) and not bool(check["healthy"])
    }


def _dependency_required(name: str, configured: bool) -> bool:
    if not configured:
        return False

    if os.getenv("READINESS_CHECK_ALL_CONFIGURED_DEPENDENCIES", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return True

    required_names = {
        item.strip().lower()
        for item in os.getenv("READINESS_REQUIRED_DEPENDENCIES", "").split(",")
        if item.strip()
    }
    return name in DEFAULT_REQUIRED_DEPENDENCIES or name in required_names


def _result(
    *,
    configured: bool,
    healthy: bool,
    message: str,
    **extra: object,
) -> dict[str, object]:
    status = "healthy" if configured and healthy else "degraded"
    if not configured:
        status = "not_configured"
    payload: dict[str, object] = {
        "configured": configured,
        "healthy": healthy,
        "status": status,
        "message": message,
    }
    payload.update(extra)
    return payload


def _global_stored_setting(
    db: Session, domain: SettingDomain, key: str
) -> object | None:
    """Read an explicitly global row through the canonical settings owner.

    Readiness has no organization context, so it cannot inspect an arbitrary
    tenant's override. Environment-backed values have already been loaded into
    settings rows at process start; this path deliberately observes the row
    owned by the settings service rather than resolving the environment again.
    """
    service = DOMAIN_SETTINGS_SERVICE.get(domain)
    if service is None:
        return None
    spec = get_spec(domain, key)
    try:
        setting = service.get_by_key(
            db,
            key,
            organization_id=None,
            inherit=spec.inherits if spec else True,
        )
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        return None
    return extract_db_value(setting)


def _setting_value(db: Session, domain: SettingDomain, key: str) -> object | None:
    return resolve_value(db, domain, key, organization_id=None)


def _setting_configured(db: Session, domain: SettingDomain, key: str) -> bool:
    return _global_stored_setting(db, domain, key) not in {None, ""}


def _check_smtp(db: Session) -> dict[str, object]:
    configured = any(
        _setting_configured(db, SettingDomain.email, key)
        for key in ("smtp_host", "smtp_username", "smtp_password", "smtp_from_email")
    )
    if not configured:
        return _result(
            configured=False,
            healthy=False,
            message="SMTP is not configured",
        )

    ok, error = validate_smtp_config(
        _get_smtp_config(db, organization_id=None), timeout_seconds=5
    )
    return _result(
        configured=True,
        healthy=ok,
        message=error or "SMTP connection validated",
    )


def _check_storage() -> dict[str, object]:
    configured = bool(
        settings.s3_endpoint_url and settings.s3_access_key and settings.s3_secret_key
    )
    if not configured:
        return _result(
            configured=False,
            healthy=False,
            message="S3/MinIO storage is not configured",
        )

    try:
        bucket_exists = _get_storage_client().bucket_exists(settings.s3_bucket_name)
        message = "Bucket reachable" if bucket_exists else "Bucket missing"
        return _result(
            configured=True,
            healthy=bucket_exists,
            message=message,
            bucket=settings.s3_bucket_name,
        )
    except ModuleNotFoundError:
        return _result(
            configured=True,
            healthy=False,
            message="MinIO client dependency is not installed",
        )
    except Exception as exc:
        return _result(
            configured=True,
            healthy=False,
            message=str(exc)[:160],
            bucket=settings.s3_bucket_name,
        )


def _check_openbao(db: Session) -> dict[str, object]:
    configured = bool(os.getenv("OPENBAO_ADDR") or os.getenv("VAULT_ADDR"))
    if not configured:
        return _result(
            configured=False,
            healthy=False,
            message="OpenBao is not configured",
        )

    try:
        addr, token, namespace, _kv_version = _openbao_config(db)
    except Exception as exc:
        return _result(
            configured=True,
            healthy=False,
            message=str(exc)[:160],
        )

    headers = {"X-Vault-Token": token}
    if namespace:
        headers["X-Vault-Namespace"] = namespace

    try:
        response = httpx.get(
            f"{addr}/v1/sys/health",
            headers=headers,
            timeout=5.0,
            verify=not _openbao_allow_insecure(db),
        )
    except httpx.HTTPError as exc:
        return _result(
            configured=True,
            healthy=False,
            message=str(exc)[:160],
        )

    healthy = response.status_code in {200, 429, 472, 473}
    return _result(
        configured=True,
        healthy=healthy,
        message=f"OpenBao health returned HTTP {response.status_code}",
    )


def _check_paystack(db: Session) -> dict[str, object]:
    enabled = bool(_setting_value(db, SettingDomain.payments, "paystack_enabled"))
    secret_key = _setting_value(db, SettingDomain.payments, "paystack_secret_key")
    public_key = _setting_value(db, SettingDomain.payments, "paystack_public_key")
    configured = enabled and bool(secret_key and public_key)

    if not configured:
        return _result(
            configured=False,
            healthy=False,
            message="Paystack is disabled or missing API keys",
        )

    try:
        config = PaystackConfig(
            secret_key=str(secret_key),
            public_key=str(public_key),
            webhook_secret=str(secret_key),
        )
        with PaystackClient(config, timeout=5.0) as client:
            client.list_banks()
    except PaystackError as exc:
        return _result(configured=True, healthy=False, message=exc.message[:160])
    except Exception as exc:
        return _result(configured=True, healthy=False, message=str(exc)[:160])

    return _result(
        configured=True,
        healthy=True,
        message="Paystack API reachable",
    )


def _check_nextcloud(db: Session) -> dict[str, object]:
    configured = is_configured(db, organization_id=None)
    if not configured:
        return _result(
            configured=False,
            healthy=False,
            message="Nextcloud Talk is not configured",
        )

    try:
        config = NextcloudConfig.from_db(db, organization_id=None)
        client = NextcloudTalkClient(
            NextcloudConfig(
                server_url=config.server_url,
                username=config.username,
                password=config.password,
                timeout=min(config.timeout, 5.0),
            )
        )
        client._request("GET", _ROOM_API)
    except Exception as exc:
        return _result(configured=True, healthy=False, message=str(exc)[:160])

    return _result(
        configured=True,
        healthy=True,
        message="Nextcloud Talk API reachable",
    )


def _check_dotmac_sub() -> dict[str, object]:
    config = DotmacSubConfig.from_settings()
    if not config.is_configured():
        return _result(
            configured=False,
            healthy=False,
            message="dotmac_sub is not configured",
        )

    try:
        config.timeout = min(config.timeout, 5.0)
        config.max_retries = 1
        with DotmacSubClient(config) as client:
            healthy = client.test_connection()
    except Exception as exc:
        return _result(configured=True, healthy=False, message=str(exc)[:160])

    return _result(
        configured=True,
        healthy=healthy,
        message="dotmac_sub API reachable"
        if healthy
        else "dotmac_sub API health check failed",
    )


def _check_remita() -> dict[str, object]:
    configured = bool(settings.remita_merchant_id and settings.remita_api_key)
    if not configured:
        return _result(
            configured=False,
            healthy=False,
            message="Remita is not configured",
        )

    base_url = REMITA_LIVE_URL if settings.remita_is_live else REMITA_DEMO_URL
    try:
        with RemitaClient(
            merchant_id=settings.remita_merchant_id,
            api_key=settings.remita_api_key,
            is_live=settings.remita_is_live,
            timeout=5.0,
        ) as client:
            response = client._get_client().get("/", follow_redirects=True)
        healthy = response.status_code < 500
        message = f"Remita endpoint reachable (HTTP {response.status_code})"
        return _result(
            configured=True,
            healthy=healthy,
            message=message,
            base_url=base_url,
        )
    except Exception as exc:
        return _result(
            configured=True,
            healthy=False,
            message=str(exc)[:160],
            base_url=base_url,
        )
