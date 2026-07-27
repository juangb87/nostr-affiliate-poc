import hashlib
import time

import pytest
from bolt11 import Bolt11, MilliSatoshi, Tag, TagChar, Tags, encode

from app.lightning import (
    LightningPaymentError,
    lightning_address_url,
    request_lnurl_invoice,
    validate_bolt11_invoice,
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
