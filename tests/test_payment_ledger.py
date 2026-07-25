from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import app.main as main
from app.lightning import LightningPaymentResult, NwcPaymentError


ADMIN = {"Authorization": "Bearer admin-test-key"}
MERCHANT = {"Authorization": "Bearer bumbei-demo-key"}


def configured_client(tmp_path, monkeypatch, *, budget_sats: int = 1_000_000) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/ledger.db")
    monkeypatch.setenv("PAYOUT_ADMIN_KEY", "admin-test-key")
    monkeypatch.setenv("LIGHTNING_PAYOUTS_ENABLED", "true")
    monkeypatch.setenv("LIGHTNING_MAX_PAYOUT_SATS", "50000")
    monkeypatch.setenv("DEFAULT_CAMPAIGN_BUDGET_SATS", str(budget_sats))
    monkeypatch.setenv("MEERAT_FEE_BPS", "1000")
    monkeypatch.setenv("FEE_MIN_SATS", "10")
    monkeypatch.setenv("NOSTR_PUBLISH", "false")
    return TestClient(main.app)


def demo_context(client: TestClient) -> dict:
    response = client.post("/demo")
    assert response.status_code == 200, response.text
    demo = response.json()
    flow = client.get(f"/flows/{demo['conversion']['conversion_id']}").json()
    return {"demo": demo, "flow": flow, "payout_id": flow["payout"]["id"]}


def test_budget_reservation_on_hold_and_balanced_ledger(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch, budget_sats=25_000)
    ctx = demo_context(client)
    campaign_id = ctx["demo"]["campaign"]["campaign_id"]
    payout = client.get(f"/payouts/{ctx['payout_id']}").json()["payout"]

    assert payout["state"] == "PAYABLE"
    assert payout["fee_sats"] == 2_000
    assert payout["reserved_sats"] == 22_000

    budget = client.get(f"/admin/campaigns/{campaign_id}/budget", headers=ADMIN)
    assert budget.status_code == 200, budget.text
    assert budget.json()["budget"]["committed_sats"] == 22_000
    assert budget.json()["budget"]["settled_sats"] == 0

    ledger = client.get(f"/admin/payouts/{ctx['payout_id']}/ledger", headers=ADMIN).json()
    assert sum(row["amount_sats"] for row in ledger["entries"] if row["direction"] == "debit") == 22_000
    assert sum(row["amount_sats"] for row in ledger["entries"] if row["direction"] == "credit") == 22_000

    ref_code = ctx["demo"]["enrollment"]["ref_code"]
    click_id = client.post("/clicks/simulate", json={"ref_code": ref_code}).json()["click_id"]
    conversion = client.post(
        "/conversions",
        json={"order_id": "budget-overflow", "click_id": click_id, "order_total": 100, "currency": "USD", "sats_per_usd": 2500},
    )
    assert conversion.status_code == 200, conversion.text
    overflow_flow = client.get(f"/flows/{conversion.json()['conversion_id']}").json()
    overflow_payout_id = overflow_flow["payout"]["id"]
    assert overflow_flow["payout"]["state"] == "ON_HOLD"
    assert overflow_flow["payout"]["reserved_sats"] == 0
    assert client.get(f"/campaigns/{campaign_id}").json()["status"] == "paused"
    assert client.get(f"/admin/campaigns/{campaign_id}/budget", headers=ADMIN).json()["budget"]["committed_sats"] == 22_000

    assert client.put(f"/admin/campaigns/{campaign_id}/budget", json={"budget_sats": 50_000}).status_code == 401
    topped_up = client.put(f"/admin/campaigns/{campaign_id}/budget", headers=ADMIN, json={"budget_sats": 50_000})
    assert topped_up.status_code == 200, topped_up.text
    released = client.post(f"/admin/payouts/{overflow_payout_id}/release-hold", headers=ADMIN)
    assert released.status_code == 200, released.text
    assert released.json()["payout_state"] == "PAYABLE"
    assert client.get(f"/admin/campaigns/{campaign_id}/budget", headers=ADMIN).json()["budget"]["committed_sats"] == 44_000


