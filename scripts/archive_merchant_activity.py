#!/usr/bin/env python3
"""Preview or apply an exact, auditable merchant activity archive.

Examples:
  python scripts/archive_merchant_activity.py preview --campaign-id ... --merchant-hex ... \
    --campaign-name 'Lightning Koffee Affiliate Program' --cutoff 2026-08-15T12:00:00Z
  python scripts/archive_merchant_activity.py apply ... --expected-sha ... \
    --backup-file /secure/lightningkoffee-archive.json --confirm ARCHIVE_TEST_ACTIVITY
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from sqlalchemy import bindparam, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main
from app.operational_archive import ArchiveRefused, apply_archive, build_archive_preview


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("preview", "apply"))
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--merchant-hex", required=True)
    parser.add_argument("--campaign-name", required=True)
    parser.add_argument("--cutoff", required=True)
    parser.add_argument("--reason", default="pre-launch test data")
    parser.add_argument("--actor", default="operator")
    parser.add_argument("--expected-sha")
    parser.add_argument("--backup-file")
    parser.add_argument("--confirm")
    return parser.parse_args()


def selected_rows(connection, table: str, key: str, ids: list[str]) -> list[dict]:
    if not ids:
        return []
    rows = connection.execute(
        text(f"SELECT * FROM {table} WHERE {key} IN :ids ORDER BY {key}").bindparams(bindparam("ids", expanding=True)),
        {"ids": ids},
    ).fetchall()
    return [dict(row._mapping) for row in rows]


def write_backup(connection, path_value: str, preview: dict) -> None:
    path = Path(path_value).expanduser().resolve()
    if path.exists():
        raise ArchiveRefused(f"refusing to overwrite backup file: {path}")
    payout_ids = preview["payout_ids"]
    snapshot = {
        "preview": preview,
        "clicks": selected_rows(connection, "clicks", "id", preview["click_ids"]),
        "conversions": selected_rows(connection, "conversions", "id", preview["conversion_ids"]),
        "tracking_events": selected_rows(connection, "tracking_events", "id", preview["tracking_event_ids"]),
        "payouts": selected_rows(connection, "payouts", "id", payout_ids),
        "payment_attempts": selected_rows(connection, "payment_attempts", "payout_id", payout_ids),
        "ledger_entries": selected_rows(connection, "ledger_entries", "payout_id", payout_ids),
        "reversals": selected_rows(connection, "reversals", "conversion_id", preview["conversion_ids"]),
        "shopify_webhook_deliveries": selected_rows(
            connection, "shopify_webhook_deliveries", "conversion_id", preview["conversion_ids"]
        ),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, sort_keys=True, indent=2, default=str)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def main_cli() -> int:
    args = arguments()
    try:
        if args.mode == "apply":
            if args.confirm != "ARCHIVE_TEST_ACTIVITY":
                raise ArchiveRefused("apply requires --confirm ARCHIVE_TEST_ACTIVITY")
            if not args.expected_sha:
                raise ArchiveRefused("apply requires --expected-sha from the approved preview")
            with main.engine().connect() as connection:
                existing = connection.execute(
                    text("SELECT id FROM operational_archive_batches WHERE manifest_sha256=:sha"),
                    {"sha": args.expected_sha},
                ).fetchone()
            if existing:
                result = apply_archive(
                    main.engine(), campaign_id=args.campaign_id, expected_merchant_hex=args.merchant_hex,
                    expected_campaign_name=args.campaign_name, cutoff=args.cutoff,
                    expected_manifest_sha256=args.expected_sha, reason=args.reason, actor=args.actor,
                )
                print(json.dumps({"result": result}, sort_keys=True, indent=2))
                return 0
        with main.engine().connect() as connection:
            preview = build_archive_preview(
                connection,
                campaign_id=args.campaign_id,
                expected_merchant_hex=args.merchant_hex,
                expected_campaign_name=args.campaign_name,
                cutoff=args.cutoff,
                reason=args.reason,
            )
        print(json.dumps(preview, sort_keys=True, indent=2))
        if args.mode == "preview":
            return 0 if preview["safe_to_apply"] else 2

        if args.expected_sha != preview["manifest_sha256"]:
            raise ArchiveRefused("expected SHA does not match the current preview")
        if not args.backup_file:
            raise ArchiveRefused("apply requires --backup-file")
        result = apply_archive(
            main.engine(),
            campaign_id=args.campaign_id,
            expected_merchant_hex=args.merchant_hex,
            expected_campaign_name=args.campaign_name,
            cutoff=args.cutoff,
            expected_manifest_sha256=args.expected_sha,
            reason=args.reason,
            actor=args.actor,
            backup_writer=lambda connection, approved: write_backup(connection, args.backup_file, approved),
        )
        print(json.dumps({"result": result, "backup_file": str(Path(args.backup_file).resolve())}, sort_keys=True, indent=2))
        return 0
    except ArchiveRefused as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main_cli())
