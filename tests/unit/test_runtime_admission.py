"""Sensitivity proof for the runtime admission decision (ADR-0018).

`app.runtime_admission.runtime_admission_violations` is scoped to ACTIVE
modules, and ERP has none today. So on every real run it returns an empty tuple
— which is precisely what a decision function that examined nothing would also
return. A check whose only evidence is "production still deploys" is an
unmonitored region, not a gate.

This module removes that ambiguity by driving the pure function with synthetic
snapshots and requiring it to FIRE on each way a runtime identity can be wrong,
one cause at a time so a passing case cannot be carried by an unrelated
failure. The conforming baseline is asserted clean FIRST, so every subsequent
assertion measures the one field it tampered with.

No database is involved and none is needed: the decision is a pure function
over a dataclass, which is the whole reason it was split out of the SQL seam.
"""

from __future__ import annotations

from dataclasses import replace

from app.runtime_admission import (
    COMPOSED_MODULES,
    MODULES_BY_CODE,
    REQUIRED_TABLE_PRIVILEGES,
    RUNTIME_ROLE,
    VACUOUS_ADMISSION_NOTICE,
    ComposedModule,
    RlsProbe,
    RuntimeSnapshot,
    SchemaUsage,
    TablePrivilege,
    active_modules,
    runtime_admission_report,
    runtime_admission_violations,
)

TENANT_A = "11111111-1111-1111-1111-111111111111"
TENANT_B = "22222222-2222-2222-2222-222222222222"

#: One real declared module, used so the proof exercises the same lookup path
#: production does rather than a fixture the registry has never seen.
ACCOUNTING = MODULES_BY_CODE["accounting"]


def _privileges(
    *modules: ComposedModule, held: bool = True
) -> tuple[TablePrivilege, ...]:
    return tuple(
        TablePrivilege(
            schema=module.schema,
            table=table,
            privilege=privilege,
            present=True,
            held=held,
        )
        for module in modules
        for table in module.tenant_tables
        for privilege in REQUIRED_TABLE_PRIVILEGES
    )


def _conforming(
    *, active: tuple[ComposedModule, ...] = (ACCOUNTING,)
) -> RuntimeSnapshot:
    """A snapshot that must be admitted, so tampering measures one field."""
    return RuntimeSnapshot(
        current_user=RUNTIME_ROLE,
        session_user=RUNTIME_ROLE,
        role_posture={RUNTIME_ROLE: (False, False)},
        escalation_memberships=(),
        schema_usage=tuple(
            SchemaUsage(schema=module.schema, present=True, usable=True)
            for module in COMPOSED_MODULES
        ),
        table_privileges=_privileges(*COMPOSED_MODULES),
        rls_probes=tuple(
            RlsProbe(
                schema=module.schema,
                table=module.probe_table,
                executed=True,
                own_tenant=TENANT_A,
                other_tenant=TENANT_B,
                own_rows=7,
                foreign_rows_under_own_context=0,
                own_rows_under_other_context=0,
            )
            for module in active
        ),
        active_modules=tuple(module.module_code for module in active),
    )


def _fires(snapshot: RuntimeSnapshot, fragment: str) -> None:
    violations = runtime_admission_violations(snapshot)
    assert violations, "the decision admitted a snapshot it must refuse"
    assert any(fragment in violation for violation in violations), (
        f"no violation mentioned {fragment!r}; got {violations}"
    )


# ---------------------------------------------------------------------------
# The baseline: it must be clean, or nothing below measures anything
# ---------------------------------------------------------------------------


def test_a_conforming_active_module_is_admitted() -> None:
    assert runtime_admission_violations(_conforming()) == ()


# ---------------------------------------------------------------------------
# (a)-(e): one tampered field at a time, each of which must be refused
# ---------------------------------------------------------------------------


def test_it_fires_when_the_runtime_connects_as_the_legacy_erp_login() -> None:
    """The defect this whole check exists for.

    `dotmac_erp_app` is ERP production's login today. Every module GRANT and
    every RLS policy names `app_user`; a different login reaches those tables
    through a different ACL and is not evaluated against those policies.
    """
    tampered = replace(
        _conforming(),
        current_user="dotmac_erp_app",
        session_user="dotmac_erp_app",
        role_posture={RUNTIME_ROLE: (False, False), "dotmac_erp_app": (False, False)},
    )
    _fires(tampered, "dotmac_erp_app")


def test_it_fires_when_the_runtime_role_is_a_superuser() -> None:
    """SUPERUSER bypasses row-level security whether or not BYPASSRLS is set,
    so a check that only read `rolbypassrls` would certify this snapshot."""
    tampered = replace(_conforming(), role_posture={RUNTIME_ROLE: (False, True)})
    _fires(tampered, "SUPERUSER")


def test_it_fires_when_the_runtime_role_can_bypass_rls() -> None:
    tampered = replace(_conforming(), role_posture={RUNTIME_ROLE: (True, False)})
    _fires(tampered, "BYPASSRLS")


