#!/usr/bin/env python3
"""Read-only Blink credential and wallet probe.

Reads credentials from environment, executes only `me.defaultAccount.wallets`, and never
prints the API key. This script intentionally contains no GraphQL mutation.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

QUERY = """
query BumbeiBlinkReadOnlyProbe {
  me {
    defaultAccount {
      wallets { id walletCurrency balance }
    }
  }
}
"""


def endpoint_from_env() -> str:
    endpoint = os.getenv("BLINK_API_URL", "https://api.blink.sv/graphql").strip()
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or parsed.hostname not in {"api.blink.sv", "api.staging.blink.sv"}:
        raise RuntimeError("BLINK_API_URL must be an official Blink HTTPS endpoint")
    return endpoint


def read_wallets(payload: dict[str, Any]) -> list[dict[str, Any]]:
    errors = payload.get("errors") or []
    if errors:
        messages = [str(row.get("message") or "Blink GraphQL error")[:200] for row in errors if isinstance(row, dict)]
        raise RuntimeError("; ".join(messages) or "Blink GraphQL error")
    wallets = ((((payload.get("data") or {}).get("me") or {}).get("defaultAccount") or {}).get("wallets") or [])
    if not isinstance(wallets, list):
        raise RuntimeError("Blink returned an invalid wallet list")
    return [
        {
            "id": str(wallet.get("id") or ""),
            "currency": str(wallet.get("walletCurrency") or ""),
            "balance": int(wallet.get("balance") or 0),
        }
        for wallet in wallets
        if isinstance(wallet, dict)
    ]


def probe(api_key: str, endpoint: str) -> list[dict[str, Any]]:
    request = Request(
        endpoint,
        data=json.dumps({"query": QUERY, "variables": {}}).encode(),
        headers={
            "Content-Type": "application/json",
            "X-API-KEY": api_key,
            "User-Agent": "Bumbei-Blink-ReadOnly-Probe/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            raw = response.read(262_145)
            if response.status != 200:
                raise RuntimeError(f"Blink returned HTTP {response.status}")
    except HTTPError as exc:
        raise RuntimeError(f"Blink returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError("Blink read-only probe could not reach the API") from exc
    if len(raw) > 262_144:
        raise RuntimeError("Blink response is too large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Blink returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Blink returned an invalid response")
    return read_wallets(payload)


def main() -> int:
    api_key = os.getenv("BLINK_API_KEY", "").strip()
    if not api_key:
        print("BLINK_API_KEY is not configured", file=sys.stderr)
        return 2
    try:
        endpoint = endpoint_from_env()
        wallets = probe(api_key, endpoint)
    except RuntimeError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    currencies = sorted({wallet["currency"] for wallet in wallets if wallet.get("currency")})
    print(json.dumps({
        "ok": True,
        "operation": "read_only_wallet_list",
        "endpoint": endpoint,
        "wallet_count": len(wallets),
        "currencies": currencies,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
