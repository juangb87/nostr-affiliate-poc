import base64
import hashlib
import hmac
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
from nostr_sdk import EventBuilder, Keys, Kind, Tag
from sqlalchemy import text
import pytest

from app import main


def client_for(tmp_path, monkeypatch, merchant: Keys) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/cashback.db")
    monkeypatch.setenv("NOSTR_PUBLISH", "false")
    monkeypatch.setenv("APP_SECRET", "test-app-secret-with-enough-entropy")
    monkeypatch.setenv("SHOPIFY_SECRET", "cashback-shopify-secret")
    monkeypatch.setenv("SHOPIFY_STORE_DOMAIN", "cashback.myshopify.com")
    monkeypatch.setenv("SHOPIFY_MERCHANT_PUBKEY", merchant.public_key().to_bech32())
    monkeypatch.setattr(main, "BASE_URL", "https://testserver")
    main._ENGINE = None
    main._ENGINE_URL = None
    client = TestClient(main.app, base_url="https://testserver")
    seed = client.post(
        "/campaigns",
        json={
            "merchant_pubkey": merchant.public_key().to_bech32(),
            "name": "Existing affiliate program",
            "commission_bps": 800,
            "attribution_window_days": 30,
            "destination_url": "https://merchant.example/shop",
            "enrollment_mode": "private",
        },
    )
    assert seed.status_code == 200, seed.text
    return client


def login(client: TestClient, keys: Keys) -> None:
    challenge = client.post("/auth/nostr/challenge", json={"role": "merchant"}).json()
    event = EventBuilder(Kind(challenge["kind"]), "").tags([
        Tag.parse(["challenge", challenge["challenge"]]),
        Tag.parse(["relay", challenge["relay"]]),
        Tag.parse(["role", "merchant"]),
    ]).sign_with_keys(keys)
    response = client.post("/auth/nostr/verify", json={"event": json.loads(event.as_json())})
    assert response.status_code == 200, response.text


def create_cashback(client: TestClient, merchant: Keys, **overrides):
    body = {
        "merchant_pubkey": merchant.public_key().to_bech32(),
        "name": "Cashback café",
        "cashback_percent": "7.25",
        "destination_url": "https://merchant.example/shop?utm=keep#drop",
        "budget_sats": 100_000,
        "max_reward_sats": 10_000,
    }
    body.update(overrides)
    if "budget_sats" in overrides and "max_reward_sats" not in overrides and overrides["budget_sats"] is not None:
        body["max_reward_sats"] = min(body["max_reward_sats"], overrides["budget_sats"])
    return client.post("/app/merchant/cashback-express", json=body, headers={"Origin": "https://testserver"})


def signed_headers(raw: bytes, webhook_id="cashback-wh-1"):
    signature = base64.b64encode(hmac.new(b"cashback-shopify-secret", raw, hashlib.sha256).digest()).decode()
    return {
        "Content-Type": "application/json",
        "X-Shopify-Hmac-Sha256": signature,
        "X-Shopify-Topic": "orders/paid",
        "X-Shopify-Shop-Domain": "cashback.myshopify.com",
        "X-Shopify-Webhook-Id": webhook_id,
    }


def test_schema_upgrade_rejects_duplicate_cashback_claims_for_manual_reconciliation(tmp_path, monkeypatch):
    database = tmp_path / "legacy-duplicates.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE cashback_rewards (id TEXT PRIMARY KEY, claim_id TEXT)")
        connection.executemany(
            "INSERT INTO cashback_rewards (id, claim_id) VALUES (?, 'duplicate-claim')",
            [("reward-a",), ("reward-b",)],
        )
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database}")
    main._ENGINE = None
    main._ENGINE_URL = None
    with pytest.raises(RuntimeError, match="manual reconciliation is required.*duplicate-claim"):
        main.init_db()


