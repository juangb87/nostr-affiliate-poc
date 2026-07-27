from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import app.main as main
from app.lightning import LightningPaymentError, LightningPaymentResult, NwcPaymentError
from app.payment_rails import PaymentResult, PaymentStatus


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
        json={"order_id": "budget-overflow", "click_id": click_id, "order_total": 100, "currency": "USD"},
    )
    assert conversion.status_code == 200, conversion.text
    overflow_flow = client.get(f"/flows/{conversion.json()['conversion_id']}").json()
    overflow_payout_id = overflow_flow["payout"]["id"]
    assert overflow_flow["payout"]["state"] == "ON_HOLD"
    assert client.get(f"/campaigns/{campaign_id}").json()["status"] == "paused"
    assert client.get(f"/admin/campaigns/{campaign_id}/budget", headers=ADMIN).json()["budget"]["committed_sats"] == 22_000

    assert client.put(f"/admin/campaigns/{campaign_id}/budget", json={"budget_sats": 50_000}).status_code == 401
    topped_up = client.put(f"/admin/campaigns/{campaign_id}/budget", headers=ADMIN, json={"budget_sats": 50_000})
    assert topped_up.status_code == 200, topped_up.text
    released = client.post(f"/admin/payouts/{overflow_payout_id}/release-hold", headers=ADMIN)
    assert released.status_code == 200, released.text
    assert released.json()["payout_state"] == "PAYABLE"
    assert client.get(f"/admin/campaigns/{campaign_id}/budget", headers=ADMIN).json()["budget"]["committed_sats"] == 44_000
    duplicate_release = client.post(f"/admin/payouts/{overflow_payout_id}/release-hold", headers=ADMIN)
    assert duplicate_release.status_code == 200
    assert duplicate_release.json()["duplicate"] is True
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
    assert reconciled.status_code == 400
    assert client.post(f"/admin/payouts/{payout_id}/execute", headers=ADMIN).status_code == 409
    assert len(client.get(f"/admin/payouts/{payout_id}/attempts", headers=ADMIN).json()["attempts"]) == 1


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
        json={"outcome": "SETTLED", "payment_hash": "ef" * 32, "routing_fee_sats": 1, "error": "operator ticket payout-ef"},
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
    after = client.get(f"/admin/campaigns/{campaign_id}/budget", headers=ADMIN).json()["budget"]
    assert after["committed_sats"] == 0
    ledger = client.get(f"/admin/payouts/{ctx['payout_id']}/ledger", headers=ADMIN).json()["entries"]
    assert sum(row["amount_sats"] for row in ledger if row["direction"] == "debit") == sum(
        row["amount_sats"] for row in ledger if row["direction"] == "credit"
    )


