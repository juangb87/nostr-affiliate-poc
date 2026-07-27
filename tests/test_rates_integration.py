import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import main
from app.rates import BtcUsdQuote


def test_usd_conversion_uses_server_quote_and_persists_immutable_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/rates-integration.db")
    client = TestClient(main.app)
    demo = client.post("/demo")
    assert demo.status_code == 200, demo.text
    ref_code = demo.json()["enrollment"]["ref_code"]
    click_id = client.post("/clicks/simulate", json={"ref_code": ref_code}).json()["click_id"]

    quote = BtcUsdQuote(
        btc_usd=Decimal("100000"),
        sats_per_usd=Decimal("1000"),
        source="coingecko",
        observed_at="2033-05-18T03:33:20+00:00",
        fetched_at="2033-05-18T03:33:21+00:00",
    )
    monkeypatch.setattr(main.btc_usd_rates, "get_quote", lambda: quote)
    payload = {
        "order_id": "live-rate-order-1",
        "bb_click_id": click_id,
        "order_total": 125,
        "currency": "USD",
    }

    override = client.post(
        "/merchant/conversions",
        headers={"Authorization": "Bearer bumbei-demo-key"},
        json={**payload, "sats_per_usd": 999999},
    )
    assert override.status_code == 422

    created = client.post(
        "/merchant/conversions",
        headers={"Authorization": "Bearer bumbei-demo-key"},
        json=payload,
    )
    assert created.status_code == 200, created.text
    result = created.json()
    assert result["order_total_sats"] == 125_000
    assert result["commission_sats"] == 10_000
    assert result["sats_per_usd_source"] == "server"
    assert result["rate_source"] == "coingecko"
    assert result["btc_usd_rate"] == "100000"
    assert result["sats_per_usd"] == "1000"
    assert result["rate_observed_at"] == quote.observed_at
    assert result["rate_stale"] is False

    receipt = client.get(f"/flows/{result['conversion_id']}")
    assert receipt.status_code == 200
    conversion = receipt.json()["conversion"]
    assert conversion["order_total_sats"] == 125_000
    assert conversion["btc_usd_rate"] == "100000"
    assert conversion["sats_per_usd"] == "1000"
    assert conversion["rate_source"] == "coingecko"
    assert conversion["rate_observed_at"] == quote.observed_at
    event_tags = json.loads(conversion["nostr_event_json"])["tags"]
    assert ["btc_usd_rate", "100000"] in event_tags
    assert ["sats_per_usd", "1000"] in event_tags
    assert ["rate_source", "coingecko"] in event_tags
    assert ["rate_observed_at", quote.observed_at] in event_tags

    monkeypatch.setattr(
        main.btc_usd_rates,
        "get_quote",
        lambda: (_ for _ in ()).throw(AssertionError("duplicate fetched a new rate")),
    )
    duplicate = client.post(
        "/merchant/conversions",
        headers={"Authorization": "Bearer bumbei-demo-key"},
        json=payload,
    )
    assert duplicate.status_code == 200, duplicate.text
    duplicate_json = duplicate.json()
    assert duplicate_json["duplicate"] is True
    assert duplicate_json["conversion_id"] == result["conversion_id"]
    assert duplicate_json["order_total_sats"] == 125_000
    assert duplicate_json["btc_usd_rate"] == "100000"
    assert duplicate_json["sats_per_usd"] == "1000"
    assert duplicate_json["rate_source"] == "coingecko"

    divergent = client.post(
        "/merchant/conversions",
        headers={"Authorization": "Bearer bumbei-demo-key"},
        json={**payload, "order_total": 126},
    )
    assert divergent.status_code == 409
    assert "different conversion payload" in divergent.json()["detail"]

    with main.engine().begin() as connection:
        connection.exec_driver_sql(
            "UPDATE campaigns SET merchant_pubkey_hex=NULL WHERE id=(SELECT campaign_id FROM conversions WHERE id=?)",
            (result["conversion_id"],),
        )
        connection.exec_driver_sql(
            "UPDATE conversions SET merchant_order_key=NULL, idempotency_payload_hash=NULL WHERE id=?",
            (result["conversion_id"],),
        )
    legacy_duplicate = client.post(
        "/merchant/conversions",
        headers={"Authorization": "Bearer bumbei-demo-key"},
        json=payload,
    )
    assert legacy_duplicate.status_code == 200, legacy_duplicate.text
    assert legacy_duplicate.json()["duplicate"] is True
    with main.engine().connect() as connection:
        backfilled = connection.exec_driver_sql(
            "SELECT merchant_order_key, idempotency_payload_hash FROM conversions WHERE id=?",
            (result["conversion_id"],),
        ).one()
    assert all(backfilled)


