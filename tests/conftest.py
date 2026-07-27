import pytest


@pytest.fixture(autouse=True)
def explicit_test_demo_mode(monkeypatch):
    """Tests use explicit demo auth and a deterministic server-side BTC/USD quote."""
    monkeypatch.setenv("ENABLE_LEGACY_DEMO_MUTATIONS", "true")
    monkeypatch.setenv("MERCHANT_API_KEYS", "bumbei-demo-key")
    monkeypatch.setenv(
        "SHOPIFY_MERCHANT_PUBKEY",
        "npub1540rxhz9x7fpc73nu5q3qydykej7lceh5j4jej6mmpc6n3saw3cqv7s8js",
    )
    from decimal import Decimal
    from app import main
    from app.rates import BtcUsdQuote

    quote = BtcUsdQuote(
        btc_usd=Decimal("40000"),
        sats_per_usd=Decimal("2500"),
        source="test_fixed",
        observed_at="2033-05-18T03:33:20+00:00",
        fetched_at="2033-05-18T03:33:20+00:00",
    )
    monkeypatch.setattr(main.btc_usd_rates, "get_quote", lambda: quote, raising=False)
