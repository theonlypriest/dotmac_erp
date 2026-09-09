"""`dotmac-accounting` is COMPOSED and DISABLED — both halves proven.

Gate A asserted the module was ABSENT four ways.  Gate C pins it and composes
its lineage, so those four assertions are not weakened or deleted quietly —
they are REPLACED by their positive counterparts, in the same reviewed change
that makes the positive statement true:

| gate A (absence)                      | gate C (presence)                        |
| ------------------------------------- | ---------------------------------------- |
| not in `pyproject.toml`                | pinned EXACTLY, from the `forgejo` source |
| not in `version_locations`             | composed, and it RESOLVES to a real lineage |
| `composition_state()` reports absent   | reports the installed version             |
| `require_composition_ready()` says "not installed" | says "not enabled"          |

One gate-A assertion survives with a release-metadata seam: nothing under
`app/` imports the package runtime.  `app.product_assembly` may import the
declarative `dotmac_accounting.manifest` and nothing else from the package.
Composition is a storage fact; any other code-level import would be the start
of a runtime dependency, and that belongs to a later gate.

`test_the_distribution_is_not_pinned` was never an oracle for whether the
release tag existed — it was an absence guard for gate A, and treating it as a
tag oracle would have made "we forgot to pin" and "there is nothing to pin"
indistinguishable.  Gate B is settled by the annotated tag
`dotmac-accounting-v0.1.0a1` (peeling to Starter `20d24703`), not by this file.

## Storage is not authority

`mod_accounting` now exists in a migrated database.  Nothing decides anything
with it: `ACCOUNTING_COMPOSITION_ENABLED` is false, no writer is repointed, no
backfill has run.  ERP already applies this exact rule to
`idempotency_ledger.v1`, whose tables have existed since
`20260820_idempotency_ledger` while its operations remain uncut-over.

The LIVE proofs — that the lineage applies, that prerequisites hold against the
real catalog, that the branch heads are exactly the expected ones — are
PostgreSQL work and live in
`tests/integration/test_accounting_lineage_composition.py`.  This file is the
static half.
"""

from __future__ import annotations

import ast
import configparser
import tomllib
from pathlib import Path

import pytest

from app import accounting_adoption

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = REPO_ROOT / "app"
PRODUCT_ASSEMBLY = APP_ROOT / "product_assembly.py"
PYPROJECT = REPO_ROOT / "pyproject.toml"
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


