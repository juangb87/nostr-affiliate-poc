import pytest


@pytest.fixture(autouse=True)
def explicit_test_demo_mode(monkeypatch):
    """Tests using legacy POC setup routes and merchant auth must opt in explicitly."""
    monkeypatch.setenv("ENABLE_LEGACY_DEMO_MUTATIONS", "true")
    monkeypatch.setenv("MERCHANT_API_KEYS", "bumbei-demo-key")
    monkeypatch.setenv(
        "SHOPIFY_MERCHANT_PUBKEY",
        "npub1540rxhz9x7fpc73nu5q3qydykej7lceh5j4jej6mmpc6n3saw3cqv7s8js",
    )
