"""
Paystack Sync Service.

Synchronizes Paystack transactions with bank statements for reconciliation.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone

try:
    from datetime import UTC  # type: ignore
except ImportError:  # pragma: no cover
    UTC = timezone.utc

from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.domain_settings import SettingDomain
from app.models.finance.banking import (
    BankAccount,
    BankStatement,
    BankStatementLine,
    BankStatementStatus,
    StatementLineType,
)
from app.services.common import coerce_uuid
from app.services.finance.payments.paystack_client import (
    PaystackClient,
    PaystackConfig,
    SettlementRecord,
    TransactionRecord,
    TransferRecord,
)
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    """Result of a sync operation."""

    success: bool
    statement_id: UUID | None = None
    transactions_synced: int = 0
    transfers_synced: int = 0
    settlements_synced: int = 0
    fees_synced: int = 0
    total_credits: Decimal = Decimal("0")
    total_debits: Decimal = Decimal("0")
    total_settlements: Decimal = Decimal("0")
    total_fees: Decimal = Decimal("0")
    message: str = ""


class PaystackSyncService:
    """
    Service for syncing Paystack transactions with bank statements.

    Creates bank statement entries from Paystack transactions and transfers
    for reconciliation purposes.
    """

    def __init__(self, db: Session, organization_id: UUID):
        self.db = db
        self.organization_id = coerce_uuid(organization_id)

    def _get_paystack_config(self) -> PaystackConfig:
        """Get Paystack configuration from settings."""
        secret_key = resolve_value(
            self.db,
            SettingDomain.payments,
            "paystack_secret_key",
            organization_id=self.organization_id,
        )
        public_key = resolve_value(
            self.db,
            SettingDomain.payments,
            "paystack_public_key",
            organization_id=self.organization_id,
        )

        if not secret_key or not public_key:
            raise ValueError("Paystack not configured")

        return PaystackConfig(
            secret_key=str(secret_key),
            public_key=str(public_key),
            webhook_secret=str(secret_key),
        )

    def _get_bank_accounts(self) -> tuple[BankAccount | None, BankAccount | None]:
        """Get the Paystack bank accounts for collections and transfers."""
        collection_id = resolve_value(
            self.db,
            SettingDomain.payments,
            "paystack_collection_bank_account_id",
            organization_id=self.organization_id,
        )
        transfer_id = resolve_value(
            self.db,
            SettingDomain.payments,
            "paystack_transfer_bank_account_id",
            organization_id=self.organization_id,
        )

        collection_account = None
        transfer_account = None

        if collection_id:
            collection_account = self.db.get(BankAccount, coerce_uuid(collection_id))

        if transfer_id:
            transfer_account = self.db.get(BankAccount, coerce_uuid(transfer_id))

        return collection_account, transfer_account

    def sync_transactions(
        self,
        from_date: date,
        to_date: date,
        user_id: UUID | None = None,
    ) -> SyncResult:
        """
        Sync Paystack transactions for a date range.

        Creates bank statements with transaction lines for both:
        - Collections (inbound payments) -> Paystack Collections account
        - Transfers (outbound payments) -> Paystack OPEX account

        Args:
            from_date: Start date for sync
            to_date: End date for sync
            user_id: User performing the sync

        Returns:
            SyncResult with sync statistics
        """
        config = self._get_paystack_config()
        collection_account, transfer_account = self._get_bank_accounts()

        if not collection_account and not transfer_account:
            return SyncResult(
                success=False,
                message="No Paystack bank accounts configured",
            )

        # Format dates for Paystack API
        from_str = f"{from_date.isoformat()}T00:00:00.000Z"
        to_str = f"{to_date.isoformat()}T23:59:59.999Z"

        transactions_synced = 0
        transfers_synced = 0
        settlements_synced = 0
        fees_synced = 0
        total_credits = Decimal("0")
        total_debits = Decimal("0")
        total_settlements = Decimal("0")
        total_fees = Decimal("0")

        with PaystackClient(config) as client:
            # Sync collections (inbound payments from customers)
            if collection_account:
                result = self._sync_collections(
                    client=client,
                    account=collection_account,
                    from_str=from_str,
                    to_str=to_str,
                    from_date=from_date,
                    to_date=to_date,
                    user_id=user_id,
                )
                transactions_synced = result["count"]
                total_credits = result["total"]

                # Sync settlements (payouts to merchant bank - UBA/Zenith)
                settlement_result = self._sync_settlements(
                    client=client,
                    account=collection_account,
                    from_str=from_str,
                    to_str=to_str,
                    from_date=from_date,
                    to_date=to_date,
                    user_id=user_id,
                )
                settlements_synced = settlement_result["count"]
                total_settlements = settlement_result["total"]
                fees_synced = settlement_result.get("fee_count", 0)
                total_fees = settlement_result.get("total_fees", Decimal("0"))

            # Sync transfers (outbound expense payments)
            if transfer_account:
                result = self._sync_transfers(
                    client=client,
                    account=transfer_account,
                    from_str=from_str,
                    to_str=to_str,
                    from_date=from_date,
                    to_date=to_date,
                    user_id=user_id,
                )
                transfers_synced = result["count"]
                total_debits = result["total"]

            self.db.flush()

            # Update bank account balances from live Paystack API
            if collection_account:
                # Collections balance = sum of pending settlements (unsettled funds)
                self._update_collections_balance_from_api(client, collection_account)
            if transfer_account:
                # OPEX balance = live Paystack balance (available for transfers)
                self._update_opex_balance_from_api(client, transfer_account)

        self.db.flush()

        logger.info(
            f"Paystack sync complete: {transactions_synced} collections, "
            f"{settlements_synced} settlements, {fees_synced} fees, {transfers_synced} transfers",
            extra={
                "from_date": from_date.isoformat(),
                "to_date": to_date.isoformat(),
                "credits": str(total_credits),
                "settlements": str(total_settlements),
                "fees": str(total_fees),
                "debits": str(total_debits),
            },
        )

        return SyncResult(
            success=True,
            transactions_synced=transactions_synced,
            transfers_synced=transfers_synced,
            settlements_synced=settlements_synced,
            fees_synced=fees_synced,
            total_credits=total_credits,
            total_debits=total_debits,
            total_settlements=total_settlements,
            total_fees=total_fees,
            message=f"Synced {transactions_synced} collections (₦{total_credits:,.2f}), "
            f"{settlements_synced} settlements (₦{total_settlements:,.2f}), "
            f"{fees_synced} fees (₦{total_fees:,.2f}), "
            f"{transfers_synced} transfers (₦{total_debits:,.2f})",
        )

    def _sync_collections(
        self,
        client: PaystackClient,
        account: BankAccount,
        from_str: str,
        to_str: str,
        from_date: date,
        to_date: date,
        user_id: UUID | None,
    ) -> dict:
        """Sync collection transactions to bank statement."""
        # Get or create statement
        statement = self._get_or_create_statement(
            account=account,
            from_date=from_date,
            to_date=to_date,
            source="paystack_collections",
            user_id=user_id,
        )

        # Fetch all transactions (paginate if needed)
        all_transactions: list[TransactionRecord] = []
        page = 1
        while True:
            transactions = client.list_transactions(
                from_date=from_str,
                to_date=to_str,
                status="success",  # Only successful payments
                per_page=100,
                page=page,
            )
            if not transactions:
                break
            all_transactions.extend(transactions)
            if len(transactions) < 100:
                break
            page += 1

        # Get existing transaction IDs to avoid duplicates across ALL
        # statements for this bank account (overlapping daily windows).
        existing_ids = set(
            self.db.scalars(
                select(BankStatementLine.transaction_id)
                .join(
                    BankStatement,
                    BankStatementLine.statement_id == BankStatement.statement_id,
                )
                .where(
                    BankStatement.bank_account_id == account.bank_account_id,
                    BankStatementLine.transaction_id.isnot(None),
                )
            ).all()
        )

        count = 0
        total = Decimal("0")
        line_number = len(existing_ids)

        for txn in all_transactions:
            txn_id = str(txn.id)
            if txn_id in existing_ids:
                continue

            # Convert kobo to naira
            amount = Decimal(txn.amount) / Decimal("100")
            fees = Decimal(txn.fees) / Decimal("100")
            net_amount = amount - fees

            line_number += 1
            line = BankStatementLine(
                line_id=uuid4(),
                statement_id=statement.statement_id,
                line_number=line_number,
                transaction_id=txn_id,
                transaction_date=self._parse_date(txn.paid_at or txn.created_at),
                transaction_type=StatementLineType.credit,
                amount=net_amount,  # Net after fees
                description=f"Payment: {txn.reference} via {txn.channel}",
                reference=txn.reference,
                payee_payer=txn.customer_email,
                bank_reference=txn.reference,
                is_matched=False,
                raw_data={
                    "paystack_id": txn.id,
                    "gross_amount": str(amount),
                    "fees": str(fees),
                    "channel": txn.channel,
                    "metadata": txn.metadata,
                },
                created_at=datetime.now(UTC),
            )
            self.db.add(line)
            count += 1
            total += net_amount

        # Update statement totals
        statement.total_credits += total
        if statement.closing_balance is not None:
            statement.closing_balance += total
        statement.total_lines += count
        statement.unmatched_lines += count

        return {"count": count, "total": total}

    def _sync_transfers(
        self,
        client: PaystackClient,
        account: BankAccount,
        from_str: str,
        to_str: str,
        from_date: date,
        to_date: date,
        user_id: UUID | None,
    ) -> dict:
        """Sync transfer transactions to bank statement."""
        # Get or create statement
        statement = self._get_or_create_statement(
            account=account,
            from_date=from_date,
            to_date=to_date,
            source="paystack_transfers",
            user_id=user_id,
        )

        # Fetch all transfers (paginate if needed)
        all_transfers: list[TransferRecord] = []
        page = 1
        while True:
            transfers = client.list_transfers(
                from_date=from_str,
                to_date=to_str,
                status="success",  # Only successful transfers
                per_page=100,
                page=page,
            )
            if not transfers:
                break
            all_transfers.extend(transfers)
            if len(transfers) < 100:
                break
            page += 1

        # Get existing transfer codes to avoid duplicates across ALL
        # statements for this bank account (overlapping daily windows).
        existing_codes = set(
            self.db.scalars(
                select(BankStatementLine.bank_reference)
                .join(
                    BankStatement,
                    BankStatementLine.statement_id == BankStatement.statement_id,
                )
                .where(
                    BankStatement.bank_account_id == account.bank_account_id,
                    BankStatementLine.bank_reference.isnot(None),
                )
            ).all()
        )

        count = 0
        total = Decimal("0")
        line_number = len(existing_codes)

        for txn in all_transfers:
            if txn.transfer_code in existing_codes:
                continue

            # Convert kobo to naira
            amount = Decimal(txn.amount) / Decimal("100")
            fees = Decimal(txn.fees or 0) / Decimal("100")
            total_debit = amount + fees

            line_number += 1
            line = BankStatementLine(
                line_id=uuid4(),
                statement_id=statement.statement_id,
                line_number=line_number,
                transaction_id=str(txn.id),
                transaction_date=self._parse_date(txn.updated_at or txn.created_at),
                transaction_type=StatementLineType.debit,
                amount=total_debit,  # Amount + fees
                description=f"Transfer: {txn.reason or txn.reference}",
                reference=txn.reference,
                payee_payer=txn.recipient_name,
                bank_reference=txn.transfer_code,
                bank_code=txn.recipient_bank_code,
                is_matched=False,
                raw_data={
                    "paystack_id": txn.id,
                    "transfer_code": txn.transfer_code,
                    "amount": str(amount),
                    "fees": str(fees),
                    "recipient_account": txn.recipient_account_number,
                    "recipient_bank": txn.recipient_bank_code,
                },
                created_at=datetime.now(UTC),
            )
            self.db.add(line)
            count += 1
            total += total_debit

        # Update statement totals
        statement.total_debits += total
        if statement.closing_balance is not None:
            statement.closing_balance -= total
        statement.total_lines += count
        statement.unmatched_lines += count

        return {"count": count, "total": total}

    def _sync_settlements(
        self,
        client: PaystackClient,
        account: BankAccount,
        from_str: str,
        to_str: str,
        from_date: date,
        to_date: date,
        user_id: UUID | None,
    ) -> dict:
        """Sync settlements (payouts to merchant bank) as debits on collections account."""
        # Get or create statement (same as collections)
        statement = self._get_or_create_statement(
            account=account,
            from_date=from_date,
            to_date=to_date,
            source="paystack_collections",
            user_id=user_id,
        )

        # Fetch all settlements (paginate if needed)
        all_settlements: list[SettlementRecord] = []
        page = 1
        while True:
            settlements = client.list_settlements(
                from_date=from_str,
                to_date=to_str,
                per_page=100,
                page=page,
            )
            if not settlements:
                break
            all_settlements.extend(settlements)
            if len(settlements) < 100:
                break
            page += 1

        # Get existing settlement IDs to avoid duplicates across ALL
        # statements for this bank account (not just the current one).
        # The daily sync window overlaps, so the same settlement can
        # appear in adjacent date ranges.
        existing_ids = set(
            self.db.scalars(
                select(BankStatementLine.transaction_id)
                .join(
                    BankStatement,
                    BankStatementLine.statement_id == BankStatement.statement_id,
                )
                .where(
                    BankStatement.bank_account_id == account.bank_account_id,
                    BankStatementLine.transaction_id.like("STL-%"),
                )
            ).all()
        )

        count = 0
        fee_count = 0
        total = Decimal("0")
        total_fees = Decimal("0")
        line_number = statement.total_lines or 0

        for stl in all_settlements:
            stl_id = f"STL-{stl.id}"
            settlement_date = self._parse_date(stl.settlement_date or stl.created_at)

            # Convert kobo to naira
            net_amount = Decimal(stl.net_amount) / Decimal("100")

            # Create settlement line if not exists
            if stl_id not in existing_ids:
                line_number += 1
                line = BankStatementLine(
                    line_id=uuid4(),
                    statement_id=statement.statement_id,
                    line_number=line_number,
                    transaction_id=stl_id,
                    transaction_date=settlement_date,
                    transaction_type=StatementLineType.debit,
                    amount=net_amount,
                    description=f"Settlement to bank: {stl.settlement_date[:10] if stl.settlement_date else 'N/A'}",
                    reference=f"STL-{stl.id}",
                    payee_payer="Bank Settlement",
                    bank_reference=str(stl.id),
                    is_matched=False,
                    raw_data={
                        "settlement_id": stl.id,
                        "total_amount_kobo": stl.total_amount,
                        "total_fees_kobo": stl.total_fees,
                        "net_amount_kobo": stl.net_amount,
                        "status": stl.status,
                        "is_interbank": True,
                        "transfer_type": "Paystack Settlement",
                    },
                    created_at=datetime.now(UTC),
                )
                self.db.add(line)
                count += 1
                total += net_amount

        # Update statement totals (settlements + fees are debits)
        statement.total_debits += total
        if statement.closing_balance is not None:
            statement.closing_balance -= total
        statement.total_lines += count
        statement.unmatched_lines += count

        return {
            "count": count,
            "total": total,
            "fee_count": fee_count,
            "total_fees": total_fees,
        }

    def _get_or_create_statement(
        self,
        account: BankAccount,
        from_date: date,
        to_date: date,
        source: str,
        user_id: UUID | None,
    ) -> BankStatement:
        """Get existing statement or create new one for the period."""
        statement_number = (
            f"PSK-{from_date.strftime('%Y%m%d')}-{to_date.strftime('%Y%m%d')}"
        )

        existing = self.db.scalar(
            select(BankStatement).where(
                BankStatement.bank_account_id == account.bank_account_id,
                BankStatement.statement_number == statement_number,
            )
        )

        if existing:
            return existing

        # Get last statement's closing balance as opening balance
        last_statement = self.db.scalar(
            select(BankStatement)
            .where(BankStatement.bank_account_id == account.bank_account_id)
            .order_by(BankStatement.statement_date.desc())
        )
        opening_balance = (
            last_statement.closing_balance if last_statement else Decimal("0")
        )

        statement = BankStatement(
            statement_id=uuid4(),
            organization_id=self.organization_id,
            bank_account_id=account.bank_account_id,
            statement_number=statement_number,
            statement_date=to_date,
            period_start=from_date,
            period_end=to_date,
            opening_balance=opening_balance,
            closing_balance=opening_balance,  # Will be updated as lines added
            total_credits=Decimal("0"),
            total_debits=Decimal("0"),
            currency_code=settings.default_functional_currency_code,
            status=BankStatementStatus.imported,
            import_source=source,
            imported_at=datetime.now(UTC),
            imported_by=user_id,
            total_lines=0,
            matched_lines=0,
            unmatched_lines=0,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        self.db.add(statement)
        self.db.flush()

        return statement

    def _parse_date(self, date_str: str) -> date:
        """Parse Paystack date string to date."""
        try:
            # Paystack format: 2026-02-01T15:33:38.000Z
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            return dt.date()
        except (ValueError, AttributeError):
            return date.today()

    def _update_account_balance(self, account: BankAccount) -> None:
        """Update bank account with latest statement balance."""
        latest_statement = self.db.scalar(
            select(BankStatement)
            .where(BankStatement.bank_account_id == account.bank_account_id)
            .order_by(BankStatement.statement_date.desc())
        )

        if latest_statement and latest_statement.closing_balance is not None:
            account.last_statement_balance = latest_statement.closing_balance
            if latest_statement.statement_date is not None:
                account.last_statement_date = latest_statement.statement_date
            account.updated_at = datetime.now(UTC)

    def _update_collections_balance_from_api(
        self, client: PaystackClient, account: BankAccount
    ) -> None:
        """Update Collections account balance from pending settlements.

        The Collections balance represents unsettled funds - collections
        that have been received but not yet paid out to the bank.
        This is calculated as the sum of all pending settlements.
        """
        try:
            # Sum all pending settlements to get unsettled balance
            pending_total = Decimal("0")
            page = 1
            while True:
                settlements = client.list_settlements(
                    status="pending", per_page=100, page=page
                )
                if not settlements:
                    break
                for s in settlements:
                    pending_total += Decimal(s.net_amount) / Decimal("100")
                if len(settlements) < 100:
                    break
                page += 1

            account.last_statement_balance = pending_total
            account.last_statement_date = date.today()
            account.updated_at = datetime.now(UTC)
            logger.info(
                f"Updated {account.account_name} unsettled balance: ₦{pending_total:,.2f}"
            )
        except Exception as e:
            logger.warning(f"Failed to fetch pending settlements: {e}")
            self._update_account_balance(account)

    def _update_opex_balance_from_api(
        self, client: PaystackClient, account: BankAccount
    ) -> None:
        """Update OPEX account balance from live Paystack balance.

        The OPEX balance represents funds available for transfers/payments.
        This is the live Paystack balance.
        """
        try:
            balance_data = client.get_balance()
            for b in balance_data:
                if (
                    b.get("currency", settings.default_functional_currency_code)
                    == settings.default_functional_currency_code
                ):
                    balance_kobo = b.get("balance", 0)
                    balance_naira = Decimal(balance_kobo) / Decimal("100")
                    account.last_statement_balance = balance_naira
                    account.last_statement_date = date.today()
                    account.updated_at = datetime.now(UTC)
                    logger.info(
                        f"Updated {account.account_name} balance: ₦{balance_naira:,.2f}"
                    )
                    break
        except Exception as e:
            logger.warning(f"Failed to fetch Paystack balance: {e}")
            self._update_account_balance(account)

    def get_balance(self) -> list[dict[str, Any]]:
        """
        Get current Paystack balance.

        Returns:
            List of balance entries
        """
        config = self._get_paystack_config()
        with PaystackClient(config) as client:
            return cast(list[dict[str, Any]], client.get_balance())
