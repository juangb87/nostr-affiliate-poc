"""Permanent local tombstones for erased Nostr identities."""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any

from sqlalchemy import text


ERASURE_MESSAGE = "Esta identidad Nostr fue eliminada y no puede volver a registrarse."


def identity_erasure_hmac(pubkey_hex: str) -> str:
    canonical = str(pubkey_hex or "").strip().lower()
    if len(canonical) != 64 or any(char not in "0123456789abcdef" for char in canonical):
        raise ValueError("pubkey_hex must be a canonical 64-character hex key")
    pepper = os.getenv("ACCOUNT_ERASURE_PEPPER")
    if not pepper:
        raise RuntimeError("ACCOUNT_ERASURE_PEPPER is required")
    return hmac.new(pepper.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def lock_nostr_identity(connection: Any, pubkey_hex: str) -> None:
    """Serialize erasure with login/enrollment for the same identity on PostgreSQL."""
    canonical = str(pubkey_hex or "").strip().lower()
    if connection.engine.dialect.name == "postgresql":
        connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"nostr-identity:{canonical}"},
        )


def is_nostr_identity_erased(connection: Any, pubkey_hex: str) -> bool:
    if not connection.execute(text("SELECT 1 FROM erased_nostr_identities LIMIT 1")).first():
        return False
    digest = identity_erasure_hmac(pubkey_hex)
    return bool(
        connection.execute(
            text("SELECT 1 FROM erased_nostr_identities WHERE identity_hmac=:digest LIMIT 1"),
            {"digest": digest},
        ).first()
    )
