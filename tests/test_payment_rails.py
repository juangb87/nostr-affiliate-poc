import asyncio

import pytest

from app.lightning import LightningPaymentError
from app.payment_rails import (
    BlinkAdapter,
    FakePaymentRail,
    PaymentResult,
    PaymentStatus,
    build_payment_rail,
)


def run(awaitable):
    return asyncio.run(awaitable)


def test_fake_rail_is_deterministic_and_idempotent():
    rail = FakePaymentRail(balance_sats=100_000)

    first = run(rail.pay_to_lightning_address("affiliate@example.com", 21_000, "reward", "idem-1"))
    duplicate = run(rail.pay_to_lightning_address("affiliate@example.com", 21_000, "reward", "idem-1"))

    assert first.status == PaymentStatus.SUCCESS
    assert first.payment_hash and len(first.payment_hash) == 64
    assert duplicate == first
    assert rail.payment_calls == 1
    assert run(rail.get_balance()) == 79_000
    assert run(rail.lookup_payment("idem-1")) == first


def test_fake_rail_supports_pending_failure_and_safe_retries():
    rail = FakePaymentRail(
        outcomes=[
            PaymentResult(status=PaymentStatus.PENDING, provider_reference="fake-pending"),
            PaymentResult(status=PaymentStatus.FAILURE, error="insufficient balance", retryable=False),
        ]
    )

    pending = run(rail.pay_to_lightning_address("a@example.com", 100, "one", "idem-a"))
    assert pending.status == PaymentStatus.PENDING
    assert run(rail.lookup_payment("idem-a")) == pending
    assert run(rail.pay_to_lightning_address("a@example.com", 100, "one", "idem-a")) == pending
    assert rail.payment_calls == 1

    failed = run(rail.pay_invoice("lnbc-test", "idem-b"))
    assert failed.status == PaymentStatus.FAILURE
    assert failed.retryable is False
    assert rail.payment_calls == 2


def test_blink_balance_selects_configured_btc_wallet_without_exposing_key():
    calls = []

    def transport(query, variables, *, endpoint, api_key):
        calls.append((query, variables, endpoint, api_key))
        return {
            "data": {
                "me": {
                    "defaultAccount": {
                        "wallets": [
                            {"id": "usd-wallet", "walletCurrency": "USD", "balance": 999},
                            {"id": "btc-wallet", "walletCurrency": "BTC", "balance": 42_000},
                        ]
                    }
                }
            }
        }

    rail = BlinkAdapter(api_key="blink-super-secret", wallet_id="btc-wallet", transport=transport)
    assert run(rail.get_balance()) == 42_000
    assert calls[0][1] == {}
    assert "blink-super-secret" not in repr(rail)
    assert "blink-super-secret" not in str(rail)


def test_blink_address_payment_parses_success_and_uses_expected_input():
    captured = {}

    def transport(query, variables, **_kwargs):
        captured["query"] = query
        captured["variables"] = variables
        return {
            "data": {
                "lnAddressPaymentSend": {
                    "status": "SUCCESS",
                    "errors": [],
                    "transaction": {
                        "id": "blink-tx-1",
                        "externalId": None,
                        "initiationVia": {"paymentHash": "ab" * 32},
                        "settlementFee": -3,
                        "status": "SUCCESS",
                    },
                }
            }
        }

    rail = BlinkAdapter(api_key="secret", wallet_id="btc-wallet", transport=transport)
    result = run(rail.pay_to_lightning_address("affiliate@example.com", 21, "Meerat reward", "idem-1"))

    assert result.status == PaymentStatus.SUCCESS
    assert result.payment_hash == "ab" * 32
    assert result.fee_paid_sats == 3
    assert result.provider_reference == "blink-tx-1"
    assert captured["variables"]["input"] == {
        "walletId": "btc-wallet",
        "lnAddress": "affiliate@example.com",
        "amount": 21,
    }
    assert "LnAddressPaymentSendInput!" in captured["query"]
    assert "idem-1" not in str(captured["variables"])
    assert "externalId" not in captured["query"]


def test_blink_success_without_payment_hash_remains_pending_for_lookup():
    def transport(_query, _variables, **_kwargs):
        return {
            "data": {
                "lnAddressPaymentSend": {
                    "status": "SUCCESS",
                    "errors": [],
                    "transaction": {"id": "blink-tx-pending", "settlementFee": 1},
                }
            }
        }

    rail = BlinkAdapter(api_key="secret", wallet_id="btc-wallet", transport=transport)
    result = run(rail.pay_to_lightning_address("affiliate@example.com", 21, "reward", "idem-pending"))
    assert result.status == PaymentStatus.PENDING
    assert result.provider_reference == "blink-tx-pending"
    assert result.payment_hash is None


