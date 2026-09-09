"""ERP's `deploy/product.toml` is a real, conformant `ProductDeploymentSpec.v1`.

The published `dotmac-deployment-foundation==0.2.0a2` distribution is an exact
dev dependency. A missing package is therefore an installation failure, never
a reason to skip this architecture gate.

`deploy/product.toml` is now the deploy path's IMAGE AUTHORITY, and no longer
only an adapter. `scripts/deploy.sh` reads the image reference out of
`deploy/rendered/docker-compose.yml` — rendered from this descriptor — through
`scripts/resolve_deploy_image.sh`, which refuses anything that is not
`sha256:<64 hex>`. The topology it deploys is still the root
`docker-compose.yml` plus the `Dockerfile`; only the image identity has moved
(the remaining rendered-topology gaps are enumerated in `deploy/README.md`).

This test proves the descriptor is well-formed and internally consistent — it
does not run a deployment. The gate that proves a tag cannot be deployed, in
both directions, is `tests/architecture/test_deploy_image_gate.py`.
"""

from __future__ import annotations

from pathlib import Path
import tomllib

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from dotmac_deployment_foundation import __version__ as foundation_version
from dotmac_deployment_foundation.conformance import check_all
from dotmac_deployment_foundation.errors import DeploymentFoundationError
from dotmac_deployment_foundation.spec import ProductDeploymentSpec
from dotmac_kernel.planes import (
    install_module_plane_selections,
    installed_module_plane_selections,
)
from dotmac_kernel.prerequisites import (
    install_prerequisite_bindings,
    installed_bindings,
)

from app.migration_bindings import ASSEMBLY_PREREQUISITE_BINDINGS
from app.migration_planes import ASSEMBLY_MODULE_PLANES
from scripts.product_manifest import product_manifest_digest

REPO_ROOT = Path(__file__).resolve().parents[2]
DESCRIPTOR_PATH = REPO_ROOT / "deploy" / "product.toml"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "deployment-conformance.yml"
DEPLOYMENT_README_PATH = REPO_ROOT / "deploy" / "README.md"
RENDERED_OTEL_PATH = REPO_ROOT / "deploy" / "rendered" / "otel-collector.yaml"
FOUNDATION_VERSION = "0.2.0a2"
FOUNDATION_RELEASE_SHA = "55750e104df3dd94b6f9f70bf8c8db53986394c7"
FOUNDATION_WORKFLOW_SHA = "55750e104df3dd94b6f9f70bf8c8db53986394c7"
# The published image this descriptor binds, and the protected-main revision it
# was built from. Protected-main CI resolved the digest for exactly that commit
# (`Publish Docker Image` -> "Resolve the registry digest of the tested image"),
# whose `org.opencontainers.image.revision` annotation equals the revision
# below. A tag is deliberately absent: the descriptor refuses mutable
# references, and a `sha-<short>` tag is one.
IMAGE_REPOSITORY = "ghcr.io/michaelayoade/dotmac_erp"
IMAGE_SOURCE_REVISION = "6140ed40949449e5d6d42e5737aaded909f25fd3"
IMAGE_DIGEST = "sha256:0d29ae34f99fbb9a97c0225666feca2428b8d1aa5b66a4d1a2dc642e486586b3"

#: The migration owner material, named explicitly here as a SECOND line of
#: defence beside spec.py's own parse-time refusal (D3: dotmac_erp's
#: .env.example used to default the runtime DSN to the Postgres superuser —
#: this credential split is the fix, and it must stay checkable even if a
#: future spec.py version stops raising on the violation itself).
MIGRATION_OWNER_MATERIAL = "MIGRATION_DATABASE_URL"


def _load() -> ProductDeploymentSpec:
    return ProductDeploymentSpec.load(DESCRIPTOR_PATH)


# ── the conformance ratchet ─────────────────────────────────────────────────
#
# The descriptor is NOT conformance-clean, and pretending otherwise is exactly
# the failure this branch was reviewed for. The findings below are listed
# EXACTLY — not as a subset — so the list fails in both directions: a new
# finding fails here, and a finding that gets fixed without this list shrinking
# fails here too. That is the two-directional ratchet `AGENTS.md` rule 25
# requires of a temporary deviation.
#
# EMPTY, and that is the ratchet arriving at its destination rather than the
# check being switched off. The last entry was the all-zero image sentinel;
# protected-main CI has now published the tested image and `deploy/product.toml`
# binds its real registry digest, so the finding is gone and the line describing
# it had to go with it. The assertion below is unchanged and still exact in both
# directions: a NEW finding fails, and re-adding an entry that no longer fires
# also fails.
KNOWN_UNRESOLVED: tuple[str, ...] = ()


