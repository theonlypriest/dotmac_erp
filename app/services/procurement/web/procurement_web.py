"""
Procurement Web Service - Context builders for HTML routes.

Provides methods to build template context for procurement management pages.
"""

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.procurement.enums import (
    ContractStatus,
    EvaluationStatus,
    PlanItemStatus,
    PrequalificationStatus,
    ProcurementMethod,
    ProcurementPlanStatus,
    QuotationResponseStatus,
    RequisitionStatus,
    RFQStatus,
    UrgencyLevel,
)
from app.models.procurement.rfq import RequestForQuotation
from app.services.common import coerce_uuid
from app.services.common_filters import build_active_filters
from app.services.procurement.contract import ContractService
from app.services.procurement.evaluation import BidEvaluationService
from app.services.procurement.procurement_plan import ProcurementPlanService
from app.services.procurement.quotation import QuotationResponseService
from app.services.procurement.requisition import RequisitionService
from app.services.procurement.rfq import RFQService
from app.services.procurement.vendor import VendorPrequalificationService
from app.services.recent_activity import get_recent_activity_for_record

logger = logging.getLogger(__name__)

# Badge CSS classes for each color used in status labels.
# Full class strings are required so Tailwind JIT can find them at build time.
_BADGE = {
    "slate": "bg-slate-100 text-slate-700 dark:bg-slate-900/30 dark:text-slate-400",
    "blue": "bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400",
    "emerald": "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400",
    "teal": "bg-teal-100 text-teal-700 dark:bg-teal-900/30 dark:text-teal-400",
    "gray": "bg-gray-100 text-gray-700 dark:bg-gray-900/30 dark:text-gray-400",
    "indigo": "bg-indigo-100 text-indigo-700 dark:bg-indigo-900/30 dark:text-indigo-400",
    "rose": "bg-rose-100 text-rose-700 dark:bg-rose-900/30 dark:text-rose-400",
    "amber": "bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400",
    "red": "bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400",
}

# Status display labels: (display_text, badge_css_classes)
PLAN_STATUS_LABELS = {
    ProcurementPlanStatus.DRAFT: ("Draft", _BADGE["slate"]),
    ProcurementPlanStatus.SUBMITTED: ("Submitted", _BADGE["blue"]),
    ProcurementPlanStatus.APPROVED: ("Approved", _BADGE["emerald"]),
    ProcurementPlanStatus.ACTIVE: ("Active", _BADGE["teal"]),
    ProcurementPlanStatus.CLOSED: ("Closed", _BADGE["gray"]),
}

REQUISITION_STATUS_LABELS = {
    RequisitionStatus.DRAFT: ("Draft", _BADGE["slate"]),
    RequisitionStatus.SUBMITTED: ("Submitted", _BADGE["blue"]),
    RequisitionStatus.BUDGET_VERIFIED: ("Budget Verified", _BADGE["indigo"]),
    RequisitionStatus.APPROVED: ("Approved", _BADGE["emerald"]),
    RequisitionStatus.CONVERTED: ("Converted", _BADGE["teal"]),
    RequisitionStatus.REJECTED: ("Rejected", _BADGE["rose"]),
    RequisitionStatus.CANCELLED: ("Cancelled", _BADGE["gray"]),
}

RFQ_STATUS_LABELS = {
    RFQStatus.DRAFT: ("Draft", _BADGE["slate"]),
    RFQStatus.PUBLISHED: ("Published", _BADGE["blue"]),
    RFQStatus.CLOSED: ("Closed", _BADGE["amber"]),
    RFQStatus.EVALUATED: ("Evaluated", _BADGE["indigo"]),
    RFQStatus.AWARDED: ("Awarded", _BADGE["emerald"]),
    RFQStatus.CANCELLED: ("Cancelled", _BADGE["gray"]),
}

CONTRACT_STATUS_LABELS = {
    ContractStatus.DRAFT: ("Draft", _BADGE["slate"]),
    ContractStatus.ACTIVE: ("Active", _BADGE["emerald"]),
    ContractStatus.COMPLETED: ("Completed", _BADGE["teal"]),
    ContractStatus.TERMINATED: ("Terminated", _BADGE["rose"]),
    ContractStatus.EXPIRED: ("Expired", _BADGE["gray"]),
}

