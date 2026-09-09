"""A production deploy cannot consume a mutable image tag.

ERP's publish lane has been correct for a while: `ci.yml` tags and pushes the
exact tested bytes, re-derives the OCI digest from `imagetools inspect --raw |
sha256sum` rather than trusting buildx's display, and persists
`image-release.json` binding digest to source SHA to manifest digest. The
descriptor records that digest, and `deploy/rendered/docker-compose.yml` is
rendered from the descriptor.

The deploy path did not consume any of it. `scripts/deploy.sh` drove the root
`docker-compose.yml`, which read
`ghcr.io/michaelayoade/dotmac_erp:${ERP_IMAGE_TAG:-latest}`, and pinned that
variable to `sha-$(git rev-parse --short=7 HEAD)`. So production ran a TAG
while the pipeline verified a DIGEST — and a `sha-<short>` tag is only
reproducible-looking: it is a registry pointer that can be repushed after
verification, and nothing bound it to the tested bytes.

`scripts/resolve_deploy_image.sh` is the gate that closes it, and this module
drives that gate in BOTH directions. A guard that only ever refuses proves
nothing about what it admits: a script that exited non-zero unconditionally —
a typo, an over-broad `set -e`, a missing file — would pass every refusal
assertion here while making every deploy impossible. So each planted refusal
is paired with the mirror case that must be ADMITTED through the identical
code path, and the two inputs differ by exactly the property under test.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "scripts" / "resolve_deploy_image.sh"
DEPLOY_SCRIPT = REPO_ROOT / "scripts" / "deploy.sh"
ROOT_COMPOSE = REPO_ROOT / "docker-compose.yml"
RENDERED_COMPOSE = REPO_ROOT / "deploy" / "rendered" / "docker-compose.yml"
DESCRIPTOR = REPO_ROOT / "deploy" / "product.toml"
ENV_EXAMPLE = REPO_ROOT / ".env.example"

IMAGE_REPOSITORY = "ghcr.io/michaelayoade/dotmac_erp"

#: A syntactically perfect digest that is NOT the descriptor's. Used as the
#: admitted half of each mirrored pair, so acceptance cannot be an artefact of
#: the real value being special-cased somewhere.
OTHER_DIGEST = "sha256:" + "ab12cd34" * 8

#: The exact shape the old path produced: `sha-` plus a 7-character short SHA.
#: Not `latest` — a gate that refuses only the obviously-floating spelling
#: would have admitted every tag ERP actually deployed for years.
PLANTED_TAG = "sha-9b3fb25"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    # Invoked through its own shebang rather than as `bash <path>`, because
    # that is how scripts/deploy.sh invokes it — testing a different
    # invocation than the deploy path uses would leave the real one unproven.
    # The executable bit this relies on is itself asserted below.
    return subprocess.run(  # noqa: S603
        [str(GATE), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )


def test_the_gate_is_executable() -> None:
    """`_run` invokes the script directly, so the mode bit is load-bearing.

    A lost executable bit would turn every refusal assertion in this module
    into a permission error that still exits non-zero — the refusals would all
    "pass" while the gate never ran. Asserted, not assumed.
    """
    assert GATE.exists(), f"{GATE} is missing"
    assert GATE.stat().st_mode & 0o111, (
        f"{GATE} is not executable; scripts/deploy.sh invokes it directly"
    )


def _compose_with_image(tmp_path: Path, reference: str, name: str) -> Path:
    """The real rendered project with only its image reference substituted.

    Built from the committed file rather than from a hand-written minimal
    fixture, so the planted case exercises the same document shape the deploy
    path really reads — quoting, indentation, the tag-named `redis:7`
    dependency and all.
    """
    text = RENDERED_COMPOSE.read_text(encoding="utf-8")
    planted = re.sub(
        rf'^(\s*image:\s*)"{re.escape(IMAGE_REPOSITORY)}@sha256:[0-9a-f]{{64}}"$',
        rf'\g<1>"{reference}"',
        text,
        flags=re.MULTILINE,
    )
    assert planted != text, "substitution matched nothing; the fixture is inert"
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(planted, encoding="utf-8")
    return path


# ── the mirrored pair: a rendered project ──────────────────────────────────


def test_a_planted_tag_in_the_rendered_project_is_refused(tmp_path: Path) -> None:
    """The direction that matters: a regression to a tag stops the deploy."""
    planted = _compose_with_image(
        tmp_path, f"{IMAGE_REPOSITORY}:{PLANTED_TAG}", "tagged/docker-compose.yml"
    )

    result = _run("--compose", str(planted))

    assert result.returncode != 0, (
        "a tag-shaped image reference was ADMITTED. Production would consume a "
        "mutable registry pointer, which is the exact defect this gate exists "
        f"to make impossible.\nstdout: {result.stdout!r}"
    )
    assert "IMAGE INTEGRITY FAILURE" in result.stderr
    assert PLANTED_TAG in result.stderr, (
        "the refusal must name the offending reference; a generic failure "
        "cannot be distinguished from the script being broken"
    )
    assert result.stdout.strip() == "", (
        "a refusal must print no reference at all — deploy.sh captures stdout, "
        "so anything emitted here would be exported as APP_IMAGE"
    )


def test_a_digest_in_the_rendered_project_is_admitted(tmp_path: Path) -> None:
    """The mirror. Identical file, identical invocation, one token different.

    Without this, every assertion above would still pass if the gate refused
    unconditionally — and an unconditionally-refusing gate is not a stricter
    gate, it is a broken deploy path that someone would soon switch off.
    """
    planted = _compose_with_image(
        tmp_path, f"{IMAGE_REPOSITORY}@{OTHER_DIGEST}", "digest/docker-compose.yml"
    )

    result = _run("--compose", str(planted))

    assert result.returncode == 0, (
        "a correctly digest-pinned project was REFUSED, so the gate refuses "
        f"everything and proves nothing.\nstderr: {result.stderr}"
    )
    assert result.stdout.strip() == f"{IMAGE_REPOSITORY}@{OTHER_DIGEST}"


# ── the mirrored pair: an explicit operator selector ────────────────────────


@pytest.mark.parametrize(
    "selector",
    [
        PLANTED_TAG,
        "latest",
        f"{IMAGE_REPOSITORY}:{PLANTED_TAG}",
        # Right shape, wrong length — 63 hex characters. A gate anchored on
        # "starts with sha256:" rather than on the full pattern admits this.
        "sha256:" + "a" * 63,
        # Uppercase hex: not a digest a registry will ever return, and a
        # case-insensitive comparison somewhere would let it through.
        "sha256:" + "AB12CD34" * 8,
        "",
    ],
)
def test_a_mutable_or_malformed_selector_is_refused(selector: str) -> None:
    result = _run("--reference", selector)
    assert result.returncode != 0, f"selector {selector!r} was admitted"
    assert result.stdout.strip() == ""


@pytest.mark.parametrize(
    "selector",
    [
        OTHER_DIGEST,
        f"{IMAGE_REPOSITORY}@{OTHER_DIGEST}",
    ],
)
def test_a_digest_selector_is_admitted_in_either_spelling(selector: str) -> None:
    """The mirror for the operator-facing path.

    A bare `sha256:...` is the spelling `deploy.sh sha256:<64 hex>` accepts and
    the one dotmac_sub uses; the fully-qualified form is what a rendered file
    carries. Both must resolve to the same immutable reference, or the two
    entry points are two gates.
    """
    result = _run("--reference", selector)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{IMAGE_REPOSITORY}@{OTHER_DIGEST}"


# ── negative controls on the real, unmodified files ─────────────────────────


def test_the_real_rendered_project_is_admitted_and_is_the_descriptor_image() -> None:
    """The committed file must pass, or every planted case above is vacuous.

    It also has to be the SAME image the descriptor declares. The rendered
    project is the deploy path's authority for which image runs, so a rendered
    file that drifted from the descriptor would deploy an artifact no
    conformance check ever looked at.
    """
    result = _run("--compose", str(RENDERED_COMPOSE))
    assert result.returncode == 0, result.stderr

    declared = tomllib.loads(DESCRIPTOR.read_text(encoding="utf-8"))["image"][
        "reference"
    ]
    assert result.stdout.strip() == declared


def test_the_root_compose_is_refused_as_an_image_authority() -> None:
    """The root compose is the RUNTIME topology, never the image source.

    It resolves `${APP_IMAGE}` at deploy time, so its `image:` lines are a
    variable rather than a reference. Feeding it to the gate must refuse
    rather than silently resolve to something — this is what keeps the
    authority in the rendered, descriptor-derived file.
    """
    result = _run("--compose", str(ROOT_COMPOSE))
    assert result.returncode != 0
    assert result.stdout.strip() == ""


def test_a_project_naming_no_product_image_is_refused(tmp_path: Path) -> None:
    """Absence must fail loudly, not resolve to the empty string.

    If it returned empty and exited zero, a renamed repository or a reshaped
    rendered file would produce `APP_IMAGE=""` and this gate would be
    asserting nothing at all while appearing to pass.
    """
    only_dependency = tmp_path / "docker-compose.yml"
    only_dependency.write_text(
        'services:\n  redis:\n    image: "redis:7"\n', encoding="utf-8"
    )
    result = _run("--compose", str(only_dependency))
    assert result.returncode != 0
    assert result.stdout.strip() == ""


def test_two_different_digests_in_one_project_are_refused(tmp_path: Path) -> None:
    """app, worker and beat must be one release.

    The retired `${ERP_IMAGE_TAG}` made this structurally impossible by sharing
    one variable across the roles. Naming the digest per service gives the
    failure mode back, so it is checked rather than assumed.
    """
    text = RENDERED_COMPOSE.read_text(encoding="utf-8")
    pattern = re.compile(
        rf'^(\s*image:\s*")({re.escape(IMAGE_REPOSITORY)}@sha256:[0-9a-f]{{64}})(")$',
        flags=re.MULTILINE,
    )
    occurrences = list(pattern.finditer(text))
    assert len(occurrences) >= 2, (
        "the rendered project names the product image fewer than twice, so a "
        "disagreement between roles cannot be represented at all"
    )

    # Repoint exactly the LAST role, by character offset, leaving every other
    # role on the real digest. Offsets rather than `str.replace` counts: the
    # digests are long, similar strings, and a replace-based fixture that
    # silently matched nothing would make this test pass by refusing a file
    # that was never actually mixed.
    final = occurrences[-1]
    split = (
        text[: final.start(2)]
        + f"{IMAGE_REPOSITORY}@{OTHER_DIGEST}"
        + text[final.end(2) :]
    )

    mixed = tmp_path / "mixed" / "docker-compose.yml"
    mixed.parent.mkdir(parents=True, exist_ok=True)
    mixed.write_text(split, encoding="utf-8")

    references = {match.group(2) for match in pattern.finditer(split)}
    assert len(references) == 2, (
        "the fixture did not actually produce two distinct digests, so this "
        f"test would pass for the wrong reason: {references}"
    )

    result = _run("--compose", str(mixed))
    assert result.returncode != 0
    assert result.stdout.strip() == ""


# ── the gate is wired into the path it is supposed to guard ────────────────


def test_the_deploy_path_resolves_its_image_through_the_gate() -> None:
    """A gate nothing calls guards nothing."""
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert "resolve_deploy_image.sh" in deploy
    assert '--compose "$PROJECT_DIR/$RENDERED_COMPOSE"' in deploy
    assert (
        'RENDERED_COMPOSE="${RENDERED_COMPOSE:-deploy/rendered/docker-compose.yml}"'
        in deploy
    )
    assert 'export APP_IMAGE="$NEW_IMAGE"' in deploy


#: A live use of the retired variable: an interpolation, or an assignment.
#: NOT a bare substring — the comments and operator messages that stop this
#: recurring necessarily name it, and banning the token would forbid the
#: explanation instead of the behaviour. This distinction is the whole point,
#: so it is enforced by the sensitivity proof below rather than trusted.
TAG_VARIABLE_REFERENCE = re.compile(r"\$\{?ERP_IMAGE_TAG\b")
TAG_VARIABLE_ASSIGNMENT = re.compile(
    r"^\s*(?:export\s+)?ERP_IMAGE_TAG\s*=", re.MULTILINE
)


def _strip_comment_lines(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def test_the_retired_tag_variable_is_gone_from_every_executable_surface() -> None:
    """`ERP_IMAGE_TAG` may survive only as prose explaining its retirement."""
    for path in (ROOT_COMPOSE, DEPLOY_SCRIPT, RENDERED_COMPOSE, ENV_EXAMPLE):
        executable = _strip_comment_lines(path.read_text(encoding="utf-8"))
        assert not TAG_VARIABLE_REFERENCE.search(executable), (
            f"{path.name} still interpolates ${{ERP_IMAGE_TAG}}"
        )
        assert not TAG_VARIABLE_ASSIGNMENT.search(executable), (
            f"{path.name} still assigns ERP_IMAGE_TAG"
        )


def test_the_retired_tag_detector_fires_on_a_planted_use() -> None:
    """Sensitivity proof: the narrowed detector must still bite.

    Narrowing a guard from a substring to a pattern is exactly where a guard
    quietly stops guarding, so both halves are proven: every real reintroduction
    shape is caught, and the prose that documents the retirement is not.
    """
    for planted in (
        "    image: ghcr.io/michaelayoade/dotmac_erp:${ERP_IMAGE_TAG:-latest}",
        '    echo "$ERP_IMAGE_TAG"',
        "    export ERP_IMAGE_TAG=$NEW_IMAGE_TAG",
    ):
        assert TAG_VARIABLE_REFERENCE.search(planted) or (
            TAG_VARIABLE_ASSIGNMENT.search(planted)
        ), planted

    for assignment in ("ERP_IMAGE_TAG=latest\n", 'export ERP_IMAGE_TAG="x"\n'):
        assert TAG_VARIABLE_ASSIGNMENT.search(assignment), assignment

    # ... and stays silent on the prose that explains the retirement, which is
    # what deploy.sh's one-time operator NOTE actually contains.
    for prose in (
        '    echo "      the first deploy after the ERP_IMAGE_TAG retirement."',
        "ERP_IMAGE_TAG is retired: compose now reads ${APP_IMAGE:?...}",
    ):
        assert not TAG_VARIABLE_REFERENCE.search(prose), prose
        assert not TAG_VARIABLE_ASSIGNMENT.search(prose), prose


def test_the_root_compose_declares_app_image_with_no_default() -> None:
    """`:?` not `:-`. The default WAS the defect.

    `${ERP_IMAGE_TAG:-latest}` meant an absent key floated production onto a
    mutable tag; the whole point of the replacement is that absence refuses.
    """
    compose = ROOT_COMPOSE.read_text(encoding="utf-8")
    image_lines = [
        line.strip()
        for line in compose.splitlines()
        if line.strip().startswith("image:")
    ]
    product_lines = [line for line in image_lines if "APP_IMAGE" in line]

    assert len(product_lines) == 3, (
        f"expected app, worker and beat to read APP_IMAGE; found {product_lines}"
    )
    for line in product_lines:
        assert "${APP_IMAGE:?" in line, (
            f"{line!r} must have NO default: a default is what let a missing "
            "pin resolve to something mutable"
        )
        assert "${APP_IMAGE:-" not in line