def test_published_foundation_is_exact_pinned_at_both_install_seams() -> None:
    pyproject = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
    dependency = pyproject["tool"]["poetry"]["group"]["dev"]["dependencies"][
        "dotmac-deployment-foundation"
    ]
    assert dependency == {"version": FOUNDATION_VERSION, "source": "forgejo"}
    assert foundation_version == FOUNDATION_VERSION

    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    immutable_call = (
        "michaelayoade/dotmac_starter_mt/"
        ".github/workflows/deployment-conformance.yml@"
        f"{FOUNDATION_WORKFLOW_SHA}"
    )
    assert immutable_call in workflow
    assert f'foundation-version: "{FOUNDATION_VERSION}"' in workflow
    assert "FORGEJO_READ_TOKEN: ${{ secrets.FORGEJO_READ_TOKEN }}" in workflow
    assert FOUNDATION_RELEASE_SHA in DEPLOYMENT_README_PATH.read_text(encoding="utf-8")


def test_descriptor_parses() -> None:
    spec = _load()
    assert spec.product == "dotmac-erp"
    assert len(spec.roles) >= 1


def test_descriptor_binds_the_canonical_product_manifest() -> None:
    spec = _load()
    assert spec.manifest_path == "deploy/product-manifest.json"
    assert spec.manifest_digest == product_manifest_digest()


def test_descriptor_heads_match_the_composed_alembic_graph() -> None:
    previous_bindings = installed_bindings()
    previous_planes = installed_module_plane_selections()
    install_prerequisite_bindings(ASSEMBLY_PREREQUISITE_BINDINGS)
    install_module_plane_selections(ASSEMBLY_MODULE_PLANES)
    try:
        config = Config(str(REPO_ROOT / "alembic.ini"))
        actual = set(ScriptDirectory.from_config(config).get_heads())
    finally:
        install_module_plane_selections(previous_planes)
        install_prerequisite_bindings(previous_bindings)
    declared = set(_load().migration.expected_heads)
    assert declared == actual


def test_public_source_revision_is_projected_into_telemetry() -> None:
    """The descriptor's public identity reaches the rendered collector.

    Both halves are pinned, because they are what makes a running container
    identifiable: the source revision the image was built from, and the
    registry digest of the image itself. They are one pairing — a revision
    naming bytes that are not the deployed bytes identifies nothing — so the
    literals move together or not at all.
    """
    spec = _load()
    assert spec.source_revision == IMAGE_SOURCE_REVISION
    assert spec.image == f"{IMAGE_REPOSITORY}@{IMAGE_DIGEST}"

    rendered = RENDERED_OTEL_PATH.read_text(encoding="utf-8")
    assert f"value: {spec.source_revision}" in rendered


def test_no_role_holds_the_migration_owner_material() -> None:
    """Explicit, named second line of defence beside spec.py's own refusal.

    spec.py's `_validate_cross_field` already raises `SpecError` at LOAD time
    if any role names `migration.owner_material` — so `_load()` above would
    already have failed before this test body ran if the file regressed. This
    assertion exists so the failure is attributed to THIS specific, named
    property (rather than "the file failed to parse, for some reason") and so
    it keeps checking the property even if spec.py's own rule is ever
    loosened.
    """
    spec = _load()
    assert spec.migration.owner_material == MIGRATION_OWNER_MATERIAL
    for role in spec.roles:
        assert MIGRATION_OWNER_MATERIAL not in role.materials, (
            f"role {role.code!r} holds the migration owner material; a "
            "runtime role holding it can create, alter and drop any table "
            "for the life of the deployment (inventory D3)"
        )


def test_app_liveness_and_readiness_are_different_paths() -> None:
    """THE central fix this descriptor exists to make checkable (D2).

    ERP's real `/health` (app/main.py:914-916) is hardcoded to
    `{"status": "ok"}` and can never fail; the real dependency-aware check is
    `/health/ready` (app/main.py:969+), previously unused by either the
    Compose healthcheck or scripts/deploy.sh's health gate. Liveness and
    readiness must be two different paths, and readiness must specifically be
    `/health/ready` — not merely "different from liveness" — because a
    descriptor that pointed readiness at some OTHER always-200 path would
    satisfy "different paths" while reintroducing the exact defect.
    """
    spec = _load()
    app_role = spec.role("app")
    assert app_role.live is not None
    assert app_role.ready is not None
    assert app_role.live.path != app_role.ready.path
    assert app_role.ready.path == "/health/ready"
    assert app_role.live.path == "/health/live"


