"""ERP's answers to "which revision supplies that database effect?".

An installable module declares the effects its lineage needs
(`ModuleManifest.requires`, `dotmac_kernel.prerequisites`); it never names a
foreign revision, because the answer differs per assembly. This file is where
ERP answers.

## ERP is the reason this indirection exists

Kernel `0001_initial_tenant_schema` creates `public.tenants` unconditionally as
its FIRST table. ERP hosts that same table in its own lineage, so kernel `0001`
can never run here — `tests/integration/test_kernel_lineage_rehearsal.py` is a
permanent negative canary proving exactly that, and it can never go green.

Under the old physical-edge model, `dotmac-files` declaring
`depends_on = ("0001_initial_tenant_schema",)` therefore made stored bytes
un-installable in ERP unless ERP first converged its entire identity, RBAC and
audit estate onto the kernel's — an enormous amount of coupling to obtain one
foreign-key target. Starter's ADR-0006 D1 amendment replaced that edge with
logical prerequisites, and these bindings are how ERP supplies them from
revisions it actually runs.

## These are claims, and they are checked

Nothing here is taken on trust. A requiring module calls
`require_prerequisites` before any DDL, which verifies the effects against the
live catalog — table shape, key and index contract, the tenant function's
semantics, and the three roles' `(rolbypassrls, rolsuper)` posture. A binding
that named a revision which did not really supply the effect would fail there,
against the database, not here.
"""

from __future__ import annotations

from typing import Final

from dotmac_kernel.prerequisites import (
    IDEMPOTENCY_LEDGER_V1,
    MODULE_DATABASE_ROLES_V1,
    OUTBOX_RELAY_V1,
    PARTY_PERSON_CATALOG_V1,
    TENANT_SCOPE_CATALOG_V1,
    PrerequisiteBinding,
)

#: `20260813_tenant_projection` hosts `public.tenants`, `public.tenant_domains`
#: and `public.app_current_tenant_id()` in ERP's own lineage, projected from the
#: authoritative Organization. `20260814_database_roles` adopts and verifies
#: `app_admin`, `app_user` and `platform_api` — it never creates them, because
#: `CREATE ROLE` needs privileges an ordinary migration must not hold; the
#: explicitly elevated `scripts/bootstrap_database_roles.py` does that.
#: `20260820_idempotency_ledger` hosts both planes of the kernel at-most-once
#: ledger while ADR-0001 keeps ERP's endpoint-response cache as ratcheted
#: transitional state; no operation is cut over merely by supplying storage.
#: `20260824_party_person_projection` hosts `public.parties` and
#: `public.party_persons` as a projection of `public.people`, which stays the
#: person authority until `dotmac-party` and `dotmac-people` cut over.
#: `20260824_outbox_relay` hosts both relay planes for MODULE events; ERP's
#: own `platform.event_outbox` remains the business-event authority and is not
#: touched by it.
ASSEMBLY_PREREQUISITE_BINDINGS: Final[tuple[PrerequisiteBinding, ...]] = (
    PrerequisiteBinding(
        prerequisite=TENANT_SCOPE_CATALOG_V1.name,
        provider_revision="20260813_tenant_projection",
        provider_owner="assembly",
    ),
    PrerequisiteBinding(
        prerequisite=MODULE_DATABASE_ROLES_V1.name,
        provider_revision="20260814_database_roles",
        provider_owner="assembly",
    ),
    PrerequisiteBinding(
        prerequisite=IDEMPOTENCY_LEDGER_V1.name,
        provider_revision="20260820_idempotency_ledger",
        provider_owner="assembly",
    ),
    PrerequisiteBinding(
        prerequisite=PARTY_PERSON_CATALOG_V1.name,
        provider_revision="20260824_party_person_projection",
        provider_owner="assembly",
    ),
    PrerequisiteBinding(
        prerequisite=OUTBOX_RELAY_V1.name,
        provider_revision="20260824_outbox_relay",
        provider_owner="assembly",
    ),
)

#: The module lineages ERP COMPOSES, and the head each one is expected to be at.
#:
#: ## ERP does not have one Alembic head, and must not expect one
#:
#: Every composed module lineage is an independent ROOT carrying its own branch
#: label (`fi_0001_stored_files` -> `files`, `ac_0001_accounting` ->
#: `accounting`).  That is the design, not an accident: a module owns its own
#: revision history so it can be released, pinned and upgraded without ERP
#: rewriting its graph.  ERP's revision map therefore has as many heads as it
#: has composed lineages, plus one for its own — which is exactly why
#: `scripts/deploy.sh` and the Makefile run `alembic upgrade heads` (plural).
#:
#: "One global head" is the WRONG acceptance criterion.  The right one, which
#: `tests/integration/test_accounting_lineage_composition.py` enforces, is:
#: exactly one head per composed module branch, at the revision named here,
#: plus exactly one ERP head, and NO unintended heads.  An unintended head is
#: the real defect this guards — a second ERP root, a stray revision whose
#: `down_revision` does not reach the tip, or a module lineage that grew a head
#: nobody reviewed.
#:
#: The values are stable because they are properties of PINNED artifacts: a
#: module head moves only when its distribution is repinned, which is a reviewed
#: change that must update this map in the same commit.
COMPOSED_MODULE_LINEAGES: Final[dict[str, str]] = {
    "files": "fi_0001_stored_files",
    "accounting": "ac_0001_accounting",
    "imports": "im_0001_import_runs",
    "numbering": "nu_0001_numbering",
    "people": "pe_0001_people_directory",
    "tax": "tx_0003_result_fingerprint",
}

__all__ = ["ASSEMBLY_PREREQUISITE_BINDINGS", "COMPOSED_MODULE_LINEAGES"]
