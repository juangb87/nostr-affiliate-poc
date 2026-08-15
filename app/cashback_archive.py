"""Exact, auditable logical archive for Cashback Express campaigns and claims."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import bindparam, text

TERMINAL_REWARD_STATES = {"paid", "declined"}


class ArchiveRefused(RuntimeError):
    pass


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _rows(connection, statement, params=None):
    return [dict(r._mapping) for r in connection.execute(statement, params or {}).fetchall()]


def _fingerprints(rows):
    return {str(r["id"] if "id" in r else r["webhook_id"]): hashlib.sha256(_canonical(r).encode()).hexdigest() for r in rows}


def build_cashback_archive_preview(connection, *, operation: str, campaign_id: str,
        claim_ids: list[str], expected_merchant_hex: str, expected_campaign_name: str,
        expected_campaign_status: str, reason: str, lock_rows: bool = False) -> dict[str, Any]:
    if operation not in {"campaign", "claims"}:
        raise ArchiveRefused("operation must be campaign or claims")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_merchant_hex):
        raise ArchiveRefused("expected merchant must be a lowercase 64-character hex pubkey")
    lock = " FOR UPDATE" if lock_rows and connection.engine.dialect.name == "postgresql" else ""
    row = connection.execute(text(f"SELECT * FROM cashback_campaigns WHERE id=:id{lock}"), {"id": campaign_id}).fetchone()
    if not row:
        raise ArchiveRefused("cashback campaign not found")
    campaign = dict(row._mapping)
    if campaign["merchant_pubkey_hex"] != expected_merchant_hex:
        raise ArchiveRefused("cashback campaign owner mismatch")
    if campaign["name"] != expected_campaign_name:
        raise ArchiveRefused("cashback campaign name mismatch")
    if campaign["status"] != expected_campaign_status:
        raise ArchiveRefused("cashback campaign status changed")
    if campaign.get("archived_at"):
        raise ArchiveRefused("cashback campaign is already archived")
    requested = sorted(set(claim_ids))
    if operation == "campaign":
        if expected_campaign_status != "paused":
            raise ArchiveRefused("whole-campaign archive requires paused status")
        claims = _rows(connection, text(f"SELECT * FROM cashback_claims WHERE campaign_id=:id ORDER BY id{lock}"), {"id": campaign_id})
    else:
        if expected_campaign_status != "active" or not requested:
            raise ArchiveRefused("claim archive requires an active campaign and explicit claim IDs")
        stmt = text(f"SELECT * FROM cashback_claims WHERE campaign_id=:campaign AND id IN :ids ORDER BY id{lock}").bindparams(bindparam("ids", expanding=True))
        claims = _rows(connection, stmt, {"campaign": campaign_id, "ids": requested})
        if [r["id"] for r in claims] != requested:
            raise ArchiveRefused("claim target set does not match the campaign")
    ids = [r["id"] for r in claims]
    rewards = []
    if ids:
        rewards = _rows(connection, text(f"SELECT * FROM cashback_rewards WHERE claim_id IN :ids ORDER BY id{lock}").bindparams(bindparam("ids", expanding=True)), {"ids": ids})
    reward_ids = [r["id"] for r in rewards]
    deliveries = []
    conditions, params = [], {}
    if ids:
        conditions.append("click_id IN :claim_ids")
        params["claim_ids"] = ids
    if reward_ids:
        conditions.append("reward_id IN :reward_ids")
        params["reward_ids"] = reward_ids
    if conditions:
        stmt = text("SELECT * FROM shopify_webhook_deliveries WHERE " + " OR ".join(conditions) + " ORDER BY webhook_id")
        if ids: stmt = stmt.bindparams(bindparam("claim_ids", expanding=True))
        if reward_ids: stmt = stmt.bindparams(bindparam("reward_ids", expanding=True))
        deliveries = _rows(connection, stmt, params)
    blockers = []
    if operation == "campaign":
        blockers = [{"reward_id": r["id"], "status": r.get("status")} for r in rewards if r.get("status") not in TERMINAL_REWARD_STATES]
    else:
        blockers.extend({"claim_id": c["id"], "reason": "consumed"} for c in claims if c.get("consumed_at"))
        blockers.extend({"claim_id": c["id"], "reason": "already archived"} for c in claims if c.get("merchant_archived_at"))
        blockers.extend({"claim_id": r["claim_id"], "reward_id": r["id"], "reason": "reward exists"} for r in rewards)
    campaign_public = {k: v for k, v in campaign.items() if k not in {"destination_url"}}
    core = {
        "version": 1, "operation": operation, "campaign_id": campaign_id,
        "merchant_pubkey_hex": expected_merchant_hex, "campaign_name": expected_campaign_name,
        "expected_campaign_status": expected_campaign_status, "reason": reason,
        "claim_ids": ids, "reward_ids": reward_ids,
        "delivery_ids": [r["webhook_id"] for r in deliveries],
        "row_fingerprints": {"campaign": hashlib.sha256(_canonical(campaign).encode()).hexdigest(),
            "cashback_claims": _fingerprints(claims), "cashback_rewards": _fingerprints(rewards),
            "shopify_webhook_deliveries": _fingerprints(deliveries)},
    }
    sha = hashlib.sha256(_canonical(core).encode()).hexdigest()
    return {**core, "campaign": campaign_public, "manifest_sha256": sha,
        "counts": {"claims": len(claims), "rewards": len(rewards), "deliveries": len(deliveries)},
        "reward_states": {s: sum(1 for r in rewards if r.get("status") == s) for s in sorted({str(r.get('status')) for r in rewards})},
        "blockers": blockers, "safe_to_apply": not blockers}


def apply_cashback_archive(engine, *, operation: str, campaign_id: str, claim_ids: list[str],
        expected_merchant_hex: str, expected_campaign_name: str, expected_campaign_status: str,
        expected_manifest_sha256: str, reason: str, actor: str = "operator",
        backup_writer: Callable[[Any, dict[str, Any]], None] | None = None) -> dict[str, Any]:
    with engine.begin() as connection:
        if connection.engine.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"cashback-campaign:{campaign_id}"})
        existing = connection.execute(text("SELECT * FROM cashback_archive_batches WHERE manifest_sha256=:sha"), {"sha": expected_manifest_sha256}).mappings().one_or_none()
        if existing:
            if any((existing["operation"] != operation, existing["campaign_id"] != campaign_id,
                    existing["merchant_pubkey_hex"] != expected_merchant_hex,
                    existing["campaign_name"] != expected_campaign_name,
                    existing["expected_campaign_status"] != expected_campaign_status)):
                raise ArchiveRefused("existing cashback archive batch target mismatch")
            return {"ok": True, "duplicate": True, "batch_id": existing["id"], "preview": json.loads(existing["manifest_json"])}
        preview = build_cashback_archive_preview(connection, operation=operation, campaign_id=campaign_id,
            claim_ids=claim_ids, expected_merchant_hex=expected_merchant_hex,
            expected_campaign_name=expected_campaign_name, expected_campaign_status=expected_campaign_status,
            reason=reason, lock_rows=True)
        if preview["manifest_sha256"] != expected_manifest_sha256:
            raise ArchiveRefused("cashback archive candidate set changed; generate a new preview")
        if not preview["safe_to_apply"]:
            raise ArchiveRefused("cashback archive has financial or claim blockers")
        if backup_writer:
            backup_writer(connection, preview)
        batch_id, applied_at = str(uuid.uuid4()), datetime.now(timezone.utc).isoformat()
        connection.execute(text("""INSERT INTO cashback_archive_batches
            (id, operation, campaign_id, merchant_pubkey_hex, campaign_name, expected_campaign_status,
             reason, actor, manifest_sha256, manifest_json, claim_count, reward_count, delivery_count,
             created_at, applied_at) VALUES
            (:id,:operation,:campaign,:merchant,:name,:status,:reason,:actor,:sha,:manifest,
             :claims,:rewards,:deliveries,:at,:at)"""), {
            "id": batch_id, "operation": operation, "campaign": campaign_id, "merchant": expected_merchant_hex,
            "name": expected_campaign_name, "status": expected_campaign_status, "reason": reason,
            "actor": actor, "sha": expected_manifest_sha256, "manifest": _canonical(preview),
            "claims": preview["counts"]["claims"], "rewards": preview["counts"]["rewards"],
            "deliveries": preview["counts"]["deliveries"], "at": applied_at})
        if operation == "campaign":
            updated = connection.execute(text("""UPDATE cashback_campaigns SET archived_at=:at, archive_batch_id=:batch
                WHERE id=:id AND status='paused' AND archived_at IS NULL"""), {"at": applied_at, "batch": batch_id, "id": campaign_id}).rowcount
            if updated != 1: raise ArchiveRefused("concurrent campaign archive change")
        ids = preview["claim_ids"]
        if ids:
            base = "campaign_id=:campaign AND id IN :ids AND merchant_archived_at IS NULL"
            if operation == "claims":
                base += " AND consumed_at IS NULL AND NOT EXISTS (SELECT 1 FROM cashback_rewards r WHERE r.claim_id=cashback_claims.id)"
            result = connection.execute(text(f"UPDATE cashback_claims SET merchant_archived_at=:at, archive_batch_id=:batch WHERE {base}").bindparams(bindparam("ids", expanding=True)),
                {"at": applied_at, "batch": batch_id, "campaign": campaign_id, "ids": ids})
            if result.rowcount != len(ids): raise ArchiveRefused("concurrent claim archive change")
        return {"ok": True, "duplicate": False, "batch_id": batch_id, "preview": preview}
