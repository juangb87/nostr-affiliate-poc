from fastapi.testclient import TestClient

import app.main as main
from app.lightning import LightningPaymentResult, NwcPaymentError


def create_demo_payout(client: TestClient) -> str:
    demo = client.post("/demo")
    assert demo.status_code == 200, demo.text
    conversion_id = demo.json()["conversion"]["conversion_id"]
    return client.get(f"/flows/{conversion_id}").json()["payout"]["id"]


def test_real_payout_requires_admin_and_records_nwc_result(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/payout.db")
    monkeypatch.setenv("PAYOUT_ADMIN_KEY", "admin-test-key")
    monkeypatch.setenv("LIGHTNING_PAYOUTS_ENABLED", "true")
    monkeypatch.setenv("LIGHTNING_MAX_PAYOUT_SATS", "50000")
    client = TestClient(main.app)
    payout_id = create_demo_payout(client)

    async def fake_publish(_event, relays):
        return [{"relay": relay, "status": "published"} for relay in relays]

    monkeypatch.setattr(main, "_publish_event", fake_publish)
    monkeypatch.setenv("NOSTR_PUBLISH", "true")

    assert client.post(f"/admin/payouts/{payout_id}/execute").status_code == 401
    assert client.post(
        f"/admin/payouts/{payout_id}/execute",
        headers={"Authorization": "Bearer wrong"},
    ).status_code == 403

    calls = []

    async def fake_prepare(address: str, amount_sats: int):
        calls.append((address, amount_sats))
        return "test-bolt11", "ab" * 32

    async def fake_payment(invoice: str, expected_hash: str):
        assert invoice == "test-bolt11"
        assert expected_hash == "ab" * 32
        return LightningPaymentResult(payment_hash="ab" * 32, fees_paid_msats=123, invoice=invoice)

    monkeypatch.setattr(main, "prepare_lnurl_payment", fake_prepare)
    monkeypatch.setattr(main, "pay_nwc_invoice", fake_payment)
    response = client.post(
        f"/admin/payouts/{payout_id}/execute",
        headers={"Authorization": "Bearer admin-test-key"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["payout_status"] == "paid"
    assert body["payment_hash"] == "ab" * 32
    assert ["payment_provider", "nwc"] in body["nostr_event"]["tags"]
    assert ["sandbox", "false"] in body["nostr_event"]["tags"]
    assert calls == [("affiliate@getalby.com", 20000)]

    persisted = client.get(f"/payouts/{payout_id}").json()["payout"]
    assert persisted["status"] == "paid"
    assert persisted["payment_provider"] == "nwc"
    assert persisted["fees_paid_msats"] == 123
    receipt = client.get(f"/payouts/{payout_id}/receipt")
    assert receipt.status_code == 200
    assert 'Non-sandbox payout receipt' in receipt.text
    assert 'data-proof-sandbox="non-sandbox"' in receipt.text
    assert 'data-event-verified="true"' in receipt.text
    assert 'provider_reported_payment' in receipt.text
    assert 'Provider-reported payment evidence is not an independently trustless proof.' in receipt.text
    assert 'No payment preimage is disclosed by this receipt.' in receipt.text
    assert 'Payment provider' in receipt.text
    assert '>nwc<' in receipt.text
    assert body["nostr_event"]["id"] in receipt.text
    assert body["nostr_event"]["sig"] in receipt.text
    assert 'Sandbox payout receipt' not in receipt.text
    assert 'Sandbox Lightning payout proof' not in receipt.text
    attempts = client.get(
        f"/admin/payouts/{payout_id}/attempts",
        headers={"Authorization": "Bearer admin-test-key"},
    ).json()["attempts"]
    assert len(attempts) == 1
    assert attempts[0]["status"] == "SETTLED"
    assert "bolt11_invoice" not in persisted
    monkeypatch.setenv("SANDBOX_PAYOUT_MARK_PAID_ENABLED", "true")
    sandbox_override = client.post(
        f"/payouts/{payout_id}/mark-paid",
        headers={"Authorization": "Bearer admin-test-key"},
        json={"payment_hash": "sandbox-override"},
    )
    assert sandbox_override.status_code == 409

    # Simulate a crash after payment evidence committed but before proof persistence.
    with main.engine().begin() as connection:
        connection.execute(
            main.text("UPDATE payouts SET nostr_event_id=NULL, nostr_event_json=NULL WHERE id=:id"),
            {"id": payout_id},
        )
    proof_retry = client.post(
        f"/admin/payouts/{payout_id}/execute",
        headers={"Authorization": "Bearer admin-test-key"},
    )
    assert proof_retry.status_code == 200
    assert proof_retry.json()["duplicate"] is False
    assert len(calls) == 1

    duplicate = client.post(
        f"/admin/payouts/{payout_id}/execute",
        headers={"Authorization": "Bearer admin-test-key"},
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert len(calls) == 1


def test_ambiguous_nwc_failure_is_not_retried(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/unknown.db")
    monkeypatch.setenv("PAYOUT_ADMIN_KEY", "admin-test-key")
    monkeypatch.setenv("LIGHTNING_PAYOUTS_ENABLED", "true")
    monkeypatch.setenv("LIGHTNING_MAX_PAYOUT_SATS", "50000")
    client = TestClient(main.app)
    payout_id = create_demo_payout(client)

    async def fake_prepare(_address: str, _amount_sats: int):
        return "test-bolt11", "cd" * 32

    async def ambiguous_failure(_invoice: str, _expected_hash: str):
        raise NwcPaymentError("NWC timeout; manual reconciliation required")

    monkeypatch.setattr(main, "prepare_lnurl_payment", fake_prepare)
    monkeypatch.setattr(main, "pay_nwc_invoice", ambiguous_failure)
    first = client.post(
        f"/admin/payouts/{payout_id}/execute",
        headers={"Authorization": "Bearer admin-test-key"},
    )
    assert first.status_code == 502
    payout = client.get(f"/payouts/{payout_id}").json()["payout"]
    assert payout["status"] == "payment_unknown"
    assert payout["payment_hash"] == "cd" * 32
    assert "bolt11_invoice" not in payout
    assert "last_error" not in payout
    attempts = client.get(
        f"/admin/payouts/{payout_id}/attempts",
        headers={"Authorization": "Bearer admin-test-key"},
    ).json()["attempts"]
    assert "manual reconciliation" in attempts[0]["error"]

    flow = client.get(f"/flows/{payout['conversion_id']}").json()
    public_urls = [
        (f"/flows/{payout['conversion_id']}", lambda body: body["payout"]),
        (f"/campaigns/{flow['campaign']['id']}/summary", lambda body: body["payouts"][0]),
        (f"/affiliates/{flow['affiliate_pubkey']}/summary", lambda body: body["payouts"][0]),
        (f"/payouts/{payout_id}", lambda body: body["payout"]),
    ]
    private_fields = {"bolt11_invoice", "lightning_address", "last_error", "processing_started_at", "reserved_sats"}
    for url, payout_selector in public_urls:
        public_response = client.get(url)
        assert public_response.status_code == 200, public_response.text
        assert "test-bolt11" not in public_response.text
        assert "affiliate@getalby.com" not in public_response.text
        public_payout = payout_selector(public_response.json())
        assert private_fields.isdisjoint(public_payout)

    second = client.post(
        f"/admin/payouts/{payout_id}/execute",
        headers={"Authorization": "Bearer admin-test-key"},
    )
    assert second.status_code == 409
