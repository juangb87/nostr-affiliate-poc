import base64
import hashlib
import hmac
import json

from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app


SHOPIFY_SECRET = "shopify-test-secret"
SHOPIFY_SHOP = "shapersfit.myshopify.com"


def shopify_headers(raw_body: bytes, *, secret: str = SHOPIFY_SECRET, webhook_id: str = "wh_test_1") -> dict[str, str]:
    signature = base64.b64encode(hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()).decode()
    return {
        "Content-Type": "application/json",
        "X-Shopify-Hmac-Sha256": signature,
        "X-Shopify-Topic": "orders/paid",
        "X-Shopify-Shop-Domain": SHOPIFY_SHOP,
        "X-Shopify-Webhook-Id": webhook_id,
        "X-Shopify-Api-Version": "2026-04",
    }


def paid_order_payload(click_id: str | None, *, order_id: int = 6001001, legacy: bool = False) -> dict:
    click_key = "bb_click_id" if legacy else "mrt_click_id"
    note_attributes = []
    if click_id:
        note_attributes.append({"name": click_key, "value": click_id})
    return {
        "id": order_id,
        "name": "#TEST-1001",
        "financial_status": "paid",
        "total_price": "1.00",
        "currency": "USD",
        "note_attributes": note_attributes,
    }