def test_create_requires_auth_same_origin_ownership_and_valid_boundaries(tmp_path, monkeypatch):
    owner, other = Keys.generate(), Keys.generate()
    client = client_for(tmp_path, monkeypatch, owner)
    unauth = create_cashback(client, owner)
    assert unauth.status_code == 401
    login(client, owner)
    no_origin = client.post("/app/merchant/cashback-express", json={
        "merchant_pubkey": owner.public_key().to_bech32(), "name": "x",
        "cashback_percent": "1", "destination_url": "https://merchant.example",
        "budget_sats": 100, "max_reward_sats": 10
})
    assert no_origin.status_code == 403
    assert create_cashback(client, other).status_code == 403
    for percent in ("0", "0.001", "100.01", "1.234"):
        assert create_cashback(client, owner, cashback_percent=percent).status_code == 422
    for days in (0, 366):
        assert create_cashback(client, owner, attribution_window_days=days).status_code == 422
    assert create_cashback(client, owner, destination_url="javascript:alert(1)").status_code == 422
    assert create_cashback(client, owner, budget_sats=None).status_code == 422
    assert create_cashback(client, owner, max_reward_sats=101, budget_sats=100).status_code == 422
    assert create_cashback(client, owner, budget_sats=2_100_000_001).status_code == 422
    low = create_cashback(client, owner, cashback_percent="0.01", attribution_window_days=1)
    high = create_cashback(client, owner, cashback_percent="100.00", attribution_window_days=365)
    assert low.status_code == high.status_code == 200
    assert low.json()["code"] != high.json()["code"]
    assert low.json()["code"] not in owner.public_key().to_hex()


def test_public_landing_and_claim_are_private_and_fail_safe(tmp_path, monkeypatch):
    merchant = Keys.generate()
    client = client_for(tmp_path, monkeypatch, merchant)
    login(client, merchant)
    campaign = create_cashback(client, merchant).json()
    assert campaign["short_url"].startswith("https://mrt.st/x/")
    code = campaign["code"]
    canonical = client.get(f"/x/{code}?lang=en", follow_redirects=False)
    assert canonical.status_code == 302
    assert canonical.headers["location"] == f"https://mrt.st/x/{code}?lang=en"
    assert canonical.headers["cache-control"] == "no-store"
    short_client = TestClient(main.app, base_url="https://mrt.st")
    page = short_client.get(f"/x/{code}?lang=en")
    assert page.status_code == 200
    assert "Cashback café" in page.text and "7.25%" in page.text
    assert "Cashback" in page.text and "reembolso" in page.text
    assert '/static/cashback-express.css?v=20260805-status2' in page.text
    assert '/static/cashback-express.js?v=20260805-status2' in page.text
    assert f'href="/x/{code}/check"' in page.text
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["referrer-policy"] == "no-referrer"
    assert "referrer no-referrer" not in page.headers["content-security-policy"]
    assert "default-src 'self'" in page.headers["content-security-policy"]
    css = short_client.get("/static/cashback-express.css", follow_redirects=False)
    script = short_client.get("/static/cashback-express.js", follow_redirects=False)
    font = short_client.get("/static/fonts/space-grotesk-latin.woff2", follow_redirects=False)
    unrelated_asset = short_client.get("/static/app.js", follow_redirects=False)
    assert css.status_code == 200 and "body [lang]{display:none}" in css.text
    assert script.status_code == 200 and "setLanguage(selector.value" in script.text
    assert font.status_code == 200 and font.content.startswith(b"wOF2")
    assert "location" not in css.headers
    assert "location" not in script.headers
    assert "location" not in font.headers
    assert unrelated_asset.status_code == 308
    assert unrelated_asset.headers["location"] == "https://testserver/static/app.js"
    assert "bb_click_id" not in page.text

    observed = []
    monkeypatch.setattr(main, "validate_lightning_address", lambda address: observed.append(address) or {"callback": "ok"})
    short_claim = short_client.post(
        f"/x/{code}/claim?response=json",
        json={"lightning_address": "short-host@wallet.example"},
        follow_redirects=False,
    )
    assert short_claim.status_code == 200
    assert urlparse(short_claim.json()["redirect_url"]).hostname == "merchant.example"
    assert "location" not in short_claim.headers

    address = "Buyer@Wallet.Example"
    claim = client.post(f"/x/{code}/claim", json={"lightning_address": address}, follow_redirects=False)
    assert claim.status_code == 303
    assert observed == []  # Public claims perform no LNURL network request.
    assert address.lower() not in claim.headers["location"]
    query = parse_qs(urlparse(claim.headers["location"]).query)
    assert set(query) == {"utm", "bb_click_id", "bb_campaign"}
    assert query["utm"] == ["keep"]
    assert urlparse(claim.headers["location"]).fragment == ""
    assert query["bb_click_id"][0].startswith("clk_")
    assert query["bb_campaign"] == [code]
    with main.engine().connect() as connection:
        row = connection.execute(
            text("SELECT * FROM cashback_claims WHERE lightning_address=:address"),
            {"address": address.lower()},
        ).mappings().one()
        assert row["lightning_address"] == address.lower()
        assert address.lower() not in (row.get("redirect_url") or "")
    json_claim = client.post(
        f"/x/{code}/claim?response=json",
        json={"lightning_address": "second@wallet.example"},
    )
    assert json_claim.status_code == 200
    status_token = json_claim.json()["status_token"]
    assert len(status_token) >= 40
    assert json_claim.json()["status_path"] == f"/x/{code}/check"
    assert "meerat_cashback_status=" in json_claim.headers["set-cookie"]
    assert "HttpOnly" in json_claim.headers["set-cookie"]
    assert f"Path=/x/{code}/check" in json_claim.headers["set-cookie"]
    assert "SameSite=lax" in json_claim.headers["set-cookie"]
    redirect_url = json_claim.json()["redirect_url"]
    assert urlparse(redirect_url).hostname == "merchant.example"
    assert "second@wallet.example" not in redirect_url
    assert parse_qs(urlparse(redirect_url).query)["bb_campaign"] == [code]
    assert status_token not in redirect_url
    with main.engine().connect() as connection:
        persisted = connection.execute(
            text("SELECT status_token_hash FROM cashback_claims WHERE lightning_address='second@wallet.example'")
        ).scalar_one()
        assert persisted == main.sha(status_token)
        assert status_token not in persisted
    assert client.post(f"/x/{code}/claim", json={"lightning_address": "not-an-address"}).status_code == 422
    with main.engine().begin() as connection:
        connection.execute(text("UPDATE cashback_campaigns SET status='paused' WHERE short_code=:code"), {"code": code})
    assert short_client.get(f"/x/{code}").status_code == 404
    assert client.post(f"/x/{code}/claim", json={"lightning_address": "buyer@wallet.example"}).status_code == 404


