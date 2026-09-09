"""
Entity Registry for Workflow Automation.

Maps entity type strings to their SQLAlchemy model classes
and primary key field names. Used by action handlers that
need to generically load and manipulate entities.
"""

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Each entry: (import_path, model_class_name, pk_field_name)
_ENTITY_REGISTRY: dict[str, tuple[str, str, str]] = {
    "INVOICE": (
        "app.models.finance.ar.invoice",
        "Invoice",
        "invoice_id",
    ),
    "BILL": (
        "app.models.finance.ap.supplier_invoice",
        "SupplierInvoice",
        "invoice_id",
    ),
    "EXPENSE": (
        "app.models.expense.expense_claim",
        "ExpenseClaim",
        "claim_id",
    ),
    "JOURNAL": (
        "app.models.finance.gl.journal_entry",
        "JournalEntry",
        "entry_id",
    ),
    "PAYMENT": (
        "app.models.finance.ap.supplier_payment",
        "SupplierPayment",
        "payment_id",
    ),
    "CUSTOMER": (
        "app.models.finance.ar.customer",
        "Customer",
        "customer_id",
    ),
    "SUPPLIER": (
        "app.models.finance.ap.supplier",
        "Supplier",
        "supplier_id",
    ),
    "QUOTE": (
        "app.models.finance.ar.quote",
        "Quote",
        "quote_id",
    ),
    "SALES_ORDER": (
        "app.models.finance.ar.sales_order",
        "SalesOrder",
        "order_id",
    ),
    "PURCHASE_ORDER": (
        "app.models.finance.ap.purchase_order",
        "PurchaseOrder",
        "po_id",
    ),
    "BANK_TRANSACTION": (
        "app.models.finance.banking.bank_statement",
        "BankStatementLine",
        "line_id",
    ),
    "RECONCILIATION": (
        "app.models.finance.banking.bank_reconciliation",
        "BankReconciliation",
        "reconciliation_id",
    ),
    "CREDIT_NOTE": (
        "app.models.finance.ar.invoice",
        "Invoice",
        "invoice_id",
    ),
    "CASH_ADVANCE": (
        "app.models.expense.cash_advance",
        "CashAdvance",
        "advance_id",
    ),
    "ASSET_DISPOSAL": (
        "app.models.finance.fa.asset_disposal",
        "AssetDisposal",
        "disposal_id",
    ),
    # People / HR entity types
    "EMPLOYEE": (
        "app.models.people.hr.employee",
        "Employee",
        "employee_id",
    ),
    "LEAVE_REQUEST": (
        "app.models.people.leave.leave_application",
        "LeaveApplication",
        "application_id",
    ),
    "DISCIPLINARY_CASE": (
        "app.models.people.discipline.disciplinary_case",
        "DisciplinaryCase",
        "case_id",
    ),
    "PERFORMANCE_APPRAISAL": (
        "app.models.people.perf.appraisal",
        "Appraisal",
        "appraisal_id",
    ),
    "LOAN": (
        "app.models.people.payroll.employee_loan",
        "EmployeeLoan",
        "loan_id",
    ),
    "RECRUITMENT": (
        "app.models.people.recruit.job_opening",
        "JobOpening",
        "job_opening_id",
    ),
    # Fleet
    "FLEET_VEHICLE": (
        "app.models.fleet.vehicle",
        "Vehicle",
        "vehicle_id",
    ),
    "FLEET_RESERVATION": (
        "app.models.fleet.vehicle_reservation",
        "VehicleReservation",
        "reservation_id",
    ),
    "FLEET_MAINTENANCE": (
        "app.models.fleet.maintenance",
        "MaintenanceRecord",
        "maintenance_id",
    ),
    "FLEET_INCIDENT": (
        "app.models.fleet.vehicle_incident",
        "VehicleIncident",
        "incident_id",
    ),
    # Inventory
    "MATERIAL_REQUEST": (
        "app.models.inventory.material_request",
        "MaterialRequest",
        "request_id",
    ),
    # Payroll
    "PAYROLL_RUN": (
        "app.models.people.payroll.payroll_entry",
        "PayrollEntry",
        "entry_id",
    ),
    "PAYROLL_ENTRY": (
        "app.models.people.payroll.payroll_entry",
        "PayrollEntry",
        "entry_id",
    ),
    "SALARY_SLIP": (
        "app.models.people.payroll.salary_slip",
        "SalarySlip",
        "slip_id",
    ),
}

# Fields the automation engine must NEVER write, keyed by entity type.
#
# `_action_update_field` loads an entity generically and `setattr`s whatever
# field a rule names.  That is unbounded by construction: any column of any
# registered entity is reachable from a workflow rule, including columns a named
# service owns exclusively.  This map is the gate.
#
# Each entry is `field name -> the owner that may write it`.  The action fails
# with the owner's name in the message, so an operator who hits it is told where
# the write belongs rather than that it is merely forbidden.
#
# A field listed here does not have to exist on the model.  `amount_received` and
# `amount_invoiced` were REMOVED from `ap.purchase_order` — they are derived by
# `purchase_order_amounts` and stored nowhere.  Naming them keeps a re-added
# column from being writable by a rule the moment it reappears.
_PROTECTED_FIELDS: dict[str, dict[str, str]] = {
    "PURCHASE_ORDER": {
        "amount_received": "app.services.finance.ap.purchase_order_amounts (derived)",
        "amount_invoiced": "app.services.finance.ap.purchase_order_amounts (derived)",
    },
}


def field_authority_owner(entity_type: str, field_name: str) -> str | None:
    """Return the owner of `field_name`, or `None` if the engine may write it.

    A non-`None` return means a named service owns the field exclusively and the
    automation engine must refuse the write.
    """
    return _PROTECTED_FIELDS.get(entity_type, {}).get(field_name)


def protected_fields(entity_type: str) -> frozenset[str]:
    """The fields the automation engine may not write for `entity_type`."""
    return frozenset(_PROTECTED_FIELDS.get(entity_type, {}))


# Cache resolved model classes to avoid repeated imports
_resolved_models: dict[str, type[Any] | None] = {}


def _get_model_class(entity_type: str) -> type[Any] | None:
    """Resolve entity type to its SQLAlchemy model class (cached)."""
    if entity_type in _resolved_models:
        return _resolved_models[entity_type]

    entry = _ENTITY_REGISTRY.get(entity_type)
    if not entry:
        _resolved_models[entity_type] = None
        return None

    module_path, class_name, _ = entry
    try:
        import importlib

        module = importlib.import_module(module_path)
        model_cls = getattr(module, class_name, None)
        _resolved_models[entity_type] = model_cls
        return model_cls
    except (ImportError, AttributeError) as e:
        logger.warning(
            "Cannot resolve entity type %s (%s.%s): %s",
            entity_type,
            module_path,
            class_name,
            e,
        )
        _resolved_models[entity_type] = None
        return None


def get_pk_field(entity_type: str) -> str | None:
    """Return the primary key field name for an entity type."""
    entry = _ENTITY_REGISTRY.get(entity_type)
    return entry[2] if entry else None


def resolve_entity(
    db: Session,
    entity_type: str,
    entity_id: UUID,
) -> Any | None:
    """Load an entity by type and ID.

    Returns:
        The SQLAlchemy model instance, or None if not found.
    """
    model_cls = _get_model_class(entity_type)
    if model_cls is None:
        return None

    return db.get(model_cls, entity_id)


def get_registered_types() -> list[str]:
    """Return all registered entity type strings."""
    return sorted(_ENTITY_REGISTRY.keys())
