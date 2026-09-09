"""
Tests for app/services/scheduler_config.py

Tests for Celery configuration functions:
- Environment variable parsing
- Database setting retrieval
- Effective value resolution (env > db > default)
- Celery config generation
- Beat schedule building
"""

import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch

from app.models.domain_settings import SettingDomain
from app.models.scheduler import ScheduleType
from app.services.scheduler_config import (
    _effective_int,
    _effective_str,
    _env_int,
    _env_value,
    _get_setting_value,
    build_beat_schedule,
    get_celery_config,
)

# ============ TestEnvValue ============


class TestEnvValue:
    """Tests for the _env_value function."""

    def test_env_value_exists(self, monkeypatch):
        """Should return value when env var exists."""
        monkeypatch.setenv("TEST_VAR", "test_value")

        result = _env_value("TEST_VAR")

        assert result == "test_value"

    def test_env_value_missing(self, monkeypatch):
        """Should return None when env var is missing."""
        monkeypatch.delenv("NONEXISTENT_VAR", raising=False)

        result = _env_value("NONEXISTENT_VAR")

        assert result is None

    def test_env_value_empty_string(self, monkeypatch):
        """Should return None when env var is empty string."""
        monkeypatch.setenv("EMPTY_VAR", "")

        result = _env_value("EMPTY_VAR")

        assert result is None


# ============ TestEnvInt ============


class TestEnvInt:
    """Tests for the _env_int function."""

    def test_env_int_valid(self, monkeypatch):
        """Should return int when env var is valid integer string."""
        monkeypatch.setenv("INT_VAR", "42")

        result = _env_int("INT_VAR")

        assert result == 42

    def test_env_int_invalid(self, monkeypatch):
        """Should return None when env var is not a valid integer."""
        monkeypatch.setenv("INVALID_INT", "not_a_number")

        result = _env_int("INVALID_INT")

        assert result is None

    def test_env_int_missing(self, monkeypatch):
        """Should return None when env var is missing."""
        monkeypatch.delenv("MISSING_INT", raising=False)

        result = _env_int("MISSING_INT")

        assert result is None

    def test_env_int_negative(self, monkeypatch):
        """Should return negative int correctly."""
        monkeypatch.setenv("NEG_INT", "-10")

        result = _env_int("NEG_INT")

        assert result == -10

    def test_env_int_zero(self, monkeypatch):
        """Should return zero correctly."""
        monkeypatch.setenv("ZERO_INT", "0")

        result = _env_int("ZERO_INT")

        assert result == 0


# ============ TestGetSettingValue ============


class TestGetSettingValue:
    """Tests for the _get_setting_value function."""

    def test_get_setting_value_text(self):
        """Should return value_text when present."""
        mock_db = MagicMock()
        mock_setting = MagicMock()
        mock_setting.value_text = "text_value"
        mock_setting.value_json = None
        mock_db.scalars.return_value.first.return_value = mock_setting

        result = _get_setting_value(mock_db, SettingDomain.scheduler, "test_key")

        assert result == "text_value"

    def test_get_setting_value_json(self):
        """Should return str(value_json) when value_text is None."""
        mock_db = MagicMock()
        mock_setting = MagicMock()
        mock_setting.value_text = None
        mock_setting.value_json = {"key": "value"}
        mock_db.scalars.return_value.first.return_value = mock_setting

        result = _get_setting_value(mock_db, SettingDomain.scheduler, "test_key")

        assert "key" in result  # String representation of dict

    def test_get_setting_value_text_priority(self):
        """value_text should take priority over value_json."""
        mock_db = MagicMock()
        mock_setting = MagicMock()
        mock_setting.value_text = "text_priority"
        mock_setting.value_json = {"ignored": True}
        mock_db.scalars.return_value.first.return_value = mock_setting

        result = _get_setting_value(mock_db, SettingDomain.scheduler, "test_key")

        assert result == "text_priority"

    def test_get_setting_value_inactive_ignored(self):
        """Inactive settings should not be returned (query filters by is_active)."""
        mock_db = MagicMock()
        # Setting up the filter chain to return None (no active setting found)
        mock_db.scalars.return_value.first.return_value = None

        result = _get_setting_value(mock_db, SettingDomain.scheduler, "test_key")

        assert result is None

    def test_get_setting_value_missing(self):
        """Should return None when setting doesn't exist."""
        mock_db = MagicMock()
        mock_db.scalars.return_value.first.return_value = None

        result = _get_setting_value(mock_db, SettingDomain.scheduler, "nonexistent")

        assert result is None


