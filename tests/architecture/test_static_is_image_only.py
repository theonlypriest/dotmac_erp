"""Served assets come from the image digest and nothing else.

ERP served a stylesheet 198 insertions behind its own image for an unknown
period, missing dark-mode and accent utilities. TWO paths carried the checkout
to the browser, and closing either alone would have left the defect standing:

1. a compose bind mount of ./static over /app/static; and
2. scripts/sync-static.sh rsyncing /root/dotmac/static/ into the nginx web root,
   which nginx served AHEAD of the application, bypassing the container
   filesystem entirely.

The image had been compiling the correct stylesheet the whole time and it was
never used. Nothing detected this because the only check asserted the file
EXISTED, and an internal check of the image would have passed too -- the image
was right.

These are ratchets at ZERO. They are not "reduce over time" budgets: one host
bind mount or one copy-to-web-root is enough to reproduce the defect, so the
only defensible count is none.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "Dockerfile"
TAILWIND_CONFIG = REPO_ROOT / "tailwind.config.js"
COMPOSE = REPO_ROOT / "docker-compose.yml"
DIGEST_RECORD = REPO_ROOT / "deploy" / "static-tree-digest.json"
STATIC_ROOT = REPO_ROOT / "static"
SCRIPTS = REPO_ROOT / "scripts"
DEPLOY_DIR = REPO_ROOT / "deploy"

#: The services whose runtime identity the image digest is claimed to determine.
#: These carry the application and must hold NOTHING from the host.
RELEASE_ROLE_SERVICES = frozenset({"app", "worker", "beat"})


def _host_path_binds(
    compose_text: str, services: frozenset[str] | None = None
) -> dict[str, list[str]]:
    """Host-path bind mounts, optionally restricted to named services.

    Named volumes are fine: they are runtime state, not source. What is
    forbidden is a HOST PATH bind, because the host side is a mutable worktree.
    """
    document = yaml.safe_load(compose_text) or {}
    offenders: dict[str, list[str]] = {}
    for service, spec in (document.get("services") or {}).items():
        if services is not None and service not in services:
            continue
        for entry in (spec or {}).get("volumes") or []:
            source = (
                entry.get("source", "")
                if isinstance(entry, dict)
                else str(entry).split(":", 1)[0]
            )
            if source.startswith((".", "/", "~")):
                offenders.setdefault(service, []).append(str(entry))
    return offenders


def test_release_role_services_bind_no_host_path() -> None:
    offenders = _host_path_binds(
        COMPOSE.read_text(encoding="utf-8"), RELEASE_ROLE_SERVICES
    )
    assert not offenders, (
        f"host-path bind mount(s) on a release role: {offenders}. The host side "
        "is a mutable git worktree, so the image digest would no longer identify "
        "the runtime. Ship the content in the image instead."
    )


def test_every_service_that_binds_a_host_path_is_profile_gated() -> None:
    """The observability agents legitimately need host paths -- promtail reads
    /var/run/docker.sock and /var/lib/docker/containers, and no image can supply
    those. They are exempt from the ratchet above, but the exemption states an
    ENFORCEABLE premise rather than being a standing pass: they are gated behind
    a compose profile and are therefore not part of the deployed release roster.

    If someone removes that gate, the premise is false and this fails -- which is
    the difference between an exemption and an unmonitored region.
    """
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8")) or {}
    services = document.get("services") or {}
    ungated: dict[str, list[str]] = {}
    for service, binds in _host_path_binds(COMPOSE.read_text(encoding="utf-8")).items():
        if service in RELEASE_ROLE_SERVICES:
            continue  # covered by the hard ratchet above
        if not (services.get(service) or {}).get("profiles"):
            ungated[service] = binds
    assert not ungated, (
        f"service(s) bind host paths without being profile-gated: {ungated}. "
        "Either remove the bind or gate the service, because an ungated service "
        "with a host-path bind is part of the deployed runtime."
    )


def test_the_static_copy_subsystem_is_gone() -> None:
    assert not (SCRIPTS / "sync-static.sh").exists(), (
        "scripts/sync-static.sh is back. It copied the checkout into the nginx "
        "web root, which nginx serves ahead of the application."
    )
    assert not (DEPLOY_DIR / "systemd").exists(), (
        "deploy/systemd is back. Its timer re-ran the static copy every two "
        "minutes; its own README predicted the failure -- 'a missed sync can "
        "serve stale JS/CSS for up to 30 days' under immutable cache headers."
    )


def test_nothing_copies_a_source_tree_into_a_web_root() -> None:
    """Catch a reintroduced copy path under any name, not just the old one."""
    web_root = "/var/www/dotmac/static"
    offenders: list[str] = []
    for path in sorted(SCRIPTS.rglob("*")):
        if not path.is_file() or path.suffix not in {".sh", ".py"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if web_root not in text:
            continue
        # deploy.sh legitimately REFUSES when nginx still serves that path.
        if "still serves /static/ from the filesystem" in text:
            continue
        offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, (
        f"script(s) reference the nginx web root {web_root}: {offenders}. "
        "Static must be served from the image, never copied to a filesystem "
        "the application does not own."
    )


def test_the_recorded_digest_matches_the_static_tree() -> None:
    record = json.loads(DIGEST_RECORD.read_text(encoding="utf-8"))
    entries = []
    for path in sorted(STATIC_ROOT.rglob("*")):
        if not path.is_file() or path.name in {".DS_Store", "Thumbs.db"}:
            continue
        entries.append(
            (
                hashlib.sha256(path.read_bytes()).hexdigest(),
                path.relative_to(STATIC_ROOT).as_posix(),
            )
        )
    entries.sort(key=lambda pair: pair[1])
    manifest = "".join(f"{digest}  {name}\n" for digest, name in entries)
    actual = "sha256:" + hashlib.sha256(manifest.encode("utf-8")).hexdigest()

    assert record["tree_digest"] == actual, (
        f"deploy/static-tree-digest.json is stale.\n  recorded {record['tree_digest']}"
        f"\n  actual   {actual}\nRun: python scripts/static_tree_digest.py static"
    )
    assert record["file_count"] == len(entries), (
        f"recorded file_count {record['file_count']} != actual {len(entries)}"
    )


# ---------------------------------------------------------------------------
# Sensitivity. Both ratchets must be able to fail.
# ---------------------------------------------------------------------------


def test_planting_a_host_bind_mount_is_detected() -> None:
    planted = """