def test_private_cashback_status_lifecycle_uses_capability_token_not_address(tmp_path, monkeypatch):
    merchant = Keys.generate()
    client = client_for(tmp_path, monkeypatch, merchant)
    login(client, merchant)
    campaign = create_cashback(client, merchant, cashback_percent="10").json()
    code = campaign["code"]
    short_client = TestClient(main.app, base_url="https://mrt.st")

    page = short_client.get(f"/x/{code}/check?lang=en")
    assert page.status_code == 200
    assert "Check your cashback" in page.text
    assert "Dirección Lightning" in page.text
    assert "buyer@wallet.example" not in page.text
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["referrer-policy"] == "no-referrer"
    assert page.headers["x-robots-tag"] == "noindex, nofollow"

    missing = short_client.post(f"/x/{code}/check", json={})
    invalid = short_client.post(f"/x/{code}/check", json={"token": "x" * 43})
    address_probe = short_client.post(
        f"/x/{code}/check", json={"lightning_address": "buyer@wallet.example"}
    )
    assert missing.status_code == invalid.status_code == 404
    assert address_probe.status_code == 422
    assert missing.json() == invalid.json() == {"detail": "Cashback status unavailable."}

    claim = short_client.post(
        f"/x/{code}/claim?response=json",
        json={"lightning_address": "buyer@wallet.example"},
    )
    assert claim.status_code == 200
    token = claim.json()["status_token"]
    tracking = short_client.post(f"/x/{code}/check", json={"token": token})
    assert tracking.status_code == 200
    assert tracking.headers["cache-control"] == "no-store"
    assert tracking.headers["referrer-policy"] == "no-referrer"
    assert tracking.json()["status"] == "tracking"
    assert tracking.json()["lightning_address_masked"] == "b***r@w***t.example"
    assert tracking.json()["campaign_name"] == "Cashback café"
    assert tracking.json()["reward_sats"] is None
    assert "lightning_address" not in tracking.json()
    assert "order_key" not in tracking.json()
    assert "payment_evidence" not in tracking.json()

    # The HttpOnly campaign-scoped cookie is enough on the same browser.
    cookie_tracking = short_client.post(f"/x/{code}/check", json={})
    assert cookie_tracking.status_code == 200
    assert cookie_tracking.json()["status"] == "tracking"

    other = create_cashback(client, merchant, name="Other cashback").json()
    assert short_client.post(f"/x/{other['code']}/check", json={"token": token}).status_code == 404

    claim_id = parse_qs(urlparse(claim.json()["redirect_url"]).query)["bb_click_id"][0]
    reward = main.process_cashback_reward(
        order_key="status-order", claim_id=claim_id, campaign_code=code,
        order_total=Decimal("1000"), currency="SATS",
        authorized_merchant_hex=merchant.public_key().to_hex(),
    )
    pending = short_client.post(f"/x/{code}/check", json={"token": token}).json()
    assert pending["status"] == "pending"
    assert pending["reward_sats"] == 100
    assert pending["purchase_confirmed_at"]
    assert pending["paid_at"] is None

    payment = {"payment_hash": "cd" * 32, "evidence": "merchant wallet receipt"}
    paid = client.post(
        f"/app/merchant/cashback-rewards/{reward['id']}/paid",
        json=payment, headers={"Origin": "https://testserver"},
    )
    assert paid.status_code == 200
    settled = short_client.post(f"/x/{code}/check", json={"token": token}).json()
    assert settled["status"] == "paid"
    assert settled["reward_sats"] == 100
    assert settled["paid_at"]
    assert settled["payment_evidence"] == "merchant_attested"
    assert settled["payment_hash_short"] == "cdcdcdcd…cdcdcdcd"
    assert "merchant wallet receipt" not in json.dumps(settled)

    with main.engine().begin() as connection:
        connection.execute(
            text("UPDATE cashback_claims SET status_access_expires_at=:expired WHERE id=:id"),
            {"expired": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), "id": claim_id},
        )
    assert main.cleanup_expired_cashback_claims() == 0
    with main.engine().begin() as connection:
        assert connection.execute(
            text("SELECT id FROM cashback_claims WHERE id=:id"), {"id": claim_id}
        ).fetchone()


