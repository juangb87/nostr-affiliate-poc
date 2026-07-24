from fastapi.testclient import TestClient

from app.main import app


def test_bumbei_tracking_endpoints(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/tracking.db")
    client = TestClient(app)

    preflight = client.options(
        "/bumbei/track",
        headers={
            "Origin": "https://shapersfit.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "https://shapersfit.com"

    track = client.post(
        "/bumbei/track",
        headers={"Origin": "https://shapersfit.com"},
        json={
            "type": "page_view",
            "shop": "shapersfit.myshopify.com",
            "bb_ref": "ref_test123",
            "bb_click_id": "clk_test123",
            "url": "https://shapersfit.com/?bb_click_id=clk_test123&bb_ref=ref_test123",
            "path": "/",
        },
    )
    assert track.status_code == 200, track.text
    assert track.headers["access-control-allow-origin"] == "https://shapersfit.com"
    assert track.json()["kind"] == "track"

    conversion = client.post(
        "/bumbei/conversion",
        headers={"Origin": "https://shapersfit.com"},
        json={
            "type": "checkout_completed",
            "shop": "shapersfit.myshopify.com",
            "bb_ref": "ref_test123",
            "bb_click_id": "clk_test123",
            "order_id": "shopify_order_test",
            "total_price": "42.50",
            "currency": "USD",
        },
    )
    assert conversion.status_code == 200, conversion.text
    assert conversion.json()["kind"] == "conversion"

    status = client.get("/bumbei/status")
    assert status.status_code == 200
    assert status.json()["total_events"] == 2
    assert status.json()["counts"] == {"conversion": 1, "track": 1}


def test_absolute_redirect_url():
    from app.main import add_query_params

    url = add_query_params("shapersfit.com", {"bb_click_id": "clk_1", "bb_ref": "ref_1"})
    assert url == "https://shapersfit.com?bb_click_id=clk_1&bb_ref=ref_1"
