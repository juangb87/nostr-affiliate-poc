from decimal import Decimal

import pytest

from app.rates import MAX_RESPONSE_BYTES, BtcUsdRateService, RateUnavailableError, _http_get_json, fiat_to_sats


class Clock:
    def __init__(self, value: float = 2_000_000_000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_coingecko_quote_calculates_sats_per_usd_with_decimal():
    clock = Clock()
    calls = []

    def get_json(url: str, timeout: float):
        calls.append((url, timeout))
        return {"bitcoin": {"usd": 100_000, "last_updated_at": int(clock())}}

    quote = BtcUsdRateService(get_json=get_json, clock=clock).get_quote()

    assert quote.btc_usd == Decimal("100000")
    assert quote.sats_per_usd == Decimal("1000")
    assert quote.source == "coingecko"
    assert quote.stale is False
    assert fiat_to_sats(Decimal("125.25"), quote) == 125_250
    assert calls == [
        (
            "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_last_updated_at=true",
            5.0,
        )
    ]


def test_yadio_is_used_when_coingecko_fails():
    clock = Clock()
    calls = []

    def get_json(url: str, timeout: float):
        calls.append(url)
        if "coingecko" in url:
            raise OSError("primary unavailable")
        return {
            "base": "USD",
            "timestamp": int(clock() * 1000),
            "BTC": 100_000,
            "USD": {"BTC": 0.00001},
        }

    quote = BtcUsdRateService(get_json=get_json, clock=clock).get_quote()

    assert quote.btc_usd == Decimal("1E+5")
    assert quote.sats_per_usd == Decimal("1000")
    assert quote.source == "yadio"
    assert calls == [
        "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_last_updated_at=true",
        "https://api.yadio.io/exrates/USD",
    ]


def test_fresh_cache_avoids_repeated_provider_calls():
    clock = Clock()
    calls = 0

    def get_json(url: str, timeout: float):
        nonlocal calls
        calls += 1
        return {"bitcoin": {"usd": 80_000, "last_updated_at": int(clock())}}

    service = BtcUsdRateService(get_json=get_json, clock=clock, cache_ttl_seconds=60)
    first = service.get_quote()
    clock.advance(59)
    second = service.get_quote()

    assert second == first
    assert calls == 1


def test_recent_stale_cache_is_used_only_when_both_providers_fail():
    clock = Clock()
    failing = False

    def get_json(url: str, timeout: float):
        if failing:
            raise OSError("provider unavailable")
        return {"bitcoin": {"usd": 80_000, "last_updated_at": int(clock())}}

    service = BtcUsdRateService(
        get_json=get_json,
        clock=clock,
        cache_ttl_seconds=60,
        max_stale_seconds=900,
    )
    service.get_quote()
    failing = True
    clock.advance(61)

    stale = service.get_quote()

    assert stale.stale is True
    assert stale.source == "coingecko"
    assert stale.age_seconds == 61

    clock.advance(840)
    with pytest.raises(RateUnavailableError, match="live BTC/USD rate is unavailable"):
        service.get_quote()


def test_invalid_or_old_provider_payloads_fail_closed():
    clock = Clock()

    def get_json(url: str, timeout: float):
        if "coingecko" in url:
            return {"bitcoin": {"usd": -1, "last_updated_at": int(clock())}}
        return {"base": "USD", "timestamp": int((clock() - 901) * 1000), "BTC": 100_000}

    service = BtcUsdRateService(get_json=get_json, clock=clock, max_provider_age_seconds=900)

    with pytest.raises(RateUnavailableError, match="live BTC/USD rate is unavailable"):
        service.get_quote()


def test_fiat_rounding_is_half_up_and_rejects_invalid_amounts():
    clock = Clock()
    service = BtcUsdRateService(
        get_json=lambda url, timeout: {"bitcoin": {"usd": 100_000, "last_updated_at": int(clock())}},
        clock=clock,
    )
    quote = service.get_quote()

    assert fiat_to_sats(Decimal("0.0005"), quote) == 1
    with pytest.raises(ValueError, match="positive"):
        fiat_to_sats(Decimal("0"), quote)


def test_future_inconsistent_and_malformed_payloads_fail_closed():
    clock = Clock()

    def get_json(url: str, timeout: float):
        if "coingecko" in url:
            return {"bitcoin": {"usd": 100_000, "last_updated_at": int(clock() + 301)}}
        return {
            "base": "USD", "timestamp": int(clock() * 1000), "BTC": 100_000,
            "USD": {"BTC": 0.00002},
        }

    with pytest.raises(RateUnavailableError):
        BtcUsdRateService(get_json=get_json, clock=clock).get_quote()
    with pytest.raises(RateUnavailableError):
        BtcUsdRateService(get_json=lambda url, timeout: {"unexpected": []}, clock=clock).get_quote()


def test_wall_clock_rollback_does_not_extend_cached_quote():
    wall = Clock()
    monotonic = Clock(1000)
    failing = False

    def get_json(url: str, timeout: float):
        if failing:
            raise OSError("offline")
        return {"bitcoin": {"usd": 100_000, "last_updated_at": int(wall())}}

    service = BtcUsdRateService(
        get_json=get_json, clock=wall, monotonic_clock=monotonic,
        cache_ttl_seconds=60, max_stale_seconds=900,
    )
    service.get_quote()
    failing = True
    wall.value -= 301
    monotonic.advance(61)
    with pytest.raises(RateUnavailableError):
        service.get_quote()


@pytest.mark.parametrize("body", [b"{", b"x" * (MAX_RESPONSE_BYTES + 1)])
def test_http_adapter_rejects_malformed_or_oversized_responses(monkeypatch, body):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            return body

    monkeypatch.setattr("app.rates.urlopen", lambda request, timeout: FakeResponse())
    with pytest.raises((ValueError, TypeError)):
        _http_get_json("https://example.invalid", 1)