def test_shopify_orders_paid_requires_valid_hmac_and_creates_authoritative_conversion(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/shopify.db")
    monkeypatch.setenv("SHOPIFY_SECRET", SHOPIFY_SECRET)
    monkeypatch.setenv("SHOPIFY_STORE_DOMAIN", SHOPIFY_SHOP)
    monkeypatch.setenv("NOSTR_PUBLISH", "false")
    client = TestClient(app)

    demo = client.post("/demo").json()
    click = client.post("/clicks/simulate", json={"ref_code": demo["enrollment"]["ref_code"]}).json()
    payload = paid_order_payload(click["click_id"])
    raw = json.dumps(payload, separators=(",", ":")).encode()

    unsigned = client.post(
        "/shopify/webhooks/orders-paid",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Topic": "orders/paid",
            "X-Shopify-Shop-Domain": SHOPIFY_SHOP,
        },
    )
    assert unsigned.status_code == 401

    bad_signature_headers = shopify_headers(raw)
    bad_signature_headers["X-Shopify-Hmac-Sha256"] = "invalid"
    invalid = client.post("/shopify/webhooks/orders-paid", content=raw, headers=bad_signature_headers)
    assert invalid.status_code == 401

    confirmed = client.post("/shopify/webhooks/orders-paid", content=raw, headers=shopify_headers(raw))
    assert confirmed.status_code == 200, confirmed.text
    result = confirmed.json()
    assert result["ok"] is True
    assert result["ignored"] is False
    assert result["duplicate"] is False
    assert result["conflict"] is False
    assert result["queued"] is True
    assert result["status"] == "pending"
    assert result["shop"] == SHOPIFY_SHOP
    assert result["topic"] == "orders/paid"

    # TestClient waits for background tasks, so the authoritative conversion is now visible.
    summary = client.get(f"/campaigns/{click['campaign_id']}/summary").json()
    matching = [row for row in summary["conversions"] if row["click_id"] == click["click_id"]]
    assert len(matching) == 1
    conversion_id = matching[0]["id"]
    assert matching[0]["order_total"] == 1.0
    assert matching[0]["currency"] == "USD"
    assert matching[0]["commission_sats"] == 200
    matching_payouts = [row for row in summary["payouts"] if row["conversion_id"] == conversion_id]
    assert len(matching_payouts) == 1
    assert matching_payouts[0]["status"] == "pending"

    webhook_status = client.get("/shopify/webhooks/status").json()
    assert webhook_status["deliveries"] == {"processed": 1}
    assert webhook_status["receipts"] == {"processed": 1}
    assert webhook_status["latest_receipt"]["status"] == "processed"

    duplicate = client.post(
        "/shopify/webhooks/orders-paid",
        content=raw,
        headers=shopify_headers(raw, webhook_id="wh_test_retry"),
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert duplicate.json()["queued"] is False
    assert duplicate.json()["conversion_id"] == conversion_id

    conflicting_payload = paid_order_payload(click["click_id"], order_id=6001001)
    conflicting_payload["total_price"] = "2.00"
    conflicting_raw = json.dumps(conflicting_payload, separators=(",", ":")).encode()
    conflict = client.post(
        "/shopify/webhooks/orders-paid",
        content=conflicting_raw,
        headers=shopify_headers(conflicting_raw, webhook_id="wh_test_conflict"),
    )
    assert conflict.status_code == 200
    assert conflict.json()["duplicate"] is True
    assert conflict.json()["conflict"] is True
    unchanged = client.get(f"/campaigns/{click['campaign_id']}/summary").json()
    assert len([row for row in unchanged["conversions"] if row["click_id"] == click["click_id"]]) == 1


def test_shopify_accepts_legacy_click_attribute_but_rejects_conflicting_namespaces(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/shopify-legacy.db")
    monkeypatch.setenv("SHOPIFY_SECRET", SHOPIFY_SECRET)
    monkeypatch.setenv("SHOPIFY_STORE_DOMAIN", SHOPIFY_SHOP)
    monkeypatch.setenv("NOSTR_PUBLISH", "false")
    client = TestClient(app)

    demo = client.post("/demo").json()
    click = client.post("/clicks/simulate", json={"ref_code": demo["enrollment"]["ref_code"]}).json()
    legacy_payload = paid_order_payload(click["click_id"], order_id=6001008, legacy=True)
    legacy_raw = json.dumps(legacy_payload, separators=(",", ":")).encode()
    legacy = client.post(
        "/shopify/webhooks/orders-paid",
        content=legacy_raw,
        headers=shopify_headers(legacy_raw, webhook_id="wh_legacy_attribute"),
    )
    assert legacy.status_code == 200, legacy.text
    assert legacy.json()["queued"] is True

    conflicting_payload = paid_order_payload(click["click_id"], order_id=6001009)
    conflicting_payload["note_attributes"].append({"name": "bb_click_id", "value": "clk_conflicting"})
    conflicting_raw = json.dumps(conflicting_payload, separators=(",", ":")).encode()
    conflict = client.post(
        "/shopify/webhooks/orders-paid",
        content=conflicting_raw,
        headers=shopify_headers(conflicting_raw, webhook_id="wh_conflicting_namespace"),
    )
    assert conflict.status_code == 422
    assert conflict.json()["detail"] == "conflicting affiliate attribution"
    status = client.get("/shopify/webhooks/status").json()
    assert status["receipts"]["conflict"] == 1
    assert status["deliveries"] == {"processed": 1}


def test_shopify_rejects_duplicate_attribution_attribute_values(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/shopify-duplicates.db")
    monkeypatch.setenv("SHOPIFY_SECRET", SHOPIFY_SECRET)
    monkeypatch.setenv("SHOPIFY_STORE_DOMAIN", SHOPIFY_SHOP)
    monkeypatch.setenv("NOSTR_PUBLISH", "false")
    client = TestClient(app)

    payload = paid_order_payload("clk_first", order_id=6001011)
    payload["note_attributes"].extend([
        {"name": "mrt_click_id", "value": "clk_second"},
        {"name": "bb_click_id", "value": "clk_second"},
    ])
    raw = json.dumps(payload, separators=(",", ":")).encode()
    response = client.post(
        "/shopify/webhooks/orders-paid",
        content=raw,
        headers=shopify_headers(raw, webhook_id="wh_duplicate_attribution"),
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "conflicting affiliate attribution"
    status = client.get("/shopify/webhooks/status").json()
    assert status["receipts"]["conflict"] == 1
    assert status["deliveries"] == {}


def test_shopify_background_processing_supports_enabled_nostr_publish(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/shopify-nostr.db")
    monkeypatch.setenv("SHOPIFY_SECRET", SHOPIFY_SECRET)
    monkeypatch.setenv("SHOPIFY_STORE_DOMAIN", SHOPIFY_SHOP)
    monkeypatch.setenv("NOSTR_PUBLISH", "false")
    client = TestClient(app)

    demo = client.post("/demo").json()
    click = client.post("/clicks/simulate", json={"ref_code": demo["enrollment"]["ref_code"]}).json()

    async def fake_publish(event_json, relays):
        return [{"relay": relay, "status": "published"} for relay in relays]

    monkeypatch.setattr(main_module, "_publish_event", fake_publish)
    monkeypatch.setenv("NOSTR_PUBLISH", "true")
    payload = paid_order_payload(click["click_id"], order_id=6001010)
    raw = json.dumps(payload, separators=(",", ":")).encode()

    response = client.post(
        "/shopify/webhooks/orders-paid",
        content=raw,
        headers=shopify_headers(raw, webhook_id="wh_nostr_enabled"),
    )
    assert response.status_code == 200, response.text
    status = client.get("/shopify/webhooks/status").json()
    assert status["deliveries"] == {"processed": 1}
    summary = client.get(f"/campaigns/{click['campaign_id']}/summary").json()
    conversion = next(row for row in summary["conversions"] if row["click_id"] == click["click_id"])
    assert conversion["nostr_event_id"]


def test_shopify_orders_paid_acknowledges_unattributed_orders(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/shopify-unattributed.db")
    monkeypatch.setenv("SHOPIFY_SECRET", SHOPIFY_SECRET)
    monkeypatch.setenv("SHOPIFY_STORE_DOMAIN", SHOPIFY_SHOP)
    client = TestClient(app)

    payload = paid_order_payload(None, order_id=6001002)
    raw = json.dumps(payload, separators=(",", ":")).encode()
    response = client.post("/shopify/webhooks/orders-paid", content=raw, headers=shopify_headers(raw))

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "ignored": True,
        "reason": "missing affiliate attribution",
        "shop": SHOPIFY_SHOP,
        "topic": "orders/paid",
        "webhook_id": "wh_test_1",
    }
    status = client.get("/shopify/webhooks/status").json()
    assert status["deliveries"] == {}
    assert status["receipts"] == {"ignored": 1}
    assert status["latest_receipt"]["status"] == "ignored"
    assert status["latest_receipt"]["reason"] == "missing affiliate attribution"


def test_shopify_orders_paid_rejects_wrong_shop_and_topic(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/shopify-validation.db")
    monkeypatch.setenv("SHOPIFY_SECRET", SHOPIFY_SECRET)
    monkeypatch.setenv("SHOPIFY_STORE_DOMAIN", SHOPIFY_SHOP)
    client = TestClient(app)

    payload = paid_order_payload("clk_unused", order_id=6001003)
    raw = json.dumps(payload, separators=(",", ":")).encode()

    wrong_shop = shopify_headers(raw)
    wrong_shop["X-Shopify-Shop-Domain"] = "attacker.myshopify.com"
    response = client.post("/shopify/webhooks/orders-paid", content=raw, headers=wrong_shop)
    assert response.status_code == 403

    wrong_topic = shopify_headers(raw)
    wrong_topic["X-Shopify-Topic"] = "orders/create"
    response = client.post("/shopify/webhooks/orders-paid", content=raw, headers=wrong_topic)
    assert response.status_code == 400
