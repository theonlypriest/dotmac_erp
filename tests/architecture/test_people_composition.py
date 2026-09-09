"""The People module is composed behind one Employment Type assembly owner."""

from __future__ import annotations

import ast
import configparser
import re
import tomllib
from pathlib import Path

from app import bill_of_materials as bom
from app.migration_bindings import COMPOSED_MODULE_LINEAGES
from app.migration_planes import ASSEMBLY_MODULE_PLANES

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = REPO_ROOT / "app"
RUNTIME_ENTRY_POINT_ROOTS = tuple(
    REPO_ROOT / root for root in ("app", "scripts", "tools")
)
PRODUCT_ASSEMBLY = APP_ROOT / "product_assembly.py"
EMPLOYMENT_TYPE_OWNER = APP_ROOT / "services" / "people" / "hr" / "employment_types.py"
REPAIR_CLI = REPO_ROOT / "scripts" / "repair_people_employment_types.py"
PYPROJECT = REPO_ROOT / "pyproject.toml"
LOCK = REPO_ROOT / "poetry.lock"
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

DISTRIBUTION = "dotmac-people"
IMPORT_PACKAGE = "dotmac_people"
EXPECTED_VERSION = "0.1.0a2"
EXPECTED_RELEASE_FILES = {
    (
        "dotmac_people-0.1.0a2-py3-none-any.whl",
        "sha256:1c239fe814d82c4e478f0c117816f69a8907a1502b4a9f720d8006643ae1a366",
    ),
    (
        "dotmac_people-0.1.0a2.tar.gz",
        "sha256:b62a121aae7fbfa8488431bda8fd462f34480de89e58bfc0b9894bbb881221e6",
    ),
}
EXPECTED_KERNEL_FLOOR = "0.1.0a98"
EXPECTED_LOCATION = "dotmac_people.migrations:versions"
EXPECTED_REVISION = "pe_0001_people_directory"
EXPECTED_TABLES = {
    "departments",
    "designations",
    "employees",
    "employment_types",
    "position_assignments",
    "positions",
}
EXPECTED_REQUIRES = {
    "tenant_scope_catalog.v1",
    "module_database_roles.v1",
    "party_person_catalog.v1",
}


def _version_locations() -> list[str]:
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


def _runtime_python_sources() -> tuple[Path, ...]:
    paths: list[Path] = []
    for root in RUNTIME_ENTRY_POINT_ROOTS:
        assert root.is_dir(), f"People runtime entry-point family disappeared: {root}"
        paths.extend(root.rglob("*.py"))
    return tuple(paths)


def _is_people_runtime_import(path: Path, module: str) -> bool:
    """Allow only declarative composition and the one public owner seam."""
    imports_package = module == IMPORT_PACKAGE or module.startswith(
        f"{IMPORT_PACKAGE}."
    )
    release_manifest_seam = (
        path == PRODUCT_ASSEMBLY and module == f"{IMPORT_PACKAGE}.manifest"
    )
    owner_seam = path == EMPLOYMENT_TYPE_OWNER and module == IMPORT_PACKAGE
    return imports_package and not release_manifest_seam and not owner_seam


def test_the_distribution_is_pinned_exactly_from_the_private_source() -> None:
    dependencies = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"][
        "poetry"
    ]["dependencies"]
    assert dependencies[DISTRIBUTION] == {
        "version": EXPECTED_VERSION,
        "source": "forgejo",
    }


def test_the_lock_resolves_the_reviewed_artifact() -> None:
    locked = [
        package
        for package in tomllib.loads(LOCK.read_text(encoding="utf-8"))["package"]
        if package["name"] == DISTRIBUTION
    ]
    assert len(locked) == 1
    assert locked[0]["version"] == EXPECTED_VERSION
    assert locked[0]["source"]["type"] == "legacy"
    assert {(item["file"], item["hash"]) for item in locked[0]["files"]} == (
        EXPECTED_RELEASE_FILES
    )


def test_the_bill_of_materials_matches_the_reviewed_a2_kernel_floor() -> None:
    locked = next(
        package
        for package in tomllib.loads(LOCK.read_text(encoding="utf-8"))["package"]
        if package["name"] == DISTRIBUTION
    )
    people_step = next(
        step for step in bom.COMPOSITION_PLAN if step.distribution == DISTRIBUTION
    )
    assert locked["dependencies"]["dotmac-kernel"] == f">={EXPECTED_KERNEL_FLOOR}"
    assert people_step.kernel_floor == EXPECTED_KERNEL_FLOOR
    assert bom.KERNEL_FLOOR_DEMANDED_BY_SELECTION == EXPECTED_KERNEL_FLOOR


def test_the_environment_resolves_the_reviewed_artifact() -> None:
    from importlib.metadata import version

    assert version(DISTRIBUTION) == EXPECTED_VERSION