def test_image_is_pinned_by_digest_not_a_tag() -> None:
    """A digest, never a tag.

    D5 was `docker-compose.yml` floating `:latest` via `${ERP_IMAGE_TAG:-latest}`.
    That variable is retired and compose now reads `${APP_IMAGE:?...}` with no
    default, so this assertion has stopped describing an aspiration the live
    path contradicted. `test_deploy_image_gate.py` holds the deploy path to it.
    """
    spec = _load()
    assert "@sha256:" in spec.image
    digest = spec.image_digest
    assert digest.startswith("sha256:")
    hex_part = digest.removeprefix("sha256:")
    assert len(hex_part) == 64
    int(hex_part, 16)  # raises ValueError if it is not actually hex


# ─────────────────────────────────────────────────────────────────────────
# Sensitivity proof (ADR-0018's standard: a guard exemption/detector must be
# demonstrated to fail, not assumed to). Two halves, and both are required:
# the POSITIVE case proves the loader actually refuses a planted violation
# (rather than, say, silently ignoring an unknown sub-table); the NEGATIVE
# CONTROL proves the same loader accepts the real, unmodified descriptor text
# unchanged. Without the negative control, a loader that rejects EVERY
# document (a typo in the TOML parser call, an overly-broad except clause)
# would make the positive case pass for the wrong reason — it would "prove"
# refusal by refusing everything, including a perfectly valid file.
# ─────────────────────────────────────────────────────────────────────────


def _descriptor_text() -> str:
    return DESCRIPTOR_PATH.read_text(encoding="utf-8")


def test_negative_control_the_real_descriptor_text_loads_cleanly() -> None:
    """Must pass, or the positive case below proves nothing."""
    text = _descriptor_text()
    spec = ProductDeploymentSpec.loads(text, source="<negative-control>")
    assert spec.product == "dotmac-erp"


def test_a_role_given_the_owner_material_is_refused() -> None:
    """Positive case: plant the exact D3-shaped violation and prove refusal.

    Appends a deliberately broken role to the real descriptor TEXT (not a
    hand-built minimal fixture) so the sensitivity proof exercises the same
    document the negative control above just proved loads cleanly — the two
    tests differ by exactly one planted defect.
    """
    # Every OTHER required field is filled in with a trivially valid value
    # (a real `[roles.resources]` table included) so the only difference from
    # a valid role is the one planted violation — otherwise this could raise
    # for an unrelated reason (e.g. a missing required `cpus`) and "pass" the
    # assertion without actually proving the credential-separation rule bit.
    text = _descriptor_text()
    broken = text + (
        "\n"
        "[[roles]]\n"
        'code = "broken_probe"\n'
        'command = ["true"]\n'
        "replicas = 0\n"
        'materials = ["MIGRATION_DATABASE_URL"]\n'
        "\n"
        "[roles.resources]\n"
        'cpus = "1.0"\n'
        'memory = "512m"\n'
    )
    with pytest.raises(DeploymentFoundationError) as excinfo:
        ProductDeploymentSpec.loads(broken, source="<sensitivity-proof>")
    assert "MIGRATION_DATABASE_URL" in str(excinfo.value)


def test_conformance_findings_are_exactly_the_known_unresolved_ones() -> None:
    """Not `== []`, and not a subset — an exact match in both directions.

    A subset assertion passes when a finding is fixed and the list is not
    updated, so the list stops describing anything. An exact match makes the
    ratchet visible: fixing a finding is a diff that removes a line here.
    """
    from dotmac_deployment_foundation.spec import ProductDeploymentSpec

    findings = check_all(ProductDeploymentSpec.load(DESCRIPTOR_PATH))
    matched = [
        finding
        for finding in findings
        if any(known in finding for known in KNOWN_UNRESOLVED)
    ]
    unexpected = [finding for finding in findings if finding not in matched]
    assert unexpected == [], f"new conformance finding(s): {unexpected}"
    assert len(matched) == len(KNOWN_UNRESOLVED), (
        f"{len(KNOWN_UNRESOLVED)} known finding(s) declared but {len(matched)} "
        "observed — a fixed finding must be removed from KNOWN_UNRESOLVED in "
        "the same change that fixes it"
    )


