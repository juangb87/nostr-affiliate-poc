"""Provider-independent Lightning payment rails for Meerat.

The durable ledger owns idempotency and state transitions. Rails only normalize provider
operations into explicit SUCCESS, PENDING, or FAILURE results. Secrets and preimages must
never be returned by public API serializers or written to logs.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Protocol, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from app.lightning import (
    LightningPaymentError,
    NwcPaymentError,
    lookup_nwc_payment,
    pay_nwc_invoice,
    prepare_lnurl_payment,
)


class PaymentStatus(str, Enum):
    SUCCESS = "SUCCESS"
    PENDING = "PENDING"
    FAILURE = "FAILURE"


@dataclass(frozen=True)
class PaymentResult:
    status: PaymentStatus
    payment_hash: str | None = None
    preimage: str | None = None
    fee_paid_sats: int | None = None
    fee_paid_msats: int | None = None
    error: str | None = None
    error_code: str | None = None
    provider_reference: str | None = None
    retryable: bool = False


class PaymentRailAmbiguousError(RuntimeError):
    """The provider may have accepted a payment; automatic retry is unsafe."""

    def __init__(self, message: str, *, payment_hash: str | None = None, provider_reference: str | None = None):
        super().__init__(message)
        self.payment_hash = payment_hash
        self.provider_reference = provider_reference


def _valid_payment_hash(value: Any) -> bool:
    candidate = str(value or "").lower()
    return len(candidate) == 64 and all(ch in "0123456789abcdef" for ch in candidate)


@runtime_checkable
class PaymentRail(Protocol):
    name: str

    async def get_balance(self) -> int:
        """Return the spendable BTC wallet balance in sats."""

    async def pay_to_lightning_address(
        self,
        address: str,
        amount_sats: int,
        memo: str,
        idempotency_key: str,
    ) -> PaymentResult:
        """Pay a Lightning Address using a durable caller-supplied identity."""

    async def pay_invoice(self, bolt11: str, idempotency_key: str) -> PaymentResult:
        """Pay a BOLT11 invoice using a durable caller-supplied identity."""

    async def lookup_payment(self, reference: str) -> PaymentResult | None:
        """Resolve an existing provider payment without creating a new payment."""


class FakePaymentRail:
    """Deterministic in-memory rail for state-machine and recovery tests."""

    name = "fake"

    def __init__(self, outcomes: list[PaymentResult] | None = None, balance_sats: int = 1_000_000):
        self._outcomes = deque(outcomes or [])
        self._balance_sats = balance_sats
        self._payments: dict[str, PaymentResult] = {}
        self.payment_calls = 0

    async def get_balance(self) -> int:
        return self._balance_sats

    def _next_result(self, idempotency_key: str, amount_sats: int) -> PaymentResult:
        if idempotency_key in self._payments:
            return self._payments[idempotency_key]
        self.payment_calls += 1
        if self._outcomes:
            result = self._outcomes.popleft()
        else:
            result = PaymentResult(
                status=PaymentStatus.SUCCESS,
                payment_hash=hashlib.sha256(f"fake:{idempotency_key}".encode()).hexdigest(),
                fee_paid_sats=0,
                provider_reference=f"fake:{idempotency_key}",
            )
        self._payments[idempotency_key] = result
        if result.provider_reference:
            self._payments[result.provider_reference] = result
        if result.status == PaymentStatus.SUCCESS:
            self._balance_sats -= amount_sats
        return result

    async def pay_to_lightning_address(
        self, address: str, amount_sats: int, memo: str, idempotency_key: str
    ) -> PaymentResult:
        if not address or amount_sats <= 0:
            return PaymentResult(status=PaymentStatus.FAILURE, error="invalid payment request", retryable=False)
        return self._next_result(idempotency_key, amount_sats)

    async def pay_invoice(self, bolt11: str, idempotency_key: str) -> PaymentResult:
        if not bolt11:
            return PaymentResult(status=PaymentStatus.FAILURE, error="missing invoice", retryable=False)
        return self._next_result(idempotency_key, 0)

    async def lookup_payment(self, reference: str) -> PaymentResult | None:
        return self._payments.get(reference)

    def set_lookup_result(self, reference: str, result: PaymentResult) -> None:
        self._payments[reference] = result


class NwcPaymentRail:
    """Compatibility adapter around the existing LNURL + NWC implementation."""

    name = "nwc"

    def __init__(self, prepare=None, pay=None, lookup=None, balance=None):
        self._prepare = prepare or prepare_lnurl_payment
        self._pay = pay or pay_nwc_invoice
        self._lookup = lookup or lookup_nwc_payment
        self._balance = balance
        self._prepared_evidence_recorder = None

    def set_prepared_evidence_recorder(self, recorder) -> None:
        self._prepared_evidence_recorder = recorder

    async def get_balance(self) -> int:
        if self._balance is None:
            raise RuntimeError("NWC balance lookup is available through the readiness probe")
        return int(await self._balance())

    async def pay_to_lightning_address(
        self, address: str, amount_sats: int, memo: str, idempotency_key: str
    ) -> PaymentResult:
        try:
            invoice, expected_hash = await self._prepare(address, amount_sats)
            if not _valid_payment_hash(expected_hash):
                raise LightningPaymentError("LNURL returned an invalid payment hash")
            if self._prepared_evidence_recorder is not None:
                recorded = self._prepared_evidence_recorder(invoice, expected_hash)
                if inspect.isawaitable(recorded):
                    await recorded
        except LightningPaymentError as exc:
            return PaymentResult(status=PaymentStatus.FAILURE, error=str(exc), retryable=False)
        try:
            paid = await self._pay(invoice, expected_hash)
        except NwcPaymentError as exc:
            raise PaymentRailAmbiguousError(
                str(exc), payment_hash=expected_hash, provider_reference=expected_hash
            ) from exc
        except LightningPaymentError as exc:
            return PaymentResult(status=PaymentStatus.FAILURE, error=str(exc), retryable=False)
        return PaymentResult(
            status=PaymentStatus.SUCCESS,
            payment_hash=paid.payment_hash,
            fee_paid_sats=None if paid.fees_paid_msats is None else (paid.fees_paid_msats + 999) // 1000,
            fee_paid_msats=paid.fees_paid_msats,
            provider_reference=paid.payment_hash,
        )

    async def pay_invoice(self, bolt11: str, idempotency_key: str) -> PaymentResult:
        raise RuntimeError("NWC invoice payments require an expected payment hash")

    async def lookup_payment(self, reference: str) -> PaymentResult | None:
        lookup = await self._lookup(reference)
        return PaymentResult(
            status=PaymentStatus(lookup.status),
            payment_hash=lookup.payment_hash,
            preimage=None,
            fee_paid_sats=lookup.fee_paid_sats,
            fee_paid_msats=lookup.fee_paid_msats,
            provider_reference=lookup.provider_reference,
            retryable=False,
        )


BlinkTransport = Callable[..., dict[str, Any]]

BLINK_BALANCE_QUERY = """
query MeeratBlinkBalance {
  me { defaultAccount { wallets { id walletCurrency balance } } }
}
"""

BLINK_ADDRESS_PAYMENT_MUTATION = """
mutation MeeratLnAddressPaymentSend($input: LnAddressPaymentSendInput!) {
  lnAddressPaymentSend(input: $input) {
    status
    errors { code message path }
    transaction {
      id
      settlementFee
      status
      initiationVia { ... on InitiationViaLn { paymentHash } }
    }
  }
}
"""

BLINK_INVOICE_PAYMENT_MUTATION = """
mutation MeeratLnInvoicePaymentSend($input: LnInvoicePaymentInput!) {
  lnInvoicePaymentSend(input: $input) {
    status
    errors { code message path }
    transaction {
      id
      settlementFee
      status
      initiationVia { ... on InitiationViaLn { paymentHash } }
    }
  }
}
"""

BLINK_TRANSACTION_QUERY = """
query MeeratBlinkTransaction($walletId: WalletId!, $transactionId: ID!) {
  me {
    defaultAccount {
      walletById(walletId: $walletId) {
        transactionById(transactionId: $transactionId) {
          id
          settlementFee
          status
          initiationVia { ... on InitiationViaLn { paymentHash } }
        }
      }
    }
  }
}
"""


class BlinkAdapter:
    """Opt-in Blink GraphQL adapter. Constructing it never performs a network call."""

    name = "blink"

    def __init__(
        self,
        *,
        api_key: str,
        wallet_id: str,
        endpoint: str = "https://api.staging.blink.sv/graphql",
        transport: BlinkTransport | None = None,
    ):
        if not api_key:
            raise RuntimeError("BLINK_API_KEY is required")
        if not wallet_id:
            raise RuntimeError("BLINK_WALLET_ID is required")
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise RuntimeError("Blink endpoint must be HTTPS")
        self._api_key = api_key
        self.wallet_id = wallet_id
        self.endpoint = endpoint
        self._transport = transport or self._http_transport

    def __repr__(self) -> str:
        return f"BlinkAdapter(wallet_id={self.wallet_id!r}, endpoint={self.endpoint!r}, api_key=[REDACTED])"

    __str__ = __repr__

    @staticmethod
    def _http_transport(
        query: str,
        variables: dict[str, Any],
        *,
        endpoint: str,
        api_key: str,
    ) -> dict[str, Any]:
        request = Request(
            endpoint,
            data=json.dumps({"query": query, "variables": variables}).encode(),
            headers={"Content-Type": "application/json", "X-API-KEY": api_key, "User-Agent": "Meerat-Payments/1.0"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=20) as response:
                raw = response.read(262_145)
                if len(raw) > 262_144:
                    raise RuntimeError("Blink response is too large")
                if response.status != 200:
                    raise RuntimeError("Blink returned a non-200 status")
        except (HTTPError, URLError, TimeoutError) as exc:
            raise PaymentRailAmbiguousError("Blink request failed; reconciliation required") from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PaymentRailAmbiguousError("Blink returned an invalid response; reconciliation required") from exc
        if not isinstance(payload, dict):
            raise PaymentRailAmbiguousError("Blink returned an invalid response; reconciliation required")
        return payload

    async def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._transport,
            query,
            variables,
            endpoint=self.endpoint,
            api_key=self._api_key,
        )

    @staticmethod
    def _errors(payload: dict[str, Any], field: str) -> list[dict[str, Any]]:
        top = payload.get("errors") or []
        nested = ((payload.get("data") or {}).get(field) or {}).get("errors") or []
        return [row for row in [*top, *nested] if isinstance(row, dict)]

    @staticmethod
    def _failure(errors: list[dict[str, Any]], fallback: str = "Blink payment failed") -> PaymentResult:
        first = errors[0] if errors else {}
        code = str(first.get("code") or "BLINK_FAILURE").upper()
        message = str(first.get("message") or fallback)
        permanent_codes = {
            "INSUFFICIENT_BALANCE",
            "INVALID_INPUT",
            "INVALID_AMOUNT",
            "INVALID_LIGHTNING_ADDRESS",
            "ACCOUNT_LOCKED",
            "WALLET_LIMIT_REACHED",
        }
        return PaymentResult(
            status=PaymentStatus.FAILURE,
            error=message[:500],
            error_code=code,
            retryable=code not in permanent_codes,
        )

    @staticmethod
    def _payment_result(payload: dict[str, Any], field: str) -> PaymentResult:
        errors = BlinkAdapter._errors(payload, field)
        body = ((payload.get("data") or {}).get(field) or {})
        if errors or str(body.get("status", "")).upper() == "FAILURE":
            return BlinkAdapter._failure(errors)
        transaction = body.get("transaction") or {}
        initiation = transaction.get("initiationVia") or {}
        payment_hash = initiation.get("paymentHash") or transaction.get("paymentHash") or body.get("paymentHash")
        reference = transaction.get("id") or body.get("id")
        raw_fee = transaction.get("settlementFee")
        try:
            fee = None if raw_fee is None else abs(int(raw_fee))
        except (TypeError, ValueError):
            fee = None
        provider_status = str(body.get("status", "")).upper()
        if provider_status not in {"SUCCESS", "ALREADY_PAID"} or not _valid_payment_hash(payment_hash):
            return PaymentResult(
                status=PaymentStatus.PENDING,
                provider_reference=None if reference is None else str(reference),
                fee_paid_sats=fee,
            )
        return PaymentResult(
            status=PaymentStatus.SUCCESS,
            payment_hash=str(payment_hash).lower(),
            fee_paid_sats=fee,
            provider_reference=None if reference is None else str(reference),
        )

    async def get_balance(self) -> int:
        payload = await self._graphql(BLINK_BALANCE_QUERY, {})
        errors = self._errors(payload, "me")
        if errors:
            raise RuntimeError(self._failure(errors, "Blink balance lookup failed").error)
        wallets = ((((payload.get("data") or {}).get("me") or {}).get("defaultAccount") or {}).get("wallets") or [])
        for wallet in wallets:
            if wallet.get("id") == self.wallet_id and str(wallet.get("walletCurrency", "")).upper() == "BTC":
                return int(wallet["balance"])
        raise RuntimeError("configured Blink BTC wallet was not found")

    async def pay_to_lightning_address(
        self, address: str, amount_sats: int, memo: str, idempotency_key: str
    ) -> PaymentResult:
        if not address or amount_sats <= 0:
            return PaymentResult(status=PaymentStatus.FAILURE, error="invalid payment request", retryable=False)
        payload = await self._graphql(
            BLINK_ADDRESS_PAYMENT_MUTATION,
            {"input": {"walletId": self.wallet_id, "lnAddress": address, "amount": amount_sats}},
        )
        return self._payment_result(payload, "lnAddressPaymentSend")

    async def pay_invoice(self, bolt11: str, idempotency_key: str) -> PaymentResult:
        if not bolt11:
            return PaymentResult(status=PaymentStatus.FAILURE, error="missing invoice", retryable=False)
        payload = await self._graphql(
            BLINK_INVOICE_PAYMENT_MUTATION,
            {"input": {"walletId": self.wallet_id, "paymentRequest": bolt11}},
        )
        return self._payment_result(payload, "lnInvoicePaymentSend")

    async def lookup_payment(self, reference: str) -> PaymentResult | None:
        if not reference:
            return None
        payload = await self._graphql(
            BLINK_TRANSACTION_QUERY,
            {"walletId": self.wallet_id, "transactionId": reference},
        )
        if payload.get("errors"):
            raise RuntimeError("Blink transaction lookup failed")
        transaction = (((((payload.get("data") or {}).get("me") or {}).get("defaultAccount") or {}).get("walletById") or {}).get("transactionById"))
        if not isinstance(transaction, dict):
            return None
        status = str(transaction.get("status") or "").upper()
        if status == "FAILURE":
            return PaymentResult(
                status=PaymentStatus.FAILURE,
                error="Blink confirmed payment failure",
                error_code="BLINK_TRANSACTION_FAILURE",
                provider_reference=str(transaction.get("id") or reference),
                retryable=False,
            )
        return self._payment_result(
            {"data": {"transactionLookup": {"status": status, "errors": [], "transaction": transaction}}},
            "transactionLookup",
        )


def build_payment_rail() -> PaymentRail:
    name = os.getenv("PAYMENT_RAIL", "nwc").strip().lower()
    if name == "nwc":
        return NwcPaymentRail()
    if name == "fake":
        if os.getenv("ALLOW_FAKE_PAYMENT_RAIL", "false").strip().lower() not in {"1", "true", "yes", "on"}:
            raise RuntimeError("fake payment rail is disabled")
        return FakePaymentRail()
    if name == "blink":
        key = os.getenv("BLINK_API_KEY", "").strip()
        wallet_id = os.getenv("BLINK_WALLET_ID", "").strip()
        if not key:
            raise RuntimeError("BLINK_API_KEY is required")
        if not wallet_id:
            raise RuntimeError("BLINK_WALLET_ID is required")
        endpoint = os.getenv("BLINK_GRAPHQL_URL", "https://api.staging.blink.sv/graphql").strip()
        return BlinkAdapter(api_key=key, wallet_id=wallet_id, endpoint=endpoint)
    raise RuntimeError(f"unsupported payment rail: {name}")