PREQUALIFICATION_STATUS_LABELS = {
    PrequalificationStatus.PENDING: ("Pending", _BADGE["amber"]),
    PrequalificationStatus.UNDER_REVIEW: ("Under Review", _BADGE["blue"]),
    PrequalificationStatus.QUALIFIED: ("Qualified", _BADGE["emerald"]),
    PrequalificationStatus.DISQUALIFIED: ("Disqualified", _BADGE["rose"]),
    PrequalificationStatus.EXPIRED: ("Expired", _BADGE["gray"]),
    PrequalificationStatus.BLACKLISTED: ("Blacklisted", _BADGE["red"]),
}

RESPONSE_STATUS_LABELS = {
    QuotationResponseStatus.RECEIVED: ("Received", _BADGE["blue"]),
    QuotationResponseStatus.UNDER_EVALUATION: ("Under Evaluation", _BADGE["amber"]),
    QuotationResponseStatus.ACCEPTED: ("Accepted", _BADGE["emerald"]),
    QuotationResponseStatus.REJECTED: ("Rejected", _BADGE["rose"]),
}

EVALUATION_STATUS_LABELS = {
    EvaluationStatus.DRAFT: ("Draft", _BADGE["slate"]),
    EvaluationStatus.IN_PROGRESS: ("In Progress", _BADGE["amber"]),
    EvaluationStatus.COMPLETED: ("Completed", _BADGE["teal"]),
    EvaluationStatus.APPROVED: ("Approved", _BADGE["emerald"]),
}