def test_it_fires_on_a_single_missing_table_privilege() -> None:
    """One privilege on one relation, not a wholesale absence.

    A gate that only noticed "no grants at all" would pass a module whose first
    UPDATE fails in production, at request time, on a tenant's data.
    """
    baseline = _conforming()
    tampered = replace(
        baseline,
        table_privileges=tuple(
            replace(entry, held=False)
            if (
                entry.schema == ACCOUNTING.schema
                and entry.table == ACCOUNTING.tenant_tables[0]
                and entry.privilege == "UPDATE"
            )
            else entry
            for entry in baseline.table_privileges
        ),
    )
    _fires(
        tampered, f"lacks UPDATE on {ACCOUNTING.schema}.{ACCOUNTING.tenant_tables[0]}"
    )


def test_it_fires_when_another_tenants_rows_are_visible() -> None:
    baseline = _conforming()
    tampered = replace(
        baseline,
        rls_probes=tuple(
            replace(probe, foreign_rows_under_own_context=3)
            for probe in baseline.rls_probes
        ),
    )
    _fires(tampered, "belonging to another tenant were visible")


def test_it_fires_when_rows_survive_a_tenant_context_switch() -> None:
    """The second probe direction, which the first cannot replace.

    On an empty table `foreign_rows_under_own_context` reads zero whether or
    not the policy works. Reading the FIRST tenant's rows after switching the
    GUC to the second is the direction that catches a policy ignoring the GUC.
    """
    baseline = _conforming()
    tampered = replace(
        baseline,
        rls_probes=tuple(
            replace(probe, own_rows_under_other_context=5)
            for probe in baseline.rls_probes
        ),
    )
    _fires(tampered, "stayed visible")


# ---------------------------------------------------------------------------
# (f): the ratchet — a composed but DISABLED module is admitted with nothing
# ---------------------------------------------------------------------------


def test_a_disabled_module_missing_every_grant_is_still_admissible() -> None:
    """ADR-0003 created module storage ahead of any cutover.

    `mod_accounting` exists, is audited by the migration gate, and has no
    `app_user` grants a deployment needs yet. Refusing that would refuse a
    deploy for having done exactly the right thing.
    """
    snapshot = RuntimeSnapshot(
        current_user="dotmac_erp_app",
        session_user="dotmac_erp_app",
        role_posture={"dotmac_erp_app": (False, False)},
        schema_usage=tuple(
            SchemaUsage(schema=module.schema, present=True, usable=False)
            for module in COMPOSED_MODULES
        ),
        table_privileges=_privileges(*COMPOSED_MODULES, held=False),
        rls_probes=(),
        active_modules=(),
    )
    assert runtime_admission_violations(snapshot) == ()


def test_todays_production_posture_is_admitted() -> None:
    """The exact safety property: this change must not break the next deploy.

    ERP production connects as `dotmac_erp_app`, no composed module declares
    itself active, and `mod_*` schemas may not even exist there yet.
    """
    snapshot = RuntimeSnapshot(
        current_user="dotmac_erp_app",
        session_user="dotmac_erp_app",
        role_posture={"dotmac_erp_app": (False, False)},
        schema_usage=tuple(
            SchemaUsage(schema=module.schema, present=False, usable=False)
            for module in COMPOSED_MODULES
        ),
        table_privileges=(),
        rls_probes=(),
        active_modules=active_modules({}),
    )
    assert active_modules({}) == ()
    assert runtime_admission_violations(snapshot) == ()


# ---------------------------------------------------------------------------
# The remaining refusals: indirection, escalation, unprovable probes
# ---------------------------------------------------------------------------


def test_it_fires_when_app_user_was_reached_with_set_role() -> None:
    """A session that can SET ROLE to `app_user` can SET ROLE back out of it,
    which makes the restriction advisory rather than enforced."""
    tampered = replace(_conforming(), session_user="dotmac_erp_app")
    _fires(tampered, "must be the LOGIN")


def test_it_fires_when_app_user_inherits_a_bypassrls_role() -> None:
    """`NOBYPASSRLS` is an attribute of a role, not a property of a session."""
    tampered = replace(_conforming(), escalation_memberships=("app_admin",))
    _fires(tampered, "is a member of 'app_admin'")


def test_it_fires_when_the_isolation_probe_did_not_run() -> None:
    """An unprovable probe fails closed. Activating a module is what takes on
    the obligation to make it provable."""
    tampered = replace(
        _conforming(),
        rls_probes=(
            RlsProbe(
                schema=ACCOUNTING.schema,
                table=ACCOUNTING.probe_table,
                executed=False,
                error="RUNTIME_ADMISSION_TENANT_ID is unset",
            ),
        ),
    )
    _fires(tampered, "did not run")


def test_it_fires_when_no_probe_was_recorded_at_all() -> None:
    """A missing probe must not read as a passing probe — that is how a gate
    becomes a catalog reading with extra steps."""
    tampered = replace(_conforming(), rls_probes=())
    _fires(tampered, "no isolation probe was recorded")


