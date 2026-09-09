"""PostgreSQL proofs for ERP's ``party_person_catalog.v1`` provider.

The positive test proves the migrated database satisfies the pinned kernel
contract. The parametrized negatives are the sensitivity proof: each one breaks
exactly one observable inside a rolled-back transaction and requires the
verifier to refuse for that specific reason. Without them a green positive
proves only that *something* passed, and a verifier that had quietly stopped
checking would look identical.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

pytestmark = pytest.mark.integration

PREREQUISITE = "party_person_catalog.v1"


@pytest.fixture(autouse=True)
def _install_erp_bindings() -> Iterator[None]:
    """Install this assembly's claims and restore process state afterwards."""
    from dotmac_kernel.prerequisites import (
        install_prerequisite_bindings,
        installed_bindings,
    )

    from app.migration_bindings import ASSEMBLY_PREREQUISITE_BINDINGS

    previous = tuple(installed_bindings())
    install_prerequisite_bindings(ASSEMBLY_PREREQUISITE_BINDINGS)
    try:
        yield
    finally:
        install_prerequisite_bindings(previous)


@contextlib.contextmanager
def _broken(engine: Engine, statement: str) -> Iterator[Connection]:
    connection = engine.connect()
    transaction = connection.begin()
    try:
        connection.execute(text(statement))
        yield connection
    finally:
        transaction.rollback()
        connection.close()


def test_migrated_erp_satisfies_the_kernel_party_contract(engine: Engine) -> None:
    from dotmac_kernel.migrations.verify import require_prerequisites

    with engine.connect() as connection:
        require_prerequisites(connection, (PREREQUISITE,))


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        pytest.param(
            # People now consumes this provider through a real FK. CASCADE is
            # confined to the rolled-back sensitivity transaction and lets the
            # verifier observe the intended missing-table defect instead of
            # PostgreSQL refusing the fixture setup first.
            "DROP TABLE public.party_persons CASCADE",
            "does not exist",
            id="person-subtype-absent",
        ),
        pytest.param(
            # The People composite FK depends on the provider's backing unique
            # index. Drop both within the rolled-back probe so this canary keeps
            # testing the provider contract rather than dependency DDL.
            "ALTER TABLE public.parties DROP CONSTRAINT uq_parties_tenant_id CASCADE",
            "unique",
            id="composite-identity-dropped",
        ),
        pytest.param(
            "ALTER TABLE public.parties DROP CONSTRAINT ck_parties_party_type",
            "party_type",
            id="party-type-unconstrained",
        ),
        pytest.param(
            "ALTER TABLE public.party_persons DROP CONSTRAINT fk_party_persons_party",
            "ON DELETE CASCADE",
            id="subtype-detached-from-its-party",
        ),
        pytest.param(
            "ALTER TABLE public.parties NO FORCE ROW LEVEL SECURITY",
            "FORCE",
            id="parties-unforced",
        ),
        pytest.param(
            "ALTER TABLE public.party_persons NO FORCE ROW LEVEL SECURITY",
            "FORCE",
            id="subtype-unforced",
        ),
        pytest.param(
            "DROP POLICY parties_tenant_isolation ON public.parties",
            "app_current_tenant_id",
            id="parties-unpolicied",
        ),
        pytest.param(
            "DROP POLICY party_persons_tenant_isolation ON public.party_persons",
            "app_current_tenant_id",
            id="subtype-unpolicied",
        ),
        pytest.param(
            "REVOKE SELECT ON TABLE public.parties FROM app_user",
            "cannot SELECT",
            id="tenant-role-cannot-read",
        ),
        pytest.param(
            "ALTER TABLE public.parties ALTER COLUMN display_name DROP NOT NULL",
            "nullable",
            id="display-name-made-optional",
        ),
    ],
)
def test_each_broken_observable_is_refused_specifically(
    engine: Engine, statement: str, expected: str
) -> None:
    from dotmac_kernel.migrations.verify import (
        PrerequisiteNotSatisfiedError,
        require_prerequisites,
    )

    with _broken(engine, statement) as connection:
        with pytest.raises(PrerequisiteNotSatisfiedError, match=expected):
            require_prerequisites(connection, (PREREQUISITE,))


def test_a_person_party_cannot_be_read_from_another_tenant(engine: Engine) -> None:
    """The policy is the isolation, so prove it with a real cross-tenant read."""
    first_tenant = uuid4()
    second_tenant = uuid4()
    party_id = uuid4()

    connection = engine.connect()
    transaction = connection.begin()
    try:
        for tenant_id, slug in (
            (first_tenant, f"erp-{first_tenant}"),
            (second_tenant, f"erp-{second_tenant}"),
        ):
            connection.execute(
                text(
                    "INSERT INTO public.tenants (id, slug, name) "
                    "VALUES (:id, :slug, :name)"
                ),
                {"id": tenant_id, "slug": slug, "name": "Isolation probe"},
            )
        connection.execute(
            text(
                "INSERT INTO public.parties "
                "(id, tenant_id, party_type, display_name, is_active) "
                "VALUES (:id, :tenant_id, 'person', 'Probe Person', true)"
            ),
            {"id": party_id, "tenant_id": first_tenant},
        )
        connection.execute(
            text(
                "INSERT INTO public.party_persons (party_id, first_name, last_name) "
                "VALUES (:party_id, 'Probe', 'Person')"
            ),
            {"party_id": party_id},
        )

        connection.execute(text("SET LOCAL ROLE app_user"))
        connection.execute(
            text("SELECT set_config('app.current_tenant', :tenant, true)"),
            {"tenant": str(second_tenant)},
        )
        assert (
            connection.scalar(
                text("SELECT count(*) FROM public.parties WHERE id = :id"),
                {"id": party_id},
            )
            == 0
        )
        assert (
            connection.scalar(
                text("SELECT count(*) FROM public.party_persons WHERE party_id = :id"),
                {"id": party_id},
            )
            == 0
        )

        connection.execute(
            text("SELECT set_config('app.current_tenant', :tenant, true)"),
            {"tenant": str(first_tenant)},
        )
        assert (
            connection.scalar(
                text("SELECT count(*) FROM public.parties WHERE id = :id"),
                {"id": party_id},
            )
            == 1
        )
        assert (
            connection.scalar(
                text("SELECT count(*) FROM public.party_persons WHERE party_id = :id"),
                {"id": party_id},
            )
            == 1
        )
    finally:
        transaction.rollback()
        connection.close()
