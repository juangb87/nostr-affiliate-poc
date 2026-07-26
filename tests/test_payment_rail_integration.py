from fastapi.testclient import TestClient
import threading

import app.main as main
from app.payment_rails import FakePaymentRail, PaymentResult, PaymentStatus


ADMIN = {"Authorization": "Bearer admin-test-key"}


def client_and_payout(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/rail-integration.db")
    monkeypatch.setenv("PAYOUT_ADMIN_KEY", "admin-test-key")
    monkeypatch.setenv("LIGHTNING_PAYOUTS_ENABLED", "true")
    monkeypatch.setenv("LIGHTNING_MAX_PAYOUT_SATS", "50000")
    monkeypatch.setenv("NOSTR_PUBLISH", "false")
    client = TestClient(main.app)
    demo = client.post("/demo").json()
    payout_id = client.get(f"/flows/{demo['conversion']['conversion_id']}").json()["payout"]["id"]
    return client, payout_id


def attempts(client, payout_id):
    return client.get(f"/admin/payouts/{payout_id}/attempts", headers=ADMIN).json()["attempts"]


def test_worker_executes_success_through_fake_rail_without_network(tmp_path, monkeypatch):
    client, payout_id = client_and_payout(tmp_path, monkeypatch)
    rail = FakePaymentRail(balance_sats=100_000)
    monkeypatch.setattr(main, "configured_payment_rail", lambda: rail)
    assert client.get("/admin/payment-rail/balance").status_code == 401
    balance = client.get("/admin/payment-rail/balance", headers=ADMIN)
    assert balance.status_code == 200
    assert balance.json() == {"rail": "fake", "balance_sats": 100_000}

    response = client.post(f"/admin/payouts/{payout_id}/execute", headers=ADMIN)
    assert response.status_code == 200, response.text
    assert response.json()["payout_state"] == "PUBLISHED"
    assert ["rail", "fake"] in response.json()["nostr_event"]["tags"]
    assert rail.payment_calls == 1

    attempt = attempts(client, payout_id)[0]
    assert attempt["rail"] == "fake"
    assert attempt["status"] == "SETTLED"
    assert attempt["provider_reference"].startswith("fake:")
    assert attempt["retryable"] == 0
    assert "preimage" not in attempt

    duplicate = client.post(f"/admin/payouts/{payout_id}/execute", headers=ADMIN)
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert rail.payment_calls == 1


def test_worker_maps_pending_to_unknown_and_blocks_retry(tmp_path, monkeypatch):
    client, payout_id = client_and_payout(tmp_path, monkeypatch)
    rail = FakePaymentRail(outcomes=[PaymentResult(
        status=PaymentStatus.PENDING,
        provider_reference="fake-pending-reference",
    )])
    monkeypatch.setattr(main, "configured_payment_rail", lambda: rail)

    response = client.post(f"/admin/payouts/{payout_id}/execute", headers=ADMIN)
    assert response.status_code == 202, response.text
    payout = client.get(f"/payouts/{payout_id}").json()["payout"]
    assert payout["state"] == "PAYING"
    assert payout["status"] == "payment_unknown"
    attempt = attempts(client, payout_id)[0]
    assert attempt["status"] == "UNKNOWN"
    assert attempt["provider_reference"] == "fake-pending-reference"
    assert attempt["retryable"] == 0

    retry = client.post(f"/admin/payouts/{payout_id}/execute", headers=ADMIN)
    assert retry.status_code == 409
    assert rail.payment_calls == 1

    rail.set_lookup_result("fake-pending-reference", PaymentResult(
        status=PaymentStatus.SUCCESS,
        payment_hash="cd" * 32,
        fee_paid_sats=1,
        provider_reference="fake-pending-reference",
    ))
    refreshed = client.post(f"/admin/payment-attempts/{attempt['id']}/refresh", headers=ADMIN)
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["payout_state"] == "PUBLISHED"
    assert rail.payment_calls == 1
    assert attempts(client, payout_id)[0]["status"] == "SETTLED"


def test_worker_persists_normalized_failure_metadata(tmp_path, monkeypatch):
    client, payout_id = client_and_payout(tmp_path, monkeypatch)
    rail = FakePaymentRail(outcomes=[PaymentResult(
        status=PaymentStatus.FAILURE,
        error="temporary provider outage",
        error_code="TEMPORARY_UNAVAILABLE",
        retryable=True,
        provider_reference="fake-failed-reference",
    )])
    monkeypatch.setattr(main, "configured_payment_rail", lambda: rail)

    response = client.post(f"/admin/payouts/{payout_id}/execute", headers=ADMIN)
    assert response.status_code == 502
    payout = client.get(f"/payouts/{payout_id}").json()["payout"]
    assert payout["state"] == "FAILED"
    attempt = attempts(client, payout_id)[0]
    assert attempt["status"] == "FAILED"
    assert attempt["error_code"] == "TEMPORARY_UNAVAILABLE"
    assert attempt["retryable"] == 1
    assert attempt["provider_reference"] == "fake-failed-reference"


def test_fake_rail_must_be_explicitly_enabled(tmp_path, monkeypatch):
    client, payout_id = client_and_payout(tmp_path, monkeypatch)
    monkeypatch.undo()
    # Reapply only the database/admin settings after restoring patched process state.
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/rail-integration.db")
    monkeypatch.setenv("PAYOUT_ADMIN_KEY", "admin-test-key")
    monkeypatch.setenv("LIGHTNING_PAYOUTS_ENABLED", "true")
    monkeypatch.setenv("PAYMENT_RAIL", "fake")
    monkeypatch.delenv("ALLOW_FAKE_PAYMENT_RAIL", raising=False)

    response = client.post(f"/admin/payouts/{payout_id}/execute", headers=ADMIN)
    assert response.status_code == 503
    assert attempts(client, payout_id) == []


def test_blink_execute_fails_closed_without_mutation(tmp_path, monkeypatch):
    client, payout_id = client_and_payout(tmp_path, monkeypatch)

    class BlinkNeverCalled:
        name = "blink"

        async def pay_to_lightning_address(self, *_args):
            raise AssertionError("Blink send must be fail-closed")

    monkeypatch.setattr(main, "configured_payment_rail", lambda: BlinkNeverCalled())
    response = client.post(f"/admin/payouts/{payout_id}/execute", headers=ADMIN)
    assert response.status_code == 503
    assert attempts(client, payout_id) == []
    assert client.get(f"/payouts/{payout_id}").json()["payout"]["state"] == "PAYABLE"


def test_refresh_requires_unknown_and_real_provider_reference(tmp_path, monkeypatch):
    client, payout_id = client_and_payout(tmp_path, monkeypatch)
    rail = FakePaymentRail(outcomes=[PaymentResult(status=PaymentStatus.PENDING)])
    monkeypatch.setattr(main, "configured_payment_rail", lambda: rail)
    assert client.post(f"/admin/payouts/{payout_id}/execute", headers=ADMIN).status_code == 202
    attempt = attempts(client, payout_id)[0]
    response = client.post(f"/admin/payment-attempts/{attempt['id']}/refresh", headers=ADMIN)
    assert response.status_code == 409


def test_refresh_rejects_retryable_provider_failure(tmp_path, monkeypatch):
    client, payout_id = client_and_payout(tmp_path, monkeypatch)
    reference = "fake-pending-reference"
    rail = FakePaymentRail(outcomes=[PaymentResult(
        status=PaymentStatus.PENDING,
        provider_reference=reference,
    )])
    monkeypatch.setattr(main, "configured_payment_rail", lambda: rail)
    assert client.post(f"/admin/payouts/{payout_id}/execute", headers=ADMIN).status_code == 202
    attempt = attempts(client, payout_id)[0]

    rail.set_lookup_result(reference, PaymentResult(
        status=PaymentStatus.FAILURE,
        error="temporary provider outage",
        error_code="TEMPORARY_UNAVAILABLE",
        provider_reference=reference,
        retryable=True,
    ))
    response = client.post(f"/admin/payment-attempts/{attempt['id']}/refresh", headers=ADMIN)
    assert response.status_code == 409
    assert attempts(client, payout_id)[0]["status"] == "UNKNOWN"
    payout = client.get(f"/payouts/{payout_id}").json()["payout"]
    assert payout["state"] == "PAYING"
    assert payout["status"] == "payment_unknown"



def test_late_success_cannot_be_manually_failed_or_trigger_second_rail_call(tmp_path, monkeypatch):
    client, payout_id = client_and_payout(tmp_path, monkeypatch)
    entered = threading.Event()
    release = threading.Event()

    class BlockingRail:
        name = "fake"
        payment_calls = 0

        async def pay_to_lightning_address(self, *_args):
            self.payment_calls += 1
            entered.set()
            assert release.wait(5)
            return PaymentResult(status=PaymentStatus.SUCCESS, payment_hash="de" * 32,
                                 provider_reference="provider-tx-late")

    rail = BlockingRail()
    monkeypatch.setattr(main, "configured_payment_rail", lambda: rail)
    result = {}

    def execute():
        response = client.post(f"/admin/payouts/{payout_id}/execute", headers=ADMIN)
        result.update(status=response.status_code, body=response.json())

    worker = threading.Thread(target=execute)
    worker.start()
    assert entered.wait(5)
    attempt = attempts(client, payout_id)[0]
    assert attempt["status"] == "PAYING"
    assert client.post(
        f"/admin/payment-attempts/{attempt['id']}/reconcile", headers=ADMIN,
        json={"outcome": "FAILED", "error": "unsafe operator action"},
    ).status_code == 400
    assert client.post(
        f"/admin/payment-attempts/{attempt['id']}/reconcile", headers=ADMIN,
        json={"outcome": "SETTLED", "payment_hash": "de" * 32, "error": "unsafe while live"},
    ).status_code == 409
    release.set()
    worker.join(5)
    assert not worker.is_alive()
    assert result["status"] == 200
    duplicate = client.post(f"/admin/payouts/{payout_id}/execute", headers=ADMIN)
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert rail.payment_calls == 1
