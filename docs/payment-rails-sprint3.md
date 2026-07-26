# Payment rails — Sprint 3

Sprint 3 introduces the provider boundary used by the durable payout ledger in application version `0.9.0`. It does **not** enable autonomous payments, execute platform fees, or move funds during deployment verification.

## Architecture

```text
payout ledger / state machine
        │ durable claim + payment_attempt
        ▼
PaymentRail protocol
        ├── FakePaymentRail   deterministic tests, no network
        ├── NwcPaymentRail    compatibility wrapper around LNURL + NWC
        └── BlinkAdapter      opt-in GraphQL adapter, staging by default
```

The worker knows only normalized rail results:

```text
SUCCESS → persist payment evidence → SETTLED → publish proof → PUBLISHED
PENDING → UNKNOWN attempt; never automatically retry
FAILURE → FAILED with retryability/error metadata
exception after provider call begins → UNKNOWN; manually reconcile
```

The database claim and `PAYING` attempt are committed before the rail is called. A provider adapter cannot bypass campaign reservation, return-window, reversal, amount-limit, or admin-auth checks.

## Contract

`app/payment_rails.py` defines:

```python
class PaymentRail(Protocol):
    async def get_balance(self) -> int: ...
    async def pay_to_lightning_address(
        self, address: str, amount_sats: int, memo: str, idempotency_key: str
    ) -> PaymentResult: ...
    async def pay_invoice(self, bolt11: str, idempotency_key: str) -> PaymentResult: ...
    async def lookup_payment(self, reference: str) -> PaymentResult | None: ...
```

`PaymentResult` contains normalized status, payment hash, provider reference, routing fee, safe error code/message, and retryability. A preimage may exist in the internal type for a future encrypted-at-rest implementation, but Sprint 3 does not persist or return it.

## FakePaymentRail

The fake rail is deterministic and keeps repeated calls with the same idempotency key from creating a second fake payment. Tests can queue:

- `SUCCESS`
- `PENDING`
- retryable or final `FAILURE`
- later lookup results for recovery

Environment construction is disabled unless both are explicit:

```text
PAYMENT_RAIL=fake
ALLOW_FAKE_PAYMENT_RAIL=true
```

Fake mode is intended for local tests and controlled staging only. It must never be presented as evidence that sats moved.

## NWC compatibility adapter

`NwcPaymentRail` wraps the existing validated LNURL invoice preparation and Alby Hub NWC payment implementation. The worker no longer depends directly on NWC-specific functions.

- LNURL/validation failure before payment is normalized as `FAILURE`.
- NWC failures that may have paid raise an ambiguous result and become `UNKNOWN`.
- Existing admin trigger and `LIGHTNING_PAYOUTS_ENABLED` gate remain mandatory.
- `PAYMENT_RAIL` defaults to `nwc` for backward compatibility.

## Blink adapter

`BlinkAdapter` is opt-in and constructing it performs no network request. Its default endpoint is staging:

```text
PAYMENT_RAIL=blink
BLINK_GRAPHQL_URL=https://api.staging.blink.sv/graphql
BLINK_API_KEY=<secret>
BLINK_WALLET_ID=<dedicated BTC wallet id>
```

It supports the provider boundary for:

- BTC-wallet balance lookup;
- Lightning Address payment;
- BOLT11 invoice payment;
- normalization of GraphQL status/errors and transaction evidence.

A Blink `SUCCESS` without a payment hash remains `PENDING`, because settlement proof is incomplete. Recovery uses the confirmed public GraphQL schema path `me.defaultAccount.walletById(...).transactionById(...)` and normalizes its `SUCCESS`, `PENDING`, or `FAILURE` status without creating a new payment. Sprint 4 will validate these operations against the dedicated Lightning Koffee staging wallet.

Blink credentials are never included in representations, normalized errors, attempts, public APIs, Nostr proofs, or logs. Merely setting credentials does not initiate a payment: execution still requires the authenticated admin endpoint and all ledger guards.

## Recovery

```text
GET  /admin/payment-attempts/recovery
POST /admin/payment-attempts/{attempt_id}/refresh
POST /admin/payment-attempts/{attempt_id}/reconcile
```

`refresh` calls only `lookup_payment` for the existing attempt. It never calls a payment method. Confirmed success/failure is passed through the same locked reconciliation path used by manual operations. Unresolved evidence remains `UNKNOWN`.

Read-only rail balance:

```text
GET /admin/payment-rail/balance
```

All endpoints require `PAYOUT_ADMIN_KEY`.

## Safety invariants

1. No rail call before the durable `PAYING` attempt exists.
2. Never more than one live attempt for `(payout, kind)`.
3. `PENDING`, timeout, malformed provider response, and unclassified post-call exceptions are never automatically repayable.
4. Reversed and unreserved obligations cannot execute.
5. Settlement is durable before Nostr proof publication.
6. Provider credentials, BOLT11, destinations, internal errors, and preimages remain outside public responses and Nostr events.
7. The affiliate reward and Meerat fee remain separate obligations; Sprint 3 executes only the existing affiliate-commission path.
8. Real adapters are opt-in and remain admin-triggered. Sprint 3 deployment verification must not call `/execute`.
