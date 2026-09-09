"""A permission a route enforces must be one the catalogue can hold.

``require_tenant_permission(key)`` in ``app/services/auth_dependencies.py``
admits on the ``admin`` role or an explicit scope, and otherwise resolves
``key`` against the ``permissions`` table:

    permission = db.scalar(
        select(Permission)
        .where(Permission.key == permission_key)
        .where(Permission.is_active.is_(True))
    )
    if not permission:
        raise HTTPException(403, "Permission not found")

So a key with no catalogue row is not "closed by policy" — it is UNREACHABLE.
No role can be granted it, no scope check can be satisfied by a role that
holds it, and the route it guards silently becomes admin-only because the
``admin`` short-circuit runs before the lookup.  Nine keys were in exactly
that state on nine mounted routes across banking, fixed-asset import,
payments and the person identity API; they were fixed by
``alembic/versions/20260828_provision_missing_route_permissions.py`` together
with the matching ``scripts/seed_rbac`` entries.

The premise, stated so it is enforceable rather than aspirational:

    Every string literal passed to ``require_tenant_permission`` anywhere
    under ``app/`` appears as a code in ``scripts.seed_rbac``'s
    ``DEFAULT_PERMISSIONS``.

Two halves, both load-bearing:

* *every literal is READABLE* — the scan is AST-based and requires the first
  argument to be a string constant.  A call whose key is computed cannot be
  checked, so it is reported as UNREADABLE and fails here rather than being
  skipped.  Without this half someone could hide a key from the gate by
  routing it through a variable, and the check would pass vacuously.
* *every readable literal is provisioned* — the actual invariant.

``DEFAULT_PERMISSIONS`` is the right side of that statement because it is the
one catalogue the whole product agrees on: the seed script writes it, and
``20260826_provision_expense_permissions`` and
``20260828_provision_missing_route_permissions`` write frozen subsets of it on
the deployment path that does not run the seed script.  A key provisioned by a
migration alone would still be invisible to a fresh seed, so being in a
migration is not an alternative to being here.

SCOPE DISCLOSURE: this gate covers ``require_tenant_permission`` under
``app/`` — the tenant-scoped route guard, and the only one whose failure mode
is a silent catalogue miss.  Other authorization seams (``require_permission``
scope sets, ``authorized_permissions`` declarations on composite guards, the
platform-actor guards) are NOT covered, and adding one is deliberately a
separate change because each has its own readability question to answer first.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from scripts.seed_rbac import DEFAULT_PERMISSIONS


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOT = REPO_ROOT / "app"
GUARD_NAME = "require_tenant_permission"

# Keys knowingly enforced without a catalogue entry. This set is EMPTY and is
# a two-directional ratchet (see the ratchet test below): it may only be
# lowered, deliberately, in the change that provisions the key. It exists so
# that a future key which genuinely cannot be seeded has somewhere to be
# declared WITH its reason, rather than quietly weakening the gate.
UNPROVISIONED_BACKLOG: frozenset[str] = frozenset()

# Non-vacuity floor for the scan. The real tree carried 242 distinct enforced
# keys when this gate was written; a scan that collapses to a handful has
# stopped looking at the application rather than proved it clean.
MINIMUM_ENFORCED_KEYS = 200


class _GuardUsage:
    """One ``require_tenant_permission(...)`` call site."""

    __slots__ = ("key", "location")

    def __init__(self, key: str | None, location: str) -> None:
        self.key = key
        self.location = location


def collect_guard_usages(source: str, location: str) -> list[_GuardUsage]:
    """Every ``require_tenant_permission`` call in one module's source.

    ``key`` is ``None`` when the first argument is not a string literal — the
    unreadable case the caller must treat as a failure, not a skip.
    """
    usages: list[_GuardUsage] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        else:
            continue
        if name != GUARD_NAME:
            continue
        first = node.args[0] if node.args else None
        readable = isinstance(first, ast.Constant) and isinstance(first.value, str)
        usages.append(
            _GuardUsage(
                first.value if readable else None,  # type: ignore[union-attr]
                f"{location}:{node.lineno}",
            )
        )
    return usages


def scan_runtime_tree(root: Path) -> list[_GuardUsage]:
    usages: list[_GuardUsage] = []
    for path in sorted(root.rglob("*.py")):
        usages.extend(
            collect_guard_usages(
                path.read_text(encoding="utf-8"),
                str(path.relative_to(REPO_ROOT)),
            )
        )
    return usages


def find_violations(
    usages: list[_GuardUsage],
    provisioned: frozenset[str],
    backlog: frozenset[str] = UNPROVISIONED_BACKLOG,
) -> list[str]:
    """Every unreadable or unprovisioned guard key, as reportable strings."""
    violations: list[str] = []
    for usage in sorted(usages, key=lambda u: (u.location, u.key or "")):
        if usage.key is None:
            violations.append(
                f"{usage.location}: {GUARD_NAME}(...) is called with a "
                f"non-literal key — its permission cannot be read, so it is "
                f"unmonitored, not exempt"
            )
            continue
        if usage.key in provisioned or usage.key in backlog:
            continue
        violations.append(
            f"{usage.location}: enforces {usage.key!r}, which is not in "
            f"scripts.seed_rbac.DEFAULT_PERMISSIONS — no permissions row can "
            f"exist for it, so no role can ever hold it and the route is "
            f"admin-only by accident"
        )
    return violations


@pytest.fixture(scope="module")
def provisioned_codes() -> frozenset[str]:
    return frozenset(code for code, _description in DEFAULT_PERMISSIONS)


@pytest.fixture(scope="module")
def runtime_usages() -> list[_GuardUsage]:
    return scan_runtime_tree(RUNTIME_ROOT)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_every_enforced_permission_is_provisioned(
    runtime_usages: list[_GuardUsage],
    provisioned_codes: frozenset[str],
) -> None:
    violations = find_violations(runtime_usages, provisioned_codes)
    assert not violations, (
        "routes enforce permissions the catalogue cannot hold:\n  "
        + "\n  ".join(violations)
        + "\n\nFix: add the code to the right section of "
        "scripts/seed_rbac.py's permission lists AND provision it in an "
        "additive Alembic migration (the deployment path runs migrations, "
        "not the seed script) — see "
        "alembic/versions/20260828_provision_missing_route_permissions.py."
    )


def test_the_catalogue_has_no_duplicate_codes(
    provisioned_codes: frozenset[str],
) -> None:
    """A duplicated code means two descriptions race for one catalogue row.

    Asserted here because this gate treats ``DEFAULT_PERMISSIONS`` as the
    authoritative right-hand side; a catalogue that disagrees with itself
    would make the main assertion mean less than it reads.
    """
    assert len(provisioned_codes) == len(DEFAULT_PERMISSIONS)


# ---------------------------------------------------------------------------
# Ratchet: the backlog is empty, in BOTH directions
# ---------------------------------------------------------------------------


def test_the_unprovisioned_backlog_is_empty_in_both_directions() -> None:
    """Equality, not ``<=``.

    A one-directional ``len(...) <= N`` check lets a real regression hide
    behind a retirement elsewhere. Equality makes a NEW exemption fail, and
    makes a RESOLVED one fail too until this line is lowered in the same
    change that provisions the key — which is what forces the reason to be
    written down rather than deleted silently.
    """
    assert len(UNPROVISIONED_BACKLOG) == 0, (
        "the unprovisioned-permission backlog changed. If you provisioned a "
        "key, remove it from UNPROVISIONED_BACKLOG and lower this count in "
        "the same change. If you added one, do not — provision it instead."
    )


# ---------------------------------------------------------------------------
# Sensitivity: the detector sees the real tree, and it bites when planted on
# ---------------------------------------------------------------------------


def test_the_detector_actually_sees_the_application_guards(
    runtime_usages: list[_GuardUsage],
) -> None:
    """A check over an empty or mis-globbed scan passes for the wrong reason.

    Pin the shape of what this gate inspects: a large number of distinct
    keys, and specific known ones from each surface the missing nine came
    from, so a scan that quietly stops walking ``app/`` fails here first.
    """
    keys = {usage.key for usage in runtime_usages}
    assert len(keys) >= MINIMUM_ENFORCED_KEYS, (
        f"only {len(keys)} distinct enforced keys found under {RUNTIME_ROOT} "
        f"— the scan is looking at the wrong tree"
    )
    assert {
        "banking:account:update",
        "banking:statement:create",
        "fa:assets:import:execute",
        "fa:assets:import:preview",
        "fa:assets:import:read",
        "payments:invoice:initialize",
        "payments:verify",
        "people:read",
        "people:write",
    } <= keys, "the nine keys this gate was written for are no longer detected"


def test_the_detector_bites_on_a_planted_unprovisioned_key() -> None:
    """Plant the exact defect on synthetic source and prove the gate fails.

    A gate that has never been observed to fail is not evidence of anything.
    The input is synthetic rather than a mutation of the real tree, so the
    proof cannot leave the repository dirty or depend on scan ordering.
    """
    source = (
        "from fastapi import Depends\n"
        "\n"
        "@router.post('/widgets')\n"
        "def create_widget(\n"
        "    auth: dict = Depends(require_tenant_permission('widgets:create')),\n"
        "):\n"
        "    return {}\n"
    )
    usages = collect_guard_usages(source, "planted/widgets.py")
    violations = find_violations(usages, frozenset({"widgets:read"}))
    assert len(violations) == 1
    assert "widgets:create" in violations[0]
    assert "admin-only by accident" in violations[0]


def test_the_same_source_passes_once_the_key_is_provisioned() -> None:
    """Provision the planted key and the gate goes quiet — so it reacts to
    the missing catalogue entry and not to the synthetic module."""
    source = (
        "from fastapi import Depends\n"
        "\n"
        "@router.post('/widgets')\n"
        "def create_widget(\n"
        "    auth: dict = Depends(require_tenant_permission('widgets:create')),\n"
        "):\n"
        "    return {}\n"
    )
    usages = collect_guard_usages(source, "planted/widgets.py")
    assert find_violations(usages, frozenset({"widgets:create"})) == []


def test_the_detector_bites_on_an_unreadable_key() -> None:
    """The other half: a guard whose key is computed is reported as
    unmonitored rather than silently skipped."""
    source = (
        "KEY = 'widgets:' + action\nauth = Depends(require_tenant_permission(KEY))\n"
    )
    usages = collect_guard_usages(source, "planted/dynamic.py")
    violations = find_violations(usages, frozenset({"widgets:create"}))
    assert len(violations) == 1
    assert "unmonitored" in violations[0]


def test_the_detector_reads_the_attribute_call_form() -> None:
    """Both import shapes this repository uses must be seen.

    ``app/api/persons.py`` imports the guard by name; other modules reach it
    through the module object. An extractor that only matched ``ast.Name``
    would have missed the second form entirely and reported a clean tree.
    """
    source = (
        "import app.services.auth_dependencies as deps\n"
        "auth = Depends(deps.require_tenant_permission('widgets:create'))\n"
    )
    usages = collect_guard_usages(source, "planted/attribute.py")
    assert [usage.key for usage in usages] == ["widgets:create"]


def test_the_detector_ignores_unrelated_calls() -> None:
    """It must not fire on some other guard's string argument."""
    source = (
        "auth = Depends(require_platform_permission('widgets:create'))\n"
        "other = require_role('admin')\n"
    )
    assert collect_guard_usages(source, "planted/unrelated.py") == []