services:
  app:
    image: example
    volumes:
    - dotmac_logs:/var/log/dotmac
    - ./static:/app/static:ro
"""
    offenders = _host_path_binds(planted, RELEASE_ROLE_SERVICES)
    assert "app" in offenders, "a planted ./static bind mount was not detected"
    assert any("./static" in entry for entry in offenders["app"])


def test_a_named_volume_is_not_mistaken_for_a_bind_mount() -> None:
    """The converse: a ratchet that flags legitimate named volumes is noise."""
    accepted = """
services:
  app:
    image: example
    volumes:
    - dotmac_logs:/var/log/dotmac
"""
    assert not _host_path_binds(accepted, RELEASE_ROLE_SERVICES)


def test_planting_a_stale_digest_is_detected() -> None:
    """The digest record must be compared, not merely present."""
    record = json.loads(DIGEST_RECORD.read_text(encoding="utf-8"))
    tampered = "sha256:" + "0" * 64
    assert record["tree_digest"] != tampered
    # Recompute exactly as the real check does and confirm it separates them.
    entries = sorted(
        (
            hashlib.sha256(p.read_bytes()).hexdigest(),
            p.relative_to(STATIC_ROOT).as_posix(),
        )
        for p in STATIC_ROOT.rglob("*")
        if p.is_file() and p.name not in {".DS_Store", "Thumbs.db"}
    )
    manifest = "".join(f"{d}  {n}\n" for d, n in sorted(entries, key=lambda x: x[1]))
    actual = "sha256:" + hashlib.sha256(manifest.encode("utf-8")).hexdigest()
    assert actual != tampered, "the digest computation cannot distinguish trees"
    assert actual == record["tree_digest"]


# ---------------------------------------------------------------------------
# The stylesheet the image compiles must be built from the FULL content set.
# ---------------------------------------------------------------------------


def _tailwind_content_globs() -> list[str]:
    text = TAILWIND_CONFIG.read_text(encoding="utf-8")
    body = text.split("content:", 1)[1].split("]", 1)[0]
    return re.findall(r'"([^"]+)"', body)


def _css_builder_stage() -> str:
    text = DOCKERFILE.read_text(encoding="utf-8")
    stage = text.split("AS css-builder", 1)[1]
    # Up to the next stage boundary.
    return stage.split("\nFROM ", 1)[0]


def test_the_css_builder_receives_every_tailwind_content_path() -> None:
    """Tailwind fails SILENTLY on content it cannot see.

    `static/js` was in tailwind.config.js `content` but was never COPYed into
    the css-builder stage, so the image's app.css omitted every class used only
    from JavaScript. Production never noticed because the compose bind mount of
    ./static shadowed the image's stylesheet with the checkout's fully-built
    one -- the mount was masking the defect. Removing the mount without fixing
    the Dockerfile would have swapped one stale-stylesheet defect for another.

    There is no error to catch here: Tailwind emits a smaller file and exits 0.
    Only holding the two lists in step prevents it.
    """
    stage = _css_builder_stage()
    missing = []
    for glob in _tailwind_content_globs():
        # "./static/js/**/*.js" -> the "static/js" prefix that must be COPYed.
        parts = glob.lstrip("./").split("/")
        root_parts = [p for p in parts if "*" not in p]
        if not root_parts:
            continue
        root = "/".join(root_parts)
        if not re.search(rf"^COPY\s+{re.escape(root)}\b", stage, re.MULTILINE):
            missing.append((glob, root))
    assert not missing, (
        "tailwind.config.js scans path(s) the css-builder stage never receives: "
        f"{missing}. Tailwind will silently omit every class found only there. "
        "Add a matching COPY to the css-builder stage in the Dockerfile."
    )


def test_the_content_path_guard_detects_an_unscanned_glob() -> None:
    """Sensitivity: the guard must notice a content path with no COPY."""
    stage = "COPY templates ./templates\n"
    globs = ["./templates/**/*.html", "./static/js/**/*.js"]
    missing = [
        g
        for g in globs
        if not re.search(
            rf"^COPY\s+{re.escape('/'.join(p for p in g.lstrip('./').split('/') if '*' not in p))}\b",
            stage,
            re.MULTILINE,
        )
    ]
    assert missing == ["./static/js/**/*.js"], missing
