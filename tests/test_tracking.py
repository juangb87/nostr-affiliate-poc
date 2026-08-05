import json

from fastapi.testclient import TestClient
from sqlalchemy import text

from app import main

app = main.app


def event_payload() -> dict:
    return {
        "type": "page_view",
        "shop": "shapersfit.myshopify.com",
        "mrt_ref": "ref_test123",
        "mrt_click_id": "clk_test123",
        "url": "https://shapersfit.com/?mrt_click_id=clk_test123&mrt_ref=ref_test123",
        "path": "/",
    }


def conversion_payload() -> dict:
    return {
        "type": "checkout_completed",
        "shop": "shapersfit.myshopify.com",
        "mrt_ref": "ref_test123",
        "mrt_click_id": "clk_test123",
        "order_id": "shopify_order_test",
        "total_price": "42.50",
        "currency": "USD",
    }


def test_v1_tracking_endpoints_are_canonical(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/tracking.db")
    client = TestClient(app)

    preflight = client.options(
        "/v1/events",
        headers={
            "Origin": "https://shapersfit.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "https://shapersfit.com"

    pixel_preflight = client.options(
        "/v1/conversions",
        headers={
            "Origin": "null",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert pixel_preflight.status_code == 200
    assert pixel_preflight.headers["access-control-allow-origin"] == "null"

    track = client.post(
        "/v1/events",
        headers={"Origin": "https://shapersfit.com"},
        json=event_payload(),
    )
    assert track.status_code == 200, track.text
    assert track.headers["access-control-allow-origin"] == "https://shapersfit.com"
    assert track.json()["kind"] == "track"
    assert track.json()["mrt_ref"] == "ref_test123"
    assert track.json()["mrt_click_id"] == "clk_test123"
    assert "bb_ref" not in track.json() and "bb_click_id" not in track.json()

    conversion = client.post(
        "/v1/conversions",
        headers={"Origin": "https://shapersfit.com"},
        json=conversion_payload(),
    )
    assert conversion.status_code == 200, conversion.text
    assert conversion.json()["kind"] == "conversion"

    status = client.get("/v1/tracking/status")
    assert status.status_code == 200
    assert status.json()["total_events"] == 2
    assert status.json()["counts"] == {"conversion": 1, "track": 1}
    assert status.json()["recent"][0]["mrt_ref"] == "ref_test123"
    assert status.json()["recent"][0]["mrt_click_id"] == "clk_test123"

    openapi = client.get("/openapi.json").json()
    assert "bb_" not in json.dumps(openapi)
    assert "Bumbei" not in json.dumps(openapi)
    paths = openapi["paths"]
    assert "/v1/events" in paths
    assert "/v1/conversions" in paths
    assert "/v1/tracking/status" in paths
    assert "/mrt.js" in paths
    mrt_content = paths["/mrt.js"]["get"]["responses"]["200"]["content"]
    assert "application/javascript" in mrt_content
    assert "application/json" not in mrt_content
    assert "/bb.js" not in paths
    assert "/bumbei/track" not in paths
    schemas = openapi["components"]["schemas"]
    for schema_name in ("ConversionIn", "BrowserEventIn", "BrowserConversionIn", "MerchantConversionIn"):
        properties = schemas[schema_name]["properties"]
        assert "bb_ref" not in properties
        assert "bb_click_id" not in properties
    assert "mrt_ref" in schemas["BrowserEventIn"]["properties"]
    assert "mrt_click_id" in schemas["MerchantConversionIn"]["properties"]
    assert "/bumbei/conversion" not in paths
    assert "/bumbei/status" not in paths


def test_meerat_snippet_reads_legacy_attribution_but_writes_only_canonical_keys():
    snippet = main.MRT_JS

    assert "p.get('bb_click_id')" in snippet
    assert "p.get('bb_ref')" in snippet
    assert "compatibleStoredValue('mrt_click_id', 'bb_click_id')" in snippet
    assert "compatibleStoredValue('mrt_ref', 'bb_ref')" in snippet
    assert "localStorage.getItem(legacy)" in snippet
    assert "getCookie(legacy)" in snippet
    assert "canonicalClick !== legacyClick" in snippet
    assert "canonicalRef !== legacyRef" in snippet
    assert "return { conflict: true }" in snippet
    assert "if(params.conflict)" in snippet
    assert "blocked = true" in snippet
    assert "if(blocked) return { mrt_click_id: null, mrt_ref: null }" in snippet
    assert "clearStoredAttribution();" in snippet
    assert "['mrt_click_id','mrt_ref','bb_click_id','bb_ref']" in snippet
    assert "localStorage.removeItem(name)" in snippet
    assert "localStorage.setItem('bb_click_id'" not in snippet
    assert "localStorage.setItem('bb_ref'" not in snippet
    assert "setCookie('bb_click_id'" not in snippet
    assert "setCookie('bb_ref'" not in snippet


def test_legacy_bumbei_routes_remain_working_aliases(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/legacy.db")
    client = TestClient(app)

    legacy_event = {**event_payload(), "bb_ref": "ref_test123", "bb_click_id": "clk_test123"}
    legacy_event.pop("mrt_ref")
    legacy_event.pop("mrt_click_id")
    legacy_conversion = {**conversion_payload(), "bb_ref": "ref_test123", "bb_click_id": "clk_test123"}
    legacy_conversion.pop("mrt_ref")
    legacy_conversion.pop("mrt_click_id")
    track = client.post("/bumbei/track", json=legacy_event)
    assert track.status_code == 200
    assert track.json()["mrt_ref"] == "ref_test123"
    assert track.json()["mrt_click_id"] == "clk_test123"
    assert client.post("/bumbei/conversion", json=legacy_conversion).status_code == 200

    status = client.get("/bumbei/status")
    assert status.status_code == 200
    assert status.json()["total_events"] == 2


def test_tracking_rejects_conflicting_canonical_and_legacy_attribution(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/conflict.db")
    client = TestClient(app)

    response = client.post("/v1/events", json={
        "mrt_ref": "ref_canonical",
        "bb_ref": "ref_legacy",
        "mrt_click_id": "clk_same",
        "bb_click_id": "clk_same",
    })
    assert response.status_code == 422


def test_tracking_minimizes_sensitive_browser_payload_and_rejects_mismatched_attribution(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/privacy.db")
    main._ENGINE = None
    main._ENGINE_URL = None
    main.init_db()
    with main.engine().begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO clicks (id, ref_code, campaign_id, affiliate_pubkey, landing_url, created_at)
                VALUES ('clk_private', 'ref_actual', 'camp_private', 'npub_private',
                        'https://merchant.example/', :created_at)
                """
            ),
            {"created_at": main.now()},
        )
    client = TestClient(app)

    mismatch = client.post(
        "/v1/events",
        json={"type": "page_view", "mrt_ref": "ref_other", "mrt_click_id": "clk_private", "path": "/shop"},
    )
    assert mismatch.status_code == 400

    conversion = client.post(
        "/v1/conversions",
        json={
            "type": "checkout_completed",
            "mrt_ref": "ref_actual",
            "mrt_click_id": "clk_private",
            "order_id": "sensitive-order-123",
            "checkout_token": "sensitive-checkout-token",
            "url": "https://merchant.example/thank-you?email=buyer@example.com&token=secret",
            "referrer": "https://merchant.example/checkout?token=other-secret",
            "path": "/thank-you",
            "user_agent": "private-browser-fingerprint",
            "total_price": "42.50",
            "currency": "USD",
            "metadata": {"event_id": "pixel-event-1", "source": "shopify_custom_pixel", "private": "drop-me"},
        },
    )
    assert conversion.status_code == 200
    with main.engine().connect() as connection:
        row = dict(
            connection.execute(
                text("SELECT * FROM tracking_events WHERE id=:id"), {"id": conversion.json()["event_id"]}
            ).one()._mapping
        )
    assert row["order_id_hash"] == main.sha("sensitive-order-123")
    assert row["checkout_token_hash"] == main.sha("sensitive-checkout-token")
    assert row["url"] == "https://merchant.example/thank-you"
    assert row["referrer"] == "https://merchant.example/checkout"
    assert "sensitive-order-123" not in row["payload_json"]
    assert "sensitive-checkout-token" not in row["payload_json"]
    assert "buyer@example.com" not in row["payload_json"]
    assert "private-browser-fingerprint" not in row["payload_json"]
    assert "drop-me" not in row["payload_json"]
    assert "pixel-event-1" in row["payload_json"]


def test_tracking_cors_includes_configured_shopify_store(monkeypatch):
    monkeypatch.setenv("TRACKING_CORS_ORIGINS", "https://lightningkoffee.io")
    monkeypatch.setenv("SHOPIFY_STORE_DOMAIN", "6c12wg-re.myshopify.com")
    origins = main.tracking_cors_origins()
    assert "https://lightningkoffee.io" in origins
    assert "https://6c12wg-re.myshopify.com" in origins
    assert "null" in origins


def test_absolute_redirect_url():
    from app.main import add_query_params

    url = add_query_params("shapersfit.com", {"mrt_click_id": "clk_1", "mrt_ref": "ref_1"})
    assert url == "https://shapersfit.com?mrt_click_id=clk_1&mrt_ref=ref_1"