def test_it_fires_when_the_module_schema_is_absent() -> None:
    baseline = _conforming()
    tampered = replace(
        baseline,
        schema_usage=tuple(
            replace(entry, present=False, usable=False)
            if entry.schema == ACCOUNTING.schema
            else entry
            for entry in baseline.schema_usage
        ),
    )
    _fires(tampered, "does not exist")


def test_it_fires_when_the_schema_is_present_but_not_usable() -> None:
    baseline = _conforming()
    tampered = replace(
        baseline,
        schema_usage=tuple(
            replace(entry, usable=False) if entry.schema == ACCOUNTING.schema else entry
            for entry in baseline.schema_usage
        ),
    )
    _fires(tampered, "no USAGE on schema")


def test_it_fires_when_an_active_code_has_no_declaration() -> None:
    """An activation flag with no declaration would assert nothing at all."""
    tampered = replace(_conforming(), active_modules=("not-a-module",))
    _fires(tampered, "RUNTIME DECLARATION")


def test_it_fires_when_the_runtime_role_is_absent_from_pg_roles() -> None:
    tampered = replace(_conforming(), role_posture={})
    _fires(tampered, "was not found in pg_roles")


# ---------------------------------------------------------------------------
# Activation is a declaration, never an inference
# ---------------------------------------------------------------------------


def test_activation_reads_only_the_declared_flag() -> None:
    assert active_modules({}) == ()
    assert active_modules({"ACCOUNTING_COMPOSITION_ENABLED": "false"}) == ()
    assert active_modules({"ACCOUNTING_COMPOSITION_ENABLED": "1"}) == ()
    assert active_modules({"ACCOUNTING_COMPOSITION_ENABLED": "yes"}) == ()
    assert active_modules({"ACCOUNTING_COMPOSITION_ENABLED": "TRUE"}) == (ACCOUNTING,)
    assert active_modules({"ACCOUNTING_COMPOSITION_ENABLED": " true "}) == (ACCOUNTING,)


def test_every_composed_module_declares_a_distinct_activation_flag() -> None:
    """Two modules sharing a flag would activate storage nobody asked for."""
    flags = [module.activation_env_var for module in COMPOSED_MODULES]
    assert all(flags), "a module with no activation flag can never be activated"
    assert len(set(flags)) == len(flags), f"duplicate activation flags: {flags}"


def test_every_module_probe_table_is_one_of_its_own_tenant_tables() -> None:
    """A probe against a relation the module does not own would prove isolation
    for somebody else's schema."""
    for module in COMPOSED_MODULES:
        assert module.probe_table in module.tenant_tables, module.module_code


# ---------------------------------------------------------------------------
# Silence is not a pass
# ---------------------------------------------------------------------------


def test_the_empty_active_set_is_reported_loudly() -> None:
    """The transcript must SAY that nothing was proven, in as many words."""
    snapshot = RuntimeSnapshot(
        current_user="dotmac_erp_app",
        session_user="dotmac_erp_app",
        role_posture={"dotmac_erp_app": (False, False)},
        active_modules=(),
    )
    report = "\n".join(runtime_admission_report(snapshot))
    assert VACUOUS_ADMISSION_NOTICE in report
    assert "NOTHING WAS PROVEN ABOUT THE RUNTIME ROLE" in report
    assert "dotmac_erp_app" in report
    for module in COMPOSED_MODULES:
        assert f"SKIPPED  {module.module_code}" in report
        assert module.activation_env_var in report


def test_the_transcript_names_every_check_it_ran_for_an_active_module() -> None:
    report = "\n".join(runtime_admission_report(_conforming()))
    assert VACUOUS_ADMISSION_NOTICE not in report
    assert f"ACTIVE   {ACCOUNTING.module_code}" in report
    assert "SKIPPED  tax" in report
    assert "SELECT/INSERT/UPDATE/DELETE" in report
    assert f"{ACCOUNTING.schema}.{ACCOUNTING.probe_table}" in report
    assert "own_rows=7" in report


def test_an_empty_probe_says_so_rather_than_claiming_evidence() -> None:
    """A module activated on a fresh database has no rows. That is admissible,
    but the transcript must not let it read as a proof against real data."""
    baseline = _conforming()
    empty = replace(
        baseline,
        rls_probes=tuple(replace(probe, own_rows=0) for probe in baseline.rls_probes),
    )
    assert runtime_admission_violations(empty) == ()
    report = "\n".join(runtime_admission_report(empty))
    assert "exercised structurally, not against data" in report


def test_the_platform_plane_is_named_and_deliberately_not_required() -> None:
    """ADR-0023 REVOKEs the control-plane tables from the tenant app role, so
    requiring `app_user` to reach them would require the isolation broken."""
    report = "\n".join(runtime_admission_report(_conforming()))
    assert "mod_files.platform_stored_files" in report
    assert "NOT required" in report
    declared_platform = {
        table for module in COMPOSED_MODULES for table in module.platform_tables
    }
    demanded = {entry.table for entry in _privileges(*COMPOSED_MODULES)}
    assert not (declared_platform & demanded)