def test_cashback_status_expiry_and_short_host_routing(tmp_path, monkeypatch):
    merchant = Keys.generate()
    client = client_for(tmp_path, monkeypatch, merchant)
    login(client, merchant)
    campaign = create_cashback(client, merchant).json()
    code = campaign["code"]
    short_client = TestClient(main.app, base_url="https://mrt.st")
    claim = short_client.post(
        f"/x/{code}/claim?response=json", json={"lightning_address": "expired@wallet.example"}
    )
    token = claim.json()["status_token"]
    claim_id = parse_qs(urlparse(claim.json()["redirect_url"]).query)["bb_click_id"][0]
    with main.engine().begin() as connection:
        connection.execute(
            text("UPDATE cashback_claims SET expires_at=:expired WHERE id=:id"),
            {"expired": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), "id": claim_id},
        )
    expired = short_client.post(f"/x/{code}/check", json={"token": token})
    assert expired.status_code == 200
    assert expired.json()["status"] == "expired"

    canonical = client.get(f"/x/{code}/check?lang=en", follow_redirects=False)
    assert canonical.status_code == 302
    assert canonical.headers["location"] == f"https://mrt.st/x/{code}/check?lang=en"
    assert canonical.headers["cache-control"] == "no-store"
    direct = short_client.get(f"/x/{code}/check", follow_redirects=False)
    lookup = short_client.post(f"/x/{code}/check", json={"token": token}, follow_redirects=False)
    assert direct.status_code == lookup.status_code == 200
    assert "location" not in direct.headers and "location" not in lookup.headers


def test_cashback_status_capability_expires_and_http_transport_is_rejected(tmp_path, monkeypatch):
    merchant = Keys.generate()
    client = client_for(tmp_path, monkeypatch, merchant)
    login(client, merchant)
    code = create_cashback(client, merchant).json()["code"]
    short_client = TestClient(main.app, base_url="https://mrt.st")
    claim = short_client.post(
        f"/x/{code}/claim?response=json",
        json={"lightning_address": "buyer@wallet.example"},
    )
    token = claim.json()["status_token"]
    with main.engine().begin() as connection:
        connection.execute(
            text("UPDATE cashback_claims SET status_access_expires_at=:expired"),
            {"expired": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()},
        )
    denied = short_client.post(f"/x/{code}/check", json={"token": token})
    assert denied.status_code == 404
    assert denied.json() == {"detail": "Cashback status unavailable."}
    assert main.cleanup_expired_cashback_claims() == 1
    with main.engine().begin() as connection:
        assert connection.execute(text("SELECT id FROM cashback_claims")).fetchone() is None

    http_client = TestClient(main.app, base_url="http://mrt.st")
    redirect = http_client.get(f"/x/{code}/check", follow_redirects=False)
    rejected_claim = http_client.post(
        f"/x/{code}/claim?response=json",
        json={"lightning_address": "other@wallet.example"},
    )
    rejected_status = http_client.post(f"/x/{code}/check", json={"token": token})
    alternate_http = TestClient(main.app, base_url="http://alternate.example")
    alternate_status = alternate_http.post(f"/x/{code}/check", json={"token": token})
    proxied_https = http_client.get(
        f"/x/{code}/check", headers={"X-Forwarded-Proto": "https"}, follow_redirects=False
    )
    assert redirect.status_code == 308
    assert redirect.headers["location"].startswith("https://mrt.st/")
    assert rejected_claim.status_code == rejected_status.status_code == alternate_status.status_code == 400
    assert proxied_https.status_code == 200
    assert "data-cashback-status" in proxied_https.text


