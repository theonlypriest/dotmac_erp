from __future__ import annotations

from contextlib import nullcontext
from unittest.mock import MagicMock
from uuid import uuid4


def test_service_session_rearms_tenant_scope_after_idempotency_commit(
    monkeypatch,
) -> None:
    """Selfcare attendance commits its reservation before storing the result."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.api import service_principal
    from app.db import session_context

    organization_id = uuid4()
    calls: list[object] = []
    monkeypatch.setattr(
        session_context,
        "set_current_organization_on_connection",
        lambda connection, org: calls.append(org),
    )
    db = Session(create_engine("sqlite://"))
    monkeypatch.setattr(service_principal, "SessionLocal", lambda: db)

    dependency = service_principal.get_db_with_service_org(
        auth={"organization_id": organization_id}
    )
    yielded_db = next(dependency)
    try:
        yielded_db.execute(text("SELECT 1"))
        yielded_db.commit()  # idempotency reservation/update boundary
        yielded_db.execute(text("SELECT 1"))
        assert calls == [organization_id, organization_id]
    finally:
        try:
            next(dependency)
        except StopIteration:
            pass


def test_service_session_keeps_auto_commit_and_cleanup(monkeypatch) -> None:
    from app.api import service_principal

    organization_id = uuid4()
    fake_session = MagicMock()
    monkeypatch.setattr(service_principal, "SessionLocal", lambda: fake_session)
    monkeypatch.setattr(
        service_principal,
        "tenant_scope_for_session",
        lambda db, org: nullcontext(db),
    )

    dependency = service_principal.get_db_with_service_org(
        auth={"organization_id": organization_id}
    )
    next(dependency)
    try:
        next(dependency)
    except StopIteration:
        pass

    fake_session.commit.assert_called_once()
    fake_session.rollback.assert_not_called()
    fake_session.close.assert_called_once()
