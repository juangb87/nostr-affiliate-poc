# Payment ledger and payout state machine — Sprint 2

This document describes the durable, provider-independent accounting layer introduced in application version `0.8.0`.

## Scope

Sprint 2 records what each merchant campaign owes, reserves budget, tracks every payment attempt, and prevents an ambiguous Lightning response from being retried automatically. It does not introduce a new real-money payment rail. Existing NWC execution remains explicitly admin-triggered; provider adapters are Sprint 3.

## Merchant-direct accounting model

Meerat does not custody a shared pool of merchant funds. Each campaign has an internal budget cap used to authorize obligations:

```text
budget_sats = committed_sats + settled_sats + available_sats
```

Each approved conversion creates two separate obligations:

```text
affiliate commission = full promised reward
Meerat fee           = max(commission × MEERAT_FEE_BPS / 10,000, FEE_MIN_SATS)
```

The fee never reduces the affiliate commission. Reservation succeeds atomically only when the campaign can cover both amounts. Insufficient budget creates an `ON_HOLD` payout and pauses the campaign.

## Payout state machine

```text
PAYABLE ──claim──> PAYING ──wallet evidence──> SETTLED ──Nostr proof──> PUBLISHED
   │                   │
   │                   ├── definitive unpaid ──> FAILED ──new attempt──> PAYING
   │                   └── ambiguous result ───> PAYING + UNKNOWN attempt
   └── reversal ──> CANCELLED

ON_HOLD ──budget top-up + explicit release──> PAYABLE

PAYING + reversal ──> CANCEL_PENDING
    ├── reconcile FAILED  ──> CANCELLED + release full reservation
    └── reconcile SETTLED ──> PUBLISHED + cancel/release only unpaid fee
```

An `UNKNOWN` attempt means the payment may have succeeded. The system will not create another attempt until an operator reconciles it as `SETTLED` or definitely `FAILED`.

`fee_state` is tracked independently. After the affiliate commission is published, the fee remains `FEE_PENDING`; fee execution is intentionally deferred to the provider-adapter sprint.

## Durable tables

### `campaign_budgets`

- `campaign_id` — one row per campaign
- `budget_sats`
- `committed_sats` — reserved but not yet settled
- `settled_sats`
- `updated_at`

### `payment_attempts`

One immutable attempt identity per payout, kind, and attempt number:

- `kind`: `commission` or `fee`
- `rail`: `nwc`, `blink`, `fake`, or `sandbox`
- stable SHA-256 `idempotency_key`
- private destination and optional private preimage
- amount, payment hash, routing fee, error, timestamps
- status: `PAYING`, `SETTLED`, `FAILED`, or `UNKNOWN`

The admin API never returns the preimage. Public payout JSON uses a strict allowlist: BOLT11 invoices, Lightning destinations, provider errors, processing timestamps, attempt counters and reserved-budget values remain private. They are not exposed through aggregate flow/campaign/affiliate endpoints or Nostr events.

### `ledger_entries`

Append-only balanced pairs. Every transaction has equal debit and credit totals:

- reservation: `merchant_budget_available → payout_reserved`
- commission settlement: `payout_reserved → affiliate_paid`
- release/cancellation: `payout_reserved → merchant_budget_available`

Every entry has a unique idempotency key. PostgreSQL budget rows are locked before checking that key and changing counters, so concurrent reserve, settlement or release calls cannot move counters twice.

## Operational API

All endpoints below require `Authorization: Bearer <PAYOUT_ADMIN_KEY>`:

```text
GET  /admin/campaigns/{campaign_id}/budget
PUT  /admin/campaigns/{campaign_id}/budget
POST /admin/payouts/{payout_id}/release-hold
GET  /admin/payouts/{payout_id}/attempts
GET  /admin/payouts/{payout_id}/ledger
POST /admin/payouts/{payout_id}/execute
GET  /admin/payment-attempts/recovery?older_than_seconds=60
POST /admin/payment-attempts/{attempt_id}/refresh
POST /admin/payment-attempts/{attempt_id}/reconcile
```

Reconciliation payloads:

```json
{"outcome":"FAILED","error":"wallet confirmed unpaid"}
```

or:

```json
{"outcome":"SETTLED","payment_hash":"<64 hex>","routing_fee_sats":1}
```

The `SETTLED` path records durable payment evidence and publishes the existing kind `2802` Nostr proof without sending another payment.

## Crash-recovery invariants

1. The payout claim and creation of the `PAYING` attempt commit in one database transaction.
2. The durable attempt and its idempotency key are stored before calling any payment rail. NWC may prepare a private invoice inside its adapter; Blink can pay a Lightning Address directly.
3. Wallet success is stored as `SETTLED` before proof publication.
4. If proof publication fails, retry publishes the proof only and never pays again.
5. If the worker disappears during a provider call, the stale `PAYING` attempt appears in the recovery endpoint.
6. `UNKNOWN` never transitions to retryable `FAILED` without explicit reconciliation.
7. There can be only one claimed payout state at a time; atomic `UPDATE ... WHERE state IN ('PAYABLE','FAILED')` prevents concurrent execution claims.
8. Execution also requires a complete reservation and a non-reversed conversion; legacy unpaid payouts without ledger reservations migrate to `ON_HOLD`.
9. Reconciliation locks the attempt and payout and uses compare-and-swap status predicates, so conflicting operator outcomes cannot both commit.

## Configuration

```text
DEFAULT_CAMPAIGN_BUDGET_SATS=1000000
MEERAT_FEE_BPS=1000
FEE_MIN_SATS=10
DEFAULT_RETURN_WINDOW_DAYS=0
PAYOUT_ADMIN_KEY=<secret>
```

For production, set campaign budgets explicitly through the admin API rather than relying on the default. No credential, NWC URI, invoice, destination, or preimage belongs in Nostr proofs or logs.