def test_short_host_middleware_does_not_redirect_to_itself(tmp_path, monkeypatch):
    merchant = Keys.generate()
    client_for(tmp_path, monkeypatch, merchant)
    monkeypatch.setattr(main, "BASE_URL", "https://mrt.st")
    short_client = TestClient(main.app, base_url="https://mrt.st")

    response = short_client.get("/x/unknown-code", follow_redirects=False)
    referral = short_client.get("/affiliate-code", follow_redirects=False)

    assert response.status_code == 404
    assert "location" not in response.headers
    assert referral.status_code == 302
    assert referral.headers["location"] == "https://mrt.st/r/affiliate-code"


def test_cashback_has_its_own_dashboard_and_click_destination_is_private(tmp_path, monkeypatch):
    merchant, outsider = Keys.generate(), Keys.generate()
    client = client_for(tmp_path, monkeypatch, merchant)
    login(client, merchant)
    campaign = create_cashback(client, merchant).json()
    click_id = _claim_id(client, campaign["code"], "buyer@wallet.example")

    affiliate_program = client.get("/app/merchant?view=campaigns")
    assert affiliate_program.status_code == 200
    assert 'id="cashback-express-create"' not in affiliate_program.text
    assert 'href="/app/merchant?view=cashback"' in affiliate_program.text

    cashback = client.get("/app/merchant?view=cashback")
    assert cashback.status_code == 200
    assert 'id="cashback-express-create"' in cashback.text
    assert 'class="cashback-campaign-card"' in cashback.text
    assert 'id="cashback-clicks"' in cashback.text
    assert click_id in cashback.text
    assert "b***r@w***t.example" in cashback.text
    assert "buyer@wallet.example" not in cashback.text
    assert f'data-cashback-claim="{click_id}"' in cashback.text

    assert client.post(f"/app/merchant/cashback-claims/{click_id}/destination").status_code == 403
    revealed = client.post(f"/app/merchant/cashback-claims/{click_id}/destination", headers={"Origin": "https://testserver"})
    assert revealed.status_code == 200
    assert revealed.json()["lightning_address"] == "buyer@wallet.example"
    assert revealed.headers["cache-control"] == "no-store"

    client.cookies.clear()
    assert client.post(f"/app/merchant/cashback-claims/{click_id}/destination", headers={"Origin": "https://testserver"}).status_code == 401
    client.post("/campaigns", json={
        "merchant_pubkey": outsider.public_key().to_bech32(), "name": "Outsider",
        "commission_bps": 500, "attribution_window_days": 30,
        "destination_url": "https://outsider.example", "enrollment_mode": "private",
    })
    login(client, outsider)
    assert client.post(f"/app/merchant/cashback-claims/{click_id}/destination", headers={"Origin": "https://testserver"}).status_code == 404


def test_shopify_cashback_reward_is_idempotent_conflict_safe_and_tenant_scoped(tmp_path, monkeypatch):
    merchant = Keys.generate()
    client = client_for(tmp_path, monkeypatch, merchant)
    login(client, merchant)
    campaign = create_cashback(client, merchant, cashback_percent="10").json()
    monkeypatch.setattr(main, "validate_lightning_address", lambda address: {"callback": "ok"})
    claimed = client.post(f"/x/{campaign['code']}/claim", json={"lightning_address": "buyer@wallet.example"}, follow_redirects=False)
    click_id = parse_qs(urlparse(claimed.headers["location"]).query)["bb_click_id"][0]
    payload = {"id": 101, "financial_status": "paid", "total_price": "1.00", "currency": "USD", "note_attributes": [
        {"name": "bb_click_id", "value": click_id}, {"name": "bb_campaign", "value": campaign["code"]}
    ]}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    first = client.post("/shopify/webhooks/orders-paid", content=raw, headers=signed_headers(raw))
    assert first.status_code == 200, first.text
    assert first.json()["queued"] is True
    with main.engine().connect() as connection:
        reward = connection.execute(text("SELECT * FROM cashback_rewards")).mappings().one()
        assert reward["order_total_decimal"] == "1"
        assert reward["order_total_sats"] == 2500
        assert reward["cashback_bps"] == 1000
        assert reward["reward_sats"] == 250
        assert reward["status"] == "pending"
        delivery = connection.execute(text("SELECT * FROM shopify_webhook_deliveries")).mappings().one()
        assert delivery["reward_id"] == reward["id"] and delivery["conversion_id"] is None
    duplicate = client.post("/shopify/webhooks/orders-paid", content=raw, headers=signed_headers(raw, "cashback-wh-2"))
    assert duplicate.json()["duplicate"] is True
    assert duplicate.json()["reward_id"] == reward["id"]
    changed = dict(payload, total_price="2.00")
    changed_raw = json.dumps(changed, separators=(",", ":")).encode()
    conflict = client.post("/shopify/webhooks/orders-paid", content=changed_raw, headers=signed_headers(changed_raw, "cashback-wh-3"))
    assert conflict.json()["conflict"] is True
    with main.engine().connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM cashback_rewards")).scalar_one() == 1
    page = client.get("/app/merchant?view=cashback")
    assert "recompensas a compradores" in page.text.lower()
    assert "b***r@w***t.example" in page.text
    assert "buyer@wallet.example" not in page.text


