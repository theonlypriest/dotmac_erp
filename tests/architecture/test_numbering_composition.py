"""`dotmac-numbering` is composed on ERP's tenant plane, with no caller cutover.

This is the static half of the composition proof.  The PostgreSQL rehearsal in
`tests/integration/test_accounting_lineage_composition.py` proves the migration
and live catalog; these tests prove the exact artifact, revision location,
assembly-selected plane and absence of a runtime dependency under `app/`.
The release-only product assembly may import exactly
`dotmac_numbering.manifest`; that declarative seam is not a runtime caller.
"""

from __future__ import annotations

import ast
import configparser
import tomllib
from pathlib import Path

from dotmac_kernel.planes import (
    ModulePlane,
    ModulePlaneSelection,
    validate_module_plane_selections,
)

from app.migration_bindings import COMPOSED_MODULE_LINEAGES
from app.migration_planes import ASSEMBLY_MODULE_PLANES

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = REPO_ROOT / "app"
PRODUCT_ASSEMBLY = APP_ROOT / "product_assembly.py"
PYPROJECT = REPO_ROOT / "pyproject.toml"
LOCK = REPO_ROOT / "poetry.lock"
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
ALEMBIC_ENV = REPO_ROOT / "alembic" / "env.py"

DISTRIBUTION = "dotmac-numbering"
IMPORT_PACKAGE = "dotmac_numbering"
EXPECTED_VERSION = "0.1.0a2"
EXPECTED_LOCATION = "dotmac_numbering.migrations:versions"
EXPECTED_REVISION = "nu_0001_numbering"
EXPECTED_SELECTION = ModulePlaneSelection(
    module="numbering", planes=(ModulePlane.TENANT,)
)


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


def _is_numbering_runtime_import(path: Path, module: str) -> bool:
    """Only the product assembly may consume the declarative manifest."""
    if module != IMPORT_PACKAGE and not module.startswith(f"{IMPORT_PACKAGE}."):
        return False
    return path != PRODUCT_ASSEMBLY or module != f"{IMPORT_PACKAGE}.manifest"


def test_the_distribution_is_pinned_exactly_from_the_private_source() -> None:
    dependencies = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"][
        "poetry"
    ]["dependencies"]
    declared = dependencies[DISTRIBUTION]
    assert declared == {"version": EXPECTED_VERSION, "source": "forgejo"}


def test_the_lock_resolves_the_reviewed_artifact() -> None:
    locked = [
        package
        for package in tomllib.loads(LOCK.read_text(encoding="utf-8"))["package"]
        if package["name"] == DISTRIBUTION
    ]
    assert len(locked) == 1
    assert locked[0]["version"] == EXPECTED_VERSION
    assert locked[0]["source"]["type"] == "legacy"


def test_the_environment_resolves_the_reviewed_artifact() -> None:
    from importlib.metadata import version

    assert version(DISTRIBUTION) == EXPECTED_VERSION


def test_alembic_resolves_the_reviewed_numbering_revision() -> None:
    from alembic.util.pyfiles import coerce_resource_to_filename

    assert EXPECTED_LOCATION in _version_locations()
    resolved = Path(coerce_resource_to_filename(EXPECTED_LOCATION))
    assert resolved.is_dir()
    assert (resolved / f"{EXPECTED_REVISION}.py").is_file()
    assert COMPOSED_MODULE_LINEAGES["numbering"] == EXPECTED_REVISION


def test_erp_selects_exactly_the_numbering_tenant_plane() -> None:
    from dotmac_numbering.manifest import module

    assert ASSEMBLY_MODULE_PLANES == (EXPECTED_SELECTION,)
    assert validate_module_plane_selections((module,), ASSEMBLY_MODULE_PLANES) == (
        EXPECTED_SELECTION,
    )
    assert ModulePlane.PLATFORM not in EXPECTED_SELECTION.planes


def test_alembic_installs_the_plane_selection_before_revision_loading() -> None:
    source = ALEMBIC_ENV.read_text(encoding="utf-8")
    installation = source.index(
        "install_module_plane_selections(ASSEMBLY_MODULE_PLANES)"
    )
    revision_loading = source.index("MODEL_MODULES =")
    assert installation < revision_loading


def test_nothing_under_app_imports_the_numbering_runtime() -> None:
    """Storage composition must not silently repoint a business caller.

    `app.product_assembly` may import the declarative package manifest to bind
    release identity. Any model, service or manifest submodule remains a
    runtime import and is rejected by the same scan.
    """
    offenders = sorted(
        path.relative_to(REPO_ROOT).as_posix()
        for path in APP_ROOT.rglob("*.py")
        if any(
            _is_numbering_runtime_import(path, module)
            for module in _imported_modules(path)
        )
    )
    assert not offenders, f"app/ imports {IMPORT_PACKAGE}: {offenders}"


def test_numbering_manifest_seam_is_narrow_and_runtime_sensitive() -> None:
    other_path = APP_ROOT / "services" / "example.py"
    assert not _is_numbering_runtime_import(
        PRODUCT_ASSEMBLY, "dotmac_numbering.manifest"
    )
    assert _is_numbering_runtime_import(other_path, "dotmac_numbering.manifest")
    assert _is_numbering_runtime_import(PRODUCT_ASSEMBLY, "dotmac_numbering")
    assert _is_numbering_runtime_import(PRODUCT_ASSEMBLY, "dotmac_numbering.service")
    assert _is_numbering_runtime_import(
        PRODUCT_ASSEMBLY, "dotmac_numbering.manifest.private"
    )
