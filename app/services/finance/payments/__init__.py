"""
Payment Services.

Services for Paystack payment integration.
"""

from app.services.finance.payments.payment_service import (
    PaymentService,
)
from app.services.finance.payments.paystack_client import (
    Bank,
    CreateRecipientResponse,
    InitializeResponse,
    InitiateTransferResponse,
    PaystackClient,
    PaystackConfig,
    PaystackError,
    PaystackUnreachable,
    ResolveAccountResponse,
    VerifyResponse,
    VerifyTransferResponse,
)
from app.services.finance.payments.web import (
    PaymentWebService,
    payment_web_service,
)
from app.services.finance.payments.webhook_service import (
    WebhookService,
)

__all__ = [
    # Client
    "Bank",
    "CreateRecipientResponse",
    "InitializeResponse",
    "InitiateTransferResponse",
    "PaystackClient",
    "PaystackConfig",
    "PaystackError",
    "PaystackUnreachable",
    "ResolveAccountResponse",
    "VerifyResponse",
    "VerifyTransferResponse",
    # Services
    "PaymentService",
    "PaymentWebService",
    "payment_web_service",
    "WebhookService",
]

from app.services.setting_domain_declaration import ModuleSettingDomains  # noqa: E402

# Setting domain(s) this module owns — payment providers and webhook verification.
# Validated by `app.services.setting_domains` at startup and at every write;
# see that module for why ownership lives here rather than in a central list.
SETTING_DOMAINS = ModuleSettingDomains(setting_domains=("payments",))