def test_reversal_during_unknown_then_failed_cancels_and_blocks_retry(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    ctx = demo_context(client)
    payout_id = ctx["payout_id"]
    conversion_id = ctx["demo"]["conversion"]["conversion_id"]
    campaign_id = ctx["demo"]["campaign"]["campaign_id"]

    async def fake_prepare(_address: str, _amount_sats: int):
        return "invoice", "12" * 32

    async def ambiguous(_invoice: str, _expected_hash: str):
        raise NwcPaymentError("wallet response lost")

    monkeypatch.setattr(main, "prepare_lnurl_payment", fake_prepare)
    monkeypatch.setattr(main, "pay_nwc_invoice", ambiguous)
    assert client.post(f"/admin/payouts/{payout_id}/execute", headers=ADMIN).status_code == 502
    attempt = client.get(f"/admin/payouts/{payout_id}/attempts", headers=ADMIN).json()["attempts"][0]

    reversal = client.post(
        f"/conversions/{conversion_id}/reverse",
        headers=MERCHANT,
        json={"reason": "refund", "refund_sats": 250_000},
    )
    assert reversal.status_code == 200, reversal.text
    assert client.get(f"/payouts/{payout_id}").json()["payout"]["state"] == "CANCEL_PENDING"

    reconciled = main._apply_provider_refresh_result(attempt["id"], PaymentResult(
        status=PaymentStatus.FAILURE,
        error="wallet confirmed unpaid",
        retryable=False,
    ))
    assert reconciled["payout_state"] == "CANCELLED"
    assert client.get(f"/admin/campaigns/{campaign_id}/budget", headers=ADMIN).json()["budget"]["committed_sats"] == 0
    assert client.post(f"/admin/payouts/{payout_id}/execute", headers=ADMIN).status_code == 409


def test_reversal_during_unknown_then_settled_cancels_only_pending_fee(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    ctx = demo_context(client)
    payout_id = ctx["payout_id"]
    conversion_id = ctx["demo"]["conversion"]["conversion_id"]
    campaign_id = ctx["demo"]["campaign"]["campaign_id"]

    async def fake_prepare(_address: str, _amount_sats: int):
        return "invoice", "34" * 32

    async def ambiguous(_invoice: str, _expected_hash: str):
        raise NwcPaymentError("wallet response lost")

    monkeypatch.setattr(main, "prepare_lnurl_payment", fake_prepare)
    monkeypatch.setattr(main, "pay_nwc_invoice", ambiguous)
    assert client.post(f"/admin/payouts/{payout_id}/execute", headers=ADMIN).status_code == 502
    attempt = client.get(f"/admin/payouts/{payout_id}/attempts", headers=ADMIN).json()["attempts"][0]
    assert client.post(
        f"/conversions/{conversion_id}/reverse",
        headers=MERCHANT,
        json={"reason": "refund", "refund_sats": 250_000},
    ).status_code == 200

    reconciled = client.post(
        f"/admin/payment-attempts/{attempt['id']}/reconcile",
        headers=ADMIN,
        json={"outcome": "SETTLED", "payment_hash": "34" * 32, "routing_fee_sats": 1, "error": "operator ticket payout-34"},
    )
    assert reconciled.status_code == 200, reconciled.text
    payout = client.get(f"/payouts/{payout_id}").json()["payout"]
    assert payout["state"] == "PUBLISHED"
    assert payout["fee_state"] == "CANCELLED"
    assert "reserved_sats" not in payout
    budget = client.get(f"/admin/campaigns/{campaign_id}/budget", headers=ADMIN).json()["budget"]
    assert budget["committed_sats"] == 0
    assert budget["settled_sats"] == 20_000


def test_legacy_paid_without_nostr_proof_is_backfilled_as_settled(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    payout_id = demo_context(client)["payout_id"]
    with main.engine().begin() as connection:
        connection.execute(main.text("""
            UPDATE payouts SET status='paid', state='PUBLISHED', nostr_event_id=NULL, nostr_event_json=NULL
            WHERE id=:id
        """), {"id": payout_id})
    main.init_db()
    assert client.get(f"/payouts/{payout_id}").json()["payout"]["state"] == "SETTLED"


def test_legacy_unreserved_payout_is_held_until_budget_is_reserved(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    ctx = demo_context(client)
    payout_id = ctx["payout_id"]
    with main.engine().begin() as connection:
        connection.execute(main.text("DELETE FROM ledger_entries WHERE payout_id=:id"), {"id": payout_id})
        connection.execute(main.text("DELETE FROM campaign_budgets"))
        connection.execute(main.text("UPDATE payouts SET state='PAYABLE', status='pending', reserved_sats=0 WHERE id=:id"), {"id": payout_id})
    main.init_db()
    held = client.get(f"/payouts/{payout_id}").json()["payout"]
    assert held["state"] == "ON_HOLD"
    assert client.post(f"/admin/payouts/{payout_id}/execute", headers=ADMIN).status_code == 409
    released = client.post(f"/admin/payouts/{payout_id}/release-hold", headers=ADMIN)
    assert released.status_code == 200, released.text
    assert released.json()["payout_state"] == "PAYABLE"


def test_reversal_while_provider_call_succeeds_settles_commission_and_releases_fee(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    ctx = demo_context(client)
    payout_id = ctx["payout_id"]
    conversion_id = ctx["demo"]["conversion"]["conversion_id"]
    campaign_id = ctx["demo"]["campaign"]["campaign_id"]

    async def fake_prepare(_address: str, _amount_sats: int):
        return "invoice", "56" * 32

    async def pay_after_reversal(invoice: str, _expected_hash: str):
        reversed_result = main.reverse_conversion(
            conversion_id,
            main.ReversalIn(reason="refund", refund_sats=250_000),
            authorization="Bearer bumbei-demo-key",
        )
        assert reversed_result["ok"] is True
        return LightningPaymentResult(payment_hash="56" * 32, fees_paid_msats=1000, invoice=invoice)

    monkeypatch.setattr(main, "prepare_lnurl_payment", fake_prepare)
    monkeypatch.setattr(main, "pay_nwc_invoice", pay_after_reversal)
    paid = client.post(f"/admin/payouts/{payout_id}/execute", headers=ADMIN)
    assert paid.status_code == 200, paid.text
    payout = client.get(f"/payouts/{payout_id}").json()["payout"]
    assert payout["state"] == "PUBLISHED"
    assert payout["fee_state"] == "CANCELLED"
    budget = client.get(f"/admin/campaigns/{campaign_id}/budget", headers=ADMIN).json()["budget"]
    assert budget["committed_sats"] == 0
    assert budget["settled_sats"] == 20_000


def test_reversal_while_preparing_invoice_definitively_fails_releases_all_budget(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    ctx = demo_context(client)
    payout_id = ctx["payout_id"]
    conversion_id = ctx["demo"]["conversion"]["conversion_id"]
    campaign_id = ctx["demo"]["campaign"]["campaign_id"]

    async def reversed_before_invoice(_address: str, _amount_sats: int):
        main.reverse_conversion(
            conversion_id,
            main.ReversalIn(reason="refund", refund_sats=250_000),
            authorization="Bearer bumbei-demo-key",
        )
        raise LightningPaymentError("invoice preparation cancelled")

    monkeypatch.setattr(main, "prepare_lnurl_payment", reversed_before_invoice)
    failed = client.post(f"/admin/payouts/{payout_id}/execute", headers=ADMIN)
    assert failed.status_code == 502
    payout = client.get(f"/payouts/{payout_id}").json()["payout"]
    assert payout["state"] == "CANCELLED"
    budget = client.get(f"/admin/campaigns/{campaign_id}/budget", headers=ADMIN).json()["budget"]
    assert budget["committed_sats"] == 0
    assert budget["settled_sats"] == 0


def test_manual_reconcile_rejects_paying_failed_and_hash_mismatch(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    payout_id = demo_context(client)["payout_id"]
    with main.engine().begin() as c:
        c.execute(main.text("UPDATE payouts SET state='PAYING', status='processing', payment_provider='nwc' WHERE id=:id"), {"id": payout_id})
        c.execute(main.text("""
            INSERT INTO payment_attempts
            (id,payout_id,kind,rail,idempotency_key,destination,amount_sats,status,attempt_number,created_at,updated_at)
            VALUES ('paying-attempt',:payout,'commission','nwc','paying-idem','a@example.com',10,'PAYING',1,:now,:now)
        """), {"payout": payout_id, "now": main.now()})
    paying = client.post("/admin/payment-attempts/paying-attempt/reconcile", headers=ADMIN,
                         json={"outcome": "SETTLED", "payment_hash": "aa" * 32, "error": "operator ticket 1"})
    assert paying.status_code == 409
    failed = client.post("/admin/payment-attempts/paying-attempt/reconcile", headers=ADMIN,
                         json={"outcome": "FAILED", "error": "operator ticket 1"})
    assert failed.status_code == 400

    with main.engine().begin() as c:
        c.execute(main.text("UPDATE payment_attempts SET status='UNKNOWN', payment_hash=:h WHERE id='paying-attempt'"), {"h": "aa" * 32})
        c.execute(main.text("UPDATE payouts SET status='payment_unknown', payment_hash=:h WHERE id=:id"), {"id": payout_id, "h": "aa" * 32})
    mismatch = client.post("/admin/payment-attempts/paying-attempt/reconcile", headers=ADMIN,
                           json={"outcome": "SETTLED", "payment_hash": "bb" * 32, "error": "operator ticket 2"})
    assert mismatch.status_code == 409


def test_nwc_prepared_evidence_is_private_and_recorded_before_pay(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    payout_id = demo_context(client)["payout_id"]
    observed = {}

    async def prepare(_address, _amount):
        return "private-bolt11", "bc" * 32

    async def pay(invoice, payment_hash):
        with main.engine().connect() as c:
            attempt = dict(c.execute(main.text("SELECT * FROM payment_attempts WHERE payout_id=:id"), {"id": payout_id}).fetchone()._mapping)
            payout = dict(c.execute(main.text("SELECT * FROM payouts WHERE id=:id"), {"id": payout_id}).fetchone()._mapping)
        observed.update(attempt_hash=attempt["payment_hash"], invoice=payout["bolt11_invoice"])
        return LightningPaymentResult(payment_hash=payment_hash, fees_paid_msats=0, invoice=invoice)

    monkeypatch.setattr(main, "prepare_lnurl_payment", prepare)
    monkeypatch.setattr(main, "pay_nwc_invoice", pay)
    response = client.post(f"/admin/payouts/{payout_id}/execute", headers=ADMIN)
    assert response.status_code == 200, response.text
    assert observed == {"attempt_hash": "bc" * 32, "invoice": "private-bolt11"}
    assert "private-bolt11" not in client.get(f"/payouts/{payout_id}").text