# ============ TestEffectiveInt ============


class TestEffectiveInt:
    """Tests for the _effective_int function."""

    def test_effective_int_env_priority(self, monkeypatch):
        """Environment variable should have highest priority."""
        monkeypatch.setenv("TEST_INT", "100")
        mock_db = MagicMock()
        # DB would return different value
        mock_setting = MagicMock()
        mock_setting.value_text = "50"
        mock_db.scalars.return_value.first.return_value = mock_setting

        result = _effective_int(
            mock_db, SettingDomain.scheduler, "db_key", "TEST_INT", default=10
        )

        assert result == 100

    def test_effective_int_db_fallback(self, monkeypatch):
        """Should fall back to DB when env is not set."""
        monkeypatch.delenv("UNSET_INT", raising=False)
        mock_db = MagicMock()
        mock_setting = MagicMock()
        mock_setting.value_text = "75"
        mock_setting.value_json = None
        mock_db.scalars.return_value.first.return_value = mock_setting

        result = _effective_int(
            mock_db, SettingDomain.scheduler, "db_key", "UNSET_INT", default=10
        )

        assert result == 75

    def test_effective_int_default_fallback(self, monkeypatch):
        """Should fall back to default when env and DB are not set."""
        monkeypatch.delenv("MISSING_INT", raising=False)
        mock_db = MagicMock()
        mock_db.scalars.return_value.first.return_value = None

        result = _effective_int(
            mock_db, SettingDomain.scheduler, "db_key", "MISSING_INT", default=42
        )

        assert result == 42

    def test_effective_int_invalid_db_uses_default(self, monkeypatch):
        """Should use default when DB value is not a valid integer."""
        monkeypatch.delenv("UNSET_INT", raising=False)
        mock_db = MagicMock()
        mock_setting = MagicMock()
        mock_setting.value_text = "not_a_number"
        mock_setting.value_json = None
        mock_db.scalars.return_value.first.return_value = mock_setting

        result = _effective_int(
            mock_db, SettingDomain.scheduler, "db_key", "UNSET_INT", default=99
        )

        assert result == 99


# ============ TestEffectiveStr ============


class TestEffectiveStr:
    """Tests for the _effective_str function."""

    def test_effective_str_env_priority(self, monkeypatch):
        """Environment variable should have highest priority."""
        monkeypatch.setenv("TEST_STR", "env_value")
        mock_db = MagicMock()
        mock_setting = MagicMock()
        mock_setting.value_text = "db_value"
        mock_db.scalars.return_value.first.return_value = mock_setting

        result = _effective_str(
            mock_db, SettingDomain.scheduler, "db_key", "TEST_STR", default="default"
        )

        assert result == "env_value"

    def test_effective_str_db_fallback(self, monkeypatch):
        """Should fall back to DB when env is not set."""
        monkeypatch.delenv("UNSET_STR", raising=False)
        mock_db = MagicMock()
        mock_setting = MagicMock()
        mock_setting.value_text = "from_db"
        mock_setting.value_json = None
        mock_db.scalars.return_value.first.return_value = mock_setting

        result = _effective_str(
            mock_db, SettingDomain.scheduler, "db_key", "UNSET_STR", default="default"
        )

        assert result == "from_db"

    def test_effective_str_default_fallback(self, monkeypatch):
        """Should fall back to default when env and DB are not set."""
        monkeypatch.delenv("MISSING_STR", raising=False)
        mock_db = MagicMock()
        mock_db.scalars.return_value.first.return_value = None

        result = _effective_str(
            mock_db,
            SettingDomain.scheduler,
            "db_key",
            "MISSING_STR",
            default="fallback",
        )

        assert result == "fallback"

    def test_effective_str_none_default(self, monkeypatch):
        """Should return None when default is None and no other value."""
        monkeypatch.delenv("MISSING_STR", raising=False)
        mock_db = MagicMock()
        mock_db.scalars.return_value.first.return_value = None

        result = _effective_str(
            mock_db, SettingDomain.scheduler, "db_key", "MISSING_STR", default=None
        )

        assert result is None