# ---------------------------------------------------------------------------
# The descriptor's invocations are the deploy path's invocations, and the
# runtime image can actually run them
# ---------------------------------------------------------------------------
#
# Every command row in `[migration]` drifted at once and nothing noticed. They
# all carried a builder-stage runner prefix that `scripts/deploy.sh` stopped
# using when the runtime image became a hardened, toolchain-free stage; two of
# the three even carried comments defending the prefix as "load-bearing, not
# decorative" and citing a deploy.sh line that no longer spelled it that way.
# The descriptor is this product's Foundation-facing statement of how it
# deploys, so a stale row is a statement that would fail if anything executed
# it.
#
# Three facts have to agree, and checking any one alone is what let the drift
# survive: the argv the descriptor declares, the argv `deploy.sh` really runs,
# and whether the runtime image contains the target at all. The third is not
# hypothetical — the image's `scripts/` directory is a file-by-file `COPY`
# allowlist, so a script merely present in the repository is NOT present at
# runtime.

DOCKERFILE_PATH = REPO_ROOT / "Dockerfile"
DEPLOY_SCRIPT_PATH = REPO_ROOT / "scripts" / "deploy.sh"

# Tooling that exists only in the builder stage. A command row naming any of
# these describes a container that cannot run it.
#
# This is checked against PARSED command arrays, never against the file's raw
# text, so a comment may name the retired spelling in order to explain why it
# is retired. A substring scan over prose would forbid the documentation that
# stops the drift recurring.
BUILDER_ONLY_TOOLING = frozenset({"poetry", "--entrypoint"})

# Command rows that have a `scripts/deploy.sh` counterpart to be compared
# against, mapped to nothing else — `heads_command` is deliberately absent
# because the deploy path never reads current heads. An unmatched row is
# unmonitored, not verified, and ADR-0018 asks for that to be said out loud.
DEPLOY_MATCHED_COMMANDS = ("preflight_command", "command")
DEPLOY_UNMATCHED_COMMANDS = ("heads_command",)


def _deploy_text() -> str:
    return DEPLOY_SCRIPT_PATH.read_text(encoding="utf-8")


def _dockerfile_text() -> str:
    return DOCKERFILE_PATH.read_text(encoding="utf-8")


def _descriptor_data() -> dict:
    return tomllib.loads(_descriptor_text())


def _command_arrays(data: dict, path: str = "") -> list[tuple[str, list[str]]]:
    """Every `*command*` row in the descriptor, as (dotted path, argv)."""
    found: list[tuple[str, list[str]]] = []
    for key, value in data.items():
        here = f"{path}.{key}" if path else key
        if isinstance(value, dict):
            found.extend(_command_arrays(value, here))
        elif (
            "command" in key
            and isinstance(value, list)
            and all(isinstance(item, str) for item in value)
        ):
            found.append((here, list(value)))
    return found


def _scripts_copied_into_the_runtime_image(dockerfile: str) -> frozenset[str]:
    """Every `scripts/*.py` path the runtime stage COPYs, verbatim."""
    return frozenset(
        line.split()[1]
        for line in dockerfile.splitlines()
        if line.startswith("COPY scripts/") and len(line.split()) >= 2
    )


def _one_off_script_invocations(deploy: str) -> frozenset[str]:
    """Every `scripts/*.py` that `deploy.sh` runs in a one-off container.

    Keyed on the executed path rather than on the surrounding `docker compose
    run`, because the flags between the two drift (`-e MIGRATION_DATABASE_URL`
    is present for the executor preflight and deliberately absent for the
    runtime admission gate) while the executed path does not.
    """
    return frozenset(
        token
        for line in deploy.splitlines()
        for token in line.split()
        if token.startswith("scripts/") and token.endswith(".py")
    )


def test_the_descriptor_command_arrays_name_no_builder_only_tooling() -> None:
    """The runtime image ships no builder toolchain and needs no entrypoint
    override, so either token in a command row describes a container that does
    not exist. `scripts/deploy.sh` is held to the same rule by
    `tests/architecture/test_runtime_image_hardening.py`; this is that rule
    applied to the descriptor, which that test does not read.
    """
    rows = _command_arrays(_descriptor_data())
    assert rows, "no command rows were found, so this test asserts nothing"

    offenders = [
        f"{where} names {sorted(BUILDER_ONLY_TOOLING.intersection(argv))}"
        for where, argv in rows
        if BUILDER_ONLY_TOOLING.intersection(argv)
    ]
    assert not offenders, (
        "deploy/product.toml command rows name builder-only tooling:\n"
        + "\n".join(offenders)
        + "\n\nThe runtime image carries neither; see the Dockerfile's runtime stage."
    )