def test_alembic_resolves_the_reviewed_people_revision() -> None:
    from alembic.util.pyfiles import coerce_resource_to_filename

    assert EXPECTED_LOCATION in _version_locations()
    resolved = Path(coerce_resource_to_filename(EXPECTED_LOCATION))
    assert resolved.is_dir()
    assert (resolved / f"{EXPECTED_REVISION}.py").is_file()
    assert COMPOSED_MODULE_LINEAGES["people"] == EXPECTED_REVISION


def test_people_is_an_atomic_tenant_only_store() -> None:
    from dotmac_people.manifest import module

    assert set(module.tables) == EXPECTED_TABLES
    assert not module.platform_tables
    assert set(module.requires) == EXPECTED_REQUIRES
    assert all(selection.module != "people" for selection in ASSEMBLY_MODULE_PLANES)


def test_only_the_employment_type_owner_imports_the_people_runtime() -> None:
    offenders = sorted(
        path.relative_to(REPO_ROOT).as_posix()
        for path in _runtime_python_sources()
        if any(
            _is_people_runtime_import(path, module)
            for module in _imported_modules(path)
        )
    )
    assert not offenders, f"runtime imports {IMPORT_PACKAGE}: {offenders}"


def test_the_operator_entry_point_is_repair_only() -> None:
    source = REPAIR_CLI.read_text(encoding="utf-8")
    assert "repair_compatibility_projection" in source
    assert "--dry-run" in source
    assert "bootstrap" not in source.casefold()
    assert "reconcile" not in source.casefold()
    assert {root.name for root in RUNTIME_ENTRY_POINT_ROOTS} == {
        "app",
        "scripts",
        "tools",
    }


REVERSE_EMPLOYMENT_TYPE_TOKENS = (
    "ReconcileEmploymentType",
    "reconcile_employment_type",
    "employment_type_bootstrap",
    "bootstrap_people_employment_types",
)


def _reverse_path_offenders(
    paths: tuple[Path, ...], root: Path
) -> dict[str, list[str]]:
    return {
        path.relative_to(root).as_posix(): [
            token
            for token in REVERSE_EMPLOYMENT_TYPE_TOKENS
            if token in path.read_text(encoding="utf-8")
        ]
        for path in paths
        if any(
            token in path.read_text(encoding="utf-8")
            for token in REVERSE_EMPLOYMENT_TYPE_TOKENS
        )
    }


def test_the_reverse_employment_type_detector_still_bites(tmp_path: Path) -> None:
    """Sensitivity proof for the emptiness the next test asserts.

    A scan that returns nothing is evidence of absence only once the same scan
    has been shown to find a planted occurrence. Without this, a renamed helper
    or a broken source walk would read as a clean repository.
    """
    planted = tmp_path / "reverse_path.py"
    planted.write_text(
        "from app import employment_type_bootstrap\n",
        encoding="utf-8",
    )
    clean = tmp_path / "owner.py"
    clean.write_text("from app import employment_types\n", encoding="utf-8")

    assert _reverse_path_offenders((planted, clean), tmp_path) == {
        "reverse_path.py": ["employment_type_bootstrap"]
    }


def test_no_reverse_employment_type_path_remains_in_runtime_sources() -> None:
    sources = _runtime_python_sources()
    # The walk itself is load-bearing; an empty one would make the assertion
    # below vacuous.
    assert len(sources) > 100, len(sources)
    assert not _reverse_path_offenders(sources, REPO_ROOT)


def test_runtime_consumers_never_read_the_legacy_employment_type_model() -> None:
    legacy_relationship_deref = re.compile(r"\b(?:employee|emp)\.employment_type\.")
    offenders = {
        path.relative_to(REPO_ROOT).as_posix(): "legacy Employment Type model read"
        for path in _runtime_python_sources()
        if path != EMPLOYMENT_TYPE_OWNER
        and (
            "select(EmploymentType)" in path.read_text(encoding="utf-8")
            or legacy_relationship_deref.search(path.read_text(encoding="utf-8"))
        )
    }
    assert not offenders


def test_people_manifest_seam_is_narrow_and_runtime_sensitive() -> None:
    other_app_file = APP_ROOT / "other.py"
    assert not _is_people_runtime_import(PRODUCT_ASSEMBLY, "dotmac_people.manifest")
    assert _is_people_runtime_import(other_app_file, "dotmac_people.manifest")
    assert _is_people_runtime_import(PRODUCT_ASSEMBLY, "dotmac_people")
    assert _is_people_runtime_import(PRODUCT_ASSEMBLY, "dotmac_people.service")
    assert _is_people_runtime_import(PRODUCT_ASSEMBLY, "dotmac_people.manifest.private")
    assert not _is_people_runtime_import(EMPLOYMENT_TYPE_OWNER, "dotmac_people")
    assert _is_people_runtime_import(EMPLOYMENT_TYPE_OWNER, "dotmac_people.service")
    assert _is_people_runtime_import(other_app_file, "dotmac_people")
