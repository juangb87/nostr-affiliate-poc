from pathlib import Path

import pytest
from sqlalchemy import text

from app import main
from app.cashback_archive import ArchiveRefused, apply_cashback_archive, build_cashback_archive_preview

MERCHANT = "a" * 64


def setup_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/archive-cashback.db")
    main._ENGINE = None
    main._ENGINE_URL = None
    main.init_db()
    return main.engine()


def seed_campaign(engine, campaign_id, name, status, *, claim_id, consumed_at=None, reward=None):
    with engine.begin() as c:
        c.execute(text("""
            INSERT INTO cashback_campaigns
              (id, merchant_pubkey, merchant_pubkey_hex, name, cashback_bps, window_days,
               destination_url, short_code, budget_sats, committed_sats, max_reward_sats,
               status, created_at)
            VALUES (:id, 'npub-test', :merchant, :name, 500, 30, 'https://example.com',
                    :code, 10000, 0, 1000, :status, '2026-08-01T00:00:00+00:00')
        """), {"id": campaign_id, "merchant": MERCHANT, "name": name, "code": campaign_id, "status": status})
        c.execute(text("""
            INSERT INTO cashback_claims
              (id, campaign_id, lightning_address, created_at, expires_at, consumed_at)
            VALUES (:id, :campaign, 'buyer@example.com', '2026-08-01T01:00:00+00:00',
                    '2026-09-01T00:00:00+00:00', :consumed)
        """), {"id": claim_id, "campaign": campaign_id, "consumed": consumed_at})
        if reward:
            c.execute(text("""
                INSERT INTO cashback_rewards
                  (id, order_key, claim_id, campaign_id, merchant_pubkey_hex, order_total,
                   order_total_decimal, currency, order_total_sats, cashback_bps, reward_sats,
                   status, created_at, paid_at, payment_hash, payment_evidence)
                VALUES (:id, :order_key, :claim, :campaign, :merchant, 10, '10', 'USD',
                        10000, 500, 500, :status, '2026-08-01T02:00:00+00:00',
                        :paid_at, 'hash', 'evidence')
            """), {"id": reward["id"], "order_key": reward["id"], "claim": claim_id,
                     "campaign": campaign_id, "merchant": MERCHANT, "status": reward["status"],
                     "paid_at": "2026-08-01T03:00:00+00:00" if reward["status"] == "paid" else None})


def test_archives_paused_campaign_but_preserves_paid_reward(tmp_path, monkeypatch):
    engine = setup_db(tmp_path, monkeypatch)
    seed_campaign(engine, "old", "Old cashback", "paused", claim_id="claim-old",
                  consumed_at="2026-08-01T02:00:00+00:00", reward={"id": "reward-old", "status": "paid"})
    with engine.connect() as c:
        preview = build_cashback_archive_preview(c, operation="campaign", campaign_id="old",
            claim_ids=[], expected_merchant_hex=MERCHANT, expected_campaign_name="Old cashback",
            expected_campaign_status="paused", reason="test")
    assert preview["safe_to_apply"] is True
    result = apply_cashback_archive(engine, operation="campaign", campaign_id="old", claim_ids=[],
        expected_merchant_hex=MERCHANT, expected_campaign_name="Old cashback",
        expected_campaign_status="paused", expected_manifest_sha256=preview["manifest_sha256"], reason="test")
    with engine.connect() as c:
        campaign = c.execute(text("SELECT archived_at, archive_batch_id FROM cashback_campaigns WHERE id='old'")).one()
        claim = c.execute(text("SELECT merchant_archived_at, archive_batch_id FROM cashback_claims WHERE id='claim-old'")).one()
        reward = dict(c.execute(text("SELECT * FROM cashback_rewards WHERE id='reward-old'")).mappings().one())
    assert campaign.archived_at and campaign.archive_batch_id == result["batch_id"]
    assert claim.merchant_archived_at and claim.archive_batch_id == result["batch_id"]
    assert reward["status"] == "paid" and reward["payment_hash"] == "hash"
    duplicate = apply_cashback_archive(engine, operation="campaign", campaign_id="old", claim_ids=[],
        expected_merchant_hex=MERCHANT, expected_campaign_name="Old cashback",
        expected_campaign_status="paused", expected_manifest_sha256=preview["manifest_sha256"], reason="test")
    assert duplicate["duplicate"] is True and duplicate["batch_id"] == result["batch_id"]


def test_archives_only_unconsumed_rewardless_claim_in_active_campaign(tmp_path, monkeypatch):
    engine = setup_db(tmp_path, monkeypatch)
    seed_campaign(engine, "active", "Live cashback", "active", claim_id="claim-test")
    with engine.connect() as c:
        preview = build_cashback_archive_preview(c, operation="claims", campaign_id="active",
            claim_ids=["claim-test"], expected_merchant_hex=MERCHANT, expected_campaign_name="Live cashback",
            expected_campaign_status="active", reason="test")
    result = apply_cashback_archive(engine, operation="claims", campaign_id="active", claim_ids=["claim-test"],
        expected_merchant_hex=MERCHANT, expected_campaign_name="Live cashback",
        expected_campaign_status="active", expected_manifest_sha256=preview["manifest_sha256"], reason="test")
    with engine.connect() as c:
        campaign = c.execute(text("SELECT status, archived_at FROM cashback_campaigns WHERE id='active'")).one()
        claim = c.execute(text("SELECT merchant_archived_at FROM cashback_claims WHERE id='claim-test'")).one()
    assert campaign.status == "active" and campaign.archived_at is None
    assert claim.merchant_archived_at
    with pytest.raises(Exception, match="archived"):
        main.process_cashback_reward(order_key="order-test", claim_id="claim-test", campaign_code="active",
            order_total=__import__('decimal').Decimal("10"), currency="SATS", authorized_merchant_hex=MERCHANT)


def test_claim_archive_refuses_consumed_or_reward_backed_claim(tmp_path, monkeypatch):
    engine = setup_db(tmp_path, monkeypatch)
    seed_campaign(engine, "active", "Live cashback", "active", claim_id="claim-used",
                  consumed_at="2026-08-01T02:00:00+00:00", reward={"id": "reward-used", "status": "paid"})
    with engine.connect() as c:
        preview = build_cashback_archive_preview(c, operation="claims", campaign_id="active", claim_ids=["claim-used"],
            expected_merchant_hex=MERCHANT, expected_campaign_name="Live cashback",
            expected_campaign_status="active", reason="test")
    assert preview["safe_to_apply"] is False
    assert {blocker["reason"] for blocker in preview["blockers"]} == {"consumed", "reward exists"}