def test_successful_attempt_is_idempotent_and_moves_budget(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    ctx = demo_context(client)
    payout_id = ctx["payout_id"]
    calls = []

    async def fake_prepare(address: str, amount_sats: int):
        calls.append((address, amount_sats))
        return "invoice", "ab" * 32

    async def fake_pay(invoice: str, expected_hash: str):
        return LightningPaymentResult(payment_hash=expected_hash, fees_paid_msats=21, invoice=invoice)

    monkeypatch.setattr(main, "prepare_lnurl_payment", fake_prepare)
    monkeypatch.setattr(main, "pay_nwc_invoice", fake_pay)

    paid = client.post(f"/admin/payouts/{payout_id}/execute", headers=ADMIN)
    assert paid.status_code == 200, paid.text
    assert paid.json()["payout_state"] == "PUBLISHED"
    assert ["fee_sats", "2000"] in paid.json()["nostr_event"]["tags"]
    assert ["rail", "nwc"] in paid.json()["nostr_event"]["tags"]

    attempts = client.get(f"/admin/payouts/{payout_id}/attempts", headers=ADMIN).json()["attempts"]
    assert len(attempts) == 1
    assert attempts[0]["kind"] == "commission"
    assert attempts[0]["status"] == "SETTLED"
    assert attempts[0]["attempt_number"] == 1
    assert attempts[0]["idempotency_key"]
    assert "preimage" not in attempts[0]

    duplicate = client.post(f"/admin/payouts/{payout_id}/execute", headers=ADMIN)
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert len(client.get(f"/admin/payouts/{payout_id}/attempts", headers=ADMIN).json()["attempts"]) == 1
    assert len(calls) == 1

    campaign_id = ctx["demo"]["campaign"]["campaign_id"]
    budget = client.get(f"/admin/campaigns/{campaign_id}/budget", headers=ADMIN).json()["budget"]
    assert budget["committed_sats"] == 2_000
    assert budget["settled_sats"] == 20_000


def test_ambiguous_attempt_requires_reconciliation_before_retry(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    ctx = demo_context(client)
    payout_id = ctx["payout_id"]

    async def fake_prepare(_address: str, _amount_sats: int):
        return "invoice", "cd" * 32

    async def ambiguous(_invoice: str, _expected_hash: str):
        raise NwcPaymentError("timeout; manual reconciliation required")

    monkeypatch.setattr(main, "prepare_lnurl_payment", fake_prepare)
    monkeypatch.setattr(main, "pay_nwc_invoice", ambiguous)
    failed = client.post(f"/admin/payouts/{payout_id}/execute", headers=ADMIN)
    assert failed.status_code == 502

    payout = client.get(f"/payouts/{payout_id}").json()["payout"]
    assert payout["state"] == "PAYING"
    attempts = client.get(f"/admin/payouts/{payout_id}/attempts", headers=ADMIN).json()["attempts"]
    assert len(attempts) == 1
    assert attempts[0]["status"] == "UNKNOWN"

    assert client.post(f"/admin/payouts/{payout_id}/execute", headers=ADMIN).status_code == 409

    stale = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    with main.engine().begin() as connection:
        connection.execute(main.text("UPDATE payment_attempts SET updated_at=:stale WHERE id=:id"), {"stale": stale, "id": attempts[0]["id"]})
    recovery = client.get("/admin/payment-attempts/recovery?older_than_seconds=60", headers=ADMIN)
    assert recovery.status_code == 200
    assert [row["id"] for row in recovery.json()["attempts"]] == [attempts[0]["id"]]

    reconciled = client.post(
        f"/admin/payment-attempts/{attempts[0]['id']}/reconcile",
        headers=ADMIN,
        json={"outcome": "FAILED", "error": "wallet confirmed unpaid"},
    )
    assert reconciled.status_code == 200, reconciled.text
    assert reconciled.json()["payout_state"] == "FAILED"

    async def paid(_invoice: str, expected_hash: str):
        return LightningPaymentResult(payment_hash=expected_hash, fees_paid_msats=0, invoice="fresh")

    monkeypatch.setattr(main, "pay_nwc_invoice", paid)
    retry = client.post(f"/admin/payouts/{payout_id}/execute", headers=ADMIN)
    assert retry.status_code == 200, retry.text
    attempts = client.get(f"/admin/payouts/{payout_id}/attempts", headers=ADMIN).json()["attempts"]
    assert [row["attempt_number"] for row in attempts] == [1, 2]
    assert len({row["idempotency_key"] for row in attempts}) == 2


def test_unknown_attempt_can_be_reconciled_as_settled_without_repaying(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    ctx = demo_context(client)
    payout_id = ctx["payout_id"]

    async def fake_prepare(_address: str, _amount_sats: int):
        return "invoice", "ef" * 32

    async def ambiguous(_invoice: str, _expected_hash: str):
        raise NwcPaymentError("wallet response lost")

    monkeypatch.setattr(main, "prepare_lnurl_payment", fake_prepare)
    monkeypatch.setattr(main, "pay_nwc_invoice", ambiguous)
    assert client.post(f"/admin/payouts/{payout_id}/execute", headers=ADMIN).status_code == 502
    attempt = client.get(f"/admin/payouts/{payout_id}/attempts", headers=ADMIN).json()["attempts"][0]

    reconciled = client.post(
        f"/admin/payment-attempts/{attempt['id']}/reconcile",
        headers=ADMIN,
        json={"outcome": "SETTLED", "payment_hash": "ef" * 32, "routing_fee_sats": 1},
    )
    assert reconciled.status_code == 200, reconciled.text
    assert reconciled.json()["payout_state"] == "PUBLISHED"
    assert reconciled.json()["nostr_event"]["kind"] == 2802
    attempts = client.get(f"/admin/payouts/{payout_id}/attempts", headers=ADMIN).json()["attempts"]
    assert len(attempts) == 1
    assert attempts[0]["status"] == "SETTLED"
    assert client.post(f"/admin/payouts/{payout_id}/execute", headers=ADMIN).json()["duplicate"] is True


def test_reversal_before_payment_releases_budget(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    ctx = demo_context(client)
    campaign_id = ctx["demo"]["campaign"]["campaign_id"]
    conversion_id = ctx["demo"]["conversion"]["conversion_id"]
    before = client.get(f"/admin/campaigns/{campaign_id}/budget", headers=ADMIN).json()["budget"]
    assert before["committed_sats"] == 22_000

    reversed_response = client.post(
        f"/conversions/{conversion_id}/reverse",
        headers=MERCHANT,
        json={"reason": "refund", "refund_sats": 250_000},
    )
    assert reversed_response.status_code == 200, reversed_response.text
    payout = client.get(f"/payouts/{ctx['payout_id']}").json()["payout"]
    assert payout["state"] == "CANCELLED"
    assert payout["reserved_sats"] == 0
    after = client.get(f"/admin/campaigns/{campaign_id}/budget", headers=ADMIN).json()["budget"]
    assert after["committed_sats"] == 0
    ledger = client.get(f"/admin/payouts/{ctx['payout_id']}/ledger", headers=ADMIN).json()["entries"]
    assert sum(row["amount_sats"] for row in ledger if row["direction"] == "debit") == sum(
        row["amount_sats"] for row in ledger if row["direction"] == "credit"
    )
