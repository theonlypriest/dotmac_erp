"""System-wide single-source-of-truth relationship registry.

Names the service boundaries that own domain decisions in dotmac_erp,
mirroring the dotmac_sub registry pattern. Declarative: routes, tasks, and
event handlers use it as the architectural map; the liveness architecture
test keeps every named owner real. Seeded from the Phase-0 platform adoption
ledger (docs/PLATFORM_ADOPTION_LEDGER.md) — entries record the AS-BUILT
owner; where ownership is fragmented today, the notes say so instead of
pretending otherwise. Every future slice that adds or moves an owner must
update this registry in the same change.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SOTService:
    name: str
    module: str
    owns: tuple[str, ...]
    depends_on: tuple[str, ...] = ()
    notes: str | None = None


@dataclass(frozen=True)
class DomainSOT:
    domain: str
    services: tuple[SOTService, ...]
    entrypoints: tuple[str, ...]
    rule: str


DOMAIN_SOT_RELATIONSHIPS: tuple[DomainSOT, ...] = (
    DomainSOT(
        domain="organization_tenancy",
        services=(
            SOTService(
                name="tenancy.mapping",
                module="app.tenancy",
                owns=("identity-preserving Organization-to-Tenant scope mapping",),
                notes=(
                    "core_org.organization remains authoritative; tenant_id is "
                    "the same UUID and the hosted tenants row is a projection."
                ),
            ),
            SOTService(
                name="tenancy.projection",
                module="app.services.tenant_projection",
                owns=(
                    "Organization-to-kernel-Tenant projection writes and repair",
                    "stable ERP tenant slug derivation",
                    "Organization deletion tenant tombstones",
                ),
                depends_on=("tenancy.mapping",),
                notes=(
                    "core_org.organization remains authoritative; the projection "
                    "mutates in the caller's ERP-owned transaction."
                ),
            ),
            SOTService(
                name="tenancy.context",
                module="app.db.session_context",
                owns=(
                    "organization and module-tenant request/task context priming",
                    "one-session-per-org rule for SET LOCAL lifetimes",
                    "cross-org session escape hatch",
                ),
                depends_on=(
                    "tenancy.mapping",
                    "tenancy.projection",
                    "tenancy.orm_filter",
                    "tenancy.rls",
                ),
                notes=(
                    "All enforcement inputs must be primed together; the "
                    "canonical deps (get_db_with_org, get_db_for_org, "
                    "session_for_org) are the only sanctioned entry points."
                ),
            ),
            SOTService(
                name="tenancy.orm_filter",
                module="app.db.org_listener",
                owns=("fail-closed SELECT-time organization filtering",),
                notes="Layer 1; UPDATE/DELETE isolation is RLS's job.",
            ),
            SOTService(
                name="tenancy.rls",
                module="app.rls",
                owns=(
                    "PostgreSQL tenant-scope GUC context "
                    "(app.current_organization_id, app.current_tenant)",
                ),
                notes=(
                    "ERP and shared-module scope GUCs are set atomically; "
                    "runtime code has no user-settable RLS bypass."
                ),
            ),
        ),
        entrypoints=("app.api.deps", "app.web.deps", "app.db.session_context"),
        rule=(
            "Organization scoping is one identity enforced by the ORM listener, "
            "ERP RLS and shared-module RLS. Canonical entry points set every "
            "input together or not at all."
        ),
    ),
    DomainSOT(
        domain="identity_access",
        services=(
            SOTService(
                name="auth.flow",
                module="app.services.auth_flow",
                owns=(
                    "login/refresh/logout and MFA flows",
                    "session token issue, rotation, and hashing",
                    "RBAC claim baking into tokens",
                ),
            ),
            SOTService(
                name="auth.guards",
                module="app.services.auth_dependencies",
                owns=(
                    "request authentication and org resolution",
                    "role/permission guard evaluation",
                ),
                depends_on=("auth.rbac",),
                notes=(
                    "The permission-check join is currently duplicated across "
                    "the api and web guard families (ledger finding 2) — a "
                    "single has_permission owner is the consolidation target."
                ),
            ),
            # No external-identity protocol owner is registered: ERP's
            # unshipped OIDC adapter was deleted, and the registry records
            # as-built owners only. Reintroduction adopts the released
            # dotmac-auth-oidc package and re-registers an owner here.
            SOTService(
                name="identity.party_projection",
                module="app.services.party_projection",
                owns=(
                    "Person-to-kernel-Party catalogue projection writes",
                    "Person-to-kernel-PartyPerson profile projection writes",
                    "person party retirement on Person deletion",
                ),
                notes=(
                    "public.people remains authoritative; the projection "
                    "supplies party_person_catalog.v1 and mutates in the "
                    "caller's ERP-owned transaction. parties.id IS people.id, "
                    "so the projection needs no mapping and is rebuildable "
                    "from the source alone. Authority moves when dotmac-party "
                    "and dotmac-people are composed and cut over."
                ),
            ),
            SOTService(
                name="auth.rbac",
                module="app.services.rbac",
                owns=(
                    "role/permission/grant CRUD",
                    "persisted permission catalogue and role grants",
                ),
                depends_on=("auth.rbac_declarations",),
                notes=(
                    "Tables are GLOBAL, not org-scoped; safety rests on the "
                    "one-person-one-org invariant (ledger finding 2). The "
                    "scope decision is explicit kernel-adoption work."
                ),
            ),
            SOTService(
                name="auth.rbac_declarations",
                module="app.authz.profile",
                owns=(
                    "product role definitions",
                    "assembly-owned baseline role-permission bundles",
                ),
                notes=(
                    "Modules own permission definitions. The full seed imports "
                    "these product declarations; startup validates their internal "
                    "references, and deployments materialize approved bundles "
                    "through scoped additive migrations."
                ),
            ),
        ),
        entrypoints=("app.api.auth_flow", "app.web.auth", "app.api.rbac"),
        rule=(
            "Person is the single ERP login identity; credentials, sessions, "
            "MFA, and API keys resolve to person_id. ERP accepts no external "
            "identity assertion today. External authorization claims and "
            "counterparties (Customer, Supplier) are not ERP identities or "
            "permission owners."
        ),
    ),
    DomainSOT(
        domain="configuration_control",
        services=(
            SOTService(
                name="control.settings",
                module="app.services.domain_settings",
                owns=(
                    "domain_settings writes with history",
                    "org-override/global/default resolution",
                ),
                depends_on=("control.settings_spec",),
                notes=(
                    "Several modules still construct DomainSetting rows "
                    "directly, bypassing history (ledger finding 5) — those "
                    "callers are migration targets, not co-owners."
                ),
            ),
            SOTService(
                name="control.settings_spec",
                module="app.services.settings_spec",
                owns=("setting specs, validation, and coercion",),
            ),
            SOTService(
                name="control.feature_flags",
                module="app.services.feature_flag_service",
                owns=(
                    "flag resolution (org row, global row, registry default)",
                    "flag-gated router/module availability",
                ),
                depends_on=("control.settings",),
            ),
        ),
        entrypoints=("app.api.settings", "app.services.module_settings_web"),
        rule=(
            "Settings are data with one canonical writer and recorded "
            "history; flags never substitute for authorization."
        ),
    ),
    DomainSOT(
        domain="audit_trail",
        services=(
            SOTService(
                name="audit.business_log",
                module="app.services.audit_dispatcher",
                owns=("manual business audit events into audit.audit_log",),
                notes=(
                    "AS-BUILT FRAGMENTATION (ledger finding 1): the HTTP "
                    "middleware writes audit_events via Celery, the ORM flush "
                    "listener and the field tracker write independently. One "
                    "writer interface must be named before kernel audit "
                    "adoption; this entry records today's manual-path owner, "
                    "it does not bless the fragmentation."
                ),
            ),
        ),
        entrypoints=("app.main", "app.services.audit_listener"),
        rule=(
            "Target: one audit writer interface. Until consolidation, no NEW "
            "audit writer may be added outside the four existing mechanisms."
        ),
    ),
    DomainSOT(
        domain="general_ledger",
        services=(
            SOTService(
                name="gl.poster",
                module="app.services.finance.gl.ledger_posting",
                owns=(
                    "posted ledger line construction (single site)",
                    "balance and functional-amount validation at posting",
                ),
                depends_on=("gl.period_guard", "fx.rates"),
                notes="docs/gl_source_of_truth.md is the domain contract.",
            ),
            SOTService(
                name="gl.period_guard",
                module="app.services.finance.gl.period_guard",
                owns=("open/soft-close/hard-close posting gates",),
            ),
            SOTService(
                name="platform.sequences",
                module="app.services.finance.platform.sequence",
                owns=("document numbering for all finance documents",),
            ),
            SOTService(
                name="fx.rates",
                module="app.services.finance.platform.fx",
                owns=(
                    "exchange-rate lookup and conversion",
                    "functional-currency resolution",
                ),
            ),
            SOTService(
                name="tax.policy",
                module="app.services.finance.tax.tax_calculation",
                owns=("tax computation from effective-dated TaxCode policy",),
                notes=(
                    "Rate stored as ratio, not percentage — the sub tax "
                    "contract doc governs the sub-facing boundary."
                ),
            ),
        ),
        entrypoints=("app.services.finance.posting.base",),
        rule=(
            "Documents reach the GL only through posting adapters with "
            "deterministic idempotency keys; posted lines are immutable and "
            "corrections are new journals; balances are derived cache."
        ),
    ),
    DomainSOT(
        domain="payment_execution",
        services=(
            SOTService(
                name="payments.intent_lifecycle",
                module="app.services.finance.payments.payment_service",
                owns=(
                    "PaymentIntent.status - every transition, from every trigger",
                    "transfer initiation, completion, failure and reversal",
                    "scheduled reconciliation of in-flight transfers",
                    "the distinction between an observed verdict and an "
                    "unobserved outcome (INDETERMINATE + unresolved_since)",
                    "resolution of unresolved payouts - the only writer that "
                    "may move an intent out of INDETERMINATE",
                ),
                notes=(
                    "Sole writer of payments.payment_intent.status. The webhook "
                    "receiver, the API routes and both scheduled reconcilers are "
                    "adapters over this service; none of them decides a status "
                    "itself. It also owns what the column may CLAIM: FAILED "
                    "requires that Paystack answered, and an outcome that was "
                    "not observed is INDETERMINATE, never FAILED or EXPIRED "
                    "(ADR-0007, adopting dotmac_starter_mt ADR-0032). Enforced "
                    "by tests/architecture/"
                    "test_payment_intent_status_single_owner.py and "
                    "tests/architecture/test_unobserved_is_not_a_verdict.py. "
                    "Implemented and tested; production enablement unconfirmed."
                ),
            ),
            SOTService(
                name="payments.provider_transport",
                module="app.services.finance.payments.paystack_client",
                owns=(
                    "Paystack HTTP transport and response parsing",
                    "the transport-failure vs provider-verdict distinction "
                    "(PaystackUnreachable vs PaystackError)",
                ),
                notes=(
                    "Transport only: it never touches an intent. Its one "
                    "authority is telling callers whether Paystack ANSWERED. "
                    "Every httpx.RequestError site and every 5xx raises "
                    "PaystackUnreachable; plain PaystackError means a parsed "
                    "provider refusal. Collapsing the two is what let a "
                    "connect timeout be recorded as a failed payout."
                ),
            ),
            SOTService(
                name="payments.webhook_intake",
                module="app.services.finance.payments.webhook_service",
                owns=(
                    "Paystack webhook signature verification and replay refusal",
                    "webhook event to payment-intent dispatch",
                ),
                depends_on=("payments.intent_lifecycle",),
                notes=(
                    "Transport only: it validates and dispatches, and never "
                    "writes an intent status of its own."
                ),
            ),
        ),
        entrypoints=(
            "app.api.finance.payments",
            "app.tasks.expense",
        ),
        rule=(
            "One service decides what a payment intent's status is, and it may "
            "only claim what was observed. Webhooks, routes and schedulers "
            "validate, authorize and delegate; a second writer is how a settled "
            "transfer gets reopened or an in-flight payout gets stamped "
            "expired, and an unobserved outcome recorded as FAILED is how a "
            "payout that may already have gone gets sent again."
        ),
    ),
    DomainSOT(
        domain="platform_events",
        services=(
            SOTService(
                name="events.outbox",
                module="app.services.finance.platform.outbox_publisher",
                owns=(
                    "transactional event publication and claim/lease, "
                    "retry, dead-letter, and replay state",
                ),
                notes=(
                    "Applied-result semantics: the relay task owns "
                    "commit/rollback (publisher methods flush only); "
                    "delivery is claim/deliver/settle with token-gated "
                    "settlement; unknown events dead-letter as unsupported "
                    "unless declared no-consequence; replay of DEAD events "
                    "is audited; the outbox reconciler repairs GL balance "
                    "projection drift via the canonical rebuild writer."
                ),
            ),
            # The kernel relay planes (public.outbox_events,
            # public.platform_outbox_events) deliberately have NO entry here.
            # This registry names live runtime owners, and 20260824_outbox_relay
            # supplies schema and privilege only — there is no ERP service
            # deciding anything on those tables, and naming a migration as an
            # owner would be a name that cannot be imported or reached. The
            # boundary it creates is recorded in this domain's rule instead.
            SOTService(
                name="events.hooks",
                module="app.services.hooks.registry",
                owns=("outbound webhook registration and emission",),
            ),
        ),
        entrypoints=("app.tasks.outbox_relay", "app.tasks.hooks"),
        rule=(
            "Cross-module consequences ride the outbox inside the business "
            "transaction; handlers never commit; external delivery goes "
            "through service hooks with signed, retried delivery. ERP business "
            "events ride platform.event_outbox; composed-module events ride "
            "the kernel relay planes. One authority each, neither reading the "
            "other's rows."
        ),
    ),
    DomainSOT(
        domain="commercial_licensing",
        services=(
            SOTService(
                name="licensing.enforcement",
                module="app.licensing.enforcement",
                owns=(
                    "startup and periodic license gates",
                    "licensed-module and limit checks",
                ),
                notes=(
                    "The embedded verification key is a placeholder at the "
                    "ledger's recon pin (finding 3) — a real key or an "
                    "explicit gate-off decision is required."
                ),
            ),
        ),
        entrypoints=("app.startup", "app.tasks.license"),
        rule="License checks gate module availability, never data integrity.",
    ),
    DomainSOT(
        domain="external_sync",
        services=(
            SOTService(
                name="sync.dotmac_sub",
                module="app.services.dotmac_sub.sync",
                owns=(
                    "Sub billing-fact ingestion (AR) and GL projection",
                    "reverse-and-repost on posted-invoice changes",
                ),
                notes=(
                    "Pull-based per the checked-in contract: Sub owns ISP "
                    "billing facts, ERP owns accounting; webhook ingress "
                    "feeds the same service — no second decision path. The "
                    "adapter never promotes Sub's customer tax identifier into "
                    "ERP's locally governed customer tax-identity field."
                ),
            ),
            SOTService(
                name="sync.sub_operational_context",
                module="app.services.sync.sub.projects",
                owns=(
                    "version-2 Sub operational-context intake",
                    "ERP project, ticket, project-task, and work-order projections",
                    "organization-scoped source-ID upsert mappings",
                ),
                notes=(
                    "Sub owns operational lifecycle decisions. ERP projections are "
                    "rebuildable context for finance and employee-expense links; "
                    "the /sync/sub/bulk route delegates to the source-neutral "
                    "idempotent projection service."
                ),
            ),
            SOTService(
                name="inventory.material_support",
                module="app.services.inventory.material_support",
                owns=(
                    "Sub material-support intake and immutable replay contract",
                    "ERP material-support outcome lookup",
                    "routing accepted support needs through ERP inventory policy",
                ),
                depends_on=("sync.sub_procurement",),
                notes=(
                    "Sub owns the service work order and material need; ERP owns "
                    "warehouse, stock, serial, fiscal-period, and issue decisions. "
                    "The source-qualified opaque reference stores the immutable "
                    "Sub request UUID, not an external lifecycle. See "
                    "docs/dotmac_sub_material_support_contract.md."
                ),
            ),
            SOTService(
                name="sync.sub_procurement",
                module="app.services.sync.sub.procurement",
                owns=(
                    "Sub material, PO, and purchase-invoice intake mappings",
                    "material-request inventory policy adapter",
                ),
                notes=(
                    "REPAIR-FIRST (ledger finding 9): the #118 money-bug "
                    "class lives on this edge; no extraction or convergence "
                    "until closed. Sub material requests enter through "
                    "inventory.material_support; external delivery belongs to "
                    "Integrator, not this service."
                ),
            ),
        ),
        entrypoints=(
            "app.tasks.dotmac_sub",
            "app.api.dotmac_sub",
            "app.api.sync.dotmac_sub",
            "app.tasks.expense",
        ),
        rule=(
            "External systems are transports or explicitly contracted "
            "authorities; local mirrors are rebuildable projections with "
            "reconciliation paths. ERP remains independently replaceable: no "
            "direct external database access, cross-system foreign keys, shared "
            "transactions, or shared business-domain runtime."
        ),
    ),
    DomainSOT(
        domain="bulk_imports",
        services=(
            SOTService(
                name="imports.run_ledger",
                module="dotmac_imports",
                owns=(
                    "durable import runs, partition claims and checkpoints",
                    "minimised per-row outcomes and promotion lifecycle",
                ),
                notes=(
                    "Reusable mechanism only; it never owns ERP field meaning, "
                    "validation, duplicate policy or domain mutation."
                ),
            ),
            SOTService(
                name="imports.customer_port",
                module="app.services.finance.import_export.durable_customers",
                owns=(
                    "customer CSV field vocabulary and typed validation",
                    "customer import duplicate policy and canonical mutation port",
                    "legacy-to-durable dry-run parity refusal",
                ),
                depends_on=("imports.run_ledger", "platform.storage"),
            ),
        ),
        entrypoints=(
            "app.api.finance.import_export",
            "app.tasks.imports",
        ),
        rule=(
            "The module owns resumable mechanics; ERP owns what a customer row "
            "means and applies it only through the canonical customer service."
        ),
    ),
    DomainSOT(
        domain="platform_services",
        services=(
            SOTService(
                name="platform.storage",
                module="app.services.storage",
                owns=("object storage access (single owner)",),
            ),
            SOTService(
                name="platform.secrets",
                module="app.services.secrets",
                owns=("OpenBao pointer resolution for settings/secrets",),
                notes="Settings hold pointers, never secret values.",
            ),
            SOTService(
                name="platform.notifications",
                module="app.services.notification",
                owns=("in-app and email notification dispatch",),
            ),
        ),
        entrypoints=("app.api.files",),
        rule="One owner per platform capability; callers never re-implement.",
    ),
)


def all_services() -> tuple[SOTService, ...]:
    return tuple(
        service for domain in DOMAIN_SOT_RELATIONSHIPS for service in domain.services
    )
