#!/usr/bin/env python3
"""Deterministic digest of a served static tree.

Why this exists. ERP served a stylesheet 198 insertions behind its own image for
an unknown period. Two paths carried the checkout to the browser -- a compose
bind mount over /app/static, and sync-static.sh rsyncing the checkout into the
nginx web root, which nginx served ahead of the application. The image had been
compiling the correct stylesheet the whole time and it was never used.

Nothing caught it because the only check asserted the file EXISTED. An internal
check of the image would also have passed the whole time: the image was right.
The single check that would have caught it is comparing what is ACTUALLY SERVED
against a digest recorded in the deployment descriptor. That is what this
computes.

The digest is over a manifest of (sha256, relative path) pairs sorted by path,
not over a tarball: tar embeds mtimes and ordering, so two identical trees would
digest differently and the check would be noise within a week.

    python scripts/static_tree_digest.py static
    python scripts/static_tree_digest.py --container dotmac_erp_app /app/static
    python scripts/static_tree_digest.py --manifest static     # show the pairs
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

#: Never part of the served identity: editor droppings and OS metadata that may
#: exist in a working tree but never reach an image. Listed explicitly so the
#: exclusion is reviewable rather than a silent quirk of the walker.
EXCLUDED_NAMES = frozenset({".DS_Store", "Thumbs.db"})


def _manifest_from_directory(root: Path) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name in EXCLUDED_NAMES:
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        entries.append((digest, path.relative_to(root).as_posix()))
    return sorted(entries, key=lambda pair: pair[1])


def _manifest_from_container(container: str, root: str) -> list[tuple[str, str]]:
    """Hash the tree inside a running container.

    This is the form that matters operationally: it reads the bytes the running
    application will actually serve, rather than the bytes a checkout happens to
    hold. Uses only find/sha256sum, so the image needs no extra tooling.
    """
    script = (
        f"cd {root} && find . -type f -print0 | LC_ALL=C sort -z | "
        "xargs -0 -r sha256sum"
    )
    completed = subprocess.run(  # noqa: S603
        ["docker", "exec", container, "sh", "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    entries: list[tuple[str, str]] = []
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        digest, _, name = line.partition("  ")
        relative = name[2:] if name.startswith("./") else name
        if Path(relative).name in EXCLUDED_NAMES:
            continue
        entries.append((digest, relative))
    return sorted(entries, key=lambda pair: pair[1])


def tree_digest(entries: list[tuple[str, str]]) -> str:
    manifest = "".join(f"{digest}  {name}\n" for digest, name in entries)
    return "sha256:" + hashlib.sha256(manifest.encode("utf-8")).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", help="directory, or in-container path with --container")
    parser.add_argument("--container", help="hash inside this running container")
    parser.add_argument(
        "--manifest", action="store_true", help="print the per-file manifest too"
    )
    parser.add_argument(
        "--expect", help="fail unless the computed digest equals this value"
    )
    args = parser.parse_args()

    if args.container:
        entries = _manifest_from_container(args.container, args.root)
    else:
        root = Path(args.root)
        if not root.is_dir():
            print(f"not a directory: {root}", file=sys.stderr)
            return 2
        entries = _manifest_from_directory(root)

    if not entries:
        # An empty tree digests to a stable value, which would silently "match"
        # a descriptor recording an empty tree. Refuse rather than certify it.
        print(f"refusing to digest an empty static tree: {args.root}", file=sys.stderr)
        return 2

    digest = tree_digest(entries)

    if args.manifest:
        for file_digest, name in entries:
            print(f"{file_digest}  {name}")
        print(f"--- {len(entries)} file(s)")

    print(digest)

    if args.expect and args.expect != digest:
        print(
            f"static tree digest mismatch:\n  expected {args.expect}\n"
            f"  actual   {digest}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
