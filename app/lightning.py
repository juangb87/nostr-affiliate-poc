from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import socket
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from bolt11 import Bolt11Exception, decode
from nostr_sdk import (
    NostrWalletConnectOptions,
    NostrWalletConnectUri,
    Nwc,
    PayInvoiceRequest,
)


class LightningPaymentError(RuntimeError):
    """Safe, non-secret-bearing payout failure."""

    payment_may_have_succeeded = False


class NwcPaymentError(LightningPaymentError):
    """An NWC failure where retrying could duplicate a completed payment."""

    payment_may_have_succeeded = True


@dataclass(frozen=True)
class LightningPaymentResult:
    payment_hash: str
    fees_paid_msats: int | None
    invoice: str


JsonGetter = Callable[[str], dict[str, Any]]


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _require_public_https_url(value: str, label: str) -> str:
    try:
        parsed = urlparse(value)
    except ValueError as exc:
        raise LightningPaymentError(f"invalid {label} URL") from exc
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise LightningPaymentError(f"{label} must be a public HTTPS URL")
    if parsed.port not in (None, 443):
        raise LightningPaymentError(f"{label} must use HTTPS port 443")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)}
    except (OSError, UnicodeError) as exc:
        raise LightningPaymentError(f"could not resolve {label} host") from exc
    if not addresses:
        raise LightningPaymentError(f"could not resolve {label} host")
    for raw in addresses:
        ip = ipaddress.ip_address(raw)
        if not ip.is_global:
            raise LightningPaymentError(f"{label} host is not public")
    return value


def _http_get_json(url: str) -> dict[str, Any]:
    _require_public_https_url(url, "LNURL")
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "Bumbei-Payouts/1.0"})
    try:
        response = build_opener(_NoRedirects()).open(request, timeout=10)
        raw = response.read(65_537)
        if len(raw) > 65_536:
            raise LightningPaymentError("LNURL response is too large")
        if getattr(response, "status", 200) != 200:
            raise LightningPaymentError("LNURL endpoint returned a non-200 status")
        data = json.loads(raw.decode("utf-8"))
    except LightningPaymentError:
        raise
    except (HTTPError, URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LightningPaymentError("LNURL endpoint request failed") from exc
    if not isinstance(data, dict):
        raise LightningPaymentError("LNURL endpoint returned invalid JSON")
    return data


def lightning_address_url(address: str) -> str:
    value = address.strip()
    if value.count("@") != 1:
        raise LightningPaymentError("invalid Lightning Address")
    name, domain = value.rsplit("@", 1)
    if not name or not domain or any(ch in value for ch in "/?#\\"):
        raise LightningPaymentError("invalid Lightning Address")
    try:
        host = domain.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise LightningPaymentError("invalid Lightning Address domain") from exc
    if len(name) > 128 or len(host) > 253 or "." not in host:
        raise LightningPaymentError("invalid Lightning Address")
    return f"https://{host}/.well-known/lnurlp/{name}"


def validate_bolt11_invoice(invoice: str, expected_sats: int) -> str:
    try:
        decoded = decode(invoice, strict=False)
        decoded.validate(strict=False)
    except (Bolt11Exception, ValueError, TypeError) as exc:
        raise LightningPaymentError("LNURL returned an invalid BOLT11 invoice") from exc
    if not decoded.is_mainnet():
        raise LightningPaymentError("BOLT11 invoice is not Bitcoin mainnet")
    expected_msats = expected_sats * 1000
    if decoded.amount_msat is None or int(decoded.amount_msat) != expected_msats:
        raise LightningPaymentError("BOLT11 invoice amount does not match payout")
    if decoded.has_expired():
        raise LightningPaymentError("BOLT11 invoice has expired")
    payment_hash = decoded.payment_hash.lower()
    if len(payment_hash) != 64 or any(ch not in "0123456789abcdef" for ch in payment_hash):
        raise LightningPaymentError("BOLT11 invoice has an invalid payment hash")
    return payment_hash


def request_lnurl_invoice(address: str, amount_sats: int, get_json: JsonGetter = _http_get_json) -> tuple[str, str]:
    if amount_sats <= 0:
        raise LightningPaymentError("payout amount must be positive")
    descriptor_url = lightning_address_url(address)
    _require_public_https_url(descriptor_url, "Lightning Address")
    descriptor = get_json(descriptor_url)
    if str(descriptor.get("status", "")).upper() == "ERROR":
        raise LightningPaymentError("Lightning Address rejected the request")
    if descriptor.get("tag") != "payRequest":
        raise LightningPaymentError("Lightning Address did not return an LNURL-pay request")
    amount_msats = amount_sats * 1000
    try:
        minimum = int(descriptor["minSendable"])
        maximum = int(descriptor["maxSendable"])
    except (KeyError, TypeError, ValueError) as exc:
        raise LightningPaymentError("LNURL-pay limits are invalid") from exc
    if not minimum <= amount_msats <= maximum:
        raise LightningPaymentError("payout amount is outside LNURL-pay limits")
    callback = str(descriptor.get("callback", ""))
    _require_public_https_url(callback, "LNURL callback")
    parsed = urlparse(callback)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["amount"] = str(amount_msats)
    callback_url = urlunparse(parsed._replace(query=urlencode(query)))
    response = get_json(callback_url)
    if str(response.get("status", "")).upper() == "ERROR":
        raise LightningPaymentError("LNURL callback rejected the request")
    invoice = str(response.get("pr", "")).strip()
    payment_hash = validate_bolt11_invoice(invoice, amount_sats)
    return invoice, payment_hash


async def pay_nwc_invoice(invoice: str, expected_payment_hash: str) -> LightningPaymentResult:
    connection_uri = os.getenv("NWC_CONNECTION_URI", "").strip()
    if not connection_uri:
        raise LightningPaymentError("NWC connection is not configured")
    try:
        uri = NostrWalletConnectUri.parse(connection_uri)
        timeout_seconds = max(5, min(int(os.getenv("NWC_TIMEOUT_SECONDS", "30")), 60))
        options = NostrWalletConnectOptions().timeout(timedelta(seconds=timeout_seconds))
        wallet = Nwc.with_opts(uri, options)
        paid = await wallet.pay_invoice(PayInvoiceRequest(id=None, invoice=invoice, amount=None))
        preimage = bytes.fromhex(paid.preimage)
    except LightningPaymentError:
        raise
    except Exception as exc:
        raise NwcPaymentError("NWC wallet payment failed; manual reconciliation required") from exc
    if len(preimage) != 32:
        raise NwcPaymentError("NWC returned an invalid payment preimage; manual reconciliation required")
    actual_hash = hashlib.sha256(preimage).hexdigest()
    if not hmac.compare_digest(actual_hash, expected_payment_hash):
        raise NwcPaymentError("NWC payment could not be matched to the invoice; manual reconciliation required")
    return LightningPaymentResult(
        payment_hash=actual_hash,
        fees_paid_msats=paid.fees_paid,
        invoice=invoice,
    )


async def prepare_lnurl_payment(address: str, amount_sats: int) -> tuple[str, str]:
    return await asyncio.to_thread(request_lnurl_invoice, address, amount_sats)


async def execute_lnurl_nwc_payment(address: str, amount_sats: int) -> LightningPaymentResult:
    invoice, payment_hash = await prepare_lnurl_payment(address, amount_sats)
    return await pay_nwc_invoice(invoice, payment_hash)