# ---------------------------------------------------------------------------
# The seed script and the provisioning migration must not drift apart
# ---------------------------------------------------------------------------


def _load_provisioning_migration():
    import importlib.util

    path = (
        REPO_ROOT
        / "alembic"
        / "versions"
        / "20260828_provision_missing_route_permissions.py"
    )
    spec = importlib.util.spec_from_file_location(
        "route_permission_provisioning_migration", path
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_is_a_frozen_copy_of_the_seeded_route_permissions() -> None:
    """The migration carries frozen literals on purpose — it must stay
    executable after the seed declarations move — so nothing but a test can
    notice when the two drift. Codes, descriptions and role grants are all
    compared, because a description-only drift still produces two different
    catalogue rows depending on which writer ran first."""
    from scripts.seed_rbac import DEFAULT_ROLES, ROLE_PERMISSIONS

    migration = _load_provisioning_migration()
    catalogue = dict(DEFAULT_PERMISSIONS)
    role_descriptions = dict(DEFAULT_ROLES)
    new_codes = {code for code, _description in migration.ROUTE_PERMISSIONS}

    assert len(migration.ROUTE_PERMISSIONS) == 9  # non-vacuity
    assert new_codes <= catalogue.keys()
    assert all(
        catalogue[code] == description
        for code, description in migration.ROUTE_PERMISSIONS
    )
    assert all(
        role_descriptions[role] == description
        for role, description in migration.ROLE_DESCRIPTIONS.items()
    )

    seeded_grants = {
        role: tuple(sorted(code for code in codes if code in new_codes))
        for role, codes in ROLE_PERMISSIONS.items()
        if any(code in new_codes for code in codes)
    }
    migrated_grants = {
        role: tuple(sorted(codes)) for role, codes in migration.ROLE_GRANTS.items()
    }
    assert seeded_grants == migrated_grants


def test_every_migrated_grant_references_a_declared_permission_and_role() -> None:
    migration = _load_provisioning_migration()
    codes = {code for code, _description in migration.ROUTE_PERMISSIONS}

    assert set(migration.ROLE_GRANTS) <= set(migration.ROLE_DESCRIPTIONS)
    assert set(migration.ROLE_DESCRIPTIONS) == set(migration.ROLE_GRANTS)
    assert all(set(granted) <= codes for granted in migration.ROLE_GRANTS.values())
    assert set().union(*migration.ROLE_GRANTS.values()) == codes
