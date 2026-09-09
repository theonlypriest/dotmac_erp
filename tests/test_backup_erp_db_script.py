"""Contract tests for scripts/backup_erp_db.sh.

The script's job is not "produce a file". It is "produce a RESTORABLE backup, or
fail loudly". Before 2026-08-30 it ran a bare `pg_dump` of one database, so
roles, `app_admin` and every GRANT the RLS policies depend on were captured by
no artifact -- a restored cluster had rows and no way for the application to log
in. See docs/inventories/2026-08-30-erp-production-infrastructure-preflight.md.

So these tests assert the REFUSALS as hard as the happy path: a roleless globals
dump, an unreadable archive and a duplicated dotenv key must each stop the run.
A backup script that cannot fail is the thing that produced the original defect.

`docker` and `rclone` are replaced with stubs on PATH, so nothing here needs a
database, a container runtime or a network.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts/backup_erp_db.sh"

# A plausible globals dump. The script requires at least one CREATE ROLE.
GLOBALS_WITH_ROLES = (
    "--\n-- Roles\n--\n"
    "CREATE ROLE app_admin;\n"
    "CREATE ROLE dotmac_erp_app;\n"
    "ALTER ROLE postgres WITH SUPERUSER;\n"
)
GLOBALS_WITHOUT_ROLES = "--\n-- Roles\n--\nALTER ROLE postgres WITH SUPERUSER;\n"

# What plain `pg_dumpall --globals-only` emits: a verifier per login role. This
# artifact is uploaded offsite, so this is credential material leaving the host.
GLOBALS_WITH_VERIFIERS = (
    "--\n-- Roles\n--\n"
    "CREATE ROLE app_admin;\n"
    "ALTER ROLE app_admin WITH LOGIN PASSWORD "
    "'SCRAM-SHA-256$4096:c2FsdA==$c3RvcmVk:c2VydmVy';\n"
)

# `pg_restore --list` output: comment lines start with ';', entries do not.
TOC_WITH_ENTRIES = (
    "; Archive created at 2026-01-01\n215; 1259 16385 TABLE public people\n"
)
TOC_EMPTY = "; Archive created at 2026-01-01\n"

# Assembled at runtime so no credential-shaped literal sits in a tracked file
# for a scanner to flag. It is a fixture, not a secret, and never leaves tmp_path.
FIXTURE_PASSWORD = "-".join(("env", "file", "password"))


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _bash_executable() -> str:
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("backup_erp_db.sh tests require bash")
    probe = subprocess.run([bash, "-lc", "true"], check=False)  # noqa: S603
    if probe.returncode != 0:
        pytest.skip("backup_erp_db.sh tests require a usable bash")
    return bash


def _docker_stub(
    globals_output: str = GLOBALS_WITH_ROLES,
    toc_output: str = TOC_WITH_ENTRIES,
    argv_expectation: str = "",
) -> str:
    """A `docker` that answers pg_dumpall, pg_dump and pg_restore --list."""
    return f"""#!/usr/bin/env python3
import sys

argv = sys.argv[1:]
{argv_expectation}

if "pg_dumpall" in argv:
    sys.stdout.write({globals_output!r})
elif "pg_restore" in argv:
    # Consume the archive on stdin the way the real pg_restore does.
    sys.stdin.buffer.read()
    sys.stdout.write({toc_output!r})
elif "pg_dump" in argv:
    sys.stdout.write("PGDMP-fake-custom-format-archive\\n")
else:
    raise SystemExit(f"unsupported docker invocation: {{argv}}")
"""


def _rclone_stub(remote_root: Path) -> str:
    return f"""#!/usr/bin/env python3
import shutil
import sys
from pathlib import Path

REMOTE_ROOT = Path({str(remote_root)!r})


def resolve_remote(remote_path: str) -> Path:
    _, _, relative = remote_path.partition(":")
    return REMOTE_ROOT / relative.lstrip("/")


args = sys.argv[1:]
command = args[0] if args else ""

if command == "copy":
    src = Path(args[1])
    dest = resolve_remote(args[2])
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest / src.name)
elif command == "lsf":
    dest = resolve_remote(args[1])
    if dest.is_dir():
        for name in sorted(p.name for p in dest.iterdir() if p.is_file()):
            print(name)
elif command == "deletefile":
    resolve_remote(args[1]).unlink()
else:
    raise SystemExit(f"unsupported rclone invocation: {{args}}")