def test_sats_conversion_does_not_require_live_rate(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/sats-no-rate.db")
    client = TestClient(main.app)
    demo = client.post("/demo")
    ref_code = demo.json()["enrollment"]["ref_code"]
    click_id = client.post("/clicks/simulate", json={"ref_code": ref_code}).json()["click_id"]
    monkeypatch.setattr(
        main.btc_usd_rates,
        "get_quote",
        lambda: (_ for _ in ()).throw(AssertionError("SATS requested BTC/USD")),
    )

    response = client.post(
        "/merchant/conversions",
        headers={"Authorization": "Bearer bumbei-demo-key"},
        json={
            "order_id": "sats-rate-independent",
            "bb_click_id": click_id,
            "order_total": 250000,
            "currency": "SATS",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["order_total_sats"] == 250000
    assert response.json()["rate_source"] == "not_required"
    assert response.json()["btc_usd_rate"] is None


def test_concurrent_same_order_creates_one_obligation_and_fetches_one_quote(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/rates-concurrent.db")
    monkeypatch.setenv("NOSTR_PUBLISH", "false")
    client = TestClient(main.app)
    demo = client.post("/demo").json()
    click_id = client.post("/clicks/simulate", json={"ref_code": demo["enrollment"]["ref_code"]}).json()["click_id"]
    quote = BtcUsdQuote(
        btc_usd=Decimal("100000"), sats_per_usd=Decimal("1000"), source="coingecko",
        observed_at="2033-05-18T03:33:20+00:00", fetched_at="2033-05-18T03:33:21+00:00",
    )
    calls = 0
    calls_lock = threading.Lock()

    def quote_once():
        nonlocal calls
        with calls_lock:
            calls += 1
        return quote

    monkeypatch.setattr(main.btc_usd_rates, "get_quote", quote_once)
    body = main.MerchantConversionIn(
        order_id="concurrent-live-rate-order", bb_click_id=click_id,
        order_total=Decimal("125.00"), currency="USD",
    )
    merchant_hex = main.configured_merchant_pubkey_hex()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: main.process_merchant_conversion(body, merchant_hex), range(8)))

    conversion_ids = {result["conversion_id"] for result in results}
    assert len(conversion_ids) == 1
    assert sum(result["duplicate"] is False for result in results) == 1
    assert calls == 1
    conversion_id = conversion_ids.pop()
    with main.engine().connect() as connection:
        payout_count = connection.exec_driver_sql(
            "SELECT COUNT(*) FROM payouts WHERE conversion_id=?", (conversion_id,)
        ).scalar_one()
        event_count = connection.exec_driver_sql(
            "SELECT COUNT(*) FROM nostr_events WHERE entity_type='conversion' AND entity_id=?", (conversion_id,)
        ).scalar_one()
        payout_id = connection.exec_driver_sql(
            "SELECT id FROM payouts WHERE conversion_id=?", (conversion_id,)
        ).scalar_one()
        ledger_count = connection.exec_driver_sql(
            "SELECT COUNT(*) FROM ledger_entries WHERE payout_id=?", (payout_id,)
        ).scalar_one()
    assert payout_count == 1
    assert event_count == 1
    assert ledger_count == 2


def test_concurrent_init_migrates_legacy_sqlite_schema_once(tmp_path, monkeypatch):
    database_path = tmp_path / "legacy-rates.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript("""
            CREATE TABLE conversions (
                id TEXT PRIMARY KEY, order_id_hash TEXT NOT NULL, click_id TEXT NOT NULL,
                campaign_id TEXT NOT NULL, affiliate_pubkey TEXT NOT NULL, order_total REAL NOT NULL,
                currency TEXT NOT NULL, commission_sats INTEGER NOT NULL, status TEXT NOT NULL,
                nostr_event_id TEXT NOT NULL, nostr_event_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            INSERT INTO conversions VALUES
                ('legacy-conversion', 'hash', 'click', 'campaign', 'affiliate', 10.25,
                 'USD', 100, 'approved', 'event', '{}', '2026-01-01T00:00:00+00:00');
            CREATE TABLE shopify_webhook_deliveries (
                webhook_id TEXT PRIMARY KEY, order_key TEXT UNIQUE NOT NULL, shop_domain TEXT NOT NULL,
                topic TEXT NOT NULL, click_id TEXT NOT NULL, order_total REAL NOT NULL, currency TEXT NOT NULL,
                status TEXT NOT NULL, conversion_id TEXT, error TEXT, created_at TEXT NOT NULL, processed_at TEXT
            );
        """)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path}")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: main.init_db(), range(8)))
    with sqlite3.connect(database_path) as connection:
        conversion_columns = {row[1] for row in connection.execute("PRAGMA table_info(conversions)")}
        shopify_columns = {row[1] for row in connection.execute("PRAGMA table_info(shopify_webhook_deliveries)")}
        legacy = connection.execute(
            "SELECT merchant_order_key, order_total_decimal, btc_usd_rate FROM conversions WHERE id='legacy-conversion'"
        ).fetchone()
    assert {"merchant_order_key", "idempotency_payload_hash", "order_total_decimal", "order_total_sats", "btc_usd_rate", "rate_source"} <= conversion_columns
    assert "order_total_decimal" in shopify_columns
    assert legacy == (None, None, None)


def test_conversion_proof_publishes_only_after_financial_commit(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/post-commit-proof.db")
    client = TestClient(main.app)
    demo = client.post("/demo").json()
    click_id = client.post("/clicks/simulate", json={"ref_code": demo["enrollment"]["ref_code"]}).json()["click_id"]
    observed = []

    def publish_after_commit(event):
        with main.engine().connect() as connection:
            conversions = connection.exec_driver_sql(
                "SELECT COUNT(*) FROM conversions WHERE nostr_event_id=?", (event["id"],)
            ).scalar_one()
            payouts = connection.exec_driver_sql(
                "SELECT COUNT(*) FROM payouts WHERE conversion_id=(SELECT id FROM conversions WHERE nostr_event_id=?)",
                (event["id"],),
            ).scalar_one()
        observed.append((conversions, payouts))
        return [{"relay": "wss://example.invalid", "status": "skipped"}]

    monkeypatch.setattr(main, "publish_event", publish_after_commit)
    response = client.post(
        "/merchant/conversions", headers={"Authorization": "Bearer bumbei-demo-key"},
        json={"order_id": "post-commit-proof", "bb_click_id": click_id, "order_total": "10.25", "currency": "USD"},
    )
    assert response.status_code == 200, response.text
    assert observed == [(1, 1)]


def test_failed_financial_transaction_never_publishes_conversion_proof(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/rollback-proof.db")
    client = TestClient(main.app)
    demo = client.post("/demo").json()
    click_id = client.post("/clicks/simulate", json={"ref_code": demo["enrollment"]["ref_code"]}).json()["click_id"]
    published = []
    monkeypatch.setattr(main, "publish_event", lambda event: published.append(event) or [])
    monkeypatch.setattr(main, "reserve_campaign_budget", lambda *args: (_ for _ in ()).throw(RuntimeError("forced rollback")))
    with pytest.raises(RuntimeError, match="forced rollback"):
        client.post(
            "/merchant/conversions", headers={"Authorization": "Bearer bumbei-demo-key"},
            json={"order_id": "rollback-proof", "bb_click_id": click_id, "order_total": 10, "currency": "USD"},
        )
    with main.engine().connect() as connection:
        count = connection.exec_driver_sql(
            "SELECT COUNT(*) FROM conversions WHERE order_id_hash=?", (main.sha("rollback-proof"),)
        ).scalar_one()
    assert count == 0
    assert published == []


def test_duplicate_retries_failed_conversion_and_campaign_outbox(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/outbox-retry.db")
    client = TestClient(main.app)
    demo = client.post("/demo").json()
    click_id = client.post("/clicks/simulate", json={"ref_code": demo["enrollment"]["ref_code"]}).json()["click_id"]
    recovered = False
    attempts = []

    def flaky_publish(event):
        event_type = next((tag[1] for tag in event["tags"] if tag[0] == "type"), "unknown")
        attempts.append(event_type)
        return [{
            "relay": "wss://example.invalid",
            "status": "published" if recovered else "failed",
            **({} if recovered else {"error": "temporary outage"}),
        }]

    monkeypatch.setattr(main, "publish_event", flaky_publish)
    monkeypatch.setattr(main, "reserve_campaign_budget", lambda *args: False)
    payload = {
        "order_id": "outbox-retry-order", "bb_click_id": click_id,
        "order_total": 10, "currency": "USD",
    }
    created = client.post(
        "/merchant/conversions", headers={"Authorization": "Bearer bumbei-demo-key"}, json=payload,
    )
    assert created.status_code == 200, created.text
    assert created.json()["payout_status"] == "on_hold"
    conversion_id = created.json()["conversion_id"]
    with main.engine().connect() as connection:
        pending = connection.exec_driver_sql("""
            SELECT entity_type FROM nostr_events
            WHERE relay_status='pending_publication'
              AND ((entity_type='conversion' AND entity_id=?)
                   OR (entity_type='campaign' AND entity_id=?))
        """, (conversion_id, demo["campaign"]["campaign_id"])).all()
    assert {row[0] for row in pending} == {"conversion", "campaign"}

    recovered = True
    duplicate = client.post(
        "/merchant/conversions", headers={"Authorization": "Bearer bumbei-demo-key"}, json=payload,
    )
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["duplicate"] is True
    with main.engine().connect() as connection:
        remaining = connection.exec_driver_sql("""
            SELECT COUNT(*) FROM nostr_events
            WHERE relay_status='pending_publication'
              AND ((entity_type='conversion' AND entity_id=?)
                   OR (entity_type='campaign' AND entity_id=?))
        """, (conversion_id, demo["campaign"]["campaign_id"])).scalar_one()
    assert remaining == 0
    assert attempts.count("affiliate_conversion") == 2
    assert attempts.count("affiliate_campaign") >= 2


def test_positive_amounts_that_round_to_zero_sats_are_rejected():
    quote = BtcUsdQuote(
        btc_usd=Decimal("100000"), sats_per_usd=Decimal("1000"), source="coingecko",
        observed_at="2033-05-18T03:33:20+00:00", fetched_at="2033-05-18T03:33:21+00:00",
    )
    for amount, currency in [
        (Decimal("0.0004"), "USD"),
        (Decimal("0.000000001"), "BTC"),
        (Decimal("0.4"), "SATS"),
        (Decimal("400"), "MSAT"),
    ]:
        with pytest.raises(main.HTTPException) as error:
            main.order_total_sats(amount, currency, quote)
        assert error.value.status_code == 422
    assert main.order_total_sats(Decimal("0.0005"), "USD", quote) == 1
    assert main.order_total_sats(Decimal("0.5"), "SATS", quote) == 1
