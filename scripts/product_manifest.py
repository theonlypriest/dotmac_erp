"""Generate and verify ERP's canonical module-identity product manifest.

The kernel's ProductManifestSnapshot intentionally projects capability codes;
it does not preserve installable module identities or persistence planes. A
deployment release needs those exact facts, so this adapter serializes the
ERP-owned ProductAssemblySpec without creating another composition owner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from dotmac_kernel.planes import ModulePlane, declared_planes

from app.product_assembly import (
    COMPOSED_MODULE_DISTRIBUTIONS,
    ERP_PRODUCT_ASSEMBLY,
)

PRODUCT_MANIFEST_SCHEMA: Final = "dotmac.product-assembly-manifest.v1"


class ProductManifestError(RuntimeError):
    """The declared assembly cannot produce or verify its release document."""


def _plane_values(planes: Sequence[ModulePlane | str]) -> list[str]:
    return sorted(ModulePlane(plane).value for plane in planes)


def _module_payloads() -> list[dict[str, object]]:
    explicit = {
        selection.module: _plane_values(selection.planes)
        for selection in ERP_PRODUCT_ASSEMBLY.module_planes
    }
    records: list[dict[str, object]] = []
    seen: set[str] = set()

    for manifest in sorted(
        ERP_PRODUCT_ASSEMBLY.modules,
        key=lambda item: str(getattr(item, "code", "")),
    ):
        code = str(getattr(manifest, "code", ""))
        version = str(getattr(manifest, "version", ""))
        if not code or not version:
            raise ProductManifestError(
                "every composed module requires a non-empty code and version"
            )
        if code in seen:
            raise ProductManifestError(f"duplicate composed module code: {code}")
        seen.add(code)

        try:
            distribution = COMPOSED_MODULE_DISTRIBUTIONS[code]
        except KeyError as exc:
            raise ProductManifestError(
                f"composed module {code!r} has no distribution binding"
            ) from exc

        declared = _plane_values(declared_planes(manifest))
        explicit_planes = explicit.get(code)
        effective = explicit_planes if explicit_planes is not None else declared
        records.append(
            {
                "code": code,
                "declared_planes": declared,
                "distribution": distribution,
                "effective_planes": effective,
                "explicit_planes": explicit_planes,
                "version": version,
            }
        )

    unbound = sorted(set(COMPOSED_MODULE_DISTRIBUTIONS) - seen)
    if unbound:
        raise ProductManifestError(
            f"distribution bindings name uncomposed modules: {unbound}"
        )
    return records


def product_manifest_payload() -> Mapping[str, object]:
    """Return the stable release document derived from the one assembly spec."""

    return {
        "modules": _module_payloads(),
        "product": ERP_PRODUCT_ASSEMBLY.name,
        "schema": PRODUCT_MANIFEST_SCHEMA,
    }


def canonical_product_manifest_bytes() -> bytes:
    """Return byte-stable UTF-8 JSON with one trailing newline."""

    encoded = json.dumps(
        product_manifest_payload(),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"{encoded}\n".encode()


def product_manifest_digest() -> str:
    """Digest the exact committed bytes consumed by deployment evidence."""

    return f"sha256:{hashlib.sha256(canonical_product_manifest_bytes()).hexdigest()}"


def generate_product_manifest(path: Path) -> str:
    """Write the canonical document to an existing release directory."""

    if not path.parent.is_dir():
        raise ProductManifestError(
            f"product manifest output directory does not exist: {path.parent}"
        )
    try:
        path.write_bytes(canonical_product_manifest_bytes())
    except OSError as exc:
        raise ProductManifestError(
            f"cannot write product manifest {path}: {exc}"
        ) from exc
    return product_manifest_digest()


def check_product_manifest(path: Path) -> str:
    """Require the committed document to equal the assembly-derived bytes."""

    try:
        observed = path.read_bytes()
    except OSError as exc:
        raise ProductManifestError(
            f"cannot read product manifest {path}: {exc}"
        ) from exc
    if observed != canonical_product_manifest_bytes():
        raise ProductManifestError(
            "product manifest is not the canonical ERP_PRODUCT_ASSEMBLY document"
        )
    return product_manifest_digest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command_name in ("generate", "check"):
        command = commands.add_parser(command_name)
        command.add_argument("--path", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "generate":
        digest = generate_product_manifest(args.path)
    else:
        digest = check_product_manifest(args.path)
    print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
