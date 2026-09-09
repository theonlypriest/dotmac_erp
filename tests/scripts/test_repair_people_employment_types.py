from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

from scripts import repair_people_employment_types as cli

ORG_ID = UUID("00000000-0000-0000-0000-000000000101")


class _Db:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _run(monkeypatch: pytest.MonkeyPatch, *args: str) -> _Db:
    db = _Db()

    def organizations(**kwargs):
        assert kwargs == {"include_inactive": True, "only": ORG_ID}
        yield ORG_ID, db

    service = SimpleNamespace(repair_compatibility_projection=lambda: 2)
    monkeypatch.setattr(cli, "for_each_organization", organizations)
    monkeypatch.setattr(cli, "EmploymentTypeService", lambda *a, **k: service)
    assert cli.main(["--organization-id", str(ORG_ID), *args]) == 0
    return db


def test_repair_commits_and_has_no_reverse_mode(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = _run(monkeypatch)
    assert (db.commits, db.rollbacks) == (1, 0)
    assert '"mode":"repair"' in capsys.readouterr().out
    assert all(
        "bootstrap" not in action.dest and "reconcile" not in action.dest
        for action in cli.build_parser()._actions
    )


def test_dry_run_rolls_back_and_never_commits(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = _run(monkeypatch, "--dry-run")
    assert (db.commits, db.rollbacks) == (0, 1)
    assert '"committed":false' in capsys.readouterr().out


def test_failure_rolls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _Db()
    monkeypatch.setattr(
        cli,
        "for_each_organization",
        lambda **kwargs: iter(((ORG_ID, db),)),
    )
    failure = SimpleNamespace(
        repair_compatibility_projection=lambda: (_ for _ in ()).throw(
            RuntimeError("drift")
        )
    )
    monkeypatch.setattr(cli, "EmploymentTypeService", lambda *a, **k: failure)
    with pytest.raises(RuntimeError, match="drift"):
        cli.main(["--organization-id", str(ORG_ID)])
    assert (db.commits, db.rollbacks) == (0, 1)