class ProcurementWebService:
    """Web service methods for procurement management pages."""

    def __init__(self, db: Session):
        self.db = db

    # ─────────────────────────────────────────────────────────────
    # Dashboard
    # ─────────────────────────────────────────────────────────────

    def dashboard_context(self, organization_id: UUID) -> dict[str, Any]:
        """Build context for procurement dashboard page."""
        org_id = coerce_uuid(organization_id)

        plan_service = ProcurementPlanService(self.db)
        req_service = RequisitionService(self.db)
        rfq_service = RFQService(self.db)
        contract_service = ContractService(self.db)

        plan_summary = plan_service.get_summary(org_id)
        pending_reqs, pending_req_count = req_service.list_requisitions(
            org_id,
            status="SUBMITTED",
            limit=5,
        )
        open_rfqs, open_rfq_count = rfq_service.list_rfqs(
            org_id,
            status="PUBLISHED",
            limit=5,
        )
        active_contracts, active_contract_count = contract_service.list_contracts(
            org_id,
            status="ACTIVE",
            limit=5,
        )

        return {
            "plan_summary": plan_summary,
            "pending_requisitions": pending_reqs,
            "pending_req_count": pending_req_count,
            "open_rfqs": open_rfqs,
            "open_rfq_count": open_rfq_count,
            "active_contracts": active_contracts,
            "active_contract_count": active_contract_count,
            "status_labels": PLAN_STATUS_LABELS,
            "req_status_labels": REQUISITION_STATUS_LABELS,
            "rfq_status_labels": RFQ_STATUS_LABELS,
            "contract_status_labels": CONTRACT_STATUS_LABELS,
        }

    # ─────────────────────────────────────────────────────────────
    # Plans
    # ─────────────────────────────────────────────────────────────

    def plan_list_context(
        self,
        organization_id: UUID,
        status: str | None = None,
        fiscal_year: str | None = None,
        search: str | None = None,
        offset: int = 0,
        limit: int = 25,
    ) -> dict[str, Any]:
        """Build context for plan list page."""
        service = ProcurementPlanService(self.db)
        plans, total = service.list_plans(
            organization_id,
            status=status,
            fiscal_year=fiscal_year,
            search=search,
            offset=offset,
            limit=limit,
        )
        active_filters = build_active_filters(
            params={"status": status, "fiscal_year": fiscal_year, "search": search},
            labels={"search": "Search"},
        )
        return {
            "plans": plans,
            "total": total,
            "offset": offset,
            "limit": limit,
            "page": (offset // limit) + 1 if limit else 1,
            "total_pages": max(1, (total + limit - 1) // limit) if limit else 1,
            "search": search or "",
            "filter_status": status,
            "filter_fiscal_year": fiscal_year,
            "status_labels": PLAN_STATUS_LABELS,
            "plan_statuses": list(ProcurementPlanStatus),
            "active_filters": active_filters,
        }

    def plan_detail_context(
        self,
        organization_id: UUID,
        plan_id: UUID,
    ) -> dict[str, Any]:
        """Build context for plan detail page."""
        service = ProcurementPlanService(self.db)
        plan = service.get_by_id(organization_id, plan_id)
        if not plan:
            from app.services.common import NotFoundError

            raise NotFoundError("Procurement plan not found")

        return {
            "plan": plan,
            "items": plan.items,
            "status_labels": PLAN_STATUS_LABELS,
            "item_statuses": list(PlanItemStatus),
            "procurement_methods": list(ProcurementMethod),
            "recent_activity": get_recent_activity_for_record(
                self.db,
                organization_id,
                record=plan,
                limit=10,
            ),
        }

    def plan_form_context(
        self,
        organization_id: UUID,
    ) -> dict[str, Any]:
        """Build context for plan create/edit form."""
        return {
            "procurement_methods": list(ProcurementMethod),
            "quarters": [1, 2, 3, 4],
            "categories": ["Goods", "Works", "Services", "Consulting"],
        }

    # ─────────────────────────────────────────────────────────────
    # Requisitions
    # ─────────────────────────────────────────────────────────────

    def requisition_list_context(
        self,
        organization_id: UUID,
        status: str | None = None,
        urgency: str | None = None,
        search: str | None = None,
        offset: int = 0,
        limit: int = 25,
    ) -> dict[str, Any]:
        """Build context for requisition list page."""
        service = RequisitionService(self.db)
        requisitions, total = service.list_requisitions(
            organization_id,
            status=status,
            urgency=urgency,
            search=search,
            offset=offset,
            limit=limit,
        )
        active_filters = build_active_filters(
            params={"status": status, "urgency": urgency, "search": search},
            labels={"search": "Search"},
        )
        return {
            "requisitions": requisitions,
            "total": total,
            "total_count": total,
            "offset": offset,
            "limit": limit,
            "page": (offset // limit) + 1 if limit else 1,
            "total_pages": max(1, (total + limit - 1) // limit) if limit else 1,
            "search": search or "",
            "filter_status": status,
            "filter_urgency": urgency,
            "status_labels": REQUISITION_STATUS_LABELS,
            "req_statuses": list(RequisitionStatus),
            "urgency_levels": list(UrgencyLevel),
            "active_filters": active_filters,
        }

    def requisition_detail_context(
        self,
        organization_id: UUID,
        requisition_id: UUID,
    ) -> dict[str, Any]:
        """Build context for requisition detail page."""
        service = RequisitionService(self.db)
        requisition = service.get_by_id(organization_id, requisition_id)
        if not requisition:
            from app.services.common import NotFoundError

            raise NotFoundError("Requisition not found")

        return {
            "requisition": requisition,
            "lines": requisition.lines,
            "status_labels": REQUISITION_STATUS_LABELS,
            "urgency_levels": list(UrgencyLevel),
            "recent_activity": get_recent_activity_for_record(
                self.db,
                organization_id,
                record=requisition,
                limit=10,
            ),
        }

    def requisition_form_context(
        self,
        organization_id: UUID,
    ) -> dict[str, Any]:
        """Build context for requisition create/edit form."""
        return {
            "urgency_levels": list(UrgencyLevel),
        }

    # ─────────────────────────────────────────────────────────────
    # RFQs
    # ─────────────────────────────────────────────────────────────

    def rfq_list_context(
        self,
        organization_id: UUID,
        status: str | None = None,
        procurement_method: str | None = None,
        search: str | None = None,
        offset: int = 0,
        limit: int = 25,
    ) -> dict[str, Any]:
        """Build context for RFQ list page."""
        service = RFQService(self.db)
        rfqs, total = service.list_rfqs(
            organization_id,
            status=status,
            procurement_method=procurement_method,
            search=search,
            offset=offset,
            limit=limit,
        )
        active_filters = build_active_filters(
            params={
                "status": status,
                "method": procurement_method,
                "search": search,
            },
            labels={"method": "Method", "search": "Search"},
        )
        return {
            "rfqs": rfqs,
            "total": total,
            "total_count": total,
            "offset": offset,
            "limit": limit,
            "page": (offset // limit) + 1 if limit else 1,
            "total_pages": max(1, (total + limit - 1) // limit) if limit else 1,
            "search": search or "",
            "filter_status": status,
            "filter_method": procurement_method,
            "status_labels": RFQ_STATUS_LABELS,
            "rfq_statuses": list(RFQStatus),
            "procurement_methods": list(ProcurementMethod),
            "active_filters": active_filters,
        }

    def rfq_detail_context(
        self,
        organization_id: UUID,
        rfq_id: UUID,
    ) -> dict[str, Any]:
        """Build context for RFQ detail page."""
        rfq_service = RFQService(self.db)
        quot_service = QuotationResponseService(self.db)

        rfq = rfq_service.get_by_id(organization_id, rfq_id)
        if not rfq:
            from app.services.common import NotFoundError

            raise NotFoundError("RFQ not found")

        responses = quot_service.list_for_rfq(organization_id, rfq_id)

        return {
            "rfq": rfq,
            "invitations": rfq.invitations,
            "responses": responses,
            "status_labels": RFQ_STATUS_LABELS,
            "response_status_labels": RESPONSE_STATUS_LABELS,
            "procurement_methods": list(ProcurementMethod),
            "recent_activity": get_recent_activity_for_record(
                self.db,
                organization_id,
                record=rfq,
                limit=10,
            ),
        }

    def rfq_form_context(
        self,
        organization_id: UUID,
    ) -> dict[str, Any]:
        """Build context for RFQ create/edit form."""
        return {
            "procurement_methods": list(ProcurementMethod),
        }

    # ─────────────────────────────────────────────────────────────
    # Evaluations
    # ─────────────────────────────────────────────────────────────

    def evaluation_list_context(
        self,
        organization_id: UUID,
        status: str | None = None,
        search: str | None = None,
        offset: int = 0,
        limit: int = 25,
    ) -> dict[str, Any]:
        """Build context for evaluation list page."""
        eval_service = BidEvaluationService(self.db)
        evaluations, total = eval_service.list_evaluations(
            organization_id,
            status=status,
            search=search,
            offset=offset,
            limit=limit,
        )

        rfq_map: dict[UUID, RequestForQuotation] = {}
        rfq_ids = {evaluation.rfq_id for evaluation in evaluations}
        if rfq_ids:
            rfqs = self.db.scalars(
                select(RequestForQuotation).where(
                    RequestForQuotation.organization_id == organization_id,
                    RequestForQuotation.rfq_id.in_(rfq_ids),
                )
            ).all()
            rfq_map = {rfq.rfq_id: rfq for rfq in rfqs}

        active_filters = build_active_filters(
            params={"status": status, "search": search},
            labels={"search": "Search"},
        )
        return {
            "evaluations": evaluations,
            "total": total,
            "offset": offset,
            "limit": limit,
            "search": search or "",
            "filter_status": status,
            "status_labels": EVALUATION_STATUS_LABELS,
            "evaluation_statuses": list(EvaluationStatus),
            "rfq_map": rfq_map,
            "active_filters": active_filters,
        }

    def evaluation_matrix_context(
        self,
        organization_id: UUID,
        rfq_id: UUID,
    ) -> dict[str, Any]:
        """Build context for evaluation matrix page."""
        rfq_service = RFQService(self.db)
        quot_service = QuotationResponseService(self.db)
        eval_service = BidEvaluationService(self.db)

        rfq = rfq_service.get_by_id(organization_id, rfq_id)
        if not rfq:
            from app.services.common import NotFoundError

            raise NotFoundError("RFQ not found")

        responses = quot_service.list_for_rfq(organization_id, rfq_id)
        evaluations, _ = eval_service.list_evaluations(
            organization_id,
            rfq_id=rfq_id,
        )

        # Build total_scores and ranks from evaluation scores
        total_scores: dict[Any, float] = {}
        scores: dict[tuple, float] = {}
        for evaluation in evaluations:
            if hasattr(evaluation, "scores"):
                for s in evaluation.scores:
                    resp_id = s.response_id
                    total_scores[resp_id] = total_scores.get(resp_id, 0) + float(
                        s.weighted_score
                    )
                    scores[(resp_id, s.criterion_name)] = float(s.score)

        # Compute ranks by sorting total_scores descending
        sorted_responses = sorted(
            total_scores.items(), key=lambda x: x[1], reverse=True
        )
        ranks: dict[Any, int] = {
            resp_id: rank + 1 for rank, (resp_id, _) in enumerate(sorted_responses)
        }

        # Build criteria list from evaluation_criteria JSON
        criteria = rfq.evaluation_criteria or []

        return {
            "rfq": rfq,
            "responses": responses,
            "evaluations": evaluations,
            "evaluation_criteria": criteria,
            "criteria": criteria,
            "total_scores": total_scores,
            "ranks": ranks,
            "scores": scores,
            "status_labels": RFQ_STATUS_LABELS,
        }

    # ─────────────────────────────────────────────────────────────
    # Contracts
    # ─────────────────────────────────────────────────────────────

    def contract_list_context(
        self,
        organization_id: UUID,
        status: str | None = None,
        search: str | None = None,
        offset: int = 0,
        limit: int = 25,
    ) -> dict[str, Any]:
        """Build context for contract list page."""
        service = ContractService(self.db)
        contracts, total = service.list_contracts(
            organization_id,
            status=status,
            search=search,
            offset=offset,
            limit=limit,
        )
        active_filters = build_active_filters(
            params={"status": status, "search": search},
            labels={"search": "Search"},
        )
        return {
            "contracts": contracts,
            "total": total,
            "offset": offset,
            "limit": limit,
            "search": search or "",
            "filter_status": status,
            "status_labels": CONTRACT_STATUS_LABELS,
            "contract_statuses": list(ContractStatus),
            "active_filters": active_filters,
        }

    def contract_detail_context(
        self,
        organization_id: UUID,
        contract_id: UUID,
    ) -> dict[str, Any]:
        """Build context for contract detail page."""
        service = ContractService(self.db)
        contract = service.get_by_id(organization_id, contract_id)
        if not contract:
            from app.services.common import NotFoundError

            raise NotFoundError("Contract not found")

        remaining = contract.contract_value - contract.amount_paid
        progress_pct = (
            float(contract.amount_paid / contract.contract_value * 100)
            if contract.contract_value > 0
            else 0
        )

        return {
            "contract": contract,
            "total_paid": contract.amount_paid,
            "remaining_amount": remaining,
            "payment_progress_pct": round(progress_pct, 1),
            "status_labels": CONTRACT_STATUS_LABELS,
            "recent_activity": get_recent_activity_for_record(
                self.db,
                organization_id,
                record=contract,
                limit=10,
            ),
        }

    def contract_form_context(
        self,
        organization_id: UUID,
    ) -> dict[str, Any]:
        """Build context for contract create/edit form."""
        from app.models.finance.ap.supplier import Supplier

        suppliers = self.db.scalars(
            select(Supplier)
            .where(Supplier.organization_id == organization_id)
            .order_by(Supplier.legal_name.asc())
            .limit(200)
        ).all()
        return {"suppliers": suppliers}

    # ─────────────────────────────────────────────────────────────
    # Vendors
    # ─────────────────────────────────────────────────────────────

    def vendor_list_context(
        self,
        organization_id: UUID,
        status: str | None = None,
        q: str | None = None,
        offset: int = 0,
        limit: int = 25,
    ) -> dict[str, Any]:
        """Build context for vendor registry page."""
        from app.models.finance.ap.supplier import Supplier

        service = VendorPrequalificationService(self.db)
        prequalifications, total = service.list_prequalifications(
            organization_id,
            status=status,
            q=q,
            offset=offset,
            limit=limit,
        )

        # Fetch supplier data for display
        supplier_ids = [p.supplier_id for p in prequalifications]
        supplier_map: dict[UUID, Any] = {}
        if supplier_ids:
            suppliers = self.db.scalars(
                select(Supplier).where(Supplier.supplier_id.in_(supplier_ids))
            ).all()
            supplier_map = {s.supplier_id: s for s in suppliers}

        # Enrich prequalification rows with supplier name/registration
        for preq in prequalifications:
            supplier = supplier_map.get(preq.supplier_id)
            preq.supplier_name = (  # type: ignore[attr-defined]
                (supplier.trading_name or supplier.legal_name)
                if supplier
                else "Unknown"
            )
            preq.registration_number = (  # type: ignore[attr-defined]
                supplier.registration_number if supplier else None
            )

        active_filters = build_active_filters(
            params={"status": status, "q": q},
            labels={"q": "Search"},
        )
        page = (offset // limit) + 1 if limit else 1
        total_pages = max(1, (total + limit - 1) // limit) if limit else 1
        return {
            "vendors": prequalifications,
            "total": total,
            "total_count": total,
            "offset": offset,
            "limit": limit,
            "page": page,
            "total_pages": total_pages,
            "filter_status": status,
            "filter_q": q,
            "search": q or "",
            "status_labels": PREQUALIFICATION_STATUS_LABELS,
            "vendor_statuses": list(PrequalificationStatus),
            "active_filters": active_filters,
        }

    def prequalification_detail_context(
        self,
        organization_id: UUID,
        prequalification_id: UUID,
    ) -> dict[str, Any]:
        """Build context for prequalification detail page."""
        from app.models.finance.ap.supplier import Supplier

        service = VendorPrequalificationService(self.db)
        preq = service.get_by_id(organization_id, prequalification_id)
        if not preq:
            from app.services.common import NotFoundError

            raise NotFoundError("Prequalification not found")

        # Enrich with supplier data
        supplier = self.db.get(Supplier, preq.supplier_id)
        preq.supplier_name = (  # type: ignore[attr-defined]
            (supplier.trading_name or supplier.legal_name) if supplier else "Unknown"
        )
        preq.registration_number = (  # type: ignore[attr-defined]
            supplier.registration_number if supplier else None
        )

        # Build compliance checklist from model fields
        checklist = [
            {
                "requirement": "Documents Verified",
                "compliant": preq.documents_verified,
                "notes": None,
            },
            {
                "requirement": "Tax Clearance Valid",
                "compliant": preq.tax_clearance_valid,
                "notes": None,
            },
            {
                "requirement": "Pension Compliance",
                "compliant": preq.pension_compliance,
                "notes": None,
            },
            {
                "requirement": "ITF Compliance",
                "compliant": preq.itf_compliance,
                "notes": "Industrial Training Fund",
            },
            {
                "requirement": "NSITF Compliance",
                "compliant": preq.nsitf_compliance,
                "notes": "Nigeria Social Insurance Trust Fund",
            },
        ]

        # Build qualification scores from capability scores
        qualification_scores = []
        if preq.financial_capability_score is not None:
            qualification_scores.append(
                {
                    "category": "Financial Capability",
                    "weight": 40,
                    "score": float(preq.financial_capability_score),
                }
            )
        if preq.technical_capability_score is not None:
            qualification_scores.append(
                {
                    "category": "Technical Capability",
                    "weight": 60,
                    "score": float(preq.technical_capability_score),
                }
            )

        return {
            "vendor": preq,
            "checklist": checklist,
            "qualification_scores": qualification_scores,
            "past_contracts": [],
            "status_labels": PREQUALIFICATION_STATUS_LABELS,
            "recent_activity": get_recent_activity_for_record(
                self.db,
                organization_id,
                record=preq,
                limit=10,
            ),
        }

    def prequalification_form_context(self, organization_id: UUID) -> dict[str, Any]:
        """Build context for prequalification create form."""
        from app.models.finance.ap.supplier import Supplier

        suppliers = self.db.scalars(
            select(Supplier)
            .where(Supplier.organization_id == organization_id)
            .order_by(Supplier.legal_name.asc())
            .limit(200)
        ).all()
        return {
            "suppliers": suppliers,
            "prequalification_categories": [
                "Goods",
                "Works",
                "Services",
                "Consulting",
                "ICT",
                "Construction",
                "Logistics",
                "Facilities",
                "Professional Services",
            ],
        }
