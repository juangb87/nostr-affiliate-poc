import json

import pytest

from scripts import blink_readonly_probe as probe


def test_probe_query_is_read_only():
    normalized = probe.QUERY.lower()
    assert "query" in normalized
    assert "wallets" in normalized
    assert "mutation" not in normalized


def test_read_wallets_normalizes_btc_wallet():
    wallets = probe.read_wallets({
        "data": {
            "me": {
                "defaultAccount": {
                    "wallets": [{"id": "wallet-1", "walletCurrency": "BTC", "balance": 1234}]
                }
            }
        }
    })
    assert wallets == [{"id": "wallet-1", "currency": "BTC", "balance": 1234}]


def test_endpoint_rejects_non_blink_host(monkeypatch):
    monkeypatch.setenv("BLINK_API_URL", "https://example.com/graphql")
    with pytest.raises(RuntimeError, match="official Blink"):
        probe.endpoint_from_env()


def test_main_prints_only_sanitized_wallet_summary(monkeypatch, capsys):
    monkeypatch.setenv("BLINK_API_KEY", "secret-test-key")
    monkeypatch.setattr(
        probe,
        "probe",
        lambda api_key, endpoint: [
            {"id": "sensitive-wallet-id", "currency": "BTC", "balance": 1234},
            {"id": "another-wallet-id", "currency": "USD", "balance": 99},
        ],
    )

    assert probe.main() == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload == {
        "ok": True,
        "operation": "read_only_wallet_list",
        "endpoint": "https://api.blink.sv/graphql",
        "wallet_count": 2,
        "currencies": ["BTC", "USD"],
    }
    rendered = json.dumps(payload)
    assert "secret-test-key" not in rendered
    assert "sensitive-wallet-id" not in rendered
    assert "another-wallet-id" not in rendered
    assert "1234" not in rendered