def test_blink_graphql_error_is_classified_without_leaking_credentials():
    def transport(_query, _variables, **_kwargs):
        return {
            "data": {
                "lnAddressPaymentSend": {
                    "status": "FAILURE",
                    "transaction": None,
                    "errors": [{"code": "INSUFFICIENT_BALANCE", "message": "balance too low"}],
                }
            }
        }

    rail = BlinkAdapter(api_key="do-not-leak", wallet_id="btc-wallet", transport=transport)
    result = run(rail.pay_to_lightning_address("affiliate@example.com", 50_000, "reward", "idem-fail"))
    assert result.status == PaymentStatus.FAILURE
    assert result.retryable is False
    assert result.error_code == "INSUFFICIENT_BALANCE"
    assert "do-not-leak" not in (result.error or "")


def test_blink_lookup_uses_transaction_id_and_normalizes_settlement():
    captured = {}

    def transport(query, variables, **_kwargs):
        captured["query"] = query
        captured["variables"] = variables
        return {
            "data": {
                "me": {
                    "defaultAccount": {
                        "walletById": {
                            "transactionById": {
                                "id": "blink-tx-lookup",
                                "externalId": None,
                                "status": "SUCCESS",
                                "settlementFee": -2,
                                "initiationVia": {"paymentHash": "ef" * 32},
                            }
                        }
                    }
                }
            }
        }

    rail = BlinkAdapter(api_key="secret", wallet_id="btc-wallet", transport=transport)
    result = run(rail.lookup_payment("blink-tx-lookup"))
    assert result.status == PaymentStatus.SUCCESS
    assert result.payment_hash == "ef" * 32
    assert result.fee_paid_sats == 2
    assert captured["variables"] == {"walletId": "btc-wallet", "transactionId": "blink-tx-lookup"}
    assert "transactionById" in captured["query"]


def test_registry_requires_explicit_fake_and_blink_configuration(monkeypatch):
    monkeypatch.setenv("PAYMENT_RAIL", "fake")
    monkeypatch.delenv("ALLOW_FAKE_PAYMENT_RAIL", raising=False)
    with pytest.raises(RuntimeError, match="disabled"):
        build_payment_rail()

    monkeypatch.setenv("ALLOW_FAKE_PAYMENT_RAIL", "true")
    assert isinstance(build_payment_rail(), FakePaymentRail)

    monkeypatch.setenv("PAYMENT_RAIL", "blink")
    monkeypatch.delenv("BLINK_API_KEY", raising=False)
    monkeypatch.delenv("BLINK_WALLET_ID", raising=False)
    with pytest.raises(RuntimeError, match="BLINK_API_KEY"):
        build_payment_rail()


def test_nwc_records_prepared_evidence_before_wallet_and_aborts_if_recorder_fails():
    calls = []

    async def prepare(_address, _amount):
        calls.append("prepare")
        return "private-invoice", "ab" * 32

    async def pay(_invoice, _payment_hash):
        calls.append("pay")
        raise AssertionError("wallet must not be called")

    async def reject(_invoice, _payment_hash):
        calls.append("record")
        raise LightningPaymentError("attempt ownership was lost")

    from app.payment_rails import NwcPaymentRail

    rail = NwcPaymentRail(prepare=prepare, pay=pay)
    rail.set_prepared_evidence_recorder(reject)
    result = run(rail.pay_to_lightning_address("a@example.com", 10, "reward", "idem"))
    assert calls == ["prepare", "record"]
    assert result.status == PaymentStatus.FAILURE


def test_nwc_definitive_preflight_failure_is_not_ambiguous():
    calls = []

    async def prepare(_address, _amount):
        return "private-invoice", "ab" * 32

    async def fail_before_send(_invoice, _payment_hash):
        calls.append("pay")
        raise LightningPaymentError("NWC_CONNECTION_URI is not configured")

    from app.payment_rails import NwcPaymentRail

    rail = NwcPaymentRail(prepare=prepare, pay=fail_before_send)
    result = run(rail.pay_to_lightning_address("a@example.com", 10, "reward", "idem"))
    assert calls == ["pay"]
    assert result.status == PaymentStatus.FAILURE
    assert result.retryable is False


def test_blink_rejects_invalid_success_payment_hash():
    def transport(_query, _variables, **_kwargs):
        return {"data": {"lnAddressPaymentSend": {
            "status": "SUCCESS", "errors": [],
            "transaction": {"id": "tx", "status": "SUCCESS", "initiationVia": {"paymentHash": "not-hex"}},
        }}}

    rail = BlinkAdapter(api_key="secret", wallet_id="btc-wallet", transport=transport)
    result = run(rail.pay_to_lightning_address("a@example.com", 10, "reward", "idem"))
    assert result.status == PaymentStatus.PENDING
    assert result.payment_hash is None
