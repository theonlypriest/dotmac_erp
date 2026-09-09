"""ERP's release-time product assembly declaration.

This is composition metadata, not a second application factory. ``app.main``
continues to own ERP startup, routes, middleware, sessions and authorization.
The deployment release path consumes this spec to bind the exact installable
module identities already composed by ERP and their persistence-plane intent.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from dotmac_accounting.manifest import module as accounting_module
from dotmac_files.manifest import module as files_module
from dotmac_imports.manifest import module as imports_module
from dotmac_kernel.assembly import ProductAssemblySpec
from dotmac_numbering.manifest import module as numbering_module
from dotmac_people.manifest import module as people_module
from dotmac_tax.manifest import module as tax_module

from app.migration_planes import ASSEMBLY_MODULE_PLANES

# This is the composed product's stable identity. ``dotmac_erp`` remains the
# historical source-repository and Python-distribution coordinate; it must not
# leak into the target product manifest as a second product name.
PRODUCT_CODE: Final = "dotmac-erp"

# Order is canonical by module code. Every entry is already represented in
# COMPOSED_MODULE_LINEAGES; an architecture test keeps those two declarations
# equal so a migration lineage cannot enter an image without release identity.
COMPOSED_MODULE_MANIFESTS: Final = (
    accounting_module,
    files_module,
    imports_module,
    numbering_module,
    people_module,
    tax_module,
)

# ModuleManifest deliberately knows its module code, not the package-manager
# distribution coordinate. ERP owns this release binding and tests it against
# the exact Poetry pins.
COMPOSED_MODULE_DISTRIBUTIONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "accounting": "dotmac-accounting",
        "files": "dotmac-files",
        "imports": "dotmac-imports",
        "numbering": "dotmac-numbering",
        "people": "dotmac-people",
        "tax": "dotmac-tax",
    }
)

ERP_PRODUCT_ASSEMBLY: Final = ProductAssemblySpec(
    name=PRODUCT_CODE,
    modules=COMPOSED_MODULE_MANIFESTS,
    module_planes=ASSEMBLY_MODULE_PLANES,
    tenancy="multi",
)

__all__ = [
    "COMPOSED_MODULE_DISTRIBUTIONS",
    "COMPOSED_MODULE_MANIFESTS",
    "ERP_PRODUCT_ASSEMBLY",
    "PRODUCT_CODE",
]
