"""The frozen bill of materials for the composed Dotmac ERP (ADR-0004).

This module is programme step 1: it closes the question "which modules does the
composed ERP install, and who owns everything else?" before any composition,
repointing or data work begins.

Two closures are declared here, and both are enforced by
``tests/architecture/test_bill_of_materials.py``:

1. **Module closure.** Every distribution in the Starter package census is
   either ``SELECTED`` or ``EXCLUDED`` — never both, never neither. A new
   Starter package cannot drift into or past this product silently; it forces a
   diff here.
2. **Capability closure.** Every capability in ``ERP_CAPABILITY_CENSUS`` is
   carried by exactly one selected module or is explicitly ``RETAINED`` as
   ERP-owned assembly code. ADR-0003 admits retained ERP owners; it does not
   admit unowned ones.

What is frozen is MEMBERSHIP and OWNERSHIP, not version pins. Steps 2 through 7
will change module code, so freezing versions now would freeze the wrong thing;
the pins move under composition and are frozen at step 10, when the final clean
production database is created.

Omission is the one failure mode a list cannot detect from its own contents:
`dotmac-work-orders` sat in no cohort of the ISP programme matrix and every
check passed, because nothing declared the set it was missing from. The two
censuses below exist so that silence is not an answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

#: The Starter revision the package census was measured at. A census is a claim
#: about another repository, so it is pinned to an exact tree rather than to
#: "main", which means something different every day.
STARTER_PACKAGE_CENSUS_REVISION: Final = "bd8d2262c26f62041cc22a813916066b9af85c7f"

STARTER_PACKAGE_CENSUS_REPOSITORY: Final = (
    "https://github.com/michaelayoade/dotmac_starter_mt"
)

#: Every distribution under `packages/` at that revision. Ninety of them.
STARTER_PACKAGE_CENSUS: Final[frozenset[str]] = frozenset(
    {
        "dotmac-accounting",
        "dotmac-ai-operations",
        "dotmac-analytics",
        "dotmac-app-sync",
        "dotmac-application-directory",
        "dotmac-approvals",
        "dotmac-assets",
        "dotmac-auth-oidc",
        "dotmac-banking",
        "dotmac-billing",
        "dotmac-brand-profiles",
        "dotmac-campaigns",
        "dotmac-collections",
        "dotmac-commercial-agreements",
        "dotmac-compliance-reporting",
        "dotmac-connector-flutterwave",
        "dotmac-connector-linkedin",
        "dotmac-connector-meta-social",
        "dotmac-connector-mono",
        "dotmac-connector-paystack",
        "dotmac-connector-remita",
        "dotmac-connector-whatsapp",
        "dotmac-content",
        "dotmac-customers",
        "dotmac-deployment-control",
        "dotmac-document-rendering",
        "dotmac-documents",
        "dotmac-durable-timers",
        "dotmac-entitlement-allocation",
        "dotmac-expenses",
        "dotmac-fiber-plant",
        "dotmac-files",
        "dotmac-finance",
        "dotmac-forms",
        "dotmac-fulfillment",
        "dotmac-fx-policy",
        "dotmac-imports",
        "dotmac-inbox",
        "dotmac-inbox-operations",
        "dotmac-integration",
        "dotmac-inventory",
        "dotmac-ipam",
        "dotmac-kernel",
        "dotmac-licensing",
        "dotmac-media-observations",
        "dotmac-network-access",
        "dotmac-network-assurance",
        "dotmac-network-control",
        "dotmac-network-inventory",
        "dotmac-network-observability",
        "dotmac-network-topology",
        "dotmac-numbering",
        "dotmac-operational-escalations",
        "dotmac-party",
        "dotmac-payables",
        "dotmac-payments",
        "dotmac-payroll",
        "dotmac-people",
        "dotmac-platform-health",
        "dotmac-pon-access",
        "dotmac-positioning",
        "dotmac-procurement",
        "dotmac-projects",
        "dotmac-publishing",
        "dotmac-qualification",
        "dotmac-records",
        "dotmac-referrals",
        "dotmac-release-catalog",
        "dotmac-remote-access",
        "dotmac-reseller-management",
        "dotmac-sales",
        "dotmac-service-access-policy",
        "dotmac-service-catalog",
        "dotmac-service-changes",
        "dotmac-service-orders",
        "dotmac-services",
        "dotmac-sites",
        "dotmac-subscriptions",
        "dotmac-support-access",
        "dotmac-surveys",
        "dotmac-tax",
        "dotmac-template-studio",
        "dotmac-ticketing",
        "dotmac-ui",
        "dotmac-usage",
        "dotmac-usage-rating",
        "dotmac-web-analytics",
        "dotmac-work-orders",
        "dotmac-workflow-runtime",
        "dotmac-workforce",
    }
)

#: `composed` is pinned in `pyproject.toml` today; `selected` is in the frozen
#: set and not yet pinned. There is no third state: a module is in the product
#: or it is not, and "maybe later" is what `EXCLUDED` is for.
COMPOSITION_STATES: Final[frozenset[str]] = frozenset({"composed", "selected"})

#: Whether an installable artifact exists yet. `unbuilt` means no package root
#: exists at the census revision — it cannot occur while module closure holds,
#: and is kept so a future BOM row for a module that must be created has a
#: truthful state instead of borrowing `unreleased`.
RELEASE_STATES: Final[frozenset[str]] = frozenset({"released", "unreleased", "unbuilt"})


@dataclass(frozen=True)
class SelectedModule:
    """A module the composed Dotmac ERP installs."""

    distribution: str
    state: str
    release_state: str
    capabilities: tuple[str, ...]
    rationale: str


@dataclass(frozen=True)
class ExcludedModule:
    """A Starter distribution the composed Dotmac ERP deliberately does not install."""

    distribution: str
    owner: str
    rationale: str


@dataclass(frozen=True)
class RetainedCapability:
    """A capability the composed assembly keeps as ERP-owned code.

    ADR-0003: "This definition allows explicitly retained ERP-owned decisions.
    'Composable' does not mean that every line of application code moves into a
    package; it means that ownership is singular, declared and enforceable."
    A row here is a NAMED owner, which is what step 7 needs; it is not a claim
    that the capability should never become a module.
    """

    capability: str
    rationale: str


SELECTED: Final[tuple[SelectedModule, ...]] = (
    SelectedModule(
        distribution="dotmac-kernel",
        state="composed",
        release_state="released",
        capabilities=(
            "tenancy",
            "authentication",
            "authorization-rbac",
            "settings-resolution",
            "audit-trail",
            "idempotency",
            "money-primitives",
        ),
        rationale="The assembly floor. Every other module declares a floor on it.",
    ),
    SelectedModule(
        distribution="dotmac-ui",
        state="composed",
        release_state="released",
        capabilities=("design-system",),
        rationale=(
            "Compiled tokens and inert Jinja components; ERP supplies the loader."
        ),
    ),
    SelectedModule(
        distribution="dotmac-files",
        state="composed",
        release_state="released",
        capabilities=("file-storage",),
        rationale=(
            "First composed foreign lineage. Stored bytes and their physical "
            "lifecycle; what the bytes mean stays with the owning domain."
        ),
    ),
    SelectedModule(
        distribution="dotmac-imports",
        state="composed",
        release_state="released",
        capabilities=("import-runs",),
        rationale=(
            "Durable run, partition, claim and per-row outcome mechanics. The "
            "step 5 import contracts are built on this, not beside it."
        ),
    ),
    SelectedModule(
        distribution="dotmac-accounting",
        state="composed",
        release_state="released",
        capabilities=(
            "general-ledger",
            "chart-of-accounts",
            "fiscal-calendar",
            "accounting-dimensions",
        ),
        rationale=(
            "Pinned and migrated with ACCOUNTING_COMPOSITION_ENABLED false. It "
            "receives the step 13 governed opening."
        ),
    ),
    SelectedModule(
        distribution="dotmac-payables",
        state="selected",
        release_state="released",
        capabilities=("accounts-payable",),
        rationale="ERP is the qualifying source; AP follows the Accounting boundary.",
    ),
    SelectedModule(
        distribution="dotmac-banking",
        state="selected",
        release_state="released",
        capabilities=("bank-accounts", "bank-reconciliation"),
        rationale=(
            "ERP is the qualifying source for statement matching and reconciliation."
        ),
    ),
    SelectedModule(
        distribution="dotmac-tax",
        state="composed",
        release_state="released",
        capabilities=("tax-determination", "tax-filing-evidence"),
        rationale=(
            "ERP is the qualifying policy/reporting source; GL mappings, journals "
            "and postings stay with Accounting."
        ),
    ),
    SelectedModule(
        distribution="dotmac-finance",
        state="selected",
        release_state="released",
        capabilities=("fixed-asset-accounting",),
        rationale="ERP's production fixed-asset stack is the qualifying source.",
    ),
    SelectedModule(
        distribution="dotmac-assets",
        state="selected",
        release_state="released",
        capabilities=("asset-register",),
        rationale=(
            "Generic asset identity, assignment, maintenance and disposal. "
            "Finance, inventory and fleet extensions stay product-owned."
        ),
    ),
    SelectedModule(
        distribution="dotmac-inventory",
        state="selected",
        release_state="released",
        capabilities=("inventory-stock", "inventory-valuation"),
        rationale="ERP's production-used ledger is the qualifying source.",
    ),
    SelectedModule(
        distribution="dotmac-procurement",
        state="selected",
        release_state="released",
        capabilities=("purchase-requisition", "purchase-order", "rfq-quotation"),
        rationale=(
            "Audit-complete candidate recovered rather than rebuilt; ERP is cutover 1."
        ),
    ),
    SelectedModule(
        distribution="dotmac-expenses",
        state="selected",
        release_state="released",
        capabilities=("expense-claims", "expense-policy"),
        rationale="ERP is the qualifying source and first exact-pin adopter.",
    ),
    SelectedModule(
        distribution="dotmac-payroll",
        state="selected",
        release_state="released",
        capabilities=("payroll-calculation", "payroll-liabilities"),
        rationale=(
            "ERP is the qualifying source; requires Accounting and the employee "
            "reference."
        ),
    ),
    SelectedModule(
        distribution="dotmac-people",
        state="composed",
        release_state="released",
        capabilities=("employment-directory",),
        rationale=(
            "The six-table tenant storage lineage and one explicit operator-only "
            "Employment Type bootstrap are composed with no runtime authority "
            "transfer. Legacy People writers remain the only authority until a "
            "domain-by-domain cutover. "
            "Six tables of employment identity. The legacy employee hub's exact "
            "ORM dependency intents are ledgered by FK identity in "
            "docs/inventories/people-dependent-references.tsv; "
            "tests/integration/people_hub_fk_catalog.tsv records the migrated FK "
            "identities and action semantics. Existing model-only and physical-only FK "
            "drift is separately ratcheted control debt, not a claim that either "
            "ledger is a subset of the other. "
            "During cutover, remaining compatibility is a local, rebuildable, "
            "read-only projection inside the clean assembly, never a reverse feed "
            "to the historical source."
        ),
    ),
    SelectedModule(
        distribution="dotmac-workforce",
        state="selected",
        release_state="unreleased",
        capabilities=("workforce-shifts", "workforce-skills"),
        rationale=(
            "Carries shifts and skills only. Attendance, leave, performance and "
            "training are retained — see RETAINED. Unreleased AND absent from "
            "the Starter release allowlist, so step 2 cannot compose it yet."
        ),
    ),
    SelectedModule(
        distribution="dotmac-projects",
        state="selected",
        release_state="released",
        capabilities=("project-lifecycle", "project-tasks"),
        rationale=(
            "Product-neutral project/task rows. Milestones, resources, time entry "
            "and costing are retained."
        ),
    ),
    SelectedModule(
        distribution="dotmac-ticketing",
        state="selected",
        release_state="released",
        capabilities=("support-tickets",),
        rationale=(
            "ERP is cutover 1 by Michael's 2026-08-13 direction; 125 files reference a "
            "ticket."
        ),
    ),
    SelectedModule(
        distribution="dotmac-approvals",
        state="selected",
        release_state="released",
        capabilities=("approval-decisions",),
        rationale="Adopted and production-proven in Vendor CP; ERP is cutover 2.",
    ),
    SelectedModule(
        distribution="dotmac-numbering",
        state="composed",
        release_state="released",
        capabilities=("document-numbering",),
        rationale=(
            "Tenant-plane storage is composed without a runtime caller. ERP is "
            "cutover 1, one series family at a time; every caller must first "
            "thread an explicit business date."
        ),
    ),
    SelectedModule(
        distribution="dotmac-durable-timers",
        state="selected",
        release_state="released",
        capabilities=("durable-timers",),
        rationale="Enabling capability for reminders, escalations and scheduled runs.",
    ),
    SelectedModule(
        distribution="dotmac-documents",
        state="selected",
        release_state="released",
        capabilities=("controlled-documents",),
        rationale=(
            "HR handbook is the first coherent slice: version, effective date, "
            "acknowledgement."
        ),
    ),
    SelectedModule(
        distribution="dotmac-records",
        state="selected",
        release_state="released",
        capabilities=("records-retention",),
        rationale=(
            "Retention, hold and deletion authority over HR and finance "
            "attachments. Requires a checked-in writer inventory first."
        ),
    ),
    SelectedModule(
        distribution="dotmac-document-rendering",
        state="selected",
        release_state="released",
        capabilities=("document-rendering",),
        rationale="Rendering is a shared mechanism; ERP keeps what the documents mean.",
    ),
    SelectedModule(
        distribution="dotmac-forms",
        state="selected",
        release_state="released",
        capabilities=("authored-forms",),
        rationale=(
            "ERP recruitment is cutover 1 with the qualifying seven-table source."
        ),
    ),
    SelectedModule(
        distribution="dotmac-workflow-runtime",
        state="selected",
        release_state="released",
        capabilities=("workflow-executions",),
        rationale=(
            "Provider-neutral executions, checkpoints and bounded retries. The "
            "subject lifecycle and every consequence stay with the domain owner."
        ),
    ),
    SelectedModule(
        distribution="dotmac-analytics",
        state="selected",
        release_state="released",
        capabilities=("analytical-measures",),
        rationale=(
            "Semantic measures and derived datasets; presentation stays retained."
        ),
    ),
    SelectedModule(
        distribution="dotmac-auth-oidc",
        state="selected",
        release_state="released",
        capabilities=("external-identity-federation",),
        rationale=(
            "ERP's local OIDC implementation was confirmed not live, so this is a "
            "deletion rather than a login migration."
        ),
    ),
    SelectedModule(
        distribution="dotmac-party",
        state="selected",
        release_state="released",
        capabilities=("party-relationships", "party-contact-points"),
        rationale=(
            "Roles, memberships, contact points and consent above the kernel's "
            "Party identity. Sub is cutover 1; ERP follows on the same release."
        ),
    ),
    SelectedModule(
        distribution="dotmac-surveys",
        state="selected",
        release_state="released",
        capabilities=("surveys",),
        rationale=(
            "ERP is cutover 2. HR anonymity, eligibility windows and every HR "
            "consequence stay in ERP."
        ),
    ),
    SelectedModule(
        distribution="dotmac-fx-policy",
        state="selected",
        release_state="unreleased",
        capabilities=("fx-rate-policy",),
        rationale=(
            "ERP is first: rate types, sources, history and direct/inverse "
            "selection. Unreleased AND absent from the Starter release "
            "allowlist. FX REVALUATION is a separate, retained capability."
        ),
    ),
    SelectedModule(
        distribution="dotmac-positioning",
        state="selected",
        release_state="released",
        capabilities=("position-evidence",),
        rationale=(
            "Cutover 2 behind Sub, through a product-owned vehicle-to-tracked-unit "
            "link. Fleet and attendance consequences stay in ERP."
        ),
    ),
    SelectedModule(
        distribution="dotmac-payments",
        state="selected",
        release_state="released",
        capabilities=("payment-intent", "payment-confirmation"),
        rationale=(
            "Intent and provider-confirmation correlation only. Treasury and cash "
            "positioning are retained and still need a named module owner."
        ),
    ),
    SelectedModule(
        distribution="dotmac-template-studio",
        state="selected",
        release_state="unreleased",
        capabilities=("message-templates",),
        rationale=(
            "Replaces app/services/automation/safe_template.py. Dossier status is "
            "audit-required and its substitution syntax is the one Sub rejects, so "
            "the contract must be settled before ERP composes it."
        ),
    ),
    SelectedModule(
        distribution="dotmac-app-sync",
        state="selected",
        release_state="unreleased",
        capabilities=("cross-application-sync",),
        rationale=(
            "Transport, authentication and atomic deduplication for Sub "
            "and Academy seams. What a synchronised row MEANS stays retained."
        ),
    ),
)


EXCLUDED: Final[tuple[ExcludedModule, ...]] = (
    ExcludedModule(
        distribution="dotmac-ai-operations",
        owner="unassigned",
        rationale=(
            "No dossier names ERP as a consumer. The ERP coach surface is "
            "retained as ai-coach-insights until a real owner is adjudicated."
        ),
    ),
    ExcludedModule(
        distribution="dotmac-application-directory",
        owner="dotmac_workspace",
        rationale=(
            "A tenant's connected-application portfolio; composed by Workspace, not a "
            "product."
        ),
    ),
    ExcludedModule(
        distribution="dotmac-billing",
        owner="dotmac_sub",
        rationale=(
            "Operational receivables, explicitly NOT statutory trade AR "
            "(Starter ADR-0020). Installing it here would create a shadow AR."
        ),
    ),
    ExcludedModule(
        distribution="dotmac-brand-profiles",
        owner="dotmac_vendor_control_plane",
        rationale=(
            "Commercial branding intent is a Vendor CP decision consumed over an API."
        ),
    ),
    ExcludedModule(
        distribution="dotmac-campaigns",
        owner="dotmac_sub",
        rationale=(
            "Outbound campaign execution; Sub is the qualifying production source."
        ),
    ),
    ExcludedModule(
        distribution="dotmac-collections",
        owner="dotmac_sub",
        rationale="Operational dunning over Billing receivables; no ERP writer.",
    ),
    ExcludedModule(
        distribution="dotmac-commercial-agreements",
        owner="dotmac_vendor_control_plane",
        rationale="Commercial agreements are a Vendor CP authority.",
    ),
    ExcludedModule(
        distribution="dotmac-compliance-reporting",
        owner="unassigned",
        rationale=(
            "No dossier names ERP; ERP statutory reporting is retained presentation."
        ),
    ),
    ExcludedModule(
        distribution="dotmac-connector-flutterwave",
        owner="dotmac_integrator",
        rationale=(
            "Provider connectors run in Integrator; products do not compose connector "
            "runtime."
        ),
    ),
    ExcludedModule(
        distribution="dotmac-connector-linkedin",
        owner="dotmac_integrator",
        rationale=(
            "Provider connectors run in Integrator; products do not compose connector "
            "runtime."
        ),
    ),
    ExcludedModule(
        distribution="dotmac-connector-meta-social",
        owner="dotmac_integrator",
        rationale=(
            "Provider connectors run in Integrator; products do not compose connector "
            "runtime."
        ),
    ),
    ExcludedModule(
        distribution="dotmac-connector-mono",
        owner="dotmac_integrator",
        rationale=(
            "ERP banking observations arrive through Integrator's typed product "
            "port; ERP never composes the connector."
        ),
    ),
    ExcludedModule(
        distribution="dotmac-connector-paystack",
        owner="dotmac_integrator",
        rationale=(
            "Provider connectors run in Integrator; products do not compose connector "
            "runtime."
        ),
    ),
    ExcludedModule(
        distribution="dotmac-connector-remita",
        owner="dotmac_integrator",
        rationale=(
            "RRR status observations arrive through Integrator; ERP remains the "
            "only writer of RRR and accounting state."
        ),
    ),
    ExcludedModule(
        distribution="dotmac-connector-whatsapp",
        owner="dotmac_integrator",
        rationale=(
            "Provider connectors run in Integrator; products do not compose connector "
            "runtime."
        ),
    ),
    ExcludedModule(
        distribution="dotmac-content",
        owner="pending-decision",
        rationale=(
            "Marketing editorial estate. Its dossier names the Dotmac ERP product "
            "as cutover 1, which was written when 'Backoffice' was the generic "
            "destination for everything. Adding a CMS widens the single atomic "
            "switch with no ERP writer to retire — see ADR-0004 open decision 1."
        ),
    ),
    ExcludedModule(
        distribution="dotmac-customers",
        owner="dotmac_sub",
        rationale="Account-to-Party binding for ISP customers; ERP has no such writer.",
    ),
    ExcludedModule(
        distribution="dotmac-deployment-control",
        owner="dotmac_vendor_control_plane",
        rationale="Platform-plane capability consumed over a versioned API.",
    ),
    ExcludedModule(
        distribution="dotmac-entitlement-allocation",
        owner="dotmac_vendor_control_plane",
        rationale="Commercial entitlement allocation is a Vendor CP authority.",
    ),
    ExcludedModule(
        distribution="dotmac-fiber-plant",
        owner="dotmac_sub",
        rationale="ISP outside plant; no ERP writer.",
    ),
    ExcludedModule(
        distribution="dotmac-fulfillment",
        owner="dotmac_sub",
        rationale="ISP service fulfillment saga; no ERP writer.",
    ),
    ExcludedModule(
        distribution="dotmac-inbox",
        owner="dotmac_sub",
        rationale=(
            "Sub is the only active production conversation owner. ERP is "
            "candidate demand behind ticket correspondence, not a current consumer."
        ),
    ),
    ExcludedModule(
        distribution="dotmac-inbox-operations",
        owner="dotmac_sub",
        rationale=(
            "Operator read state, routing and assignment over Inbox; no ERP writer."
        ),
    ),
    ExcludedModule(
        distribution="dotmac-integration",
        owner="dotmac_integrator",
        rationale=(
            "The independently deployed Integrator owns connector runtime and "
            "plugins. Products do not compose it."
        ),
    ),
    ExcludedModule(
        distribution="dotmac-ipam",
        owner="dotmac_sub",
        rationale="ISP address management; no ERP writer.",
    ),
    ExcludedModule(
        distribution="dotmac-licensing",
        owner="dotmac_vendor_control_plane",
        rationale=(
            "ERP verifies signed licence facts locally through the kernel; it "
            "does not own the licensing lineage."
        ),
    ),
    ExcludedModule(
        distribution="dotmac-media-observations",
        owner="pending-decision",
        rationale=(
            "PAUSED by Michael on 2026-08-18. A paused capability may not enter a "
            "frozen bill of materials."
        ),
    ),
    ExcludedModule(
        distribution="dotmac-network-access",
        owner="dotmac_sub",
        rationale="ISP network suite; no ERP writer.",
    ),
    ExcludedModule(
        distribution="dotmac-network-assurance",
        owner="dotmac_sub",
        rationale="ISP network suite; no ERP writer.",
    ),
    ExcludedModule(
        distribution="dotmac-network-control",
        owner="dotmac_sub",
        rationale="ISP network suite; no ERP writer.",
    ),
    ExcludedModule(
        distribution="dotmac-network-inventory",
        owner="dotmac_sub",
        rationale=(
            "ISP network stock and plant estate. Distinct from ERP warehouse "
            "inventory, which dotmac-inventory carries."
        ),
    ),
    ExcludedModule(
        distribution="dotmac-network-observability",
        owner="dotmac_sub",
        rationale="ISP network suite; no ERP writer.",
    ),
    ExcludedModule(
        distribution="dotmac-network-topology",
        owner="dotmac_sub",
        rationale="ISP network suite; no ERP writer.",
    ),
    ExcludedModule(
        distribution="dotmac-operational-escalations",
        owner="dotmac_sub",
        rationale="Ticket, outage and Inbox escalation policy for the ISP estate.",
    ),
    ExcludedModule(
        distribution="dotmac-platform-health",
        owner="dotmac_vendor_control_plane",
        rationale="Platform-plane capability consumed over a versioned API.",
    ),
    ExcludedModule(
        distribution="dotmac-pon-access",
        owner="dotmac_sub",
        rationale="ISP network suite; no ERP writer.",
    ),
    ExcludedModule(
        distribution="dotmac-publishing",
        owner="pending-decision",
        rationale=(
            "Marketing publication estate. See ADR-0004 open decision 1, "
            "with dotmac-content."
        ),
    ),
    ExcludedModule(
        distribution="dotmac-qualification",
        owner="dotmac_sub",
        rationale="ISP service qualification; no ERP writer.",
    ),
    ExcludedModule(
        distribution="dotmac-referrals",
        owner="dotmac_sub",
        rationale=(
            "Referral programmes and codes; Sub is cutover 1 and ERP has no writer."
        ),
    ),
    ExcludedModule(
        distribution="dotmac-release-catalog",
        owner="dotmac_vendor_control_plane",
        rationale="Release selection is a Vendor CP authority.",
    ),
    ExcludedModule(
        distribution="dotmac-remote-access",
        owner="dotmac_sub",
        rationale="ISP remote access; no ERP writer.",
    ),
    ExcludedModule(
        distribution="dotmac-reseller-management",
        owner="dotmac_sub",
        rationale="ISP reseller estate; no ERP writer.",
    ),
    ExcludedModule(
        distribution="dotmac-sales",
        owner="dotmac_sub",
        rationale=(
            "Installed only where Dotmac ERP is the named writer, and it is not: "
            "ERP's sales surface is the Sub synchronisation projection."
        ),
    ),
    ExcludedModule(
        distribution="dotmac-service-access-policy",
        owner="dotmac_sub",
        rationale="ISP service access policy; no ERP writer.",
    ),
    ExcludedModule(
        distribution="dotmac-service-catalog",
        owner="dotmac_sub",
        rationale="ISP service catalog; no ERP writer.",
    ),
    ExcludedModule(
        distribution="dotmac-service-changes",
        owner="dotmac_sub",
        rationale="ISP plan change, relocation and hold requests; no ERP writer.",
    ),
    ExcludedModule(
        distribution="dotmac-service-orders",
        owner="dotmac_sub",
        rationale="ISP installation and activation readiness; no ERP writer.",
    ),
    ExcludedModule(
        distribution="dotmac-services",
        owner="dotmac_sub",
        rationale="Realized ISP services; no ERP writer.",
    ),
    ExcludedModule(
        distribution="dotmac-sites",
        owner="pending-decision",
        rationale=(
            "Marketing site estate. See ADR-0004 open decision 1, with dotmac-content."
        ),
    ),
    ExcludedModule(
        distribution="dotmac-subscriptions",
        owner="dotmac_sub",
        rationale=(
            "Recurring commercial subscriptions for the ISP estate; no ERP writer."
        ),
    ),
    ExcludedModule(
        distribution="dotmac-support-access",
        owner="dotmac_vendor_control_plane",
        rationale="Platform-plane capability consumed over a versioned API.",
    ),
    ExcludedModule(
        distribution="dotmac-usage",
        owner="dotmac_sub",
        rationale="ISP usage collection; no ERP writer.",
    ),
    ExcludedModule(
        distribution="dotmac-usage-rating",
        owner="dotmac_sub",
        rationale="ISP usage rating; no ERP writer.",
    ),
    ExcludedModule(
        distribution="dotmac-web-analytics",
        owner="pending-decision",
        rationale=(
            "First-party web observations. See ADR-0004 open decision 1, "
            "with dotmac-content."
        ),
    ),
    ExcludedModule(
        distribution="dotmac-work-orders",
        owner="dotmac_sub",
        rationale=(
            "Physical execution of a dispatched field job. ERP has no work-order "
            "capability, so installing it would retire no writer."
        ),
    ),
)


RETAINED: Final[tuple[RetainedCapability, ...]] = (
    RetainedCapability(
        capability="trade-accounts-receivable",
        rationale=(
            "The largest unowned finance domain. dotmac-billing is operational "
            "receivables and is explicitly not statutory trade AR, so no module "
            "can carry it. Building one now would put an unbuilt module on the "
            "critical path of every step. ERP keeps it, and a shared owner stays "
            "a later product decision — ADR-0004 open decision 2."
        ),
    ),
    RetainedCapability(
        capability="customer-statements-dunning",
        rationale="Statement and reminder presentation over retained AR.",
    ),
    RetainedCapability(
        capability="treasury-cash-positioning",
        rationale=(
            "dotmac-payments owns intent and confirmation correlation, not cash "
            "position or payment execution. No module carries this."
        ),
    ),
    RetainedCapability(
        capability="consolidation-intercompany",
        rationale=(
            "app/services/finance/cons has no module owner anywhere in the census."
        ),
    ),
    RetainedCapability(
        capability="fx-revaluation",
        rationale=(
            "dotmac-fx-policy owns rate policy and selection. Period-end "
            "revaluation is an accounting consequence and stays with ERP."
        ),
    ),
    RetainedCapability(
        capability="financial-statement-presentation",
        rationale="Presentation is product-specific and may stay in the thin assembly.",
    ),
    RetainedCapability(
        capability="financial-reporting",
        rationale=(
            "app/services/finance/rpt: saved reports, execution and export "
            "presentation."
        ),
    ),
    RetainedCapability(
        capability="budgeting-planning",
        rationale="No module owner in the census.",
    ),
    RetainedCapability(
        capability="lease-accounting",
        rationale="app/services/finance/lease. IFRS 16 treatment has no module owner.",
    ),
    RetainedCapability(
        capability="ipsas-presentation",
        rationale=(
            "app/services/finance/ipsas. Public-sector presentation has no module "
            "owner."
        ),
    ),
    RetainedCapability(
        capability="monetary-coverage",
        rationale=(
            "app/services/finance/coverage.py. The kernel coverage surface has no "
            "released owner and no production consumer."
        ),
    ),
    RetainedCapability(
        capability="posting-adapters",
        rationale=(
            "The expense/inventory/fixed-asset posting adapters that turn a domain "
            "event into a journal request. Adapters are assembly work by design."
        ),
    ),
    RetainedCapability(
        capability="attendance-time",
        rationale=(
            "app/services/people/attendance. dotmac-workforce excludes attendance."
        ),
    ),
    RetainedCapability(
        capability="leave-management",
        rationale="app/services/people/leave. dotmac-workforce excludes leave.",
    ),
    RetainedCapability(
        capability="compensation-benefits",
        rationale="Grades and benefits sit beside payroll and have no module owner.",
    ),
    RetainedCapability(
        capability="performance-discipline",
        rationale="app/services/people/perf and /discipline have no module owner.",
    ),
    RetainedCapability(
        capability="training-development",
        rationale="app/services/people/training has no module owner.",
    ),
    RetainedCapability(
        capability="recruitment-careers",
        rationale=(
            "dotmac-forms carries the authored form. The requisition, candidate "
            "and offer lifecycle stays in ERP."
        ),
    ),
    RetainedCapability(
        capability="employee-self-service",
        rationale="Presentation over retained and module-owned HR facts.",
    ),
    RetainedCapability(
        capability="hr-notifications",
        rationale=(
            "HR-specific consequence routing over the retained notification surface."
        ),
    ),
    RetainedCapability(
        capability="fleet-vehicle-lifecycle",
        rationale="dotmac-assets keeps vehicle extensions product-owned by design.",
    ),
    RetainedCapability(
        capability="fleet-fuel",
        rationale="Fleet extension; no module owner.",
    ),
    RetainedCapability(
        capability="fleet-incidents",
        rationale="Fleet extension; no module owner.",
    ),
    RetainedCapability(
        capability="fleet-reservations",
        rationale="Fleet extension; no module owner.",
    ),
    RetainedCapability(
        capability="fleet-maintenance",
        rationale="Operating maintenance beside the asset register; no module owner.",
    ),
    RetainedCapability(
        capability="supplier-prequalification",
        rationale=(
            "app/services/procurement/vendor and /evaluation exceed the module's scope."
        ),
    ),
    RetainedCapability(
        capability="contract-administration",
        rationale="app/services/procurement/contract.py has no module owner.",
    ),
    RetainedCapability(
        capability="three-way-match",
        rationale=(
            "The purchase-order/receipt/AP match spans Procurement, Inventory and "
            "Payables. It is assembly composition, and it needs one named owner "
            "before step 7 can reach zero."
        ),
    ),
    RetainedCapability(
        capability="project-milestones",
        rationale="dotmac-projects excludes milestones.",
    ),
    RetainedCapability(
        capability="project-resource-planning",
        rationale="dotmac-projects excludes resource planning.",
    ),
    RetainedCapability(
        capability="project-time-entry",
        rationale="dotmac-projects excludes time entry.",
    ),
    RetainedCapability(
        capability="project-costing",
        rationale="dotmac-projects excludes costing.",
    ),
    RetainedCapability(
        capability="service-level-obligations",
        rationale=(
            "ERP computes SLA targets at read time. dotmac-response-obligations "
            "exists only off Starter main and is not in the census, so it cannot "
            "be selected; Sub is its qualifying source in any case."
        ),
    ),
    RetainedCapability(
        capability="help-knowledge-center",
        rationale="app/services/help has no module owner anywhere in the census.",
    ),
    RetainedCapability(
        capability="ai-coach-insights",
        rationale="app/services/coach. dotmac-ai-operations names no ERP consumer.",
    ),
    RetainedCapability(
        capability="operational-dashboards",
        rationale=(
            "Presentation over module-owned measures; explicitly product-specific."
        ),
    ),
    RetainedCapability(
        capability="notification-delivery",
        rationale=(
            "Delivery routing stays with the product; templates move to Template "
            "Studio."
        ),
    ),
    RetainedCapability(
        capability="email-branding",
        rationale="Tenant email presentation over the shared design system.",
    ),
    RetainedCapability(
        capability="batch-operations",
        rationale="Bulk admin actions over module-owned services; an adapter surface.",
    ),
    RetainedCapability(
        capability="infrastructure-health",
        rationale="Deployment-local health surface; not a tenant domain decision.",
    ),
    RetainedCapability(
        capability="workspace-provisioning",
        rationale=(
            "Mailcow and Nextcloud provisioning; no module owner and no tenant domain "
            "fact."
        ),
    ),
    RetainedCapability(
        capability="external-system-projections",
        rationale=(
            "What a synchronised Sub or Academy row MEANS in ERP. "
            "dotmac-app-sync carries only the transport and deduplication."
        ),
    ),
    RetainedCapability(
        capability="material-support-contract",
        rationale=(
            "The Sub material-support obligation projected onto ERP inventory; a "
            "product contract, not a reusable capability."
        ),
    ),
)


def _module_capabilities() -> tuple[str, ...]:
    return tuple(
        capability for module in SELECTED for capability in module.capabilities
    )


#: Every capability the composed Dotmac ERP must own on the day of the switch.
#: Derived, deliberately: the census IS the union of what the selected modules
#: carry and what the assembly retains, so a capability cannot be silently
#: dropped by editing one list — it has to be removed from the list that
#: actually owns it.
ERP_CAPABILITY_CENSUS: Final[frozenset[str]] = frozenset(
    _module_capabilities() + tuple(entry.capability for entry in RETAINED)
)

SELECTED_DISTRIBUTIONS: Final[frozenset[str]] = frozenset(
    module.distribution for module in SELECTED
)

EXCLUDED_DISTRIBUTIONS: Final[frozenset[str]] = frozenset(
    module.distribution for module in EXCLUDED
)

COMPOSED_DISTRIBUTIONS: Final[frozenset[str]] = frozenset(
    module.distribution for module in SELECTED if module.state == "composed"
)


# ---------------------------------------------------------------------------
# Step 2 — composition.
#
# Composing a module is a FOUR-file atomic change: the pin in `pyproject.toml`,
# the lineage in `alembic.ini`'s `version_locations`, the expected head in
# `app/migration_bindings.py`, and the row here. Three of those four have gone
# out of step before, in both directions, and none of the three noticed. The
# checks in `tests/architecture/test_bill_of_materials.py` now compare all four
# against each other, so a half-composed module fails the build instead of
# failing a migration.
# ---------------------------------------------------------------------------

#: The database effects THIS assembly supplies, and the revision that supplies
#: each one, mirrored from `app.migration_bindings`. A module never names a
#: foreign revision; it declares the effect it needs and the assembly answers.
ASSEMBLY_SUPPLIED_EFFECTS: Final[frozenset[str]] = frozenset(
    {
        "tenant_scope_catalog.v1",
        "module_database_roles.v1",
        "idempotency_ledger.v1",
        "party_person_catalog.v1",
        "outbox_relay.v1",
    }
)


@dataclass(frozen=True)
class CompositionStep:
    """One selected module's composition facts, measured from its manifest.

    `requires_effects` is the set needed for the plane ERP installs — the
    TENANT plane — so a dual-plane module's `tenant_requires` is folded in
    here rather than kept as a separate field nobody reads.
    """

    distribution: str
    tranche: int
    kernel_floor: str | None
    schema: str | None
    lineage_branch: str | None
    lineage_head: str | None
    requires_effects: tuple[str, ...]


#: Tranche 0 is composed. Tranche 1 is unblocked once the kernel is repinned.
#: Tranche 2 and 3 wait on an assembly-supplied effect that does not exist yet.
#: Tranche 4 has no installable artifact.
TRANCHE_NAMES: Final[dict[int, str]] = {
    0: "composed",
    1: "unblocked by the kernel repin",
    2: "blocked on party_person_catalog.v1 — closed 2026-08-24, no members",
    3: "blocked on outbox_relay.v1 — closed 2026-08-24, no members",
    4: "blocked on release",
}

_TENANT = "tenant_scope_catalog.v1"
_ROLES = "module_database_roles.v1"
_IDEMPOTENCY = "idempotency_ledger.v1"
_OUTBOX = "outbox_relay.v1"
_PARTY_PERSON = "party_person_catalog.v1"

COMPOSITION_PLAN: Final[tuple[CompositionStep, ...]] = (
    # -- tranche 0: composed today ------------------------------------------
    CompositionStep(
        distribution="dotmac-kernel",
        tranche=0,
        kernel_floor=None,
        schema=None,
        lineage_branch=None,
        lineage_head=None,
        requires_effects=(),
    ),
    CompositionStep(
        distribution="dotmac-ui",
        tranche=0,
        kernel_floor=None,
        schema=None,
        lineage_branch=None,
        lineage_head=None,
        requires_effects=(),
    ),
    CompositionStep(
        distribution="dotmac-files",
        tranche=0,
        kernel_floor="0.1.0a61",
        schema="mod_files",
        lineage_branch="files",
        lineage_head="fi_0001_stored_files",
        requires_effects=(_TENANT, _ROLES),
    ),
    CompositionStep(
        distribution="dotmac-imports",
        tranche=0,
        kernel_floor="0.1.0a56",
        schema="mod_imports",
        lineage_branch="imports",
        lineage_head="im_0001_import_runs",
        requires_effects=(_TENANT, _ROLES),
    ),
    CompositionStep(
        distribution="dotmac-accounting",
        tranche=0,
        kernel_floor="0.1.0a85",
        schema="mod_accounting",
        lineage_branch="accounting",
        lineage_head="ac_0001_accounting",
        requires_effects=(_TENANT, _ROLES, _IDEMPOTENCY),
    ),
    CompositionStep(
        distribution="dotmac-tax",
        tranche=0,
        kernel_floor="0.1.0a85",
        schema="mod_tax",
        lineage_branch="tax",
        lineage_head="tx_0003_result_fingerprint",
        requires_effects=(_TENANT, _ROLES),
    ),
    # -- tranche 1: released, allowlisted, every effect already supplied -----
    CompositionStep(
        distribution="dotmac-payables",
        tranche=1,
        kernel_floor="0.1.0a85",
        schema="mod_payables",
        lineage_branch="payables",
        lineage_head="pa_0001_payables",
        requires_effects=(_TENANT, _ROLES, _IDEMPOTENCY),
    ),
    CompositionStep(
        distribution="dotmac-banking",
        tranche=1,
        kernel_floor="0.1.0a85",
        schema="mod_banking",
        lineage_branch="banking",
        lineage_head="bk_0001_banking",
        requires_effects=(_TENANT, _ROLES),
    ),
    CompositionStep(
        distribution="dotmac-finance",
        tranche=1,
        kernel_floor="0.1.0a85",
        schema="mod_finance",
        lineage_branch="finance",
        lineage_head="fn_0001_asset_accounting",
        requires_effects=(_TENANT, _ROLES),
    ),
    CompositionStep(
        distribution="dotmac-assets",
        tranche=1,
        kernel_floor="0.1.0a83",
        schema="mod_assets",
        lineage_branch="assets",
        lineage_head="as_0001_assets",
        requires_effects=(_TENANT, _ROLES),
    ),
    CompositionStep(
        distribution="dotmac-inventory",
        tranche=1,
        kernel_floor="0.1.0a83",
        schema="mod_inventory",
        lineage_branch="inventory",
        lineage_head="iv_0001_inventory",
        requires_effects=(_TENANT, _ROLES),
    ),
    CompositionStep(
        distribution="dotmac-procurement",
        tranche=1,
        kernel_floor="0.1.0a85",
        schema="mod_procurement",
        lineage_branch="procurement",
        lineage_head="pc_0001_procurement",
        requires_effects=(_TENANT, _ROLES),
    ),
    CompositionStep(
        distribution="dotmac-payroll",
        tranche=1,
        kernel_floor="0.1.0a85",
        schema="mod_payroll",
        lineage_branch="payroll",
        lineage_head="py_0001_payroll",
        requires_effects=(_TENANT, _ROLES),
    ),
    CompositionStep(
        distribution="dotmac-projects",
        tranche=1,
        kernel_floor="0.1.0a85",
        schema="mod_projects",
        lineage_branch="projects",
        lineage_head="pj_0001_projects",
        requires_effects=(_TENANT, _ROLES),
    ),
    CompositionStep(
        distribution="dotmac-ticketing",
        tranche=1,
        kernel_floor="0.1.0a61",
        schema="mod_tkt",
        lineage_branch="ticketing",
        lineage_head="tk_0001_tickets",
        requires_effects=(_TENANT, _ROLES),
    ),
    CompositionStep(
        distribution="dotmac-numbering",
        tranche=0,
        kernel_floor="0.1.0a66",
        schema="mod_numbering",
        lineage_branch="numbering",
        lineage_head="nu_0001_numbering",
        requires_effects=(_TENANT, _ROLES, _IDEMPOTENCY),
    ),
    CompositionStep(
        distribution="dotmac-documents",
        tranche=1,
        kernel_floor="0.1.0a85",
        schema="mod_documents",
        lineage_branch="documents",
        lineage_head="do_0001_documents",
        requires_effects=(_TENANT, _ROLES),
    ),
    CompositionStep(
        distribution="dotmac-records",
        tranche=1,
        kernel_floor="0.1.0a85",
        schema="mod_records",
        lineage_branch="records",
        lineage_head="re_0001_records",
        requires_effects=(_TENANT, _ROLES),
    ),
    CompositionStep(
        distribution="dotmac-forms",
        tranche=1,
        kernel_floor="0.1.0a88",
        schema="mod_forms",
        lineage_branch="forms",
        lineage_head="fm_0001_forms",
        requires_effects=(_TENANT, _ROLES),
    ),
    CompositionStep(
        distribution="dotmac-workflow-runtime",
        tranche=1,
        kernel_floor="0.1.0a88",
        schema="mod_workflow",
        lineage_branch="workflow_runtime",
        lineage_head="wr_0001_runtime",
        requires_effects=(_TENANT, _ROLES),
    ),
    CompositionStep(
        distribution="dotmac-analytics",
        tranche=1,
        kernel_floor="0.1.0a85",
        schema="mod_analytics",
        lineage_branch="analytics",
        lineage_head="ay_0001_analytics",
        requires_effects=(_TENANT, _ROLES, _IDEMPOTENCY),
    ),
    CompositionStep(
        distribution="dotmac-surveys",
        tranche=1,
        kernel_floor="0.1.0a85",
        schema="mod_surveys",
        lineage_branch="surveys",
        lineage_head="sv_0001_surveys",
        requires_effects=(_TENANT, _ROLES),
    ),
    CompositionStep(
        distribution="dotmac-positioning",
        tranche=1,
        kernel_floor="0.1.0a83",
        schema="mod_pos",
        lineage_branch="positioning",
        lineage_head="po_0001_positioning",
        requires_effects=(_TENANT, _ROLES),
    ),
    CompositionStep(
        distribution="dotmac-payments",
        tranche=1,
        kernel_floor="0.1.0a91",
        schema="mod_payments",
        lineage_branch="payments",
        lineage_head="pm_0001_payment_intents",
        requires_effects=(_TENANT, _ROLES),
    ),
    CompositionStep(
        distribution="dotmac-document-rendering",
        tranche=1,
        kernel_floor="0.1.0a88",
        schema=None,
        lineage_branch=None,
        lineage_head=None,
        requires_effects=(),
    ),
    CompositionStep(
        distribution="dotmac-auth-oidc",
        tranche=1,
        kernel_floor=None,
        schema=None,
        lineage_branch=None,
        lineage_head=None,
        requires_effects=(),
    ),
    # -- tranche 2 is empty: party_person_catalog.v1 is now supplied by
    #    20260824_party_person_projection, so its three members moved to
    #    tranche 1. The tranche keeps its number and its name -- renumbering
    #    would silently rewrite what an earlier review approved.
    CompositionStep(
        distribution="dotmac-people",
        tranche=0,
        kernel_floor="0.1.0a98",
        schema="mod_people",
        lineage_branch="people",
        lineage_head="pe_0001_people_directory",
        requires_effects=(_TENANT, _ROLES, _PARTY_PERSON),
    ),
    CompositionStep(
        distribution="dotmac-party",
        tranche=1,
        kernel_floor="0.1.0a85",
        schema="mod_party",
        lineage_branch="party",
        lineage_head="pt_0001_party_context",
        requires_effects=(_TENANT, _ROLES, _PARTY_PERSON),
    ),
    CompositionStep(
        distribution="dotmac-expenses",
        tranche=1,
        kernel_floor="0.1.0a85",
        schema="mod_expenses",
        lineage_branch="expenses",
        lineage_head="ex_0001_expenses",
        requires_effects=(_TENANT, _ROLES, _PARTY_PERSON),
    ),
    # -- tranche 3 is empty: outbox_relay.v1 is now supplied by
    #    20260824_outbox_relay, so both members moved to tranche 1. The
    #    tranche keeps its number for the same reason tranche 2 does.
    CompositionStep(
        distribution="dotmac-approvals",
        tranche=1,
        kernel_floor="0.1.0a67",
        schema="mod_approvals",
        lineage_branch="approvals",
        lineage_head="ap_0002_outbox_relay",
        requires_effects=(_TENANT, _ROLES, _OUTBOX),
    ),
    CompositionStep(
        distribution="dotmac-durable-timers",
        tranche=1,
        kernel_floor="0.1.0a72",
        schema="mod_timers",
        lineage_branch="durable_timers",
        lineage_head="dt_0001_durable_timers",
        requires_effects=(_TENANT, _ROLES, _OUTBOX),
    ),
    # -- tranche 4: no installable artifact --------------------------------
    CompositionStep(
        distribution="dotmac-workforce",
        tranche=4,
        kernel_floor=None,
        schema="mod_workforce",
        lineage_branch="workforce",
        lineage_head="wf_0001_workforce",
        requires_effects=(_TENANT, _ROLES),
    ),
    CompositionStep(
        distribution="dotmac-fx-policy",
        tranche=4,
        kernel_floor=None,
        schema="mod_fx_policy",
        lineage_branch="fx_policy",
        lineage_head="fx_0001_fx_policy",
        requires_effects=(_TENANT, _ROLES),
    ),
    CompositionStep(
        distribution="dotmac-template-studio",
        tranche=4,
        kernel_floor=None,
        schema="mod_tstudio",
        lineage_branch="template_studio",
        lineage_head="ts_0001_templates",
        requires_effects=(_TENANT, _ROLES),
    ),
    CompositionStep(
        distribution="dotmac-app-sync",
        tranche=4,
        kernel_floor=None,
        schema=None,
        lineage_branch=None,
        lineage_head=None,
        requires_effects=(),
    ),
)

#: The highest kernel floor the SELECTED set demands, from `dotmac-people` a2.
#: ERP pins that exact 0.1.0a98 floor, so every selected release is loadable.
#: This is a measured property of the plan, restated as a constant so the
#: repin is a visible obligation rather than something discovered by a
#: resolver error.
KERNEL_FLOOR_DEMANDED_BY_SELECTION: Final = "0.1.0a98"

#: Effects a selected module needs that this assembly does not supply. Each is
#: a new assembly migration ERP must write before the modules listed can be
#: composed at all — not a repin, not a version bump.
#: Empty since 2026-08-24: the assembly supplies every effect the frozen
#: selection declares. The map stays, and the derived-versus-declared check
#: stays with it — the next module added to the bill of materials may well
#: need an effect nothing provides, and an absent map would answer that
#: question with silence.
MISSING_EFFECTS: Final[dict[str, tuple[str, ...]]] = {}