def _version_locations() -> list[str]:
    """Read the raw value; `%(here)s` is Alembic's to interpolate, not ours."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(ALEMBIC_INI)
    return parser["alembic"]["version_locations"].split()


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _is_accounting_runtime_import(path: Path, module: str) -> bool:
    """Only the product assembly may consume the declarative manifest."""
    package = accounting_adoption.IMPORT_PACKAGE
    if module != package and not module.startswith(f"{package}."):
        return False
    return path != PRODUCT_ASSEMBLY or module != f"{package}.manifest"


def test_the_distribution_is_pinned_exactly_from_the_private_source() -> None:
    """A range would let a deploy resolve a `mod_accounting` lineage nobody
    reviewed — the one kind of dependency drift that changes the DATABASE rather
    than the code.  ERP pins every dotmac distribution exactly for that reason,
    and a module carrying its own migrations is the sharpest case.
    """
    declared = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["poetry"][
        "dependencies"
    ][accounting_adoption.DISTRIBUTION]
    assert declared["source"] == "forgejo"
    version = declared["version"]
    assert not any(character in version for character in "^~><*="), (
        f"{accounting_adoption.DISTRIBUTION} must be pinned exactly, got {version!r}"
    )
    assert version == accounting_adoption.EXPECTED_VERSION


def test_the_installed_version_is_the_pinned_version() -> None:
    """The pin is a claim about what a deploy WILL resolve; this checks what
    this environment actually did resolve.  A lock that drifted from the pin, or
    an editable install someone left behind, fails here."""
    from importlib.metadata import version

    assert version(accounting_adoption.DISTRIBUTION) == (
        accounting_adoption.EXPECTED_VERSION
    )


def test_nothing_under_app_imports_the_module() -> None:
    """No runtime import is hidden behind the release-only manifest seam.

    `accounting_adoption.py` describes the storage composition without importing
    the package. `product_assembly.py` binds immutable release identity and may
    therefore import exactly the package manifest, never a model or service.
    """
    offenders = sorted(
        path.relative_to(REPO_ROOT).as_posix()
        for path in APP_ROOT.rglob("*.py")
        if any(
            _is_accounting_runtime_import(path, module)
            for module in _imported_modules(path)
        )
    )
    assert not offenders, (
        f"app/ imports {accounting_adoption.IMPORT_PACKAGE}: {offenders}"
    )


def test_accounting_manifest_seam_is_narrow_and_runtime_sensitive() -> None:
    other_path = APP_ROOT / "services" / "example.py"
    assert not _is_accounting_runtime_import(
        PRODUCT_ASSEMBLY, "dotmac_accounting.manifest"
    )
    assert _is_accounting_runtime_import(other_path, "dotmac_accounting.manifest")
    assert _is_accounting_runtime_import(PRODUCT_ASSEMBLY, "dotmac_accounting")
    assert _is_accounting_runtime_import(PRODUCT_ASSEMBLY, "dotmac_accounting.service")
    assert _is_accounting_runtime_import(
        PRODUCT_ASSEMBLY, "dotmac_accounting.manifest.private"
    )


def test_alembic_composes_the_accounting_lineage() -> None:
    assert accounting_adoption.MIGRATION_VERSION_LOCATION in _version_locations()


def test_the_version_location_resolves_to_the_installed_distribution() -> None:
    """Not "is the string right" — "does the string resolve to a directory
    holding the revision ERP expects".

    A typo in `version_locations` is invisible until `alembic upgrade` finds no
    such revision, which on a deploy is the worst possible moment.  This reads
    the artifact Alembic itself will read.
    """
    from alembic.util.pyfiles import coerce_resource_to_filename

    resolved = Path(
        coerce_resource_to_filename(accounting_adoption.MIGRATION_VERSION_LOCATION)
    )
    assert resolved.is_dir()
    assert (resolved / "ac_0001_accounting.py").is_file()


def test_alembic_still_never_composes_the_kernel_lineage() -> None:
    """Composing a SECOND module lineage must not smuggle in the one that can
    never run here.  Kernel `0001` creates `public.tenants`, which ERP owns."""
    offenders = [
        location
        for location in _version_locations()
        if "dotmac_kernel.migrations" in location
    ]
    assert not offenders, f"ERP must not compose the kernel lineage: {offenders}"


def test_the_kernel_lineage_guard_is_sensitive() -> None:
    """The assertion above passes over a list that happens to be clean; prove it
    would bite on a dirty one (ADR-0018)."""
    dirty = ["%(here)s/alembic/versions", "dotmac_kernel.migrations:versions"]
    assert [loc for loc in dirty if "dotmac_kernel.migrations" in loc]


def test_composition_reports_the_installed_version_and_is_still_not_ready() -> None:
    """Installed is not enabled.  `composition_state()` reports what the
    environment actually has, and `ready` stays false because the flag is the
    thing that separates storage from authority."""
    state = accounting_adoption.composition_state()
    assert state["installed_version"] == accounting_adoption.EXPECTED_VERSION
    assert state["enabled"] is False
    assert state["ready"] is False


def test_asking_for_the_module_now_refuses_for_the_OTHER_reason() -> None:
    """The refusal must name which gate is unmet, because the operator response
    differs: at gate A the wheel was missing; now it is present and deliberately
    switched off.  A single "not ready" message would hide a deploy that failed
    to install the wheel behind one that simply has not been told to use it.
    """
    with pytest.raises(accounting_adoption.AccountingCompositionNotReady) as excinfo:
        accounting_adoption.require_composition_ready()
    message = str(excinfo.value)
    assert "ACCOUNTING_COMPOSITION_ENABLED is false" in message
    assert "not installed" not in message


def test_required_prerequisites_are_already_bound_to_erp_revisions() -> None:
    """The prerequisite bindings RESOLVE — which is weaker than verified.

    ERP hosts `public.tenants` itself and can never run kernel `0001`, so a
    module needing a tenant FK target, the three database roles and the
    at-most-once ledger installs here only because ERP supplies all three
    effects from revisions it actually runs.  If a rebase dropped one, the
    failure belongs here and not at `alembic upgrade` on a first adopter.

    What this does NOT prove: that the bound revisions really supply the effects
    in a database.  `require_prerequisites` checks that against the live catalog
    at migration time — table shape, key and index contract, the tenant
    function's semantics, the three roles' posture — and it has not run for
    Accounting, because the lineage has never been composed here.  This test
    covers the declaration layer only, and gate C covers the rest.
    """
    from dotmac_kernel.prerequisites import (
        install_prerequisite_bindings,
        resolve_depends_on,
    )

    from app.migration_bindings import ASSEMBLY_PREREQUISITE_BINDINGS

    install_prerequisite_bindings(ASSEMBLY_PREREQUISITE_BINDINGS)
    edges = set(resolve_depends_on(accounting_adoption.REQUIRED_PREREQUISITES))
    assert edges == {
        "20260813_tenant_projection",
        "20260814_database_roles",
        "20260820_idempotency_ledger",
    }
    assert not any(edge.startswith("0001_") for edge in edges)


def test_the_relation_map_covers_every_gl_relation_exactly_once() -> None:
    """A relation missing from the map is a table nobody decided about — the
    exact gap that turns into "we forgot the batches" during a cutover."""
    from tests.architecture.test_accounting_gl_boundary import GL_MODELS

    assert set(accounting_adoption.RELATION_OWNERSHIP) == set(GL_MODELS.values())


def test_every_gl_relation_has_exactly_one_of_three_outcomes() -> None:
    """Migrating, retained, or retiring-with-its-writer — and never two of them.

    Two outcomes are not enough.  A relation with no module counterpart is not
    automatically one ERP keeps: `gl.posting_batch` has no module table AND no
    surviving ERP writer once the poster is sealed.  Collapsing that third case
    into "kept" promises a writer that will not exist; collapsing it into
    "migrating" promises a table that will not exist.  This partition is what the
    writer ledger's `keep_local` invariant reads.
    """
    migrating = set(accounting_adoption.migrating_relations())
    retained = set(accounting_adoption.RETAINED_ERP_RELATIONS)
    retiring = set(accounting_adoption.relations_retiring_with_their_writer())

    assert migrating | retained | retiring == set(
        accounting_adoption.RELATION_OWNERSHIP
    )
    assert not migrating & retained
    assert not migrating & retiring
    assert not retained & retiring


def test_the_three_outcomes_hold_the_relations_they_are_supposed_to() -> None:
    assert set(accounting_adoption.RETAINED_ERP_RELATIONS) == {
        "gl.account_balance",
        "gl.balance_refresh_queue",
        "gl.budget",
        "gl.budget_line",
    }
    assert accounting_adoption.relations_retiring_with_their_writer() == (
        "gl.posting_batch",
    )


def test_a_retained_relation_really_has_no_module_counterpart() -> None:
    """ERP cannot keep writing a relation the module also owns — that is two
    writers over one fact, which is the whole thing a cutover exists to end."""
    for relation in accounting_adoption.RETAINED_ERP_RELATIONS:
        assert accounting_adoption.RELATION_OWNERSHIP[relation] is None, relation


def test_module_relation_lookup_fails_loudly_on_an_unknown_relation() -> None:
    assert accounting_adoption.module_relation_for("gl.journal_entry") == (
        "journal_entries"
    )
    assert accounting_adoption.module_relation_for("gl.budget") is None
    with pytest.raises(KeyError):
        accounting_adoption.module_relation_for("gl.journal_entries")


def test_migrating_relations_are_ordered_by_dependency() -> None:
    """The backfill and the comparator both iterate this; an account must exist
    before a line references it, and a period before a journal is dated into it."""
    order = accounting_adoption.migrating_relations()
    assert order.index("gl.account_category") < order.index("gl.account")
    assert order.index("gl.account") < order.index("gl.journal_entry_line")
    assert order.index("gl.fiscal_year") < order.index("gl.fiscal_period")
    assert order.index("gl.fiscal_period") < order.index("gl.journal_entry")
    assert order.index("gl.journal_entry") < order.index("gl.journal_entry_line")
    assert order.index("gl.journal_entry_line") < order.index("gl.posted_ledger_line")


def test_the_declared_tables_match_the_modules_own_manifest() -> None:
    """`EXPECTED_MODULE_TABLES` is a claim about someone else's artifact.  The
    artifact is installed, so the claim is checked against it on every run.

    This is what replaces the gate-A skip: a claim that was unverifiable while
    the wheel was absent is now simply verified, and a divergence is a failing
    test rather than a surprise during backfill.
    """
    from dotmac_accounting.manifest import module

    assert set(module.tables) == set(accounting_adoption.EXPECTED_MODULE_TABLES)


def test_the_declared_prerequisites_match_the_modules_own_manifest() -> None:
    from dotmac_accounting.manifest import module

    assert set(module.requires) == set(accounting_adoption.REQUIRED_PREREQUISITES)


def test_the_declared_module_code_and_version_match_the_manifest() -> None:
    from dotmac_accounting.manifest import module

    assert module.code == accounting_adoption.MODULE_CODE
    assert module.version == accounting_adoption.EXPECTED_VERSION


def test_the_module_is_not_core_so_composition_stays_a_choice() -> None:
    """A `core=True` module would be mandatory in every assembly that installs
    the kernel.  Accounting is selectable, which is what lets ERP compose its
    storage while leaving the decision switched off."""
    from dotmac_accounting.manifest import module

    assert module.core is False


def test_the_module_owns_its_own_schema_and_migration_prefix() -> None:
    """One immutable `mod_<short_code>` schema per stateful module, and a
    migration prefix that keeps its revision ids distinguishable in a shared
    `alembic_version` table."""
    from dotmac_accounting.manifest import module

    assert module.short_code == "accounting"
    assert module.migration_prefix == "ac"
    assert module.migration_branch == "accounting"
