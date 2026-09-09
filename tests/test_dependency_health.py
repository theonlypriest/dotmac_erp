import app.dependency_health as dependency_health_module
from app.models.domain_settings import SettingDomain


class _DummySession:
    def close(self) -> None:
        pass


def test_global_setting_read_uses_owner_with_explicit_global_scope(monkeypatch) -> None:
    observed: dict[str, object] = {}

    class _Setting:
        value_json = None
        value_text = "smtp.example.com"

    class _SettingsOwner:
        def get_by_key(self, db, key, *, organization_id, inherit):
            observed.update(
                db=db,
                key=key,
                organization_id=organization_id,
                inherit=inherit,
            )
            return _Setting()

    db = _DummySession()
    monkeypatch.setitem(
        dependency_health_module.DOMAIN_SETTINGS_SERVICE,
        SettingDomain.email,
        _SettingsOwner(),
    )

    value = dependency_health_module._global_stored_setting(
        db, SettingDomain.email, "smtp_host"
    )

    assert value == "smtp.example.com"
    assert observed == {
        "db": db,
        "key": "smtp_host",
        "organization_id": None,
        "inherit": True,
    }


def test_tenant_configured_dependency_checks_use_explicit_global_scope(
    monkeypatch,
) -> None:
    smtp_scopes: list[object] = []
    nextcloud_scopes: list[tuple[str, object]] = []
    resolved_scopes: list[object] = []

    monkeypatch.setattr(
        dependency_health_module,
        "_setting_configured",
        lambda db, domain, key: True,
    )

    def _smtp_config(db, *, organization_id):
        smtp_scopes.append(organization_id)
        return {}

    monkeypatch.setattr(dependency_health_module, "_get_smtp_config", _smtp_config)
    monkeypatch.setattr(
        dependency_health_module,
        "validate_smtp_config",
        lambda config, timeout_seconds: (True, None),
    )

    def _resolve_value(db, domain, key, *, organization_id):
        resolved_scopes.append(organization_id)
        return False

    monkeypatch.setattr(dependency_health_module, "resolve_value", _resolve_value)

    def _nextcloud_configured(db, *, organization_id):
        nextcloud_scopes.append(("configured", organization_id))
        return True

    monkeypatch.setattr(
        dependency_health_module, "is_configured", _nextcloud_configured
    )

    class _NextcloudConfig:
        def __init__(self, *, server_url, username, password, timeout):
            self.server_url = server_url
            self.username = username
            self.password = password
            self.timeout = timeout

        @classmethod
        def from_db(cls, db, *, organization_id):
            nextcloud_scopes.append(("config", organization_id))
            return cls(
                server_url="https://cloud.example.com",
                username="bot",
                password="secret",
                timeout=30.0,
            )

    class _NextcloudClient:
        def __init__(self, config):
            pass

        def _request(self, method, path):
            return {}

    monkeypatch.setattr(dependency_health_module, "NextcloudConfig", _NextcloudConfig)
    monkeypatch.setattr(
        dependency_health_module, "NextcloudTalkClient", _NextcloudClient
    )

    db = _DummySession()
    assert dependency_health_module._check_smtp(db)["healthy"] is True
    assert dependency_health_module._check_paystack(db)["configured"] is False
    assert dependency_health_module._check_nextcloud(db)["healthy"] is True

    assert smtp_scopes == [None]
    assert resolved_scopes == [None, None, None]
    assert nextcloud_scopes == [("configured", None), ("config", None)]


def test_collect_dependency_health_marks_required_dependencies(monkeypatch) -> None:
    monkeypatch.delenv("READINESS_CHECK_ALL_CONFIGURED_DEPENDENCIES", raising=False)
    monkeypatch.setenv("READINESS_REQUIRED_DEPENDENCIES", "storage,paystack")
    monkeypatch.setattr(
        dependency_health_module,
        "SessionLocal",
        lambda: _DummySession(),
    )
    monkeypatch.setattr(
        dependency_health_module,
        "_check_smtp",
        lambda db: {
            "configured": True,
            "healthy": True,
            "status": "healthy",
            "message": "ok",
        },
    )
    monkeypatch.setattr(
        dependency_health_module,
        "_check_storage",
        lambda: {
            "configured": True,
            "healthy": True,
            "status": "healthy",
            "message": "ok",
        },
    )
    monkeypatch.setattr(
        dependency_health_module,
        "_check_openbao",
        lambda db: {
            "configured": True,
            "healthy": True,
            "status": "healthy",
            "message": "ok",
        },
    )
    monkeypatch.setattr(
        dependency_health_module,
        "_check_paystack",
        lambda db: {
            "configured": True,
            "healthy": True,
            "status": "healthy",
            "message": "ok",
        },
    )
    monkeypatch.setattr(
        dependency_health_module,
        "_check_nextcloud",
        lambda db: {
            "configured": False,
            "healthy": False,
            "status": "not_configured",
            "message": "missing",
        },
    )
    monkeypatch.setattr(
        dependency_health_module,
        "_check_dotmac_sub",
        lambda: {
            "configured": True,
            "healthy": True,
            "status": "healthy",
            "message": "ok",
        },
    )
    monkeypatch.setattr(
        dependency_health_module,
        "_check_remita",
        lambda: {
            "configured": True,
            "healthy": True,
            "status": "healthy",
            "message": "ok",
        },
    )

    checks = dependency_health_module.collect_dependency_health()

    assert checks["storage"]["required"] is True
    assert checks["openbao"]["required"] is True
    assert checks["paystack"]["required"] is True
    assert checks["nextcloud"]["required"] is False
    assert checks["dotmac_sub"]["required"] is False


def test_readiness_failures_only_include_required_unhealthy_dependencies() -> None:
    failures = dependency_health_module.readiness_failures(
        {
            "storage": {
                "configured": True,
                "healthy": False,
                "required": True,
            },
            "nextcloud": {
                "configured": True,
                "healthy": False,
                "required": False,
            },
            "smtp": {
                "configured": True,
                "healthy": True,
                "required": True,
            },
        }
    )

    assert list(failures) == ["storage"]