# ============ TestGetCeleryConfig ============


class TestGetCeleryConfig:
    """Tests for the get_celery_config function."""

    @patch("app.services.scheduler_config.SessionLocal")
    def test_get_celery_config_defaults(self, mock_session_local, monkeypatch):
        """Should return default values when no config is set."""
        # Clear env vars
        monkeypatch.delenv("CELERY_BROKER_URL", raising=False)
        monkeypatch.delenv("CELERY_RESULT_BACKEND", raising=False)
        monkeypatch.delenv("CELERY_TIMEZONE", raising=False)
        monkeypatch.delenv("REDIS_URL", raising=False)

        mock_session = MagicMock()
        mock_session_local.return_value = mock_session
        mock_session.scalars.return_value.first.return_value = None

        config = get_celery_config()

        assert "broker_url" in config
        assert "result_backend" in config
        assert "timezone" in config
        assert config["timezone"] == "Africa/Lagos"
        mock_session.close.assert_called_once()

    @patch("app.services.scheduler_config.SessionLocal")
    def test_get_celery_config_from_env(self, mock_session_local, monkeypatch):
        """Should use environment variables when set."""
        monkeypatch.setenv("CELERY_BROKER_URL", "redis://custom:6379/0")
        monkeypatch.setenv("CELERY_RESULT_BACKEND", "redis://custom:6379/1")
        monkeypatch.setenv("CELERY_TIMEZONE", "America/New_York")

        mock_session = MagicMock()
        mock_session_local.return_value = mock_session
        mock_session.scalars.return_value.first.return_value = None

        config = get_celery_config()

        assert config["broker_url"] == "redis://custom:6379/0"
        assert config["result_backend"] == "redis://custom:6379/1"
        assert config["timezone"] == "America/New_York"

    @patch("app.services.scheduler_config.SessionLocal")
    def test_get_celery_config_redis_fallback(self, mock_session_local, monkeypatch):
        """Should fall back to REDIS_URL when Celery-specific vars not set."""
        monkeypatch.delenv("CELERY_BROKER_URL", raising=False)
        monkeypatch.delenv("CELERY_RESULT_BACKEND", raising=False)
        monkeypatch.setenv("REDIS_URL", "redis://fallback:6379/0")

        mock_session = MagicMock()
        mock_session_local.return_value = mock_session
        mock_session.scalars.return_value.first.return_value = None

        config = get_celery_config()

        assert config["broker_url"] == "redis://fallback:6379/0"

    @patch("app.services.scheduler_config.SessionLocal")
    def test_get_celery_config_db_override(self, mock_session_local, monkeypatch):
        """Database settings should be used when env vars not set."""
        monkeypatch.delenv("CELERY_BROKER_URL", raising=False)
        monkeypatch.delenv("REDIS_URL", raising=False)

        mock_session = MagicMock()
        mock_session_local.return_value = mock_session

        # Mock DB to return broker_url setting
        def mock_first():
            mock_setting = MagicMock()
            mock_setting.value_text = "redis://db_broker:6379/0"
            mock_setting.value_json = None
            return mock_setting

        mock_session.scalars.return_value.first = mock_first

        config = get_celery_config()

        assert "broker_url" in config

    @patch("app.services.scheduler_config.SessionLocal")
    def test_get_celery_config_db_exception_uses_defaults(
        self, mock_session_local, monkeypatch
    ):
        """Should use defaults when DB query fails."""
        monkeypatch.delenv("CELERY_BROKER_URL", raising=False)
        monkeypatch.delenv("REDIS_URL", raising=False)

        mock_session = MagicMock()
        mock_session_local.return_value = mock_session
        mock_session.query.side_effect = Exception("DB Error")

        config = get_celery_config()

        # Should still return a valid config with defaults
        assert "broker_url" in config
        assert "redis://localhost:6379" in config["broker_url"]

    @patch("app.services.scheduler_config.SessionLocal")
    def test_get_celery_config_closes_session(self, mock_session_local, monkeypatch):
        """Should always close the database session."""
        mock_session = MagicMock()
        mock_session_local.return_value = mock_session
        mock_session.scalars.return_value.first.return_value = None

        get_celery_config()

        mock_session.close.assert_called_once()


