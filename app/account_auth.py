from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone
from typing import Any

from nostr_sdk import Event, PublicKey

# Ephemeral Meerat authentication proof. Do not use 22242: Amber and other
# signers correctly reserve that kind for NIP-42 relay authentication.
AUTH_EVENT_KIND = 27236
ALLOWED_ROLES = {"merchant", "affiliate", "ops"}
SESSION_COOKIE = "meerat_session"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def random_token(size: int = 32) -> str:
    return secrets.token_urlsafe(size)


def normalize_role(value: str) -> str:
    role = str(value or "").strip().lower()
    if role not in ALLOWED_ROLES:
        raise ValueError("role must be merchant, affiliate, or ops")
    return role


def _single_tag(tags: list[list[str]], name: str) -> str:
    values = [tag[1] for tag in tags if len(tag) >= 2 and tag[0] == name]
    if len(values) != 1 or not values[0]:
        raise ValueError(f"authentication event requires exactly one {name} tag")
    return values[0]


def verify_auth_event(
    event_json: dict[str, Any],
    *,
    expected_challenge: str,
    expected_role: str,
    expected_relay: str,
    max_clock_skew_seconds: int = 90,
) -> dict[str, str]:
    """Verify a one-use Meerat browser authentication event."""
    try:
        event = Event.from_json(json.dumps(event_json, separators=(",", ":")))
    except Exception as exc:
        raise ValueError("invalid Nostr event") from exc
    if not event.verify():
        raise ValueError("invalid Nostr event signature or id")

    canonical = json.loads(event.as_json())
    if int(canonical.get("kind", -1)) != AUTH_EVENT_KIND:
        raise ValueError(f"authentication event kind must be {AUTH_EVENT_KIND}")
    if canonical.get("content", "") != "":
        raise ValueError("authentication event content must be empty")

    created_at = int(canonical.get("created_at", 0))
    age = abs(int(utcnow().timestamp()) - created_at)
    if age > max_clock_skew_seconds:
        raise ValueError("authentication event timestamp is outside the allowed window")

    tags = canonical.get("tags") or []
    challenge = _single_tag(tags, "challenge")
    role = _single_tag(tags, "role")
    relay = _single_tag(tags, "relay").rstrip("/")
    if not secrets.compare_digest(challenge, expected_challenge):
        raise ValueError("authentication challenge does not match")
    if not secrets.compare_digest(role, expected_role):
        raise ValueError("authentication role does not match")
    if not secrets.compare_digest(relay, expected_relay.rstrip("/")):
        raise ValueError("authentication origin does not match")

    pubkey_hex = str(canonical["pubkey"]).lower()
    public_key = PublicKey.parse(pubkey_hex)
    return {
        "hex": public_key.to_hex(),
        "npub": public_key.to_bech32(),
        "event_id": str(canonical["id"]),
    }


def parse_pubkey_set(raw: str) -> set[str]:
    values: set[str] = set()
    for item in str(raw or "").split(","):
        candidate = item.strip()
        if not candidate:
            continue
        try:
            values.add(PublicKey.parse(candidate).to_hex())
        except Exception as exc:
            raise ValueError("invalid pubkey in allowlist") from exc
    return values


def parse_merchant_bindings(raw: str) -> list[tuple[str, str]]:
    """Parse owner:merchant pairs, normalizing both identities to hex."""
    bindings: list[tuple[str, str]] = []
    for item in str(raw or "").split(","):
        candidate = item.strip()
        if not candidate:
            continue
        if ":" not in candidate:
            raise ValueError("merchant binding must use owner_pubkey:merchant_pubkey")
        owner, merchant = candidate.split(":", 1)
        try:
            owner_hex = PublicKey.parse(owner.strip()).to_hex()
            merchant_hex = PublicKey.parse(merchant.strip()).to_hex()
        except Exception as exc:
            raise ValueError("invalid pubkey in merchant binding") from exc
        bindings.append((owner_hex, merchant_hex))
    return bindings
