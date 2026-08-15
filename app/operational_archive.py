"""Auditable, exact-target archival of pre-launch merchant activity.

Financial records are never deleted. This module only marks operational clicks,
conversions, and tracking events so merchant dashboards can exclude test data.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import bindparam, text

TERMINAL_PAYOUT_STATES = {"SETTLED", "PUBLISHED", "CANCELLED"}
TERMINAL_FEE_STATES = {"FEE_SETTLED", "CANCELLED"}


class ArchiveRefused(RuntimeError):
    """Raised when an archive guard or financial invariant does not hold."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _validate_cutoff(cutoff: str) -> str:
    try:
        parsed = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ArchiveRefused("cutoff must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ArchiveRefused("cutoff must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _rows(connection, statement, params: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(row._mapping) for row in connection.execute(statement, params).fetchall()]


def build_archive_preview(
    connection,
    *,
    campaign_id: str,
    expected_merchant_hex: str,
    expected_campaign_name: str,
    cutoff: str,
    reason: str = "pre-launch test data",
    lock_rows: bool = False,
) -> dict[str, Any]:
    """Return a deterministic, read-only manifest for an exact campaign target."""
    cutoff = _validate_cutoff(cutoff)
    if not re.fullmatch(r"[0-9a-f]{64}", expected_merchant_hex):
        raise ArchiveRefused("expected merchant must be a lowercase 64-character hex pubkey")
    campaign_row = connection.execute(
        text("SELECT id, name, merchant_pubkey, merchant_pubkey_hex, status, archived_at FROM campaigns WHERE id=:id"),
        {"id": campaign_id},
    ).fetchone()
    if not campaign_row:
        raise ArchiveRefused("campaign not found")
    campaign = dict(campaign_row._mapping)
    if expected_merchant_hex != str(campaign.get("merchant_pubkey_hex") or ""):
        raise ArchiveRefused("campaign owner does not match expected merchant")
    if campaign["name"] != expected_campaign_name:
        raise ArchiveRefused("campaign name does not match expected name")
    if campaign.get("archived_at"):
        raise ArchiveRefused("campaign itself is archived")
    lock_clause = " FOR UPDATE" if lock_rows and connection.engine.dialect.name == "postgresql" else ""

    click_rows = _rows(
        connection,
        text(f"""
            SELECT * FROM clicks
            WHERE campaign_id=:campaign_id AND created_at<=:cutoff
              AND merchant_archived_at IS NULL
            ORDER BY id
            {lock_clause}
        """),
        {"campaign_id": campaign_id, "cutoff": cutoff},
    )
    conversion_rows = _rows(
        connection,
        text(f"""
            SELECT *
            FROM conversions
            WHERE campaign_id=:campaign_id AND created_at<=:cutoff
              AND merchant_archived_at IS NULL
            ORDER BY id
            {lock_clause}
        """),
        {"campaign_id": campaign_id, "cutoff": cutoff},
    )
    conversion_ids = [row["id"] for row in conversion_rows]
    payout_rows: list[dict[str, Any]] = []
    if conversion_ids:
        payout_rows = _rows(
            connection,
            text(f"""
                SELECT *
                FROM payouts WHERE conversion_id IN :conversion_ids ORDER BY id
                {lock_clause}
            """).bindparams(bindparam("conversion_ids", expanding=True)),
            {"conversion_ids": conversion_ids},
        )
    click_ids = [row["id"] for row in click_rows]
    ref_codes = [
        row._mapping["ref_code"]
        for row in connection.execute(
            text("SELECT ref_code FROM enrollments WHERE campaign_id=:campaign_id ORDER BY ref_code"),
            {"campaign_id": campaign_id},
        ).fetchall()
    ]
    tracking_rows: list[dict[str, Any]] = []
    if click_ids or ref_codes:
        tracking_rows = _rows(
            connection,
            text(f"""
                SELECT * FROM tracking_events
                WHERE (click_id IN :click_ids OR ref_code IN :ref_codes) AND created_at<=:cutoff
                  AND merchant_archived_at IS NULL
                ORDER BY id
                {lock_clause}
            """).bindparams(bindparam("click_ids", expanding=True), bindparam("ref_codes", expanding=True)),
            {"click_ids": click_ids, "ref_codes": ref_codes, "cutoff": cutoff},
        )

    blockers = [
        {"payout_id": row["id"], "conversion_id": row["conversion_id"], "state": row["state"]}
        for row in payout_rows
        if row.get("state") not in TERMINAL_PAYOUT_STATES
        or (
            (int(row.get("fee_sats") or 0) > 0 or int(row.get("reserved_sats") or 0) > 0)
            and row.get("fee_state") not in TERMINAL_FEE_STATES
        )
    ]
    attempt_rows: list[dict[str, Any]] = []
    ledger_rows: list[dict[str, Any]] = []
    payout_ids = [row["id"] for row in payout_rows]
    if payout_ids:
        attempt_rows = _rows(
            connection,
            text(f"""
                SELECT * FROM payment_attempts
                WHERE payout_id IN :payout_ids ORDER BY id
                {lock_clause}
            """).bindparams(bindparam("payout_ids", expanding=True)),
            {"payout_ids": payout_ids},
        )
        ledger_rows = _rows(
            connection,
            text(f"""
                SELECT * FROM ledger_entries
                WHERE payout_id IN :payout_ids ORDER BY id
                {lock_clause}
            """).bindparams(bindparam("payout_ids", expanding=True)),
            {"payout_ids": payout_ids},
        )
    reversal_rows: list[dict[str, Any]] = []
    if conversion_ids:
        reversal_rows = _rows(
            connection,
            text(f"""
                SELECT * FROM reversals
                WHERE conversion_id IN :conversion_ids ORDER BY id
                {lock_clause}
            """).bindparams(bindparam("conversion_ids", expanding=True)),
            {"conversion_ids": conversion_ids},
        )
    blockers.extend(
        {"payout_id": row["payout_id"], "attempt_id": row["id"], "state": row["status"]}
        for row in attempt_rows if row.get("status") in {"PAYING", "UNKNOWN"}
    )
    fingerprints = {
        "clicks": {row["id"]: hashlib.sha256(_canonical_json(row).encode()).hexdigest() for row in click_rows},
        "conversions": {row["id"]: hashlib.sha256(_canonical_json(row).encode()).hexdigest() for row in conversion_rows},
        "tracking_events": {row["id"]: hashlib.sha256(_canonical_json(row).encode()).hexdigest() for row in tracking_rows},
        "payouts": {row["id"]: hashlib.sha256(_canonical_json(row).encode()).hexdigest() for row in payout_rows},
        "payment_attempts": {row["id"]: hashlib.sha256(_canonical_json(row).encode()).hexdigest() for row in attempt_rows},
        "ledger_entries": {str(row["id"]): hashlib.sha256(_canonical_json(row).encode()).hexdigest() for row in ledger_rows},
        "reversals": {row["id"]: hashlib.sha256(_canonical_json(row).encode()).hexdigest() for row in reversal_rows},
    }
    manifest_core = {
        "version": 2,
        "campaign_id": campaign_id,
        "merchant_pubkey_hex": expected_merchant_hex,
        "campaign_name": expected_campaign_name,
        "cutoff": cutoff,
        "reason": reason,
        "click_ids": click_ids,
        "conversion_ids": conversion_ids,
        "tracking_event_ids": [row["id"] for row in tracking_rows],
        "payout_ids": payout_ids,
        "payment_attempt_ids": [row["id"] for row in attempt_rows],
        "ledger_entry_ids": [row["id"] for row in ledger_rows],
        "reversal_ids": [row["id"] for row in reversal_rows],
        "row_fingerprints": fingerprints,
    }
    manifest_sha256 = hashlib.sha256(_canonical_json(manifest_core).encode()).hexdigest()
    states: dict[str, dict[str, int]] = {}
    for row in payout_rows:
        bucket = states.setdefault(str(row.get("state") or "UNKNOWN"), {"count": 0, "amount_sats": 0})
        bucket["count"] += 1
        bucket["amount_sats"] += int(row.get("amount_sats") or 0)
    return {
        **manifest_core,
        "manifest_sha256": manifest_sha256,
        "counts": {
            "clicks": len(click_rows),
            "conversions": len(conversion_rows),
            "tracking_events": len(tracking_rows),
            "payouts": len(payout_rows),
            "payment_attempts": len(attempt_rows),
            "ledger_entries": len(ledger_rows),
            "reversals": len(reversal_rows),
        },
        "payout_states": states,
        "blocking_payouts": blockers,
        "safe_to_apply": not blockers,
    }


def apply_archive(
    engine,
    *,
    campaign_id: str,
    expected_merchant_hex: str,
    expected_campaign_name: str,
    cutoff: str,
    expected_manifest_sha256: str,
    reason: str = "pre-launch test data",
    actor: str = "operator",
    backup_writer: Callable[[Any, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Apply a previewed archive atomically; reject drift and non-terminal payouts."""
    with engine.begin() as connection:
        if connection.engine.dialect.name == "postgresql":
            connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"campaign-state:{campaign_id}"},
            )
        existing = connection.execute(
            text("""
                SELECT id, campaign_id, merchant_pubkey_hex, campaign_name, cutoff, applied_at, manifest_json
                FROM operational_archive_batches WHERE manifest_sha256=:sha
            """),
            {"sha": expected_manifest_sha256},
        ).fetchone()
        if existing:
            saved_row = existing._mapping
            normalized_cutoff = _validate_cutoff(cutoff)
            if (
                saved_row["campaign_id"] != campaign_id
                or saved_row["merchant_pubkey_hex"] != expected_merchant_hex
                or saved_row["campaign_name"] != expected_campaign_name
                or saved_row["cutoff"] != normalized_cutoff
            ):
                raise ArchiveRefused("existing archive batch does not match the requested target")
            saved = json.loads(saved_row["manifest_json"])
            return {"ok": True, "duplicate": True, "batch_id": saved_row["id"], "preview": saved}
        preview = build_archive_preview(
            connection,
            campaign_id=campaign_id,
            expected_merchant_hex=expected_merchant_hex,
            expected_campaign_name=expected_campaign_name,
            cutoff=cutoff,
            reason=reason,
            lock_rows=True,
        )
        if preview["manifest_sha256"] != expected_manifest_sha256:
            raise ArchiveRefused("archive candidate set changed after preview; generate a new preview")
        if preview["blocking_payouts"]:
            raise ArchiveRefused("archive contains non-terminal payouts")
        if backup_writer:
            backup_writer(connection, preview)

        batch_id = str(uuid.uuid4())
        applied_at = datetime.now(timezone.utc).isoformat()
        manifest_json = _canonical_json(preview)
        connection.execute(
            text("""
                INSERT INTO operational_archive_batches
                  (id, campaign_id, merchant_pubkey_hex, campaign_name, cutoff, reason, actor,
                   manifest_sha256, manifest_json, click_count, conversion_count,
                   tracking_event_count, payout_count, created_at, applied_at)
                VALUES
                  (:id, :campaign_id, :merchant, :campaign_name, :cutoff, :reason, :actor,
                   :sha, :manifest_json, :clicks, :conversions, :tracking_events, :payouts,
                   :created_at, :applied_at)
            """),
            {
                "id": batch_id, "campaign_id": campaign_id, "merchant": expected_merchant_hex,
                "campaign_name": expected_campaign_name, "cutoff": preview["cutoff"], "reason": reason,
                "actor": actor, "sha": expected_manifest_sha256, "manifest_json": manifest_json,
                "clicks": preview["counts"]["clicks"], "conversions": preview["counts"]["conversions"],
                "tracking_events": preview["counts"]["tracking_events"], "payouts": preview["counts"]["payouts"],
                "created_at": applied_at, "applied_at": applied_at,
            },
        )
        for table, key in (
            ("clicks", "click_ids"),
            ("conversions", "conversion_ids"),
            ("tracking_events", "tracking_event_ids"),
        ):
            ids = preview[key]
            if ids:
                result = connection.execute(
                    text(f"""
                        UPDATE {table}
                        SET merchant_archived_at=:archived_at, archive_batch_id=:batch_id
                        WHERE id IN :ids AND merchant_archived_at IS NULL
                    """).bindparams(bindparam("ids", expanding=True)),
                    {"archived_at": applied_at, "batch_id": batch_id, "ids": ids},
                )
                if result.rowcount != len(ids):
                    raise ArchiveRefused(f"concurrent change while archiving {table}")
        return {"ok": True, "duplicate": False, "batch_id": batch_id, "preview": preview}
