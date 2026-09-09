"""Write non-secret immutable image publication evidence for CI."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Sequence
from pathlib import Path

IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
GIT_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
IMAGE_COMPONENT = r"[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?"
IMAGE_REPOSITORY_RE = re.compile(rf"^ghcr\.io/{IMAGE_COMPONENT}/{IMAGE_COMPONENT}$")
SCHEMA = "dotmac.image-release.v2"


class ImageReleaseError(RuntimeError):
    """Publication output cannot be represented as immutable evidence."""


def canonical_image_release_bytes(
    *, repository: str, digest: str, git_sha: str, manifest_digest: str
) -> bytes:
    """Return the minimal release record; it contains no credentials or tags."""

    if not IMAGE_REPOSITORY_RE.fullmatch(repository):
        raise ImageReleaseError(f"invalid GHCR repository: {repository!r}")
    if not IMAGE_DIGEST_RE.fullmatch(digest):
        raise ImageReleaseError(f"invalid image digest: {digest!r}")
    if not GIT_SHA_RE.fullmatch(git_sha):
        raise ImageReleaseError(f"invalid GITHUB_SHA: {git_sha!r}")
    if not IMAGE_DIGEST_RE.fullmatch(manifest_digest):
        raise ImageReleaseError(
            f"invalid canonical product manifest digest: {manifest_digest!r}"
        )
    payload = {
        "digest": digest,
        "git_sha": git_sha,
        "manifest_digest": manifest_digest,
        "reference": f"{repository}@{digest}",
        "schema": SCHEMA,
    }
    return f"{json.dumps(payload, separators=(',', ':'), sort_keys=True)}\n".encode()


def write_image_release(
    *,
    output: Path,
    repository: str,
    digest: str,
    git_sha: str,
    manifest_digest: str,
) -> None:
    if not output.parent.is_dir():
        raise ImageReleaseError(
            f"image release output directory does not exist: {output.parent}"
        )
    try:
        output.write_bytes(
            canonical_image_release_bytes(
                repository=repository,
                digest=digest,
                git_sha=git_sha,
                manifest_digest=manifest_digest,
            )
        )
    except OSError as exc:
        raise ImageReleaseError(f"cannot write image release {output}: {exc}") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--git-sha", required=True)
    parser.add_argument("--manifest-digest", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    write_image_release(
        output=args.output,
        repository=args.repository,
        digest=args.digest,
        git_sha=args.git_sha,
        manifest_digest=args.manifest_digest,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
