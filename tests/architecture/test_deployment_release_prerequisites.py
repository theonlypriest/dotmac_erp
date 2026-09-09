"""Deployment evidence binds an immutable image to ERP's real module set."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from dotmac_kernel.assembly import ProductAssemblySpec

from app.migration_bindings import COMPOSED_MODULE_LINEAGES
from app.product_assembly import (
    COMPOSED_MODULE_DISTRIBUTIONS,
    ERP_PRODUCT_ASSEMBLY,
)
from scripts.product_manifest import (
    ProductManifestError,
    canonical_product_manifest_bytes,
    check_product_manifest,
    product_manifest_digest,
)
from scripts.write_image_release import (
    ImageReleaseError,
    canonical_image_release_bytes,
)

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "deploy" / "product-manifest.json"
EXPECTED_MANIFEST_DIGEST = (
    "sha256:9c3547745e453ffbd9339ce0d662af64a5071067087f16008f4630aae8b469b9"
)


def test_product_assembly_is_the_exact_composed_module_set() -> None:
    assert isinstance(ERP_PRODUCT_ASSEMBLY, ProductAssemblySpec)
    assert ERP_PRODUCT_ASSEMBLY.name == "dotmac-erp"
    codes = tuple(manifest.code for manifest in ERP_PRODUCT_ASSEMBLY.modules)
    assert codes == tuple(sorted(COMPOSED_MODULE_LINEAGES))
    assert set(COMPOSED_MODULE_DISTRIBUTIONS) == set(codes)


def test_manifest_versions_are_the_exact_distribution_pins() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["tool"]["poetry"]["dependencies"]
    for manifest in ERP_PRODUCT_ASSEMBLY.modules:
        distribution = COMPOSED_MODULE_DISTRIBUTIONS[manifest.code]
        assert dependencies[distribution] == {
            "version": manifest.version,
            "source": "forgejo",
        }


def test_committed_product_manifest_is_canonical_and_digest_bound() -> None:
    assert MANIFEST_PATH.read_bytes() == canonical_product_manifest_bytes()
    assert check_product_manifest(MANIFEST_PATH) == EXPECTED_MANIFEST_DIGEST
    assert product_manifest_digest() == EXPECTED_MANIFEST_DIGEST

    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert payload["product"] == "dotmac-erp"
    assert [row["code"] for row in payload["modules"]] == sorted(
        COMPOSED_MODULE_LINEAGES
    )
    numbering = next(row for row in payload["modules"] if row["code"] == "numbering")
    assert numbering["declared_planes"] == ["platform", "tenant"]
    assert numbering["explicit_planes"] == ["tenant"]
    assert numbering["effective_planes"] == ["tenant"]
    people = next(row for row in payload["modules"] if row["code"] == "people")
    assert people["distribution"] == "dotmac-people"
    assert people["version"] == "0.1.0a2"
    assert people["declared_planes"] == ["tenant"]
    assert people["explicit_planes"] is None
    assert people["effective_planes"] == ["tenant"]


def test_manifest_checker_refuses_noncanonical_bytes(tmp_path: Path) -> None:
    changed = tmp_path / "product-manifest.json"
    changed.write_bytes(
        canonical_product_manifest_bytes().replace(b"dotmac-erp", b"other-erp")
    )
    with pytest.raises(ProductManifestError, match="not the canonical"):
        check_product_manifest(changed)


def test_manifest_cli_is_root_aware_in_package_mode_false_project() -> None:
    # Every executable and argument is fixed by this test.
    completed = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "scripts.product_manifest",
            "check",
            "--path",
            str(MANIFEST_PATH),
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == f"{EXPECTED_MANIFEST_DIGEST}\n"


def test_release_manifest_does_not_use_the_identity_losing_snapshot() -> None:
    source = (ROOT / "scripts" / "product_manifest.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert not any(
        isinstance(node, ast.Name) and node.id == "ProductManifestSnapshot"
        for node in ast.walk(tree)
    )
    assert not any(
        isinstance(node, ast.Attribute) and node.attr == "ProductManifestSnapshot"
        for node in ast.walk(tree)
    )


def test_docker_context_excludes_the_release_control_plane() -> None:
    ignored = {
        line.strip().rstrip("/")
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert "deploy" in ignored


def test_publish_job_waits_for_every_ci_gate_and_uploads_exact_evidence() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    publish_job = workflow.split("  publish-image:\n", 1)[1]
    assert {
        "lint",
        "type-check",
        "test",
        "security",
        "pre-commit",
        "secret-scan",
        "release-engineering-standards",
        "docker-build",
        "integration-test",
    } == set(re.findall(r"^      - ([a-z][a-z-]+)$", publish_job, re.MULTILINE))
    assert "id: registry" in publish_job
    assert "steps.registry.outputs.digest" in publish_job
    assert '--git-sha "${GITHUB_SHA}"' in publish_job
    assert '--manifest-digest "${{ steps.product-manifest.outputs.digest }}"' in (
        publish_job
    )
    assert "path: image-release.json" in publish_job
    assert "actions/upload-artifact@v4" in publish_job


def test_publish_consumes_the_tested_image_and_cannot_rebuild_it() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    docker_job = workflow.split("  docker-build:\n", 1)[1].split(
        "\n  integration-test:\n", 1
    )[0]
    publish_job = workflow.split("  publish-image:\n", 1)[1]

    main_push = (
        "if: success() && github.ref == 'refs/heads/main' "
        "&& github.event_name == 'push'"
    )
    assert docker_job.count(main_push) == 2
    assert workflow.count("docker/build-push-action") == 1
    assert "${{ steps.tested-meta.outputs.labels }}" in docker_job
    assert docker_job.count("io.dotmac.product-manifest.digest") == 2
    assert "steps.product-manifest.outputs.digest" in docker_job
    assert "docker image save dotmac-erp:ci" in docker_job
    assert "name: tested-image-${{ github.sha }}" in docker_job
    assert "actions/upload-artifact@v4" in docker_job
    assert (
        docker_job.index("- name: Wait for health check")
        < docker_job.index("- name: Export the tested image")
        < docker_job.index("- name: Upload the tested image for publication")
    )

    assert "docker/build-push-action" not in publish_job
    assert "actions/download-artifact@v4" in publish_job
    assert "name: tested-image-${{ github.sha }}" in publish_job
    assert "docker image load" in publish_job
    assert publish_job.count("io.dotmac.product-manifest.digest") == 1
    assert "deploy/product-manifest.json" in publish_job
    assert "docker image tag dotmac-erp:ci" in publish_job
    assert "docker image push" in publish_job
    assert "type=sha,prefix=sha-,format=short" in publish_job
    assert "type=sha,prefix=sha-,format=long" in publish_job
    assert "docker buildx imagetools inspect --raw" in publish_job
    assert "sha256sum" in publish_job


def test_release_standards_gate_uses_the_profile_pinned_revision() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    release_gate = workflow.split("  release-engineering-standards:\n", 1)[1]
    release_gate = release_gate.split("\n  docker-build:\n", 1)[0]
    authoritative = (
        ROOT / ".github" / "workflows" / "engineering-standards.yml"
    ).read_text(encoding="utf-8")
    profile = json.loads(
        (ROOT / ".dotmac" / "standards-profile.json").read_text(encoding="utf-8")
    )
    revision = profile["governance_model"]["revision"]
    action = "michaelayoade/dotmac_governance/.github/actions/standards-check@"
    for source in (release_gate, authoritative):
        assert re.findall(rf"{re.escape(action)}([0-9a-f]{{40}})", source) == [revision]


def test_image_release_evidence_is_minimal_exact_and_non_secret() -> None:
    digest = f"sha256:{'a' * 64}"
    git_sha = "b" * 40
    manifest_digest = f"sha256:{'c' * 64}"
    payload = json.loads(
        canonical_image_release_bytes(
            repository="ghcr.io/michaelayoade/dotmac_erp",
            digest=digest,
            git_sha=git_sha,
            manifest_digest=manifest_digest,
        )
    )
    assert payload == {
        "digest": digest,
        "git_sha": git_sha,
        "manifest_digest": manifest_digest,
        "reference": f"ghcr.io/michaelayoade/dotmac_erp@{digest}",
        "schema": "dotmac.image-release.v2",
    }


@pytest.mark.parametrize(
    ("repository", "digest", "git_sha", "manifest_digest"),
    (
        (
            "ghcr.io/example/app:latest",
            f"sha256:{'a' * 64}",
            "b" * 40,
            f"sha256:{'c' * 64}",
        ),
        (
            "ghcr.io/example/app",
            "sha256:short",
            "b" * 40,
            f"sha256:{'c' * 64}",
        ),
        (
            "ghcr.io/example/app",
            f"sha256:{'a' * 64}",
            "not-a-sha",
            f"sha256:{'c' * 64}",
        ),
        (
            "ghcr.io/example/app",
            f"sha256:{'a' * 64}",
            "b" * 40,
            "sha256:short",
        ),
    ),
)
def test_image_release_evidence_refuses_mutable_or_malformed_identity(
    repository: str, digest: str, git_sha: str, manifest_digest: str
) -> None:
    with pytest.raises(ImageReleaseError):
        canonical_image_release_bytes(
            repository=repository,
            digest=digest,
            git_sha=git_sha,
            manifest_digest=manifest_digest,
        )
