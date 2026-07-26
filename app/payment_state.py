"""Payment ledger and payout state-machine primitives for Meerat Sprint 2."""

from __future__ import annotations

import hashlib

PAYOUT_STATES = {
    "PAYABLE",
    "PAYING",
    "SETTLED",
    "PUBLISHED",
    "FAILED",
    "ON_HOLD",
    "CANCEL_PENDING",
    "CANCELLED",
}
FEE_STATES = {"FEE_PENDING", "FEE_PAYING", "FEE_SETTLED", "FEE_FAILED", "CANCELLED"}
ATTEMPT_KINDS = {"commission", "fee"}
ATTEMPT_STATUSES = {"PAYING", "SETTLED", "FAILED", "UNKNOWN"}
ACTIVE_ATTEMPT_STATUSES = {"PAYING", "UNKNOWN"}
RAILS = {"nwc", "blink", "fake", "sandbox"}


def calculate_fee_sats(commission_sats: int, fee_bps: int, minimum_sats: int) -> int:
    if commission_sats <= 0 or fee_bps <= 0:
        return 0
    return max((commission_sats * fee_bps) // 10_000, max(0, minimum_sats))


def payment_idempotency_key(payout_id: str, kind: str, attempt_group: int) -> str:
    raw = f"{payout_id}:{kind}:{attempt_group}".encode()
    return hashlib.sha256(raw).hexdigest()