def test_expired_claim_never_creates_reward_and_snippets_carry_no_address(tmp_path, monkeypatch):
    merchant = Keys.generate()
    client = client_for(tmp_path, monkeypatch, merchant)
    login(client, merchant)
    campaign = create_cashback(client, merchant).json()
    monkeypatch.setattr(main, "validate_lightning_address", lambda address: {"callback": "ok"})
    claimed = client.post(f"/x/{campaign['code']}/claim", json={"lightning_address": "buyer@wallet.example"}, follow_redirects=False)
    click_id = parse_qs(urlparse(claimed.headers["location"]).query)["bb_click_id"][0]
    with main.engine().begin() as connection:
        connection.execute(text("UPDATE cashback_claims SET expires_at=:expired WHERE id=:id"), {
            "expired": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), "id": click_id
        })
    payload = {"id": 102, "total_price": "1.00", "currency": "USD", "note_attributes": [{"name": "bb_click_id", "value": click_id}, {"name": "bb_campaign", "value": campaign["code"]}]}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    response = client.post("/shopify/webhooks/orders-paid", content=raw, headers=signed_headers(raw))
    assert response.status_code == 200
    with main.engine().connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM cashback_rewards")).scalar_one() == 0
    snippets = main.shopify_installation_snippets("https://testserver", "cashback.myshopify.com")
    assert "bb_campaign" in snippets["theme_script"] and "bb_campaign" in snippets["custom_pixel"]
    assert "if (campaignCode) return;" in snippets["theme_script"]
    assert "if (campaignCode) return;" in snippets["custom_pixel"]
    assert "lightning_address" not in snippets["theme_script"] + snippets["custom_pixel"]
    with main.engine().connect() as connection:
        indexes = connection.execute(text("PRAGMA index_list(cashback_rewards)")).mappings().all()
        assert any(row["name"] == "uq_cashback_rewards_order_key" and row["unique"] for row in indexes)


def _claim_id(client: TestClient, code: str, address: str = "buyer@wallet.example") -> str:
    response = client.post(f"/x/{code}/claim", json={"lightning_address": address}, follow_redirects=False)
    assert response.status_code == 303, response.text
    return parse_qs(urlparse(response.headers["location"]).query)["bb_click_id"][0]


def test_single_store_configuration_is_required_and_enforced(tmp_path, monkeypatch):
    merchant, other = Keys.generate(), Keys.generate()
    client = client_for(tmp_path, monkeypatch, merchant)
    login(client, merchant)
    monkeypatch.delenv("SHOPIFY_MERCHANT_PUBKEY")
    assert create_cashback(client, merchant).status_code == 503
    monkeypatch.setenv("SHOPIFY_MERCHANT_PUBKEY", other.public_key().to_bech32())
    assert create_cashback(client, merchant).status_code == 403


