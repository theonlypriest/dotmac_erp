from __future__ import annotations

from pathlib import Path

import pytest
from celery.beat import Scheduler

from app.celery_scheduler import HEARTBEAT_PATH_ENV, DbScheduler


def _scheduler_without_init() -> DbScheduler:
    return object.__new__(DbScheduler)


def test_tick_publishes_heartbeat_only_after_scheduler_returns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    heartbeat = tmp_path / "beat-heartbeat"
    scheduler = _scheduler_without_init()
    calls: list[str] = []

    monkeypatch.setenv(HEARTBEAT_PATH_ENV, str(heartbeat))
    monkeypatch.setattr(
        DbScheduler,
        "_refresh_schedule",
        lambda self: calls.append("refresh"),
    )

    def _tick(_self: Scheduler) -> float:
        calls.append("tick")
        assert not heartbeat.exists()
        return 7.5

    monkeypatch.setattr(Scheduler, "tick", _tick)

    assert scheduler.tick() == 7.5
    assert calls == ["refresh", "tick"]
    assert int(heartbeat.read_text(encoding="ascii")) > 0
    assert list(tmp_path.glob(".*.tmp")) == []


def test_failed_scheduler_iteration_does_not_publish_heartbeat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    heartbeat = tmp_path / "beat-heartbeat"
    scheduler = _scheduler_without_init()

    monkeypatch.setenv(HEARTBEAT_PATH_ENV, str(heartbeat))
    monkeypatch.setattr(DbScheduler, "_refresh_schedule", lambda self: None)

    def _fail(_self: Scheduler) -> float:
        raise RuntimeError("scheduler failed")

    monkeypatch.setattr(Scheduler, "tick", _fail)

    with pytest.raises(RuntimeError, match="scheduler failed"):
        scheduler.tick()
    assert not heartbeat.exists()


def test_unwritable_heartbeat_path_fails_the_scheduler_iteration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scheduler = _scheduler_without_init()
    missing_parent = tmp_path / "missing" / "beat-heartbeat"

    monkeypatch.setenv(HEARTBEAT_PATH_ENV, str(missing_parent))
    monkeypatch.setattr(DbScheduler, "_refresh_schedule", lambda self: None)
    monkeypatch.setattr(Scheduler, "tick", lambda self: 1.0)

    with pytest.raises(FileNotFoundError):
        scheduler.tick()
    assert not missing_parent.exists()