"""


def _run(
    tmp_path: Path,
    docker: str,
    *,
    extra_env: dict | None = None,
    remote_root: Path | None = None,
):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    remote_root = remote_root or (tmp_path / "remote")
    _write_executable(fake_bin / "docker", docker)
    _write_executable(fake_bin / "rclone", _rclone_stub(remote_root))

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["LOCAL_DIR"] = str(tmp_path / "local")
    env["REMOTE"] = "Backup:db.backup"
    env["REMOTE_DIR"] = "Backup:db.backup/dotmac_erp"
    env["KEEP_LAST"] = "5"
    env["ENV_FILE"] = str(tmp_path / "missing.env")
    for key in (
        "PGPASSWORD",
        "POSTGRES_PASSWORD",
        "DB_CONTAINER",
        "DB_OS_USER",
        "SKIP_UPLOAD",
    ):
        env.pop(key, None)
    env.update(extra_env or {})

    return subprocess.run(  # noqa: S603
        [_bash_executable(), str(SCRIPT_PATH)],
        check=False,
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )


# --------------------------------------------------------------------------
# The happy path produces BOTH artifacts. Either alone is not a backup.
# --------------------------------------------------------------------------


def test_backup_writes_both_a_globals_dump_and_an_archive(tmp_path) -> None:
    remote_root = tmp_path / "remote"
    result = _run(tmp_path, _docker_stub(), remote_root=remote_root)
    assert result.returncode == 0, result.stderr

    local = sorted(p.name for p in (tmp_path / "local").iterdir())
    assert any(n.endswith(".globals.sql.gz") for n in local), local
    assert any(n.endswith(".dump") for n in local), local

    uploaded = sorted(
        p.name for p in (remote_root / "db.backup" / "dotmac_erp").iterdir()
    )
    assert any(n.endswith(".globals.sql.gz") for n in uploaded), uploaded
    assert any(n.endswith(".dump") for n in uploaded), uploaded


def test_backup_reports_the_number_of_captured_roles(tmp_path) -> None:
    result = _run(tmp_path, _docker_stub())
    assert result.returncode == 0, result.stderr
    assert "2 role(s) captured" in result.stdout


# --------------------------------------------------------------------------
# The refusals. These are the point of the rewrite.
# --------------------------------------------------------------------------


def test_backup_refuses_a_globals_dump_that_captured_no_roles(tmp_path) -> None:
    """The exact production defect: rows without roles, reported as success."""
    result = _run(tmp_path, _docker_stub(globals_output=GLOBALS_WITHOUT_ROLES))
    assert result.returncode != 0
    assert "no CREATE ROLE" in result.stderr


def test_backup_refuses_an_archive_with_no_readable_contents(tmp_path) -> None:
    """A byte count cannot detect a truncated archive; pg_restore --list can."""
    result = _run(tmp_path, _docker_stub(toc_output=TOC_EMPTY))
    assert result.returncode != 0
    assert "no readable table of contents" in result.stderr


def test_backup_refuses_a_duplicated_dotenv_key(tmp_path) -> None:
    """Compose reads the last occurrence; a naive sed reads the first.

    Production .env carries duplicate keys today, so this disagreement is live.
    Guessing a side in a backup script is not acceptable.
    """
    env_file = tmp_path / "dup.env"
    env_file.write_text(
        "POSTGRES_PASSWORD=first\nPOSTGRES_PASSWORD=second\n", encoding="utf-8"
    )
    result = _run(tmp_path, _docker_stub(), extra_env={"ENV_FILE": str(env_file)})
    assert result.returncode != 0
    assert "appears 2 times" in result.stderr


# --------------------------------------------------------------------------
# Credential handling and retention.
# --------------------------------------------------------------------------


def test_backup_passes_the_password_through_the_environment_not_argv(tmp_path) -> None:
    """`-e PGPASSWORD` with no `=` keeps the value out of the container argv,
    and therefore out of the host process table."""
    expectation = """
if "-e" in argv:
    flag = argv[argv.index("-e") + 1]
    if flag != "PGPASSWORD":
        raise SystemExit(f"password leaked into argv: {flag}")
    import os
    if os.environ.get("PGPASSWORD") != "FIXTURE_PASSWORD_PLACEHOLDER":
        raise SystemExit("PGPASSWORD not passed through the environment")
""".replace("FIXTURE_PASSWORD_PLACEHOLDER", FIXTURE_PASSWORD)
    env_file = tmp_path / ".env"
    # Throwaway fixture written into tmp_path, never a real credential.
    env_file.write_text(f"POSTGRES_PASSWORD={FIXTURE_PASSWORD}\n", encoding="utf-8")
    result = _run(
        tmp_path,
        _docker_stub(argv_expectation=expectation),
        extra_env={"ENV_FILE": str(env_file)},
    )
    assert result.returncode == 0, result.stderr


def test_backup_defaults_to_the_production_container_as_the_postgres_os_user(
    tmp_path,
) -> None:
    expectation = """