def test_matched_descriptor_commands_are_the_argv_the_deploy_path_runs() -> None:
    """A spelling only the descriptor uses is one nothing can execute."""
    migration = _descriptor_data()["migration"]
    deploy = _deploy_text()

    for key in DEPLOY_MATCHED_COMMANDS:
        argv = migration[key]
        joined = " ".join(argv)
        assert joined in deploy, (
            f"deploy/product.toml [migration].{key} declares {argv!r}, but "
            f"scripts/deploy.sh never runs {joined!r}."
        )


def test_unmatched_descriptor_commands_are_declared_unmonitored() -> None:
    """`heads_command` has no deploy-path counterpart, and that is recorded
    rather than quietly skipped — the two lists must partition the rows, so a
    new command row cannot land in neither and escape both checks.
    """
    migration = _descriptor_data()["migration"]
    declared = set(DEPLOY_MATCHED_COMMANDS) | set(DEPLOY_UNMATCHED_COMMANDS)
    actual = {key for key, _ in _command_arrays({"migration": migration})}
    actual = {key.split(".", 1)[1] for key in actual}
    assert actual == declared, (
        f"[migration] command rows {sorted(actual)} do not match the declared "
        f"partition {sorted(declared)}. A new command row must be added to "
        "DEPLOY_MATCHED_COMMANDS or explicitly to DEPLOY_UNMATCHED_COMMANDS."
    )
    for key in DEPLOY_UNMATCHED_COMMANDS:
        assert " ".join(migration[key]) not in _deploy_text(), (
            f"[migration].{key} is declared unmatched but scripts/deploy.sh "
            "does run it — move it to DEPLOY_MATCHED_COMMANDS."
        )


def test_every_script_the_deploy_path_runs_is_in_the_runtime_image() -> None:
    """A one-off container can only run a script the image actually holds.

    The runtime stage COPYs `scripts/` file by file, so adding a deploy step
    that calls a new script is two edits, not one. Missing the second produces
    a step that fails file-not-found on every deploy and passes every
    repository-only check — which is exactly what happened when the runtime
    admission gate was first written.
    """
    invoked = _one_off_script_invocations(_deploy_text())
    copied = _scripts_copied_into_the_runtime_image(_dockerfile_text())

    assert invoked, (
        "no scripts/*.py invocation was found in scripts/deploy.sh, so this "
        "test is asserting nothing. The parser and the script have diverged."
    )
    missing = sorted(invoked - copied)
    assert not missing, (
        f"scripts/deploy.sh runs {missing!r} in a one-off container, but the "
        "Dockerfile's runtime stage does not COPY them. Every one of those "
        "steps would fail file-not-found on a real deploy."
    )


# --- sensitivity proofs (ADR-0018): each detector must actually fire --------


def test_the_builder_tooling_detector_fires_on_a_planted_row() -> None:
    planted = {"migration": {"command": ["poetry", "run", "alembic", "upgrade"]}}
    rows = _command_arrays(planted)
    assert rows == [("migration.command", ["poetry", "run", "alembic", "upgrade"])]
    assert BUILDER_ONLY_TOOLING.intersection(rows[0][1]) == {"poetry"}

    override = {"roles": {"app": {"run_command": ["--entrypoint", "", "sh"]}}}
    assert BUILDER_ONLY_TOOLING.intersection(_command_arrays(override)[0][1])


def test_the_builder_tooling_detector_does_not_fire_on_the_real_descriptor() -> None:
    for _, argv in _command_arrays(_descriptor_data()):
        assert not BUILDER_ONLY_TOOLING.intersection(argv)


def test_the_image_reachability_detector_fires_on_an_uncopied_script() -> None:
    invoked = _one_off_script_invocations(
        "docker compose run --rm app python scripts/not_copied_anywhere.py\n"
    )
    copied = _scripts_copied_into_the_runtime_image(_dockerfile_text())
    assert invoked == {"scripts/not_copied_anywhere.py"}
    assert invoked - copied == {"scripts/not_copied_anywhere.py"}


def test_the_image_reachability_detector_does_not_fire_on_a_copied_script() -> None:
    copied = _scripts_copied_into_the_runtime_image(_dockerfile_text())
    assert "scripts/bootstrap_database_roles.py" in copied
    assert "scripts/verify_runtime_admission.py" in copied, (
        "the runtime admission gate's script must be image-reachable; it and "
        "the executor preflight are one contract."
    )
    invoked = _one_off_script_invocations(
        "docker compose run --rm app python scripts/bootstrap_database_roles.py\n"
    )
    assert not invoked - copied
