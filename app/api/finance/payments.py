"""
Payment API Routes.

Handles payment initialization, verification, and webhooks for Paystack integration.
"""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.api.deps import (
    get_db_with_org,
    require_organization_id,
    require_tenant_auth,
)
from app.db.session_context import allow_cross_org, prime_session
from app.db import SessionLocal
from app.models.domain_settings import SettingDomain
from app.models.finance.payments.payment_intent import PaymentIntentStatus
from app.rls import set_current_organization_sync
from app.services.auth_dependencies import (
    has_live_admin_grant,
    require_tenant_permission,
)
from app.services.common import coerce_uuid
from app.services.finance.payments import (
    PaymentService,
    PaystackConfig,
    PaystackError,
    WebhookService,
)
from app.services.expense.limit_service import ExpenseLimitServiceError
from app.services.finance.payments.payment_service import TransferOutcomeUnknown
from app.services.finance.platform.authorization import AuthorizationService
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)

# Main router for authenticated endpoints
router = APIRouter(prefix="/payments", tags=["payments"])

# Separate router for webhook (no authentication - uses signature verification)
webhook_router = APIRouter(prefix="/payments", tags=["payments-webhook"])


def get_db():
    """Un-primed DB session for the Paystack webhook (unauth).

    .. warning::
        Tenant-scoped payment routes use ``get_db_with_org``. This
        yielder exists only for ``paystack_webhook`` which has no auth
        context to derive an org from — it resolves the org from the
        ``reference`` field on the webhook payload (via
        ``allow_cross_org`` + ``prime_session``). If RLS is ever
        enabled on a payments schema, route the webhook through a
        service-account auth context rather than this bare yielder.
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# =============================================================================
# Reimbursement authorization
# =============================================================================
#
# The reimburse flow used to run through ONE dependency,
# ``require_expense_reimburse_access``, whose admit set mixed the read
# permission ``payments:read`` in with the execute permissions and admitted on
# any intersection. It guarded all five reimburse routes, including
# ``POST /transfers/{intent_id}/initiate`` — which performs a real Paystack
# ``POST /transfer``. A principal holding only ``payments:read`` could
# therefore move money: a read permission bought a disbursement.
#
# It is now three guards, tiered by what the route can actually do:
#
#   lookup  - provider lookups that write nothing here and nothing at Paystack
#             (list banks, resolve an account name). ``payments:read`` belongs
#             on this tier and ONLY on this tier.
#   prepare - mutates local payout state and Paystack recipient state, but
#             moves no money (create / reset a payment intent).
#   execute - sends the money. ONE exact permission, no list, no intersection,
#             no blanket module scope.
#
# Each guard declares the permission set it admits on the function object
# (``authorized_permissions``) so
# ``tests/architecture/test_money_routes_reject_read_permissions.py`` can read
# the authorization of a mounted route statically. A mutating payments route
# whose guard does not declare its set fails that test as unmonitored.


def _declares(*permissions: str):
    """Record, on a guard, the exact permission set it admits.

    A guard's admit set is otherwise only visible by reading its body, which
    is how ``payments:read`` sat unnoticed in a payout admit set. Declaring it
    makes the set machine-readable for the architecture gate.
    """

    def _decorate(guard):
        guard.authorized_permissions = frozenset(permissions)  # type: ignore[attr-defined]
        return guard

    return _decorate


# Lookup tier: unchanged from the historic admit set. These routes read from
# Paystack and write nothing, so a read permission is correct here.
PAYMENTS_LOOKUP_PERMISSIONS: tuple[str, ...] = (
    "payments:read",
    "payments:expense:initialize",
    "payments:transfer:initiate",
    "expense:claims:reimburse",
    "expense:claims:approve:tier1",
    "expense:claims:approve:tier2",
    "expense:claims:approve:tier3",
)

# Prepare tier: the historic admit set MINUS ``payments:read``. That single
# removal is the whole change on this tier — every other principal the old
# guard admitted is still admitted. Narrowing the tier approvers out of payout
# preparation is a separate, product-owned decision and is deliberately not
# made here.
EXPENSE_PAYOUT_PREPARE_PERMISSIONS: tuple[str, ...] = (
    "payments:expense:initialize",
    "payments:transfer:initiate",
    "expense:claims:reimburse",
    "expense:claims:approve:tier1",
    "expense:claims:approve:tier2",
    "expense:claims:approve:tier3",
)

# Execute tier: the disbursement itself. Exactly one permission.
EXPENSE_PAYOUT_EXECUTE_PERMISSION = "payments:transfer:initiate"


def _admit_by_any_permission(
    auth: dict,
    db: Session,
    permissions: tuple[str, ...],
) -> dict:
    """Historic ``require_expense_reimburse_access`` admit logic, verbatim.

    Kept intact for the lookup and prepare tiers so this change does not alter
    who can reach a non-disbursing route. The ``admin`` role and the
    ``finance:access`` module scope still short-circuit here; see
    :func:`require_expense_payout_execute_access` for why neither does on the
    execute tier.
    """
    roles = set(auth.get("roles") or [])
    if "admin" in roles:
        return auth

    scopes = set(auth.get("scopes") or [])
    if "finance:access" in scopes:
        return auth

    if scopes.intersection(permissions):
        return auth

    person_id = auth.get("person_id")
    organization_id = auth.get("organization_id")
    if person_id and organization_id:
        if AuthorizationService.check_any_permission(
            db,
            coerce_uuid(person_id),
            list(permissions),
            coerce_uuid(organization_id),
        ):
            return auth

    raise HTTPException(status_code=403, detail="Forbidden")


@_declares(*PAYMENTS_LOOKUP_PERMISSIONS)
def require_payments_lookup_access(
    auth=Depends(require_tenant_auth),
    db: Session = Depends(get_db_with_org),
):
    """READ tier: Paystack bank list and account-name resolution.

    These routes read from the payment provider and write nothing, locally or
    at Paystack, so ``payments:read`` is a correct admit here.
    """
    return _admit_by_any_permission(auth, db, PAYMENTS_LOOKUP_PERMISSIONS)


@_declares(*EXPENSE_PAYOUT_PREPARE_PERMISSIONS)
def require_expense_payout_prepare_access(
    auth=Depends(require_tenant_auth),
    db: Session = Depends(get_db_with_org),
):
    """PREPARE tier: create or reset an expense payout intent.

    No money moves on these routes, but they are not reads: they create a
    Paystack transfer recipient, write ``recipient_account_name`` onto the
    claim, insert a ``PaymentIntent``, or abandon an existing one so a payout
    can be retried. A read permission must not reach any of that.
    """
    return _admit_by_any_permission(auth, db, EXPENSE_PAYOUT_PREPARE_PERMISSIONS)


@_declares(EXPENSE_PAYOUT_EXECUTE_PERMISSION)
def require_expense_payout_execute_access(
    auth=Depends(require_tenant_auth),
    db: Session = Depends(get_db_with_org),
):
    """EXECUTE tier: the outbound transfer. Money leaves the account here.

    Exactly one permission, ``payments:transfer:initiate``. The check is set
    MEMBERSHIP, so it is an exact string equality against each held scope —
    never an intersection over a list, a ``startswith``, or an ``in`` over a
    string. Neither ``payments:transfer`` nor a longer
    ``payments:transfer:initiate:review`` satisfies it.

    In practice the grant is read from the DATABASE, not the token:
    ``auth_flow._load_rbac_claims`` puts only module-access keys in a JWT, and
    ``payments:transfer:initiate`` is not one, so the scope branch below is a
    fast path for a token that carries it while the real answer comes from
    ``person_roles`` per request. That is the desired shape for a disbursement
    — revoking the grant takes effect immediately rather than at token expiry.
    ``scripts/seed_rbac.py`` grants it to ``expense_admin``,
    ``expense_processor`` and ``expense_reimburser``: exactly the roles that
    could already reach this route through the old shared guard.

    ``finance:access`` deliberately no longer short-circuits. It is a
    module-access scope granted to a dozen roles by ``scripts/seed_rbac.py``
    and it is one of the few permissions that JWTs actually carry
    (``auth_flow._load_rbac_claims`` restricts token scopes to module-access
    keys). A blanket "can see the finance module" scope satisfying a
    disbursement guard is the same defect one level up, so it is gone from
    this tier. Operators who reimburse must hold
    ``payments:transfer:initiate``.

    The ``admin`` role is preserved as an authority path, but it is no longer
    taken from the TOKEN. A token's ``roles`` claim is a login-time snapshot:
    revoking someone's admin role leaves every already-issued token asserting
    it until that token expires. On a payout that window is a removed
    administrator who can still move money, so the grant is re-asked of the
    live tables per request via
    :func:`app.services.auth_dependencies.has_live_admin_grant` — the function
    extracted for exactly this hazard.
    """
    scopes = set(auth.get("scopes") or [])
    if EXPENSE_PAYOUT_EXECUTE_PERMISSION in scopes:
        return auth

    person_id = auth.get("person_id")
    organization_id = auth.get("organization_id")
    if person_id and organization_id:
        if AuthorizationService.check_permission(
            db,
            coerce_uuid(person_id),
            EXPENSE_PAYOUT_EXECUTE_PERMISSION,
            coerce_uuid(organization_id),
        ):
            return auth

    if has_live_admin_grant(db, person_id):
        return auth

    raise HTTPException(status_code=403, detail="Forbidden")


# =============================================================================
# Pydantic Schemas
# =============================================================================


class InitializeInvoicePaymentRequest(BaseModel):
    """Request to initialize a payment for an invoice."""

    invoice_id: UUID


class InitializePaymentResponse(BaseModel):
    """Response from payment initialization."""

    intent_id: UUID
    authorization_url: str
    reference: str
    amount: float
    currency: str


class PaymentStatusResponse(BaseModel):
    """Response with payment status."""

    intent_id: UUID
    status: str
    amount: float
    currency: str
    paid_at: str | None = None
    invoice_number: str | None = None
    customer_payment_id: UUID | None = None


class WebhookResponse(BaseModel):
    """Response to webhook."""

    status: str
    message: str | None = None


# -----------------------------------------------------------------------------
# Expense Transfer Schemas
# -----------------------------------------------------------------------------


class BankInfo(BaseModel):
    """Bank information."""

    code: str
    name: str


class ResolveAccountRequest(BaseModel):
    """Request to resolve a bank account."""

    bank_code: str = Field(..., description="Bank code (e.g., '058' for GTBank)")
    account_number: str = Field(..., min_length=10, max_length=10)


class ResolveAccountResponse(BaseModel):
    """Response from account resolution."""

    account_number: str
    account_name: str
    bank_code: str


class InitializeExpensePaymentRequest(BaseModel):
    """Request to initialize expense reimbursement."""

    expense_claim_id: UUID
    bank_code: str | None = Field(
        default=None, description="Recipient's bank code (ignored; claim data is used)"
    )
    account_number: str | None = Field(
        default=None,
        min_length=10,
        max_length=10,
        description="Recipient's bank account number (ignored; claim data is used)",
    )


class ExpensePaymentResponse(BaseModel):
    """Response for expense payment intent."""

    intent_id: UUID
    reference: str
    amount: float
    currency: str
    status: str
    recipient_account_name: str | None = None
    recipient_bank_code: str | None = None
    recipient_account_number: str | None = None


class ResetExpensePaymentIntentRequest(BaseModel):
    """Request to reset a failed/abandoned/expired expense payment intent."""

    reason: str | None = Field(default=None, max_length=500)
    force: bool = False


class ResetExpensePaymentIntentResponse(BaseModel):
    """Response when an expense payment intent is reset."""

    expense_claim_id: UUID
    intent_id: UUID
    status: str
    message: str


class InitiateTransferResponse(BaseModel):
    """Response from transfer initiation."""

    intent_id: UUID
    transfer_code: str
    status: str  # PROCESSING, COMPLETED, or FAILED
    amount: float
    currency: str
    completed_immediately: bool = False  # True if transfer completed without webhook
    claim_status: str | None = None  # Status of the expense claim after transfer
    message: str | None = None  # Human-readable status message


# =============================================================================
# Helpers
# =============================================================================


def get_paystack_config(db: Session, organization_id: UUID) -> PaystackConfig:
    """
    Get Paystack configuration for organization.

    Raises HTTPException if Paystack is not enabled or configured.
    """
    # Check if enabled
    enabled = resolve_value(
        db,
        SettingDomain.payments,
        "paystack_enabled",
        organization_id=organization_id,
    )
    if not enabled:
        raise HTTPException(
            status_code=400,
            detail="Paystack payment integration is not enabled for this organization",
        )

    # Get keys
    secret_key = resolve_value(
        db,
        SettingDomain.payments,
        "paystack_secret_key",
        organization_id=organization_id,
    )
    public_key = resolve_value(
        db,
        SettingDomain.payments,
        "paystack_public_key",
        organization_id=organization_id,
    )

    if not secret_key:
        raise HTTPException(
            status_code=500,
            detail="Paystack secret key not configured",
        )

    if not public_key:
        raise HTTPException(
            status_code=500,
            detail="Paystack public key not configured",
        )

    # Paystack uses the API secret key for webhook signature verification
    # (there's no separate webhook secret in Paystack)
    return PaystackConfig(
        secret_key=str(secret_key),
        public_key=str(public_key),
        webhook_secret=str(secret_key),
    )


def set_payment_tenant_context(db: Session, organization_id: UUID) -> None:
    """Scope the payment API route session to the authenticated organization."""
    set_current_organization_sync(db, organization_id)


# =============================================================================
# Endpoints
# =============================================================================


@router.post("/initialize/invoice", response_model=InitializePaymentResponse)
def initialize_invoice_payment(
    request_data: InitializeInvoicePaymentRequest,
    request: Request,
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
    auth: dict = Depends(require_tenant_permission("payments:invoice:initialize")),
):
    """
    Initialize a Paystack payment for an invoice.

    Creates a payment intent and returns the Paystack authorization URL
    to redirect the customer for payment.
    """
    set_payment_tenant_context(db, organization_id)
    config = get_paystack_config(db, organization_id)

    # Build callback URL
    # Check for configured base URL first, then fall back to request base
    callback_base = resolve_value(
        db,
        SettingDomain.payments,
        "paystack_callback_base_url",
        organization_id=organization_id,
    )
    if callback_base:
        base_url = str(callback_base).rstrip("/")
    else:
        base_url = str(request.base_url).rstrip("/")

    callback_url = f"{base_url}/finance/payments/callback"

    svc = PaymentService(db, organization_id)
    try:
        intent = svc.create_invoice_payment_intent(
            invoice_id=request_data.invoice_id,
            callback_url=callback_url,
            paystack_config=config,
        )
    except PaystackError as e:
        logger.error(f"Paystack initialization failed: {e}")
        raise HTTPException(
            status_code=502,
            detail=f"Payment gateway error: {e.message}",
        )

    return InitializePaymentResponse(
        intent_id=intent.intent_id,
        authorization_url=intent.authorization_url or "",
        reference=intent.paystack_reference,
        amount=float(intent.amount),
        currency=intent.currency_code,
    )


@router.get("/status/{reference}", response_model=PaymentStatusResponse)
def get_payment_status(
    reference: str,
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
    auth: dict = Depends(require_tenant_permission("payments:read")),
):
    """
    Get payment status by reference.

    Returns the current status of a payment intent.
    """
    set_payment_tenant_context(db, organization_id)
    intent = PaymentService.get_intent_by_reference(db, reference, organization_id)

    if not intent:
        raise HTTPException(status_code=404, detail="Payment not found")

    return PaymentStatusResponse(
        intent_id=intent.intent_id,
        status=intent.status.value,
        amount=float(intent.amount),
        currency=intent.currency_code,
        paid_at=intent.paid_at.isoformat() if intent.paid_at else None,
        invoice_number=intent.intent_metadata.get("invoice_number")
        if intent.intent_metadata
        else None,
        customer_payment_id=intent.customer_payment_id,
    )


@router.get("/intent/{intent_id}", response_model=PaymentStatusResponse)
def get_payment_intent(
    intent_id: UUID,
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
    auth: dict = Depends(require_tenant_permission("payments:read")),
):
    """
    Get payment intent by ID.

    Returns the current status of a payment intent.
    """
    set_payment_tenant_context(db, organization_id)
    svc = PaymentService(db, organization_id)
    intent = svc.get_intent_by_id(intent_id)

    if not intent:
        raise HTTPException(status_code=404, detail="Payment intent not found")

    return PaymentStatusResponse(
        intent_id=intent.intent_id,
        status=intent.status.value,
        amount=float(intent.amount),
        currency=intent.currency_code,
        paid_at=intent.paid_at.isoformat() if intent.paid_at else None,
        invoice_number=intent.intent_metadata.get("invoice_number")
        if intent.intent_metadata
        else None,
        customer_payment_id=intent.customer_payment_id,
    )


@router.post("/verify/{reference}", response_model=PaymentStatusResponse)
def verify_payment(
    reference: str,
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
    auth: dict = Depends(require_tenant_permission("payments:verify")),
):
    """
    Verify a payment with Paystack.

    Queries Paystack to get the current status of a payment and updates
    the local payment intent accordingly. Use this if webhook was missed.
    """
    set_payment_tenant_context(db, organization_id)
    config = get_paystack_config(db, organization_id)
    svc = PaymentService(db, organization_id)
    try:
        intent = svc.verify_payment_by_reference(reference, config)
    except PaystackError as e:
        logger.error(f"Paystack verification failed: {e}")
        raise HTTPException(
            status_code=502,
            detail=f"Payment verification failed: {e.message}",
        )

    return PaymentStatusResponse(
        intent_id=intent.intent_id,
        status=intent.status.value,
        amount=float(intent.amount),
        currency=intent.currency_code,
        paid_at=intent.paid_at.isoformat() if intent.paid_at else None,
        invoice_number=intent.intent_metadata.get("invoice_number")
        if intent.intent_metadata
        else None,
        customer_payment_id=intent.customer_payment_id,
    )


# =============================================================================
# Expense Reimbursement (Transfer) Endpoints
# =============================================================================


@router.get("/banks", response_model=list[BankInfo])
def list_banks(
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
    auth: dict = Depends(require_payments_lookup_access),
):
    """
    List supported banks for transfers.

    Returns list of Nigerian banks supported by Paystack.
    """
    from app.services.finance.payments import PaystackClient

    set_payment_tenant_context(db, organization_id)
    config = get_paystack_config(db, organization_id)

    try:
        with PaystackClient(config) as client:
            banks = client.list_banks(country="nigeria")

        return [BankInfo(code=b.code, name=b.name) for b in banks]

    except PaystackError as e:
        logger.error(f"Failed to list banks: {e}")
        raise HTTPException(
            status_code=502, detail=f"Payment gateway error: {e.message}"
        )


@router.post("/resolve-account", response_model=ResolveAccountResponse)
def resolve_bank_account(
    request_data: ResolveAccountRequest,
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
    auth: dict = Depends(require_payments_lookup_access),
):
    """
    Resolve a bank account to verify it exists and get the account name.

    Use this before initiating expense reimbursement to confirm bank details.
    """
    from app.services.finance.payments import PaystackClient

    set_payment_tenant_context(db, organization_id)
    config = get_paystack_config(db, organization_id)

    try:
        with PaystackClient(config) as client:
            result = client.resolve_account(
                account_number=request_data.account_number,
                bank_code=request_data.bank_code,
            )

        return ResolveAccountResponse(
            account_number=result.account_number,
            account_name=result.account_name,
            bank_code=request_data.bank_code,
        )

    except PaystackError as e:
        logger.error(f"Account resolution failed: {e}")
        if "Could not resolve account name" in str(e.message):
            raise HTTPException(
                status_code=400,
                detail="Invalid account number or bank code",
            )
        raise HTTPException(
            status_code=502, detail=f"Payment gateway error: {e.message}"
        )


@router.post("/initialize/expense", response_model=ExpensePaymentResponse)
def initialize_expense_payment(
    request_data: InitializeExpensePaymentRequest,
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
    auth: dict = Depends(require_expense_payout_prepare_access),
):
    """
    Initialize an expense reimbursement payment (transfer).

    Creates a payment intent for an expense claim. This does NOT initiate
    the transfer yet - call /transfers/{intent_id}/initiate to execute.

    Requires:
    - Paystack transfers must be enabled
    - Expense claim must be approved
    - Bank account details must be valid (from the expense claim)
    - No existing active payment intent for this claim
    """
    set_payment_tenant_context(db, organization_id)

    # Check if transfers are enabled
    transfers_enabled = resolve_value(
        db,
        SettingDomain.payments,
        "paystack_transfers_enabled",
        organization_id=organization_id,
    )
    if not transfers_enabled:
        raise HTTPException(
            status_code=400,
            detail="Paystack transfers are not enabled. Contact administrator.",
        )

    config = get_paystack_config(db, organization_id)

    svc = PaymentService(db, organization_id)
    try:
        intent = svc.create_expense_payment_intent(
            expense_claim_id=request_data.expense_claim_id,
            paystack_config=config,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except PaystackError as e:
        logger.error(f"Expense payment initialization failed: {e}")
        raise HTTPException(
            status_code=502, detail=f"Payment gateway error: {e.message}"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Unexpected error in expense payment: {e}")
        raise HTTPException(status_code=500, detail=f"Expense payment error: {str(e)}")

    return ExpensePaymentResponse(
        intent_id=intent.intent_id,
        reference=intent.paystack_reference,
        amount=float(intent.amount),
        currency=intent.currency_code,
        status=intent.status.value,
        recipient_account_name=intent.recipient_account_name,
        recipient_bank_code=intent.recipient_bank_code,
        recipient_account_number=intent.recipient_account_number,
    )


@router.post(
    "/expense-claims/{expense_claim_id}/reset-payment-intent",
    response_model=ResetExpensePaymentIntentResponse,
)
def reset_expense_payment_intent(
    expense_claim_id: UUID,
    request_data: ResetExpensePaymentIntentRequest,
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
    auth: dict = Depends(require_expense_payout_prepare_access),
):
    """
    Reset a non-completed expense payment intent so reimbursement can be retried.
    """
    set_payment_tenant_context(db, organization_id)
    svc = PaymentService(db, organization_id)
    try:
        intent = svc.reset_expense_payment_intent(
            expense_claim_id=expense_claim_id,
            reason=request_data.reason,
            force=request_data.force,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(
            f"Unexpected error resetting expense payment intent for claim {expense_claim_id}: {e}"
        )
        raise HTTPException(
            status_code=500,
            detail="Could not reset expense payment intent. Please try again.",
        )

    return ResetExpensePaymentIntentResponse(
        expense_claim_id=expense_claim_id,
        intent_id=intent.intent_id,
        status=intent.status.value,
        message="Payment intent reset. Re-run reimbursement to create a fresh transfer.",
    )


@router.post("/transfers/{intent_id}/initiate", response_model=InitiateTransferResponse)
def initiate_transfer(
    intent_id: UUID,
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
    auth: dict = Depends(require_expense_payout_execute_access),
):
    """
    Initiate a Paystack transfer for an expense reimbursement.

    The payment intent must have been created with /initialize/expense first.
    This actually sends the money to the recipient's bank account.

    Authorization: requires the exact ``payments:transfer:initiate``
    permission (or a live ``admin`` grant). A read permission, or the
    ``finance:access`` module scope, is NOT sufficient — see
    :func:`require_expense_payout_execute_access`.
    """
    set_payment_tenant_context(db, organization_id)

    # Check if transfers are enabled
    transfers_enabled = resolve_value(
        db,
        SettingDomain.payments,
        "paystack_transfers_enabled",
        organization_id=organization_id,
    )
    if not transfers_enabled:
        raise HTTPException(
            status_code=400,
            detail="Paystack transfers are not enabled",
        )

    config = get_paystack_config(db, organization_id)
    svc = PaymentService(db, organization_id)

    intent = svc.get_intent_by_id(intent_id)
    if not intent:
        raise HTTPException(status_code=404, detail="Payment intent not found")

    if intent.status != PaymentIntentStatus.PENDING:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot initiate transfer: intent status is {intent.status.value}",
        )

    if intent.direction.value != "OUTBOUND":
        raise HTTPException(
            status_code=400,
            detail="This payment intent is not an outbound transfer",
        )

    try:
        updated_intent = svc.initiate_expense_transfer(
            intent=intent,
            paystack_config=config,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ExpenseLimitServiceError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except TransferOutcomeUnknown as e:
        # 409, not 502, and the distinction is the whole point.
        #
        # A 502 says "the upstream failed" and every retry policy in the world
        # -- proxies, HTTP client libraries, the operator's instinct, a queue's
        # backoff -- treats 5xx as safe to repeat. Repeating THIS request is how
        # an employee gets reimbursed twice, because the first attempt may
        # already have moved the money and nobody can currently say.
        #
        # 409 says the resource is in a state that conflicts with what was
        # asked, which is exactly true: the intent is INDETERMINATE and no
        # further initiation is permitted from it. 4xx is not in any default
        # retry set, it does not read as "we failed", and it puts the caller on
        # the only correct path -- wait for reconciliation, or have a human
        # confirm with Paystack.
        #
        # 202 was the alternative and was rejected: it is a SUCCESS code, and a
        # client that treats "accepted" as "it worked" would mark the claim as
        # paid in its own UI on the strength of an outcome nobody observed.
        logger.error(
            "Transfer initiation outcome UNKNOWN for intent %s: %s",
            e.intent_id,
            e.reason,
        )
        raise HTTPException(
            status_code=409,
            detail={
                "error": "transfer_outcome_unknown",
                "intent_id": str(e.intent_id),
                "message": (
                    "The transfer was sent to Paystack but its outcome could "
                    "not be confirmed, so it may or may not have moved money. "
                    "Do NOT retry this payout. It has been recorded as "
                    "unresolved and is being reconciled; the expense claim "
                    "stays approved and unpaid until a real outcome is known."
                ),
                "retryable": False,
                "reason": e.reason,
            },
        )
    except PaystackError as e:
        logger.error(f"Transfer initiation failed: {e}")
        raise HTTPException(status_code=502, detail=f"Transfer failed: {e.message}")

    result = svc.build_transfer_result(updated_intent)

    return InitiateTransferResponse(
        intent_id=updated_intent.intent_id,
        transfer_code=updated_intent.transfer_code or "",
        status=updated_intent.status.value,
        amount=float(updated_intent.amount),
        currency=updated_intent.currency_code,
        completed_immediately=result["completed_immediately"],
        claim_status=result["claim_status"],
        message=result["message"],
    )


@router.get("/transfers/pending")
def list_pending_transfers(
    organization_id: UUID = Depends(require_organization_id),
    db: Session = Depends(get_db_with_org),
    auth: dict = Depends(require_tenant_permission("payments:read")),
):
    """
    List pending expense reimbursement transfers.

    Returns transfers that have been initialized but not yet completed.
    """
    set_payment_tenant_context(db, organization_id)
    svc = PaymentService(db, organization_id)
    intents = svc.list_pending_transfers()

    return [
        ExpensePaymentResponse(
            intent_id=i.intent_id,
            reference=i.paystack_reference,
            amount=float(i.amount),
            currency=i.currency_code,
            status=i.status.value,
            recipient_account_name=i.recipient_account_name,
            recipient_bank_code=i.recipient_bank_code,
            recipient_account_number=i.recipient_account_number,
        )
        for i in intents
    ]


# =============================================================================
# Webhook Endpoint (No Authentication - Uses Signature Verification)
# =============================================================================


@webhook_router.post("/webhook/paystack", response_model=WebhookResponse)
async def paystack_webhook(
    request: Request,
    x_paystack_signature: str = Header(None, alias="X-Paystack-Signature"),
    db: Session = Depends(get_db),
):
    """
    Handle Paystack webhook events.

    This endpoint does NOT require authentication - it uses Paystack's
    signature verification instead.

    Paystack will send webhooks for events like:
    - charge.success: Payment was successful
    - charge.failed: Payment failed
    - transfer.success: Transfer completed
    - transfer.failed: Transfer failed
    """
    if not x_paystack_signature:
        logger.warning("Webhook received without signature")
        raise HTTPException(status_code=400, detail="Missing signature")

    raw_body = await request.body()

    try:
        payload = await request.json()
    except Exception as e:
        logger.error(f"Invalid webhook payload: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event_type = payload.get("event")
    event_data = payload.get("data", {})

    if not event_type:
        logger.warning("Webhook received without event type")
        raise HTTPException(status_code=400, detail="Missing event type")

    # Get reference to find organization and config
    reference = event_data.get("reference", "")

    # The reference→org lookup is genuinely cross-org — we don't know the
    # tenant until we resolve it. Wrap so the planned ORM listener doesn't
    # raise MissingOrgContextError when it fires on this SELECT.
    with allow_cross_org(db):
        intent = PaymentService.get_intent_by_reference(db, reference)

    if not intent:
        # Selfcare references are not ERP payment intents. Verify the provider
        # signature before forwarding only the reference; Selfcare performs its
        # own gateway verification before deciding any financial consequence.
        from app.services.dotmac_sub import (
            DotmacSubClient,
            DotmacSubConfig,
            DotmacSubRateLimitError,
        )
        from app.services.finance.payments.paystack_client import PaystackClient

        try:
            if not settings.default_organization_id:
                raise RuntimeError("default organization is not configured")
            default_org_id = UUID(settings.default_organization_id)
            prime_session(db, default_org_id)
            set_payment_tenant_context(db, default_org_id)
            config = get_paystack_config(db, default_org_id)
            if not PaystackClient(config).verify_webhook_signature(
                raw_body, x_paystack_signature
            ):
                raise HTTPException(status_code=401, detail="Invalid webhook signature")
            if event_type != "charge.success" or not reference:
                return WebhookResponse(status="ignored", message="Unsupported event")
            if not reference.startswith("DMAC-"):
                return WebhookResponse(status="ignored", message="Unknown reference")
            sub_config = DotmacSubConfig.for_org(db, default_org_id)
            if not sub_config.is_configured():
                raise RuntimeError("dotmac_sub integration is not configured")
            await run_in_threadpool(
                DotmacSubClient(sub_config).relay_paystack_webhook,
                raw_payload=raw_body,
                signature=x_paystack_signature,
            )
            logger.info(
                "Relayed verified Paystack webhook to dotmac_sub",
                extra={
                    "event_type": event_type,
                    "payment_reference": reference,
                    "relay_outcome": "forwarded",
                },
            )
            return WebhookResponse(status="forwarded")
        except DotmacSubRateLimitError as exc:
            retry_after = int(exc.retry_after or 1)
            logger.warning(
                "dotmac_sub rate-limited verified Paystack webhook relay",
                extra={
                    "event_type": event_type,
                    "payment_reference": reference,
                    "relay_outcome": "rate_limited",
                    "retry_after_seconds": retry_after,
                },
            )
            raise HTTPException(
                status_code=503,
                detail="Payment notification delivery is temporarily unavailable",
                headers={"Retry-After": str(retry_after)},
            )
        except HTTPException:
            raise
        except Exception:
            logger.exception(
                "Failed to forward verified Paystack reference to dotmac_sub",
                extra={
                    "event_type": event_type,
                    "payment_reference": reference,
                    "relay_outcome": "failed",
                },
            )
            # A non-2xx response asks Paystack to retry delivery.
            raise HTTPException(
                status_code=503,
                detail="Payment notification delivery is temporarily unavailable",
            )

    # Prime both the Python-side ORM listener marker AND the PostgreSQL GUC
    # so subsequent queries in this handler see the resolved tenant.
    prime_session(db, intent.organization_id)
    set_payment_tenant_context(db, intent.organization_id)

    # Get Paystack config for this organization
    try:
        config = get_paystack_config(db, intent.organization_id)
    except HTTPException as e:
        logger.error(f"Failed to get Paystack config: {e.detail}")
        return WebhookResponse(
            status="error",
            message="Paystack not configured for organization",
        )

    # Process webhook
    svc = WebhookService(db)
    try:
        webhook = svc.process_webhook(
            event_type=event_type,
            event_data=event_data,
            paystack_config=config,
            raw_payload=raw_body,
            signature=x_paystack_signature,
        )
        return WebhookResponse(
            status=webhook.status.value,
            message=webhook.error_message,
        )

    except ValueError as e:
        # Signature verification failed
        logger.warning(f"Webhook signature verification failed: {e}")
        raise HTTPException(status_code=401, detail=str(e))

    except Exception as e:
        logger.exception(f"Webhook processing error: {e}")
        raise HTTPException(status_code=500, detail="Webhook processing failed")
