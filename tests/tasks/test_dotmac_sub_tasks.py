from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.tasks import dotmac_sub


def test_incremental_sync_has_bounded_execution_time() -> None:
    task = dotmac_sub.run_dotmac_sub_incremental_sync

    assert task.soft_time_limit == 25 * 60
    assert task.time_limit == 28 * 60


def test_incremental_sync_phase_has_bounded_execution_time() -> None:
    task = dotmac_sub.run_dotmac_sub_incremental_sync_phase

    assert task.soft_time_limit == 8 * 60
    assert task.time_limit == 10 * 60


def test_incremental_sync_enqueues_bounded_phase_workflow(monkeypatch) -> None:
    organization_id = uuid4()
    history_id = uuid4()
    db = MagicMock()
    service = MagicMock()
    signatures = []

    class _Workflow:
        id = "workflow-1"

        def apply_async(self):
            return self

    def fake_chain(*items):
        signatures.extend(items)
        return _Workflow()

    def fake_signature(*args, **kwargs):
        return {"args": args, "kwargs": kwargs}

    monkeypatch.setattr(
        dotmac_sub,
        "session_for_org",
        lambda _organization_id: nullcontext(db),
    )
    monkeypatch.setattr(
        dotmac_sub,
        "_try_acquire_incremental_sync_lock",
        lambda _db, _organization_id: True,
    )
    monkeypatch.setattr(dotmac_sub, "_running_incremental_history_id", lambda *_: None)
    monkeypatch.setattr(
        dotmac_sub,
        "_build_sync_context",
        lambda *_: (service, history_id, organization_id),
    )
    monkeypatch.setattr(dotmac_sub, "chain", fake_chain)
    monkeypatch.setattr(
        dotmac_sub.run_dotmac_sub_incremental_sync_phase,
        "si",
        fake_signature,
    )

    result = dotmac_sub.run_dotmac_sub_incremental_sync.run(
        str(organization_id), batch_size=123
    )

    assert result["accepted"] is True
    assert result["history_id"] == str(history_id)
    assert result["phases"] == list(dotmac_sub._INCREMENTAL_SYNC_PHASES)
    assert [item["args"][2] for item in signatures] == list(
        dotmac_sub._INCREMENTAL_SYNC_PHASES
    )
    assert all(item["kwargs"] == {"batch_size": 123} for item in signatures)
    db.commit.assert_called_once()
    service.close.assert_called_once()


def test_incremental_sync_skips_when_single_flight_lock_is_held(monkeypatch) -> None:
    organization_id = uuid4()
    db = MagicMock()
    build_context = MagicMock()

    monkeypatch.setattr(
        dotmac_sub,
        "session_for_org",
        lambda _organization_id: nullcontext(db),
    )
    monkeypatch.setattr(
        dotmac_sub,
        "_try_acquire_incremental_sync_lock",
        lambda _db, _organization_id: False,
    )
    monkeypatch.setattr(dotmac_sub, "_build_sync_context", build_context)

    result = dotmac_sub.run_dotmac_sub_incremental_sync.run(str(organization_id))

    assert result == {
        "success": True,
        "organization_id": str(organization_id),
        "skipped": True,
        "reason": "already_running",
    }
    build_context.assert_not_called()


def test_incremental_posting_phase_uses_bounded_batch(monkeypatch) -> None:
    organization_id = uuid4()
    history_id = uuid4()
    db = MagicMock()
    history = SimpleNamespace(
        status=dotmac_sub.SyncJobStatus.RUNNING,
        total_records=0,
        synced_count=0,
        skipped_count=0,
        error_count=0,
        add_error=MagicMock(),
        complete=MagicMock(),
        touch=MagicMock(),
    )
    service = MagicMock()
    service.post_unposted_payments.return_value = {"posted": 7, "errors": []}
    db.get.return_value = history

    monkeypatch.setattr(
        dotmac_sub,
        "session_for_org",
        lambda _organization_id: nullcontext(db),
    )
    monkeypatch.setattr(
        dotmac_sub,
        "_try_acquire_incremental_sync_lock",
        lambda _db, _organization_id: True,
    )
    monkeypatch.setattr(dotmac_sub, "_release_incremental_sync_lock", lambda *_: True)
    monkeypatch.setattr(
        dotmac_sub,
        "_build_sync_service_context",
        lambda *_: (service, organization_id),
    )

    result = dotmac_sub.run_dotmac_sub_incremental_sync_phase.run(
        str(organization_id), str(history_id), "post_payments", batch_size=123
    )

    service.post_unposted_payments.assert_called_once_with(
        created_by_user_id=dotmac_sub.SYSTEM_USER_ID,
        limit=dotmac_sub._INCREMENTAL_POST_BATCH_SIZE,
    )
    history.complete.assert_called_once()
    assert result["complete"] is True
    assert result["synced_count"] == 7
    history.touch.assert_called_once()


def test_interrupted_session_invalidates_connection_when_rollback_fails() -> None:
    db = MagicMock()
    db.rollback.side_effect = RuntimeError("connection is busy")

    assert dotmac_sub._rollback_interrupted_session(db) is False

    db.invalidate.assert_called_once_with()


def test_stale_cleanup_uses_last_activity_heartbeat(monkeypatch) -> None:
    db = MagicMock()
    db.scalars.return_value.all.return_value = []
    monkeypatch.setattr(
        dotmac_sub,
        "cross_org_session",
        lambda: nullcontext(db),
    )

    result = dotmac_sub.cleanup_stale_dotmac_sub_sync_history.run()

    assert result == {"success": True, "checked": 0, "marked_failed": 0}
    statement = str(db.scalars.call_args.args[0]).lower()
    assert "coalesce(sync.sync_history.last_activity_at" in statement
    assert "sync.sync_history.sync_type" in statement


def test_incremental_sync_lock_is_session_scoped() -> None:
    organization_id = uuid4()
    db = MagicMock()
    db.scalar.return_value = True

    assert dotmac_sub._try_acquire_incremental_sync_lock(db, organization_id) is True

    statement = str(db.scalar.call_args.args[0])
    assert "pg_try_advisory_lock" in statement
    assert "pg_try_advisory_xact_lock" not in statement
    assert "hashtextextended" in statement
    assert db.scalar.call_args.args[1] == {
        "lock_identity": f"dotmac_sub:incremental:{organization_id}"
    }


def test_incremental_sync_lock_release_uses_same_identity() -> None:
    organization_id = uuid4()
    db = MagicMock()
    db.scalar.return_value = True

    assert dotmac_sub._release_incremental_sync_lock(db, organization_id) is True

    statement = str(db.scalar.call_args.args[0])
    assert "pg_advisory_unlock" in statement
    assert "hashtextextended" in statement
    assert db.scalar.call_args.args[1] == {
        "lock_identity": f"dotmac_sub:incremental:{organization_id}"
    }
