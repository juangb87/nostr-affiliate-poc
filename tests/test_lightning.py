import asyncio
import hashlib
import time
from types import SimpleNamespace

import pytest
from bolt11 import Bolt11, MilliSatoshi, Tag, TagChar, Tags, encode
from nostr_sdk import Method, TransactionState

import app.lightning as lightning
from app.lightning import (
    LightningPaymentError,
    lightning_address_url,
    request_lnurl_invoice,
    validate_bolt11_invoice,
    validate_lightning_address,
)



PRIVATE_KEY = "11" * 32
PAYMENT_SECRET = "22" * 32


def make_invoice(amount_sats: int = 200, *, currency: str = "bc", timestamp: int | None = None) -> tuple[str, str]:
    preimage = bytes.fromhex("33" * 32)
    payment_hash = hashlib.sha256(preimage).hexdigest()
    invoice = Bolt11(
        currency=currency,
        date=timestamp or int(time.time()),
        amount_msat=MilliSatoshi(amount_sats * 1000),
        tags=Tags(
            [
                Tag(TagChar.payment_hash, payment_hash),
                Tag(TagChar.payment_secret, PAYMENT_SECRET),
                Tag(TagChar.description, "Bumbei affiliate payout"),
                Tag(TagChar.expire_time, 3600),
            ]
        ),
    )
    return encode(invoice, private_key=PRIVATE_KEY), payment_hash


def test_lightning_address_url():
    assert lightning_address_url("juangb87@cash.app") == "https://cash.app/.well-known/lnurlp/juangb87"
    for value in ("missing-at", "x@localhost", "x@example.com/path", "x@@example.com"):
        with pytest.raises(LightningPaymentError):
            lightning_address_url(value)


def test_validate_lightning_address_requires_live_lnurl_pay_descriptor():
    valid = {
        "tag": "payRequest",
        "callback": "https://api.cash.app/lnurl/payreq/example",
        "minSendable": 1_000,
        "maxSendable": 1_000_000,
    }
    assert validate_lightning_address("juangb87@cash.app", lambda _url: valid) == valid

    with pytest.raises(LightningPaymentError, match="rejected"):
        validate_lightning_address(
            "juang87@cash.app",
            lambda _url: {"status": "ERROR", "reason": "Error generating LUD06"},
        )
    with pytest.raises(LightningPaymentError, match="LNURL-pay"):
        validate_lightning_address("user@example.com", lambda _url: {**valid, "tag": "withdrawRequest"})
    with pytest.raises(LightningPaymentError, match="limits"):
        validate_lightning_address("user@example.com", lambda _url: {**valid, "minSendable": 2_000_000})
    with pytest.raises(LightningPaymentError, match="public HTTPS"):
        validate_lightning_address("user@example.com", lambda _url: {**valid, "callback": "http://localhost/pay"})


def test_validate_bolt11_requires_exact_mainnet_unexpired_invoice():
    invoice, payment_hash = make_invoice()
    assert validate_bolt11_invoice(invoice, 200) == payment_hash

    with pytest.raises(LightningPaymentError, match="amount"):
        validate_bolt11_invoice(invoice, 201)

    testnet, _ = make_invoice(currency="tb")
    with pytest.raises(LightningPaymentError, match="mainnet"):
        validate_bolt11_invoice(testnet, 200)

    expired, _ = make_invoice(timestamp=int(time.time()) - 7200)
    with pytest.raises(LightningPaymentError, match="expired"):
        validate_bolt11_invoice(expired, 200)

    expires_too_soon, _ = make_invoice(timestamp=int(time.time()) - 3550)
    with pytest.raises(LightningPaymentError, match="too soon"):
        validate_bolt11_invoice(expires_too_soon, 200)

    with pytest.raises(LightningPaymentError, match="length"):
        validate_bolt11_invoice("lnbc" + "a" * 5000, 200)


def test_request_lnurl_invoice_enforces_limits_and_callback_amount():
    invoice, payment_hash = make_invoice()
    calls: list[str] = []

    def fake_get(url: str):
        calls.append(url)
        if "/.well-known/lnurlp/" in url:
            return {
                "tag": "payRequest",
                "callback": "https://example.com/lnurl/callback?token=abc",
                "minSendable": 1000,
                "maxSendable": 1_000_000,
            }
        assert "amount=200000" in url
        assert "token=abc" in url
        return {"pr": invoice}

    assert request_lnurl_invoice("affiliate@example.com", 200, fake_get) == (invoice, payment_hash)
    assert len(calls) == 2

    def too_small(_url: str):
        return {
            "tag": "payRequest",
            "callback": "https://example.com/callback",
            "minSendable": 300_000,
            "maxSendable": 1_000_000,
        }

    with pytest.raises(LightningPaymentError, match="outside"):
        request_lnurl_invoice("affiliate@example.com", 200, too_small)


