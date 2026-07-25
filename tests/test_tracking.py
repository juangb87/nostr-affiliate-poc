from fastapi.testclient import TestClient

from app.main import app


def event_payload() -> dict:
    return {
        "type": "page_view",
        "shop": "shapersfit.myshopify.com",
        "bb_ref": "ref_test123",
        "bb_click_id": "clk_test123",
        "url": "https://shapersfit.com/?bb_click_id=clk_test123&bb_ref=ref_test123",
        "path": "/",
    }


def conversion_payload() -> dict:
    return {
        "type": "checkout_completed",
        "shop": "shapersfit.myshopify.com",
        "bb_ref": "ref_test123",
        "bb_click_id": "clk_test123",
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

    track = client.post(
        "/v1/events",
        headers={"Origin": "https://shapersfit.com"},
        json=event_payload(),
    )
    assert track.status_code == 200, track.text
    assert track.headers["access-control-allow-origin"] == "https://shapersfit.com"
    assert track.json()["kind"] == "track"

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

    paths = client.get("/openapi.json").json()["paths"]
    assert "/v1/events" in paths
    assert "/v1/conversions" in paths
    assert "/v1/tracking/status" in paths
    assert "/bumbei/track" not in paths
    assert "/bumbei/conversion" not in paths
    assert "/bumbei/status" not in paths


def test_legacy_bumbei_routes_remain_working_aliases(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/legacy.db")
    client = TestClient(app)

    assert client.post("/bumbei/track", json=event_payload()).status_code == 200
    assert client.post("/bumbei/conversion", json=conversion_payload()).status_code == 200

    status = client.get("/bumbei/status")
    assert status.status_code == 200
    assert status.json()["total_events"] == 2


def test_absolute_redirect_url():
    from app.main import add_query_params

    url = add_query_params("shapersfit.com", {"bb_click_id": "clk_1", "bb_ref": "ref_1"})
    assert url == "https://shapersfit.com?bb_click_id=clk_1&bb_ref=ref_1"