def test_claim_is_single_use_same_order_idempotent_and_budget_is_atomic(tmp_path, monkeypatch):
    merchant = Keys.generate()
    client = client_for(tmp_path, monkeypatch, merchant)
    login(client, merchant)
    campaign = create_cashback(client, merchant, cashback_percent="10", budget_sats=250).json()
    claim_one = _claim_id(client, campaign["code"], "one@wallet.example")
    claim_two = _claim_id(client, campaign["code"], "two@wallet.example")
    merchant_hex = merchant.public_key().to_hex()

    def fund(order: str, claim: str):
        try:
            return main.process_cashback_reward(
                order_key=order, claim_id=claim, campaign_code=campaign["code"],
                order_total=Decimal("2500"), currency="SATS", authorized_merchant_hex=merchant_hex,
            )
        except Exception as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda args: fund(*args), [("order-a", claim_one), ("order-b", claim_two)]))
    successes = [result for result in results if isinstance(result, dict)]
    assert len(successes) == 1
    winner = successes[0]
    assert fund(winner["order_key"], winner["claim_id"])["id"] == winner["id"]
    duplicate_claim = fund("different-order", winner["claim_id"])
    assert getattr(duplicate_claim, "status_code", None) == 409
    with main.engine().connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM cashback_rewards")).scalar_one() == 1
        row = connection.execute(text("SELECT committed_sats, budget_sats FROM cashback_campaigns WHERE id=:id"), {"id": campaign["campaign_id"]}).mappings().one()
        assert row["committed_sats"] == row["budget_sats"] == 250
        assert connection.execute(text("SELECT consumed_at FROM cashback_claims WHERE id=:id"), {"id": winner["claim_id"]}).scalar_one()


def test_reward_reveal_and_paid_cas_are_authenticated_scoped_and_audited(tmp_path, monkeypatch):
    merchant, outsider = Keys.generate(), Keys.generate()
    client = client_for(tmp_path, monkeypatch, merchant)
    login(client, merchant)
    campaign = create_cashback(client, merchant, cashback_percent="10").json()
    claim = _claim_id(client, campaign["code"])
    reward = main.process_cashback_reward(
        order_key="payable-order", claim_id=claim, campaign_code=campaign["code"],
        order_total=Decimal("1000"), currency="SATS", authorized_merchant_hex=merchant.public_key().to_hex(),
    )
    reward_id = reward["id"]
    dashboard = client.get("/app/merchant?view=cashback")
    assert "buyer@wallet.example" not in dashboard.text
    def outbound_must_not_run(_address):
        raise AssertionError("destination reveal must not perform outbound Lightning calls")
    monkeypatch.setattr(main, "validate_lightning_address", outbound_must_not_run)

    client.cookies.clear()
    assert client.post(f"/app/merchant/cashback-rewards/{reward_id}/destination", headers={"Origin": "https://testserver"}).status_code == 401
    outsider_seed = client.post("/campaigns", json={
        "merchant_pubkey": outsider.public_key().to_bech32(), "name": "Outsider program",
        "commission_bps": 500, "attribution_window_days": 30,
        "destination_url": "https://outsider.example/shop", "enrollment_mode": "private",
    })
    assert outsider_seed.status_code == 200
    login(client, outsider)
    assert client.post(f"/app/merchant/cashback-rewards/{reward_id}/destination", headers={"Origin": "https://testserver"}).status_code == 404
    client.cookies.clear()
    login(client, merchant)
    assert client.post(f"/app/merchant/cashback-rewards/{reward_id}/destination").status_code == 403
    reveal = client.post(f"/app/merchant/cashback-rewards/{reward_id}/destination", headers={"Origin": "https://testserver"})
    assert reveal.status_code == 200 and reveal.json()["lightning_address"] == "buyer@wallet.example"
    assert reveal.headers["cache-control"] == "no-store"

    evidence = {"payment_hash": "ab" * 32, "evidence": "wallet receipt 123"}
    assert client.post(f"/app/merchant/cashback-rewards/{reward_id}/paid", json=evidence).status_code == 403
    paid = client.post(f"/app/merchant/cashback-rewards/{reward_id}/paid", json=evidence, headers={"Origin": "https://testserver"})
    assert paid.status_code == 200 and paid.json()["idempotent"] is False
    again = client.post(f"/app/merchant/cashback-rewards/{reward_id}/paid", json=evidence, headers={"Origin": "https://testserver"})
    assert again.status_code == 200 and again.json()["idempotent"] is True
    changed = dict(evidence, evidence="different receipt")
    assert client.post(f"/app/merchant/cashback-rewards/{reward_id}/paid", json=changed, headers={"Origin": "https://testserver"}).status_code == 409
    with main.engine().connect() as connection:
        audited = connection.execute(text("SELECT * FROM cashback_rewards WHERE id=:id"), {"id": reward_id}).mappings().one()
        assert audited["destination_revealed_at"] and audited["paid_at"]
        assert audited["payment_hash"] == evidence["payment_hash"] and audited["payment_evidence"] == evidence["evidence"]


