"""Session context primitives for multi-tenant scoping.

This assembly has **three coordinated isolation inputs** for one tenancy id:

1. The SQLAlchemy ORM listener — reads ``session.info["organization_id"]``
   and injects ``WHERE organization_id = :org`` into ORM queries. Set by
   :func:`prime_session`.
2. ERP's PostgreSQL RLS policies — read the ``app.current_organization_id``
   GUC (``current_setting('app.current_organization_id')``) and filter at
   the database layer. Set by :func:`app.rls.set_current_organization_sync`.
3. Shared-module PostgreSQL RLS policies — read ``app.current_tenant``. The
   same RLS helper sets it atomically from ERP's identity-preserving
   Organization-to-Tenant mapping.

**Production code that opens a tenant session MUST set all three.** Partial
priming is a silent bug: queries can filter to zero rows under one RLS family,
or skip filtering under the ORM listener on a table not yet protected by RLS.
This is the same hazard that has hit Celery tasks repeatedly — a session
opened for "an org" that doesn't actually scope its queries to that org.

Public API for callers
----------------------

Use the high-level context managers — they establish the applicable input and
clean up:

- :func:`tenant_scope_for_session` — bind an existing, newly-opened Session
  (HTTP dependencies) and keep its database scope armed across commits.
- :func:`session_for_org` — single-org tenant session (Celery tasks,
  CLI scripts, anything outside a web request).
- :func:`for_each_organization` — every organization in turn, each in its own
  tenant session. This is what a fleet-wide batch job wants: discovery goes
  through the narrow catalog definer in :mod:`app.tenant_catalog`, never
  through a cross-org read of ``core_org.organization``.
- :func:`cross_org_session` — application-layer cross-tenant access to tables
  that have **no** PostgreSQL RLS policy. It does not bypass PostgreSQL RLS,
  so it cannot be used to enumerate organizations; see its own warning.

The low-level primitives below (:func:`prime_session`,
:func:`allow_cross_org`) exist for infrastructure code only — the web
dependencies and background-session helpers compose them through
:func:`tenant_scope_for_session`, which owns the session-lifecycle contract.
New code should not compose primitives manually.

A note on ``SET LOCAL`` and commits
------------------------------------

``SET LOCAL app.current_organization_id = ...`` is **transaction-scoped**
— it is reset at COMMIT and ROLLBACK. A session that committed in the
middle of its work therefore lost its RLS GUC and silently started
returning zero rows, while ``session.info`` kept the ORM-listener layer
looking primed: it read as scoped and behaved as unscoped.

``tenant_scope_for_session`` re-arms its scope GUCs on SQLAlchemy's
``after_begin``, which fires as each new transaction opens. Both the HTTP
dependencies and ``session_for_org`` use that one lifecycle owner.
Commit-and-continue inside a block is therefore safe, and the call site is
no longer the contract owner for it.

One tenant session per org remains the better shape, but for the other
reason — identity-map contamination across tenants:

    for org_id in org_ids:
        with session_for_org(org_id) as db:
            service.run()
            db.commit()
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.rls import (
    set_current_organization_on_connection,
    set_current_organization_sync,
)
from app.tenancy import OrganizationTenantContext


def prime_session(session: Session, organization_id: UUID) -> None:
    """Set the mapped Organization/Tenant identity in ``session.info``.

    .. warning::

       This sets ONLY the SQLAlchemy session-info half of tenant scoping. It
       does NOT set either PostgreSQL GUC. Calling this alone leaves ERP and
       shared-module RLS unprimed and causes silent zero-row reads.

       **Do not use this as a Celery task entry-point helper.** Use
       :func:`session_for_org` instead — it sets both layers.

       Direct callers are limited to infrastructure code that already
       composes both layers explicitly. Application code should not import
       this.

    Calling on an already-primed session overwrites the previous value —
    useful for tasks that iterate orgs *within a single session*, though
    one-session-per-org (via :func:`session_for_org`) is preferred.
    """
    context = OrganizationTenantContext.for_organization(organization_id)
    session.info["organization_id"] = context.organization_id
    session.info["tenant_id"] = context.tenant_id


@contextmanager
def allow_cross_org(session: Session) -> Iterator[None]:
    """Temporarily bypass the SQLAlchemy ORM-listener org filter.

    .. warning::

       Like :func:`prime_session`, this bypasses ONLY the ORM listener
       layer. It does NOT bypass PostgreSQL RLS policies. Use
       :func:`cross_org_session` for genuine cross-tenant application work —
       it opens a dedicated session but retains that database boundary.

       Direct use is reserved for infrastructure code that composes both
       layers (web admin-bypass dependencies, audit listeners that
       deliberately re-pin context).

    Restores prior state in ``finally`` so an exception inside the block
    does not leak the bypass. Nested usage preserves outer state.
    """
    prior = session.info.get("allow_cross_org", False)
    session.info["allow_cross_org"] = True
    try:
        yield
    finally:
        session.info["allow_cross_org"] = prior


def prime_tenant_context(session: Session, organization_id: UUID) -> None:
    """Set every tenant-isolation input on an *existing* session.

    Use this when a request opens unprimed (the org isn't known at
    request entry — e.g., a public portal route resolving an org from a
    URL slug, or an onboarding portal resolving from a token) and the
    service has just looked the org up under :func:`allow_cross_org`. After
    the lookup, retroactively
    prime the session for the rest of the work:

        # Route opens unprimed because slug → org_id isn't known yet
        @router.get("/{org_slug}/jobs")
        def list_jobs(org_slug: str, db: Session = Depends(get_db)):
            org = service.resolve_org_from_slug(org_slug)  # bypass internally
            # ... lookup complete; switch the session to tenant scope:
            prime_tenant_context(db, org.organization_id)
            return service.list_jobs(org.organization_id)

    This composes :func:`prime_session` (mapped Organization/Tenant identity
    via ``session.info``) and :func:`app.rls.set_current_organization_sync`
    (both PostgreSQL scope GUCs) so callers cannot forget one input — the same
    complete guarantee that :func:`session_for_org` and
    :func:`app.api.deps.get_db_with_org` provide for the
    open-already-primed flow.

    .. warning::

       Only use this for the open-unprimed-then-resolve pattern.
       New Celery tasks should use :func:`session_for_org`; new HTTP
       routes should use :func:`app.api.deps.get_db_with_org` (API) or
       :func:`app.web.deps.get_db_for_org` (web). This helper exists
       for the legitimately-public-with-resolved-org case only.
    """
    prime_session(session, organization_id)
    if session.get_bind().dialect.name == "postgresql":
        set_current_organization_sync(session, organization_id)


@contextmanager
def tenant_scope_for_session(
    session: Session, organization_id: UUID
) -> Iterator[Session]:
    """Bind an existing Session to one tenant for its whole lifecycle.

    ``SET LOCAL`` disappears at every commit or rollback. This context manager
    owns the matching SQLAlchemy ``after_begin`` listener so the database GUCs
    are present for the first transaction and automatically restored for every
    later transaction. It also primes ``session.info`` for the ORM tenant
    filter.

    Use this only with a newly-opened session that is not already in a
    transaction. The caller remains responsible for commit/rollback and close.
    """

    def _arm(sess: Session, transaction: object, connection: Connection) -> None:
        # Emitted on the Connection because the Session is still provisioning
        # that connection while ``after_begin`` runs.
        set_current_organization_on_connection(connection, organization_id)

    prime_session(session, organization_id)
    event.listen(session, "after_begin", _arm)
    try:
        yield session
    finally:
        event.remove(session, "after_begin", _arm)


@contextmanager
def session_for_org(organization_id: UUID) -> Iterator[Session]:
    """Canonical tenant-scoped session for non-HTTP entry points.

    Opens a fresh ``SessionLocal``, sets the mapped session-info context and
    both PostgreSQL scope GUCs, yields the session, and closes
    it on exit (even on exception).

    Use this in every Celery task, CLI script, scheduled job, or other
    non-HTTP entry point that operates on a single org's data::

        @shared_task
        def process_payroll(org_id: str) -> dict:
            with session_for_org(UUID(org_id)) as db:
                PayrollService(db).run()
                db.commit()
                return {"ok": True}

    For tasks that span multiple organizations, open one session per org
    in the loop — this prevents identity-map contamination across tenants::

        for org_id in org_ids:
            with session_for_org(org_id) as db:
                Service(db).run()
                db.commit()

    That guidance used to carry a second reason: ``SET LOCAL`` is
    transaction-scoped, so a commit inside the block silently un-set the GUC
    while ``session.info`` kept layer 1 looking primed. That reason is gone —
    the context is now re-armed on every transaction — so committing inside
    the block is safe. Per-org sessions are still right, but for the
    identity-map reason alone.
    """
    # Local import: SessionLocal is at module top-level of app.db; importing
    # it here avoids a circular dependency at import time.
    from app.db import SessionLocal

    session = SessionLocal()

    try:
        with tenant_scope_for_session(session, organization_id):
            yield session
    finally:
        session.close()


@contextmanager
def cross_org_session() -> Iterator[Session]:
    """Canonical session for cross-organization ERP work (admin/batch).

    Opens a fresh ``SessionLocal`` with ``allow_cross_org`` active for the ORM
    listener. It does not bypass PostgreSQL RLS.

    .. warning::

       **This is not how you enumerate organizations.**
       ``core_org.organization`` is RLS-protected at migration heads, so
       ``select(Organization.organization_id)`` here returns every row under
       today's ``postgres`` superuser runtime and *zero* rows under
       ``app_user`` — a scheduled task that silently processes nothing and
       exits 0. Use :func:`for_each_organization` below, or
       :func:`app.tenant_catalog.active_organization_ids` when you need the ids
       without the sessions.

    What remains legitimate here is discovering rows in tables that have **no**
    RLS policy at migration heads, then processing each module scope under its
    own per-org session::

        with cross_org_session() as cross_db:
            ids = list(cross_db.scalars(select(UnprotectedThing.id)).all())
        for org_id in org_ids:
            with session_for_org(org_id) as db:
                ...

    Don't query a PostgreSQL-RLS-protected table or reuse a
    ``cross_org_session`` for per-org work — switching contexts mid-session is
    the bug class this helper exists to prevent.
    """
    from app.db import SessionLocal

    session = SessionLocal()

    try:
        # ORM listener bypass — session.info marker the listener checks.
        session.info["allow_cross_org"] = True
        yield session
    finally:
        session.info["allow_cross_org"] = False
        session.close()


def for_each_organization(
    *,
    include_inactive: bool = False,
    only: UUID | None = None,
) -> Generator[tuple[UUID, Session], None, None]:
    """Yield ``(organization_id, tenant-scoped session)`` for each organization.

    This is the canonical replacement for the retired
    "``cross_org_session`` to list orgs, then ``session_for_org`` to work" pair.
    Discovery goes through the narrow catalog definer
    (:mod:`app.tenant_catalog`); each iteration then gets a fully primed
    tenant session from :func:`session_for_org`::

        for org_id, db in for_each_organization():
            Service(db).run(org_id)
            db.commit()

    The discovery session is closed before the first tenant session opens, so
    an unscoped session is never alive at the same time as a scoped one.

    Each organization's session is opened and closed inside its own iteration.
    An exception raised in the caller's loop body therefore closes that
    organization's session and propagates — it does not leak into the next
    organization's scope. Callers that must continue past a failing tenant
    wrap their own body in ``try/except``, which is what every task in
    ``app/tasks`` already does to collect per-org errors.

    A caller that stops early (a batch budget, a ``break``) leaves one session
    open until this generator is closed, so it is typed as a ``Generator``
    rather than a bare ``Iterator``: ``contextlib.closing`` around the call is
    the spelling that closes it at a known point instead of at collection.
    See ``refresh_stale_balances`` in ``app/tasks/finance.py``.

    Args:
        include_inactive: passed straight to
            :func:`app.tenant_catalog.organization_ids`.
        only: restrict to a single organization, for tasks that accept an
            optional ``organization_id`` argument.
    """
    from app.tenant_catalog import organization_ids

    for organization_id in organization_ids(
        include_inactive=include_inactive, only=only
    ):
        with session_for_org(organization_id) as session:
            yield organization_id, session