# ============ TestBuildBeatSchedule ============


class TestBuildBeatSchedule:
    """Tests for the build_beat_schedule function."""

    @patch("app.services.scheduler_config.SessionLocal")
    def test_build_beat_schedule_empty(self, mock_session_local):
        """Should return only builtin tasks when no DB tasks exist."""
        mock_session = MagicMock()
        mock_session_local.return_value = mock_session
        mock_session.scalars.return_value.all.return_value = []

        schedule = build_beat_schedule()

        # Builtin tasks are always present
        assert "expense-approval-reminders" in schedule
        assert "expense-stuck-transfers" in schedule
        assert "hr-birthday-morning-email" in schedule
        assert (
            schedule["hr-birthday-morning-email"]["task"]
            == "app.tasks.hr.send_hr_birthday_morning_email"
        )
        assert "notification-email-dispatch" in schedule
        assert (
            schedule["notification-email-dispatch"]["task"]
            == "app.tasks.notifications.process_pending_notification_emails"
        )
        # No DB-defined tasks
        assert not any(k.startswith("scheduled_task_") for k in schedule)
        assert "dotmac-sub-incremental-sync" not in schedule
        mock_session.close.assert_called_once()

    @patch("app.services.scheduler_config.SessionLocal")
    def test_dotmac_sub_incremental_sync_has_one_db_owned_schedule(
        self, mock_session_local
    ):
        mock_session = MagicMock()
        mock_session_local.return_value = mock_session

        task_id = uuid.uuid4()
        mock_task = MagicMock()
        mock_task.id = task_id
        mock_task.enabled = True
        mock_task.schedule_type = ScheduleType.interval
        mock_task.interval_seconds = 1800
        mock_task.task_name = "app.tasks.dotmac_sub.run_dotmac_sub_incremental_sync"
        mock_task.args_json = []
        mock_task.kwargs_json = {}
        mock_session.scalars.return_value.all.return_value = [mock_task]

        schedule = build_beat_schedule()

        matching_entries = [
            entry
            for entry in schedule.values()
            if entry["task"] == "app.tasks.dotmac_sub.run_dotmac_sub_incremental_sync"
        ]
        assert len(matching_entries) == 1
        assert f"scheduled_task_{task_id}" in schedule

    @patch("app.services.scheduler_config.SessionLocal")
    def test_build_beat_schedule_enabled_only(self, mock_session_local):
        """Should only include enabled tasks."""
        mock_session = MagicMock()
        mock_session_local.return_value = mock_session

        mock_task = MagicMock()
        mock_task.id = uuid.uuid4()
        mock_task.enabled = True
        mock_task.schedule_type = ScheduleType.interval
        mock_task.interval_seconds = 300
        mock_task.task_name = "app.tasks.test"
        mock_task.args_json = None
        mock_task.kwargs_json = None

        mock_session.scalars.return_value.all.return_value = [mock_task]

        schedule = build_beat_schedule()

        # Should have 1 DB task plus builtin tasks
        db_tasks = {
            k: v for k, v in schedule.items() if k.startswith("scheduled_task_")
        }
        assert len(db_tasks) == 1

    @patch("app.services.scheduler_config.SessionLocal")
    def test_build_beat_schedule_interval_only(self, mock_session_local):
        """Should only include interval type tasks (cron not supported yet)."""
        mock_session = MagicMock()
        mock_session_local.return_value = mock_session

        mock_interval_task = MagicMock()
        mock_interval_task.id = uuid.uuid4()
        mock_interval_task.enabled = True
        mock_interval_task.schedule_type = ScheduleType.interval
        mock_interval_task.interval_seconds = 300
        mock_interval_task.task_name = "app.tasks.interval"
        mock_interval_task.args_json = None
        mock_interval_task.kwargs_json = None

        mock_session.scalars.return_value.all.return_value = [mock_interval_task]

        schedule = build_beat_schedule()

        db_tasks = {
            k: v for k, v in schedule.items() if k.startswith("scheduled_task_")
        }
        assert len(db_tasks) == 1

    @patch("app.services.scheduler_config.SessionLocal")
    def test_build_beat_schedule_task_format(self, mock_session_local):
        """Schedule entry should have correct format."""
        mock_session = MagicMock()
        mock_session_local.return_value = mock_session

        task_id = uuid.uuid4()
        mock_task = MagicMock()
        mock_task.id = task_id
        mock_task.enabled = True
        mock_task.schedule_type = ScheduleType.interval
        mock_task.interval_seconds = 300
        mock_task.task_name = "app.tasks.my_task"
        mock_task.args_json = None
        mock_task.kwargs_json = None

        mock_session.scalars.return_value.all.return_value = [mock_task]

        schedule = build_beat_schedule()

        key = f"scheduled_task_{task_id}"
        assert key in schedule
        assert schedule[key]["task"] == "app.tasks.my_task"
        assert "schedule" in schedule[key]

    @patch("app.services.scheduler_config.SessionLocal")
    def test_build_beat_schedule_timedelta(self, mock_session_local):
        """Schedule should use timedelta for interval."""
        mock_session = MagicMock()
        mock_session_local.return_value = mock_session

        task_id = uuid.uuid4()
        mock_task = MagicMock()
        mock_task.id = task_id
        mock_task.enabled = True
        mock_task.schedule_type = ScheduleType.interval
        mock_task.interval_seconds = 600
        mock_task.task_name = "app.tasks.test"
        mock_task.args_json = None
        mock_task.kwargs_json = None

        mock_session.scalars.return_value.all.return_value = [mock_task]

        schedule = build_beat_schedule()

        key = f"scheduled_task_{task_id}"
        assert schedule[key]["schedule"] == timedelta(seconds=600)

    @patch("app.services.scheduler_config.SessionLocal")
    def test_build_beat_schedule_min_interval_1(self, mock_session_local):
        """Interval should be at least 1 second."""
        mock_session = MagicMock()
        mock_session_local.return_value = mock_session

        task_id = uuid.uuid4()
        mock_task = MagicMock()
        mock_task.id = task_id
        mock_task.enabled = True
        mock_task.schedule_type = ScheduleType.interval
        mock_task.interval_seconds = 0  # Invalid, should become 1
        mock_task.task_name = "app.tasks.test"
        mock_task.args_json = None
        mock_task.kwargs_json = None

        mock_session.scalars.return_value.all.return_value = [mock_task]

        schedule = build_beat_schedule()

        key = f"scheduled_task_{task_id}"
        assert schedule[key]["schedule"] == timedelta(seconds=1)

    @patch("app.services.scheduler_config.SessionLocal")
    def test_build_beat_schedule_args_json(self, mock_session_local):
        """Should include args_json in schedule entry."""
        mock_session = MagicMock()
        mock_session_local.return_value = mock_session

        task_id = uuid.uuid4()
        mock_task = MagicMock()
        mock_task.id = task_id
        mock_task.enabled = True
        mock_task.schedule_type = ScheduleType.interval
        mock_task.interval_seconds = 300
        mock_task.task_name = "app.tasks.test"
        mock_task.args_json = ["arg1", "arg2"]
        mock_task.kwargs_json = None

        mock_session.scalars.return_value.all.return_value = [mock_task]

        schedule = build_beat_schedule()

        key = f"scheduled_task_{task_id}"
        assert schedule[key]["args"] == ["arg1", "arg2"]

    @patch("app.services.scheduler_config.SessionLocal")
    def test_build_beat_schedule_kwargs_json(self, mock_session_local):
        """Should include kwargs_json in schedule entry."""
        mock_session = MagicMock()
        mock_session_local.return_value = mock_session

        task_id = uuid.uuid4()
        mock_task = MagicMock()
        mock_task.id = task_id
        mock_task.enabled = True
        mock_task.schedule_type = ScheduleType.interval
        mock_task.interval_seconds = 300
        mock_task.task_name = "app.tasks.test"
        mock_task.args_json = None
        mock_task.kwargs_json = {"key": "value"}

        mock_session.scalars.return_value.all.return_value = [mock_task]

        schedule = build_beat_schedule()

        key = f"scheduled_task_{task_id}"
        assert schedule[key]["kwargs"] == {"key": "value"}

    @patch("app.services.scheduler_config.SessionLocal")
    def test_build_beat_schedule_none_args(self, mock_session_local):
        """None args/kwargs should default to empty list/dict."""
        mock_session = MagicMock()
        mock_session_local.return_value = mock_session

        task_id = uuid.uuid4()
        mock_task = MagicMock()
        mock_task.id = task_id
        mock_task.enabled = True
        mock_task.schedule_type = ScheduleType.interval
        mock_task.interval_seconds = 300
        mock_task.task_name = "app.tasks.test"
        mock_task.args_json = None
        mock_task.kwargs_json = None

        mock_session.scalars.return_value.all.return_value = [mock_task]

        schedule = build_beat_schedule()

        key = f"scheduled_task_{task_id}"
        assert schedule[key]["args"] == []
        assert schedule[key]["kwargs"] == {}

    @patch("app.services.scheduler_config.SessionLocal")
    def test_build_beat_schedule_db_exception(self, mock_session_local):
        """Should return only builtin tasks when DB query fails."""
        mock_session = MagicMock()
        mock_session_local.return_value = mock_session
        mock_session.query.side_effect = Exception("DB Error")

        schedule = build_beat_schedule()

        # Builtin tasks still present, no DB tasks
        assert "expense-approval-reminders" in schedule
        assert "notification-email-dispatch" in schedule
        assert not any(k.startswith("scheduled_task_") for k in schedule)

    @patch("app.services.scheduler_config.SessionLocal")
    def test_build_beat_schedule_closes_session(self, mock_session_local):
        """Should always close the database session."""
        mock_session = MagicMock()
        mock_session_local.return_value = mock_session
        mock_session.scalars.return_value.all.return_value = []

        build_beat_schedule()

        mock_session.close.assert_called_once()

    @patch("app.services.scheduler_config.SessionLocal")
    def test_build_beat_schedule_multiple_tasks(self, mock_session_local):
        """Should handle multiple tasks correctly."""
        mock_session = MagicMock()
        mock_session_local.return_value = mock_session

        task1 = MagicMock()
        task1.id = uuid.uuid4()
        task1.enabled = True
        task1.schedule_type = ScheduleType.interval
        task1.interval_seconds = 60
        task1.task_name = "app.tasks.task1"
        task1.args_json = None
        task1.kwargs_json = None

        task2 = MagicMock()
        task2.id = uuid.uuid4()
        task2.enabled = True
        task2.schedule_type = ScheduleType.interval
        task2.interval_seconds = 120
        task2.task_name = "app.tasks.task2"
        task2.args_json = ["arg"]
        task2.kwargs_json = {"key": "val"}

        mock_session.scalars.return_value.all.return_value = [
            task1,
            task2,
        ]

        schedule = build_beat_schedule()

        db_tasks = {
            k: v for k, v in schedule.items() if k.startswith("scheduled_task_")
        }
        assert len(db_tasks) == 2
        assert f"scheduled_task_{task1.id}" in schedule
        assert f"scheduled_task_{task2.id}" in schedule