def test_campaign_pause_resume_is_same_origin_owned_and_reflected_in_ui(tmp_path, monkeypatch):
    merchant, outsider = Keys.generate(), Keys.generate()
    client = client_for(tmp_path, monkeypatch, merchant)
    login(client, merchant)
    campaign = create_cashback(client, merchant).json()
    endpoint = f"/app/merchant/cashback-express/{campaign['campaign_id']}/status"
    assert client.post(endpoint, json={"status": "paused"}).status_code == 403
    paused = client.post(endpoint, json={"status": "paused"}, headers={"Origin": "https://testserver"})
    assert paused.status_code == 200 and paused.json()["status"] == "paused"
    assert client.get(f"/x/{campaign['code']}").status_code == 404
    page = client.get("/app/merchant?view=cashback&lang=en")
    assert "Resume" in page.text and "data-cashback-campaign-status" in page.text

    client.cookies.clear()
    seed = client.post("/campaigns", json={
        "merchant_pubkey": outsider.public_key().to_bech32(), "name": "Outsider",
        "commission_bps": 500, "attribution_window_days": 30,
        "destination_url": "https://outsider.example", "enrollment_mode": "private",
    })
    assert seed.status_code == 200
    login(client, outsider)
    assert client.post(endpoint, json={"status": "active"}, headers={"Origin": "https://testserver"}).status_code == 404
    client.cookies.clear()
    login(client, merchant)
    resumed = client.post(endpoint, json={"status": "active"}, headers={"Origin": "https://testserver"})
    assert resumed.status_code == 200
    assert client.get(f"/x/{campaign['code']}").status_code == 200


def test_stale_processing_delivery_is_requeued_without_duplicate_reward(tmp_path, monkeypatch):
    merchant = Keys.generate()
    client = client_for(tmp_path, monkeypatch, merchant)
    login(client, merchant)
    campaign = create_cashback(client, merchant, cashback_percent="10").json()
    claim = _claim_id(client, campaign["code"])
    payload = {"id": 901, "total_price": "1.00", "currency": "USD", "note_attributes": [
        {"name": "bb_click_id", "value": claim}, {"name": "bb_campaign", "value": campaign["code"]}
    ]}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    assert client.post("/shopify/webhooks/orders-paid", content=raw, headers=signed_headers(raw, "stale-1")).status_code == 200
    stale = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
    with main.engine().begin() as connection:
        connection.execute(text("""
            UPDATE shopify_webhook_deliveries
            SET status='processing', processing_started_at=:stale, processed_at=NULL
        """), {"stale": stale})
    retried = client.post("/shopify/webhooks/orders-paid", content=raw, headers=signed_headers(raw, "stale-2"))
    assert retried.status_code == 200 and retried.json()["queued"] is True
    with main.engine().connect() as connection:
        delivery = connection.execute(text("SELECT * FROM shopify_webhook_deliveries")).mappings().one()
        assert delivery["status"] == "processed" and delivery["processing_started_at"] is None
        assert connection.execute(text("SELECT COUNT(*) FROM cashback_rewards")).scalar_one() == 1


def test_pending_cashback_total_is_not_limited_to_visible_fifty(tmp_path, monkeypatch):
    merchant = Keys.generate()
    client = client_for(tmp_path, monkeypatch, merchant)
    login(client, merchant)
    campaign = create_cashback(client, merchant).json()
    created = datetime.now(timezone.utc).isoformat()
    with main.engine().begin() as connection:
        for index in range(60):
            claim_id = f"aggregate-claim-{index}"
            connection.execute(text("""
                INSERT INTO cashback_claims (id,campaign_id,lightning_address,created_at,expires_at,consumed_at,consumed_order_key)
                VALUES (:id,:campaign,'buyer@wallet.example',:created,:expires,:created,:order_key)
            """), {"id": claim_id, "campaign": campaign["campaign_id"], "created": created, "expires": created, "order_key": f"aggregate-order-{index}"})
            connection.execute(text("""
                INSERT INTO cashback_rewards
                  (id,order_key,claim_id,campaign_id,merchant_pubkey_hex,order_total,order_total_decimal,currency,
                   order_total_sats,cashback_bps,reward_sats,status,created_at,rate_stale)
                VALUES (:id,:order_key,:claim,:campaign,:merchant,1,'1','SATS',1,10000,1,'pending',:created,0)
            """), {"id": f"aggregate-reward-{index}", "order_key": f"aggregate-order-{index}", "claim": claim_id,
                     "campaign": campaign["campaign_id"], "merchant": merchant.public_key().to_hex(), "created": created})
    page = client.get("/app/merchant?view=cashback")
    assert page.status_code == 200
    assert "60 sats pendientes" in page.text
