from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts/deploy.sh"
#: The image-mutability gate is copied in REAL, not stubbed. Stubbing it would
#: leave the deploy path's most important refusal unexercised by every test in
#: this module — the harness would prove the steps run in order while saying
#: nothing about what image they run.
IMAGE_GATE_PATH = REPO_ROOT / "scripts/resolve_deploy_image.sh"

IMAGE_REPOSITORY = "ghcr.io/michaelayoade/dotmac_erp"
#: The synthetic release's digest and the revision it was built from. They are
#: one pairing, exactly as they are in deploy/product.toml, and deploy.sh
#: compares the pulled image's OCI revision label against the descriptor.
HARNESS_DIGEST = "sha256:" + "d33c172a" * 8
HARNESS_REVISION = "9b3fb250ac9b0a8ed47cf60060d0eae737f0d4fd"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _deployment_harness(
    tmp_path: Path,
    rendered_image: str | None = None,
    env_file: str | None = None,
) -> tuple[Path, dict[str, str], Path]:
    """Build a synthetic release worktree.

    `rendered_image` is the image reference the synthetic
    `deploy/rendered/docker-compose.yml` carries. It defaults to the immutable
    digest a real render produces; a test passes a tag to prove the deploy
    path refuses one.

    `env_file` writes a synthetic `.env`. Absent by default, which models a
    host that has never been deployed by this script -- and which is also the
    real state of the first deploy after the ERP_IMAGE_TAG retirement.
    """
    project_dir = tmp_path / "release-worktree"
    scripts_dir = project_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    deploy_script = scripts_dir / "deploy.sh"
    shutil.copy2(SCRIPT_PATH, deploy_script)
    # The REAL gate, not a stub -- see IMAGE_GATE_PATH.
    shutil.copy2(IMAGE_GATE_PATH, scripts_dir / IMAGE_GATE_PATH.name)

    # deploy.sh resolves the image it deploys from the rendered Compose
    # project, and then proves the pulled image's OCI revision label against
    # the descriptor. Both files are part of the deploy path now, so the
    # harness has to supply both.
    reference = rendered_image or f"{IMAGE_REPOSITORY}@{HARNESS_DIGEST}"
    deploy_dir = project_dir / "deploy"
    (deploy_dir / "rendered").mkdir(parents=True)
    (deploy_dir / "product.toml").write_text(
        f'schema = "ProductDeploymentSpec.v1"\n'
        f'product = "dotmac-erp"\n'
        f'environment = "production"\n'
        f'source_revision = "{HARNESS_REVISION}"\n',
        encoding="utf-8",
    )
    if env_file is not None:
        (project_dir / ".env").write_text(env_file, encoding="utf-8")
        # Only reachable with a .env present: deploy.sh syncs APP_VERSION from
        # pyproject.toml into .env, and `awk` on a missing file is a non-zero
        # exit that `set -e` turns into an aborted deploy. A real release
        # worktree always has one.
        (project_dir / "pyproject.toml").write_text(
            '[tool.poetry]\nversion = "1.33.4"\n', encoding="utf-8"
        )
    (deploy_dir / "rendered" / "docker-compose.yml").write_text(
        "services:\n"
        '  redis:\n    image: "redis:7"\n'
        f'  app:\n    image: "{reference}"\n'
        f'  worker:\n    image: "{reference}"\n'
        f'  beat:\n    image: "{reference}"\n',
        encoding="utf-8",
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    invocation_log = tmp_path / "docker-invocations.log"

    _write_executable(
        fake_bin / "git",
        """#!/usr/bin/env python3
import sys

args = sys.argv[1:]
if args == ["rev-parse", "HEAD"]:
    print("abcdef0123456789abcdef0123456789abcdef01")
elif args == ["rev-parse", "--short=7", "HEAD"]:
    print("abcdef0")
""",
    )
    _write_executable(
        fake_bin / "docker",
        """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

args = sys.argv[1:]
log_path = Path(os.environ["DEPLOY_TEST_LOG"])
with log_path.open("a", encoding="utf-8") as log:
    project = os.environ.get("COMPOSE_PROJECT_NAME", "")
    rendered = " ".join(args).replace("\\n", "\\\\n")
    log.write(f"{project}|{rendered}\\n")

if args[:2] == ["inspect", "--format"]:
    if ".State.Running" in args[2]:
        print("true")
    else:
        # The running container's IMAGE ID. deploy.sh deliberately no longer
        # reads `.Config.Image` here: that is whatever reference started the
        # container (historically a mutable tag), and restoring it on rollback
        # would put production back on a tag.
        print("sha256:feedface" + "0" * 56)

if args[:2] == ["image", "inspect"]:
    fmt = ""
    for index, argument in enumerate(args):
        if argument == "--format" and index + 1 < len(args):
            fmt = args[index + 1]
    if "RepoDigests" in fmt:
        # The immutable name of the previously-running bytes -- the rollback
        # target. A test may blank it to model an image with no registry
        # digest (a local build), which must disable image rollback loudly
        # rather than silently fall back to a tag.
        previous = os.environ.get(
            "DEPLOY_TEST_PREV_REPO_DIGEST",
            "ghcr.io/michaelayoade/dotmac_erp@sha256:" + "aa" * 32,
        )
        if previous:
            print(previous)
    elif "org.opencontainers.image.revision" in fmt:
        print(os.environ.get("DEPLOY_TEST_IMAGE_REVISION", "9b3fb250ac9b0a8ed47cf60060d0eae737f0d4fd"))
    raise SystemExit(0)

if args and args[0] == "ps":
    service = ""
    for argument in args:
        prefix = "label=com.docker.compose.service="
        if argument.startswith(prefix):
            service = argument.removeprefix(prefix)
    if os.environ.get("DEPLOY_TEST_RUNNING_APP_DEV") == "1" and service == "app-dev":
        print("dotmac_erp_app_dev")
    if os.environ.get("DEPLOY_TEST_RUNNING_ONE_OFF") == "1" and service == "app":
        print("dotmac-run-app-one-off")

# The deploy makes three `compose run` calls: the executor preflight, the
# migration, and the runtime admission check. Fail only the operation named by
# each test flag.
if os.environ.get("DEPLOY_TEST_FAIL_MIGRATION") == "1":
    if args[:2] == ["compose", "run"] and "alembic" in args and "upgrade" in args:
        raise SystemExit(1)

if os.environ.get("DEPLOY_TEST_FAIL_ROLE_PREFLIGHT") == "1":
    if args[:2] == ["compose", "run"] and "--verify-only" in args:
        raise SystemExit(1)

if os.environ.get("DEPLOY_TEST_FAIL_ADMISSION") == "1":
    if args[:2] == ["compose", "run"] and any(
        "verify_runtime_admission.py" in arg for arg in args
    ):
        raise SystemExit(1)

if any("probe_people_employment_type_activation.py" in argument for argument in args):
    state = os.environ.get("DEPLOY_TEST_ACTIVATION_PROBE", "pre-activation")
    if state == "activated":
        raise SystemExit(0)
    if state == "pre-activation":
        raise SystemExit(3)
    raise SystemExit(4)

admission_failure = os.environ.get("DEPLOY_TEST_FAIL_RUNTIME_ADMISSION")
if args[:2] == ["compose", "exec"]:
    if admission_failure == "worker" and any(
        "inspect ping" in argument for argument in args
    ):
        raise SystemExit(1)
    if admission_failure == "beat" and any(
        "dotmac-erp-beat-heartbeat" in argument for argument in args
    ):
        raise SystemExit(1)
""",
    )
    _write_executable(
        fake_bin / "curl",
        """#!/usr/bin/env python3
import os
if os.environ.get("DEPLOY_TEST_FAIL_HEALTH") == "1":
    raise SystemExit(1)
raise SystemExit(0)
""",
    )
    # sync-static.sh is GONE: it copied the checkout into the nginx web root,
    # which is how a stylesheet 198 insertions behind the image reached
    # browsers. Only the image serves static now.
    _write_executable(
        scripts_dir / "prune_docker_images.sh",
        """#!/usr/bin/env bash
set -euo pipefail
""",
    )

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["DEPLOY_TEST_LOG"] = str(invocation_log)
    env["SKIP_BACKUP"] = "1"
    env["HEALTH_TIMEOUT"] = "1"
    env["RUNTIME_ADMISSION_TIMEOUT"] = "1"
    env["MIGRATION_DATABASE_URL"] = (
        "postgresql+psycopg://app_admin@database.test/dotmac_erp"
    )
    env["DEPLOY_TEST_IMAGE_REVISION"] = HARNESS_REVISION
    env.pop("COMPOSE_PROJECT_NAME", None)
    return deploy_script, env, invocation_log


def test_deploy_script_exports_stable_compose_project_name(tmp_path: Path) -> None:
    deploy_script, env, invocation_log = _deployment_harness(tmp_path)

    result = subprocess.run(  # noqa: S603
        [str(deploy_script)],
        cwd=deploy_script.parent.parent,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    invocations = invocation_log.read_text(encoding="utf-8").splitlines()
    assert any("|compose run " in line for line in invocations)
    assert any("|compose up -d app" in line for line in invocations)
    assert any("|compose up -d worker beat" in line for line in invocations)
    assert all(line.startswith("dotmac|") for line in invocations)
    one_off = [line for line in invocations if "|compose run " in line]
    runtime = [line for line in invocations if "|compose up " in line]
    assert one_off
    # The runtime admission check is the ONE one-off that must NOT carry the
    # migration credential: it verifies the connection the application serves
    # on, and re-verifying `app_admin` there would prove nothing. Every other
    # one-off is a migration-executor operation and still carries it.
    admission = [line for line in one_off if "verify_runtime_admission.py" in line]
    executor = [line for line in one_off if "verify_runtime_admission.py" not in line]
    assert len(admission) == 1, admission
    assert "-e MIGRATION_DATABASE_URL" not in admission[0]
    assert executor
    assert all("-e MIGRATION_DATABASE_URL" in line for line in executor)
    assert all("MIGRATION_DATABASE_URL" not in line for line in runtime)
    assert all("PEOPLE_EMPLOYMENT_TYPE_ACTIVATION" not in line for line in one_off)
    assert "compose: dotmac" in result.stdout


def test_the_deploy_path_refuses_a_tag_in_the_rendered_project(
    tmp_path: Path,
) -> None:
    """The property this whole change exists to establish, at deploy level.

    `tests/architecture/test_deploy_image_gate.py` proves the gate itself
    refuses a tag. This proves the DEPLOY PATH is actually wired to it — a
    correct gate nothing calls guards nothing — and that the refusal lands
    before any container is touched.

    The planted reference is `sha-abcdef0`, the exact shape the retired
    `ERP_IMAGE_TAG` path produced. A gate that only caught `:latest` would have
    admitted every image ERP really deployed.
    """
    deploy_script, env, invocation_log = _deployment_harness(
        tmp_path, rendered_image=f"{IMAGE_REPOSITORY}:sha-abcdef0"
    )

    result = subprocess.run(  # noqa: S603
        [str(deploy_script)],
        cwd=deploy_script.parent.parent,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0, (
        "a tag-shaped rendered image was DEPLOYED. Production would consume a "
        f"mutable registry pointer.\nstdout: {result.stdout}"
    )
    assert "IMAGE INTEGRITY FAILURE" in result.stderr
    assert "sha-abcdef0" in result.stderr

    # Nothing was recreated. The refusal must land before any container
    # mutation, or a "refusal" still leaves production half-moved.
    invocations = (
        invocation_log.read_text(encoding="utf-8").splitlines()
        if invocation_log.exists()
        else []
    )
    assert not any("|compose up " in line for line in invocations), invocations
    assert not any("|compose run " in line for line in invocations), invocations


def test_the_deploy_path_pins_the_digest_from_the_rendered_project(
    tmp_path: Path,
) -> None:
    """The mirror, through the identical path.

    Without this, the refusal above would still pass if deploy.sh refused
    every image — which is not a stricter deploy path, it is a broken one.
    The two tests differ by exactly one token in the rendered file.
    """
    deploy_script, env, invocation_log = _deployment_harness(tmp_path)

    result = subprocess.run(  # noqa: S603
        [str(deploy_script)],
        cwd=deploy_script.parent.parent,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert f"{IMAGE_REPOSITORY}@{HARNESS_DIGEST}" in result.stdout
    assert "Image revision verified" in result.stdout

    invocations = invocation_log.read_text(encoding="utf-8").splitlines()
    assert any("|compose up -d app" in line for line in invocations)


def test_a_revision_mismatch_between_image_and_descriptor_is_refused(
    tmp_path: Path,
) -> None:
    """A digest names bytes; the revision says which release those bytes are.

    Both halves are required. A digest-pinned deploy of an image built from
    some other revision is precisely pinned and wrong about what it pinned,
    which is worse than an obviously loose reference because it invites trust
    it has not earned.
    """
    deploy_script, env, invocation_log = _deployment_harness(tmp_path)
    env["DEPLOY_TEST_IMAGE_REVISION"] = "0" * 40

    result = subprocess.run(  # noqa: S603
        [str(deploy_script)],
        cwd=deploy_script.parent.parent,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "IMAGE INTEGRITY FAILURE" in result.stderr
    assert HARNESS_REVISION in result.stderr

    invocations = invocation_log.read_text(encoding="utf-8").splitlines()
    assert not any("|compose up " in line for line in invocations), invocations


def test_a_tag_shaped_previous_pin_is_announced_and_discarded(
    tmp_path: Path,
) -> None:
    """A rollback target that is not a digest is not a rollback target.

    Modelled through `.env`, because that is the only way a non-digest value
    can reach `PREV_IMAGE`: the RepoDigests lookup is already filtered to
    `@sha256:<64 hex>`, so a running container can only ever yield a digest or
    nothing. A hand-edited or legacy pin is the real source.

    The old path would have restored that mutable tag on failure. This one
    says so, discards it, and still deploys forward -- announced, never
    silently restored.
    """
    deploy_script, env, _ = _deployment_harness(
        tmp_path,
        env_file=f"APP_IMAGE={IMAGE_REPOSITORY}:sha-old\nAPP_VERSION=1.0.0\n",
    )
    # No running container, so the pin in .env is the only candidate.
    env["DEPLOY_TEST_PREV_REPO_DIGEST"] = ""

    result = subprocess.run(  # noqa: S603
        [str(deploy_script)],
        cwd=deploy_script.parent.parent,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "automatic IMAGE rollback is unavailable" in result.stderr

    # And the deploy re-pins .env to the immutable digest it just deployed,
    # so the next run has a real rollback target.
    written = (deploy_script.parent.parent / ".env").read_text(encoding="utf-8")
    assert f"APP_IMAGE={IMAGE_REPOSITORY}@{HARNESS_DIGEST}" in written


def test_quick_deploy_refuses_when_no_immutable_image_is_pinned(
    tmp_path: Path,
) -> None:
    """`--quick` resolves no new image, so with no previous one it has nothing.

    Refused up front rather than several irreversible steps later on
    `${APP_IMAGE:?}` -- and refused rather than defaulted, which is the whole
    difference from the `${ERP_IMAGE_TAG:-latest}` shape this replaced.
    """
    deploy_script, env, invocation_log = _deployment_harness(tmp_path)
    env["DEPLOY_TEST_PREV_REPO_DIGEST"] = ""

    result = subprocess.run(  # noqa: S603
        [str(deploy_script), "--quick"],
        cwd=deploy_script.parent.parent,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "--quick has no image to keep" in result.stderr
    assert not invocation_log.exists() or not any(
        "|compose " in line
        for line in invocation_log.read_text(encoding="utf-8").splitlines()
    )


def test_deploy_script_refuses_a_missing_migration_credential(
    tmp_path: Path,
) -> None:
    deploy_script, env, invocation_log = _deployment_harness(tmp_path)
    env.pop("MIGRATION_DATABASE_URL")

    result = subprocess.run(  # noqa: S603
        [str(deploy_script)],
        cwd=deploy_script.parent.parent,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "MIGRATION_DATABASE_URL is required" in result.stderr
    assert not invocation_log.exists()


def test_deploy_script_rejects_conflicting_compose_project_name(
    tmp_path: Path,
) -> None:
    deploy_script, env, invocation_log = _deployment_harness(tmp_path)
    env["COMPOSE_PROJECT_NAME"] = "production-release-worktree"

    result = subprocess.run(  # noqa: S603
        [str(deploy_script)],
        cwd=deploy_script.parent.parent,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "COMPOSE_PROJECT_NAME must be 'dotmac'" in result.stderr
    assert not invocation_log.exists()


def test_deploy_script_rollback_keeps_stable_compose_project_name(
    tmp_path: Path,
) -> None:
    deploy_script, env, invocation_log = _deployment_harness(tmp_path)
    env["DEPLOY_TEST_FAIL_MIGRATION"] = "1"

    result = subprocess.run(  # noqa: S603
        [str(deploy_script)],
        cwd=deploy_script.parent.parent,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    invocations = invocation_log.read_text(encoding="utf-8").splitlines()
    assert any("|compose run " in line for line in invocations)
    assert any("|compose up -d app worker beat" in line for line in invocations)
    assert all(line.startswith("dotmac|") for line in invocations)
    assert "Rolling back code" in result.stdout


def test_the_executor_preflight_runs_before_migrations(tmp_path: Path) -> None:
    deploy_script, env, invocation_log = _deployment_harness(tmp_path)

    result = subprocess.run(  # noqa: S603
        [str(deploy_script)],
        cwd=deploy_script.parent.parent,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    invocations = invocation_log.read_text(encoding="utf-8").splitlines()
    preflights = [
        index
        for index, line in enumerate(invocations)
        if "|compose run " in line and "--verify-only" in line
    ]
    migrations = [
        index
        for index, line in enumerate(invocations)
        if "|compose run " in line and "alembic" in line and "upgrade" in line
    ]
    assert preflights, "the migration-executor preflight was not recorded"
    assert migrations, "the Alembic migration invocation was not recorded"
    assert preflights[0] < migrations[0]


def test_a_failed_executor_preflight_stops_before_migrating(
    tmp_path: Path,
) -> None:
    deploy_script, env, invocation_log = _deployment_harness(tmp_path)
    env["DEPLOY_TEST_FAIL_ROLE_PREFLIGHT"] = "1"

    result = subprocess.run(  # noqa: S603
        [str(deploy_script)],
        cwd=deploy_script.parent.parent,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "DEPLOY STOPPED" in result.stderr
    assert "bootstrap_database_roles.py" in result.stderr
    invocations = invocation_log.read_text(encoding="utf-8").splitlines()
    assert not any("alembic" in line and "upgrade" in line for line in invocations)
    assert "Rolling back code" not in result.stdout


def test_the_runtime_admission_runs_after_migrations_and_before_the_app(
    tmp_path: Path,
) -> None:
    """The seam this step must occupy, asserted as an ORDER, not a presence.

    After the DDL, because it inspects grants and row-level security those
    migrations have just (re)created. Before `up -d app`, because refusing a
    runtime identity is only useful while the previous container is still
    serving requests. A step that merely EXISTS somewhere in the script would
    satisfy neither requirement.
    """
    deploy_script, env, invocation_log = _deployment_harness(tmp_path)

    result = subprocess.run(  # noqa: S603
        [str(deploy_script)],
        cwd=deploy_script.parent.parent,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    invocations = invocation_log.read_text(encoding="utf-8").splitlines()
    migrations = [
        index
        for index, line in enumerate(invocations)
        if "|compose run " in line and "alembic" in line and "upgrade" in line
    ]
    admissions = [
        index
        for index, line in enumerate(invocations)
        if "|compose run " in line and "verify_runtime_admission.py" in line
    ]
    recreations = [
        index
        for index, line in enumerate(invocations)
        if line.endswith("|compose up -d app")
    ]
    assert migrations, "the Alembic migration invocation was not recorded"
    assert admissions, "the runtime admission check was not recorded"
    assert recreations, "the app container was never recreated"
    assert migrations[0] < admissions[0] < recreations[0]


def test_a_failed_runtime_admission_stops_before_recreating_the_app(
    tmp_path: Path,
) -> None:
    """A refused runtime identity must not reach the containers.

    The rollback that follows reverts code and image only — the migrations from
    step 3b stay applied — so the assertion that matters is the negative one:
    `up -d app` never ran on the new image.
    """
    deploy_script, env, invocation_log = _deployment_harness(tmp_path)
    env["DEPLOY_TEST_FAIL_ADMISSION"] = "1"

    result = subprocess.run(  # noqa: S603
        [str(deploy_script)],
        cwd=deploy_script.parent.parent,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "DEPLOY STOPPED" in result.stderr
    assert "verify_runtime_admission.py" not in result.stderr
    assert "not admissible" in result.stderr
    # The operator is told, in the failure itself, that the DDL is still there.
    assert "NOT rolled" in result.stderr

    invocations = invocation_log.read_text(encoding="utf-8").splitlines()
    assert not any(line.endswith("|compose up -d app") for line in invocations), (
        "the app container was recreated despite an inadmissible runtime connection"
    )


def test_employment_type_activation_drains_old_writers_before_migrating(
    tmp_path: Path,
) -> None:
    deploy_script, env, invocation_log = _deployment_harness(tmp_path)

    result = subprocess.run(  # noqa: S603
        [str(deploy_script), "--people-employment-type-activation"],
        cwd=deploy_script.parent.parent,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    invocations = invocation_log.read_text(encoding="utf-8").splitlines()
    drained = next(
        index
        for index, line in enumerate(invocations)
        if "|compose stop app worker beat" in line
    )
    migrated = next(
        index
        for index, line in enumerate(invocations)
        if "|compose run " in line and "alembic" in line and "upgrade" in line
    )
    started = next(
        index for index, line in enumerate(invocations) if "|compose up -d app" in line
    )
    assert drained < migrated < started
    migration = invocations[migrated]
    assert "-e MIGRATION_DATABASE_URL" in migration
    assert "-e PEOPLE_EMPLOYMENT_TYPE_ACTIVATION=1" in migration


def test_activation_migration_failure_can_restore_the_old_image(
    tmp_path: Path,
) -> None:
    deploy_script, env, invocation_log = _deployment_harness(tmp_path)
    env["DEPLOY_TEST_FAIL_MIGRATION"] = "1"

    result = subprocess.run(  # noqa: S603
        [str(deploy_script), "--people-employment-type-activation"],
        cwd=deploy_script.parent.parent,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "Rolling back code" in result.stdout
    assert "FORWARD-FIX-ONLY" not in result.stderr
    invocations = invocation_log.read_text(encoding="utf-8").splitlines()
    assert any("|compose stop app worker beat" in line for line in invocations)
    assert any("|compose up -d app worker beat" in line for line in invocations)
    assert any(
        "probe_people_employment_type_activation.py" in line for line in invocations
    )


@pytest.mark.parametrize("probe_state", ["activated", "ambiguous"])
def test_ambiguous_or_committed_migration_failure_never_restarts_legacy_writers(
    tmp_path: Path,
    probe_state: str,
) -> None:
    deploy_script, env, invocation_log = _deployment_harness(tmp_path)
    env["DEPLOY_TEST_FAIL_MIGRATION"] = "1"
    env["DEPLOY_TEST_ACTIVATION_PROBE"] = probe_state

    result = subprocess.run(  # noqa: S603
        [str(deploy_script), "--people-employment-type-activation"],
        cwd=deploy_script.parent.parent,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "Rolling back code" not in result.stdout
    assert "FORWARD-FIX-ONLY" in result.stderr
    invocations = invocation_log.read_text(encoding="utf-8").splitlines()
    assert any(
        "probe_people_employment_type_activation.py" in line for line in invocations
    )
    assert not any("|compose up -d app worker beat" in line for line in invocations)


def test_activation_health_failure_never_restores_legacy_writers(
    tmp_path: Path,
) -> None:
    deploy_script, env, invocation_log = _deployment_harness(tmp_path)
    env["DEPLOY_TEST_FAIL_HEALTH"] = "1"

    result = subprocess.run(  # noqa: S603
        [str(deploy_script), "--people-employment-type-activation"],
        cwd=deploy_script.parent.parent,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "Rolling back code" not in result.stdout
    assert "FORWARD-FIX-ONLY" in result.stderr
    assert "previous image remains stopped" in result.stderr
    invocations = invocation_log.read_text(encoding="utf-8").splitlines()
    assert any("|compose stop app worker beat" in line for line in invocations)
    assert not any("|compose up -d app worker beat" in line for line in invocations)


# `test_activation_post_health_failure_remains_forward_fix_only` lived here. Its
# only trigger was making sync-static.sh fail, and that script is retired. The
# property it asserted -- a POST-HEALTH failure must stay forward-fix-only
# rather than roll back -- is not dropped: it is covered by
# `test_activation_runtime_admission_failure_remains_forward_fix_only` below,
# parameterised over both worker and beat admission, which fail at the same
# point in the deploy for the same reason.


def test_deploy_refuses_while_nginx_still_serves_static_from_disk(
    tmp_path: Path,
) -> None:
    """The sync that kept /var/www/dotmac/static fresh is retired.

    A filesystem-served /static/ is therefore FROZEN, and nginx sets 30-day
    immutable cache headers on it -- so the staleness would be invisible and
    long-lived. That is the exact shape of the defect being closed, so the
    deploy must refuse rather than quietly deploy into it.
    """
    deploy_script, env, invocation_log = _deployment_harness(tmp_path)
    site = tmp_path / "erp.dotmac.io"
    site.write_text(
        "server {\n    location /static/ {\n"
        "        alias /var/www/dotmac/static/;\n"
        "        expires 30d;\n    }\n}\n",
        encoding="utf-8",
    )
    env["NGINX_SITE"] = str(site)

    result = subprocess.run(  # noqa: S603
        [str(deploy_script)],
        cwd=deploy_script.parent.parent,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "still serves /static/ from the filesystem" in result.stderr
    # Refused in PREFLIGHT: nothing may have been mutated. No backup, no pull,
    # no container change -- a refusal that leaves work half-done is worse than
    # no guard, because the operator then has to reason about partial state.
    invocations = invocation_log.read_text(encoding="utf-8").splitlines()
    assert not any("compose pull" in line for line in invocations), invocations
    assert not any("compose up" in line for line in invocations), invocations
    assert not any("compose run" in line for line in invocations), invocations


def test_deploy_proceeds_when_nginx_proxies_static(tmp_path: Path) -> None:
    """The converse. A guard that fires on a correct configuration is noise and
    gets disabled by the first person it inconveniences."""
    deploy_script, env, invocation_log = _deployment_harness(tmp_path)
    site = tmp_path / "erp.dotmac.io"
    site.write_text(
        "server {\n    location /static/ {\n"
        "        proxy_pass http://dotmac_erp;\n    }\n}\n",
        encoding="utf-8",
    )
    env["NGINX_SITE"] = str(site)

    result = subprocess.run(  # noqa: S603
        [str(deploy_script)],
        cwd=deploy_script.parent.parent,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "proxied, not filesystem-served" in result.stdout


def test_activation_refuses_a_running_app_dev_before_mutation(tmp_path: Path) -> None:
    deploy_script, env, invocation_log = _deployment_harness(tmp_path)
    env["DEPLOY_TEST_RUNNING_APP_DEV"] = "1"

    result = subprocess.run(  # noqa: S603
        [str(deploy_script), "--people-employment-type-activation"],
        cwd=deploy_script.parent.parent,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "refuses while app-dev is running" in result.stderr
    invocations = invocation_log.read_text(encoding="utf-8").splitlines()
    assert not any("|compose stop" in line for line in invocations)
    assert not any("alembic" in line and "upgrade" in line for line in invocations)


def test_activation_refuses_a_running_one_off_after_the_drain(tmp_path: Path) -> None:
    deploy_script, env, invocation_log = _deployment_harness(tmp_path)
    env["DEPLOY_TEST_RUNNING_ONE_OFF"] = "1"

    result = subprocess.run(  # noqa: S603
        [str(deploy_script), "--people-employment-type-activation"],
        cwd=deploy_script.parent.parent,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "legacy-capable Compose containers remain" in result.stderr
    invocations = invocation_log.read_text(encoding="utf-8").splitlines()
    assert any("|compose stop app worker beat" in line for line in invocations)
    assert not any("alembic" in line and "upgrade" in line for line in invocations)
    assert any("|compose up -d app worker beat" in line for line in invocations)


@pytest.mark.parametrize("failed_runtime", ["worker", "beat"])
def test_activation_runtime_admission_failure_remains_forward_fix_only(
    tmp_path: Path,
    failed_runtime: str,
) -> None:
    deploy_script, env, invocation_log = _deployment_harness(tmp_path)
    env["DEPLOY_TEST_FAIL_RUNTIME_ADMISSION"] = failed_runtime

    result = subprocess.run(  # noqa: S603
        [str(deploy_script), "--people-employment-type-activation"],
        cwd=deploy_script.parent.parent,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "FORWARD-FIX-ONLY" in result.stderr
    assert "Rolling back code" not in result.stdout
    invocations = invocation_log.read_text(encoding="utf-8").splitlines()
    assert any("|compose up -d worker beat" in line for line in invocations)
    assert any(
        "inspect ping" in line and "--destination" in line for line in invocations
    )
    assert not any("|compose up -d app worker beat" in line for line in invocations)


def test_activation_refuses_quick_mode_before_any_docker_action(
    tmp_path: Path,
) -> None:
    deploy_script, env, invocation_log = _deployment_harness(tmp_path)

    result = subprocess.run(  # noqa: S603
        [
            str(deploy_script),
            "--quick",
            "--people-employment-type-activation",
        ],
        cwd=deploy_script.parent.parent,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "cannot use --quick" in result.stderr
    assert not invocation_log.exists()