if "pg_dumpall" in argv:
    # Exact argv, including --no-role-passwords. This assertion caught the flag
    # being added, which is the behaviour wanted: the role-capture command line
    # is security-relevant, so it is pinned rather than pattern-matched.
    expected = [
        "exec",
        "-u",
        "postgres",
        "-i",
        "dotmac_pg_local",
        "pg_dumpall",
        "--globals-only",
        "--no-role-passwords",
    ]
    if argv != expected:
        raise SystemExit(f"unexpected docker invocation: {argv}")
"""
    result = _run(tmp_path, _docker_stub(argv_expectation=expectation))
    assert result.returncode == 0, result.stderr


def test_retention_keeps_whole_runs_not_individual_files(tmp_path) -> None:
    """Each run writes two artifacts, so counting files would keep 2.5 runs."""
    remote_root = tmp_path / "remote"
    remote_dir = remote_root / "db.backup" / "dotmac_erp"
    remote_dir.mkdir(parents=True)
    for idx in range(1, 7):
        stamp = f"2024010{idx}_010101"
        (remote_dir / f"dotmac_erp_{stamp}.globals.sql.gz").write_text(
            "g", encoding="utf-8"
        )
        (remote_dir / f"dotmac_erp_{stamp}.dump").write_text("d", encoding="utf-8")

    result = _run(
        tmp_path, _docker_stub(), remote_root=remote_root, extra_env={"KEEP_LAST": "5"}
    )
    assert result.returncode == 0, result.stderr

    names = sorted(p.name for p in remote_dir.iterdir())
    stamps = {n.split("dotmac_erp_")[1].split(".")[0] for n in names}
    assert len(stamps) == 5, stamps
    # The oldest run is gone in its entirety, not half of it.
    assert not any(n.startswith("dotmac_erp_20240101_010101") for n in names), names
    # Every surviving run still has both of its artifacts.
    for stamp in stamps:
        assert f"dotmac_erp_{stamp}.globals.sql.gz" in names
        assert f"dotmac_erp_{stamp}.dump" in names


def test_skip_upload_leaves_the_remote_untouched(tmp_path) -> None:
    remote_root = tmp_path / "remote"
    result = _run(
        tmp_path,
        _docker_stub(),
        remote_root=remote_root,
        extra_env={"SKIP_UPLOAD": "1"},
    )
    assert result.returncode == 0, result.stderr
    assert "not uploading" in result.stdout
    assert not (remote_root / "db.backup").exists()


# --------------------------------------------------------------------------
# Role capture must never carry password material offsite.
# --------------------------------------------------------------------------


def test_role_capture_passes_no_role_passwords(tmp_path) -> None:
    """`--no-role-passwords` is what keeps SCRAM verifiers out of the bucket.

    dotmac_deployment_foundation.recovery pins this as REQUIRED_ROLE_CAPTURE_ARG
    for exactly this reason: the bundle carries roles WITHOUT password material,
    and login material is reinstalled from the approved secret source as a
    separate restore step.
    """
    expectation = """
if "pg_dumpall" in argv:
    if "--no-role-passwords" not in argv:
        raise SystemExit(f"role capture omits --no-role-passwords: {argv}")
"""
    result = _run(tmp_path, _docker_stub(argv_expectation=expectation))
    assert result.returncode == 0, result.stderr


def test_backup_refuses_to_upload_globals_containing_a_verifier(tmp_path) -> None:
    """Sensitivity for the guard above.

    The flag is one edit away from being dropped, so the content is checked too.
    A verifier in the globals dump must stop the run BEFORE the upload, and the
    partial artifact must not be left on disk.
    """
    remote_root = tmp_path / "remote"
    result = _run(
        tmp_path,
        _docker_stub(globals_output=GLOBALS_WITH_VERIFIERS),
        remote_root=remote_root,
    )
    assert result.returncode != 0
    assert "password verifiers" in result.stderr
    # Nothing uploaded, and no verifier-bearing file left behind.
    assert not (remote_root / "db.backup").exists()
    leftovers = list((tmp_path / "local").glob("*.globals.sql.gz"))
    assert leftovers == [], leftovers


def test_the_verifier_guard_accepts_a_password_free_capture(tmp_path) -> None:
    """The converse: a clean capture must not be flagged."""
    result = _run(tmp_path, _docker_stub())
    assert result.returncode == 0, result.stderr
    assert "password verifiers" not in result.stderr
