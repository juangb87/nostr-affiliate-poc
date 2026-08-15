#!/usr/bin/env python3
"""Preview/apply an exact Cashback Express logical archive."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import bindparam, text
from app import main
from app.cashback_archive import ArchiveRefused, apply_cashback_archive, build_cashback_archive_preview


def dump(value):
    print(json.dumps(value, indent=2, sort_keys=True))


def write_exclusive(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush(); os.fsync(stream.fileno())
        dirfd = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(dirfd)
        finally: os.close(dirfd)
    except Exception:
        try: path.unlink()
        except OSError: pass
        raise


def backup_writer(path: Path):
    def writer(connection, preview):
        ids, reward_ids, delivery_ids = preview["claim_ids"], preview["reward_ids"], preview["delivery_ids"]
        campaign = dict(connection.execute(text("SELECT * FROM cashback_campaigns WHERE id=:id"), {"id": preview["campaign_id"]}).mappings().one())
        def rows(table, key, values):
            if not values: return []
            stmt = text(f"SELECT * FROM {table} WHERE {key} IN :ids ORDER BY {key}").bindparams(bindparam("ids", expanding=True))
            return [dict(r) for r in connection.execute(stmt, {"ids": values}).mappings().all()]
        write_exclusive(path, {"preview": preview, "cashback_campaign": campaign,
            "cashback_claims": rows("cashback_claims", "id", ids),
            "cashback_rewards": rows("cashback_rewards", "id", reward_ids),
            "shopify_webhook_deliveries": rows("shopify_webhook_deliveries", "webhook_id", delivery_ids)})
    return writer


def parser():
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=("preview", "apply"))
    p.add_argument("--operation", choices=("campaign", "claims"), required=True)
    p.add_argument("--campaign-id", required=True)
    p.add_argument("--claim-id", action="append", default=[])
    p.add_argument("--merchant-hex", required=True)
    p.add_argument("--campaign-name", required=True)
    p.add_argument("--expected-status", choices=("active", "paused"), required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--expected-sha")
    p.add_argument("--backup-file")
    p.add_argument("--actor", default="operator")
    p.add_argument("--confirm")
    return p


def main_cli():
    args = parser().parse_args()
    main.init_db()
    common = dict(operation=args.operation, campaign_id=args.campaign_id, claim_ids=args.claim_id,
        expected_merchant_hex=args.merchant_hex, expected_campaign_name=args.campaign_name,
        expected_campaign_status=args.expected_status, reason=args.reason)
    if args.mode == "preview":
        with main.engine().connect() as connection:
            preview = build_cashback_archive_preview(connection, **common)
        dump(preview)
        return 0 if preview["safe_to_apply"] else 2
    if not args.expected_sha or not args.backup_file or args.confirm != "ARCHIVE_CASHBACK":
        raise ArchiveRefused("apply requires --expected-sha, --backup-file and --confirm ARCHIVE_CASHBACK")
    result = apply_cashback_archive(main.engine(), **common, expected_manifest_sha256=args.expected_sha,
        actor=args.actor, backup_writer=backup_writer(Path(args.backup_file)))
    dump(result)
    return 0


if __name__ == "__main__":
    try: raise SystemExit(main_cli())
    except ArchiveRefused as exc:
        dump({"ok": False, "error": str(exc)})
        raise SystemExit(2)