def test_nwc_readiness_and_lookup_use_safe_provider_evidence(monkeypatch):
    preimage = "44" * 32
    payment_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()

    class Wallet:
        async def get_info(self):
            return SimpleNamespace(
                alias="Pilot wallet",
                network="mainnet",
                methods=[Method.PAY_INVOICE, Method.LOOKUP_INVOICE, Method.GET_INFO, Method.GET_BALANCE],
                notifications=["payment_sent"],
            )

        async def get_balance(self):
            return 250_000

        async def lookup_invoice(self, request):
            assert request.payment_hash == payment_hash
            assert request.invoice is None
            return SimpleNamespace(
                state=TransactionState.SETTLED,
                payment_hash=payment_hash,
                preimage=preimage,
                fees_paid=1_500,
            )

    monkeypatch.setattr(lightning, "_nwc_client", lambda: Wallet())
    readiness = asyncio.run(lightning.probe_nwc_wallet())
    assert readiness == {
        "connected": True,
        "authenticated": True,
        "capabilities_discovered": True,
        "alias": "Pilot wallet",
        "network": "mainnet",
        "methods": ["get_balance", "get_info", "lookup_invoice", "pay_invoice"],
        "notifications": ["payment_sent"],
        "supports_pay_invoice": True,
        "supports_lookup_invoice": True,
        "balance_accessible": True,
        "has_canary_balance": True,
    }
    assert "secret" not in str(readiness).lower()

    result = asyncio.run(lightning.lookup_nwc_payment(payment_hash))
    assert result.status == "SUCCESS"
    assert result.payment_hash == payment_hash
    assert result.fee_paid_msats == 1_500
    assert result.fee_paid_sats == 2
    assert result.provider_reference == payment_hash
    assert result.preimage is None


def test_nwc_lookup_rejects_mismatched_or_invalid_provider_evidence(monkeypatch):
    expected_hash = "55" * 32

    class Wallet:
        async def lookup_invoice(self, _request):
            return SimpleNamespace(
                state=TransactionState.SETTLED,
                payment_hash="66" * 32,
                preimage=None,
                fees_paid=0,
            )

    monkeypatch.setattr(lightning, "_nwc_client", lambda: Wallet())
    with pytest.raises(lightning.NwcPaymentError, match="match"):
        asyncio.run(lightning.lookup_nwc_payment(expected_hash))
    with pytest.raises(LightningPaymentError, match="payment hash"):
        asyncio.run(lightning.lookup_nwc_payment("not-a-payment-hash"))


def test_nwc_readiness_falls_back_to_standard_info_event_for_wallet_extensions(monkeypatch):
    class Wallet:
        async def get_info(self):
            raise RuntimeError("SDK cannot decode wallet extension method")

        async def get_balance(self):
            return 21_000

    async def capabilities():
        return ["get_balance", "get_budget", "lookup_invoice", "pay_invoice"], ["payment_sent"]

    monkeypatch.setattr(lightning, "_nwc_client", lambda: Wallet())
    monkeypatch.setattr(lightning, "_nwc_info_capabilities", capabilities)
    readiness = asyncio.run(lightning.probe_nwc_wallet())
    assert readiness["connected"] is True
    assert readiness["authenticated"] is True
    assert readiness["capabilities_discovered"] is True
    assert readiness["alias"] is None
    assert readiness["network"] is None
    assert readiness["methods"] == ["get_balance", "get_budget", "lookup_invoice", "pay_invoice"]
    assert readiness["supports_lookup_invoice"] is True
    assert readiness["has_canary_balance"] is True


def test_nwc_public_capability_event_alone_is_not_authenticated_readiness(monkeypatch):
    class Wallet:
        async def get_info(self):
            raise TimeoutError("wallet did not answer")

        async def get_balance(self):
            raise TimeoutError("wallet did not answer")

    async def capabilities():
        return ["get_balance", "lookup_invoice", "pay_invoice"], []

    monkeypatch.setattr(lightning, "_nwc_client", lambda: Wallet())
    monkeypatch.setattr(lightning, "_nwc_info_capabilities", capabilities)
    readiness = asyncio.run(lightning.probe_nwc_wallet())
    assert readiness["capabilities_discovered"] is True
    assert readiness["authenticated"] is False
    assert readiness["connected"] is False
    assert readiness["balance_accessible"] is False
