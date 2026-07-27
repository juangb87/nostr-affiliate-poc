from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Callable
from urllib.request import Request, urlopen


SATS_PER_BTC = Decimal("100000000")
COINGECKO_URL = (
    "https://api.coingecko.com/api/v3/simple/price"
    "?ids=bitcoin&vs_currencies=usd&include_last_updated_at=true"
)
YADIO_URL = "https://api.yadio.io/exrates/USD"
MAX_RESPONSE_BYTES = 128 * 1024
MIN_BTC_USD = Decimal("1000")
MAX_BTC_USD = Decimal("10000000")

JsonGetter = Callable[[str, float], dict[str, Any]]
Clock = Callable[[], float]


class RateUnavailableError(RuntimeError):
    """No sufficiently recent, valid BTC/USD quote is available."""


@dataclass(frozen=True)
class BtcUsdQuote:
    btc_usd: Decimal
    sats_per_usd: Decimal
    source: str
    observed_at: str
    fetched_at: str
    stale: bool = False
    age_seconds: int = 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "btc_usd_rate": format(self.btc_usd, "f"),
            "sats_per_usd": format(self.sats_per_usd, "f"),
            "rate_source": self.source,
            "rate_observed_at": self.observed_at,
            "rate_fetched_at": self.fetched_at,
            "rate_stale": self.stale,
        }


def _iso_timestamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _positive_decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{label} is invalid")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    if not parsed.is_finite():
        raise ValueError(f"{label} is invalid")
    if parsed <= 0:
        raise ValueError(f"{label} must be positive")
    return parsed


def _http_get_json(url: str, timeout: float) -> dict[str, Any]:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Meerat-rates/1.0 (+https://meer.at)",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("rate provider response is too large")
    payload = json.loads(raw, parse_float=Decimal)
    if not isinstance(payload, dict):
        raise ValueError("rate provider response must be an object")
    return payload


class BtcUsdRateService:
    def __init__(
        self,
        *,
        get_json: JsonGetter = _http_get_json,
        clock: Clock = time.time,
        monotonic_clock: Clock | None = None,
        timeout_seconds: float = 5.0,
        cache_ttl_seconds: int = 60,
        max_stale_seconds: int = 900,
        max_provider_age_seconds: int = 900,
        coingecko_url: str = COINGECKO_URL,
        yadio_url: str = YADIO_URL,
    ) -> None:
        self._get_json = get_json
        self._clock = clock
        self._monotonic_clock = monotonic_clock or (time.monotonic if clock is time.time else clock)
        self._timeout_seconds = max(1.0, min(float(timeout_seconds), 10.0))
        self._cache_ttl_seconds = max(1, int(cache_ttl_seconds))
        self._max_stale_seconds = max(self._cache_ttl_seconds, int(max_stale_seconds))
        self._max_provider_age_seconds = max(60, int(max_provider_age_seconds))
        self._coingecko_url = coingecko_url
        self._yadio_url = yadio_url
        self._cached_quote: BtcUsdQuote | None = None
        self._cached_at_monotonic: float | None = None
        self._lock = threading.Lock()

    def _validated_quote(
        self,
        *,
        btc_usd: Any,
        source: str,
        observed_epoch: Any,
        fetched_epoch: float,
    ) -> BtcUsdQuote:
        price = _positive_decimal(btc_usd, "BTC/USD rate")
        if not MIN_BTC_USD <= price <= MAX_BTC_USD:
            raise ValueError("BTC/USD rate is outside the accepted range")
        observed = float(observed_epoch)
        if observed > 10_000_000_000:
            observed /= 1000
        provider_age = fetched_epoch - observed
        if provider_age < -300 or provider_age > self._max_provider_age_seconds:
            raise ValueError("rate provider timestamp is outside the accepted age")
        return BtcUsdQuote(
            btc_usd=price,
            sats_per_usd=SATS_PER_BTC / price,
            source=source,
            observed_at=_iso_timestamp(observed),
            fetched_at=_iso_timestamp(fetched_epoch),
        )

    def _fetch_coingecko(self, fetched_epoch: float) -> BtcUsdQuote:
        payload = self._get_json(self._coingecko_url, self._timeout_seconds)
        bitcoin = payload.get("bitcoin")
        if not isinstance(bitcoin, dict):
            raise ValueError("CoinGecko response is missing bitcoin")
        observed = bitcoin.get("last_updated_at")
        if observed is None:
            raise ValueError("CoinGecko response is missing last_updated_at")
        return self._validated_quote(
            btc_usd=bitcoin.get("usd"),
            source="coingecko",
            observed_epoch=observed,
            fetched_epoch=fetched_epoch,
        )

    def _fetch_yadio(self, fetched_epoch: float) -> BtcUsdQuote:
        payload = self._get_json(self._yadio_url, self._timeout_seconds)
        if str(payload.get("base", "")).upper() != "USD":
            raise ValueError("Yadio base currency is invalid")
        btc_usd = _positive_decimal(payload.get("BTC"), "Yadio BTC rate")
        nested_usd = payload.get("USD")
        if isinstance(nested_usd, dict) and nested_usd.get("BTC") is not None:
            nested_btc_per_usd = _positive_decimal(nested_usd.get("BTC"), "Yadio nested BTC rate")
            nested_btc_usd = Decimal(1) / nested_btc_per_usd
            divergence = abs(btc_usd - nested_btc_usd) / btc_usd
            if divergence > Decimal("0.05"):
                raise ValueError("Yadio BTC rates are inconsistent")
        return self._validated_quote(
            btc_usd=btc_usd,
            source="yadio",
            observed_epoch=payload.get("timestamp"),
            fetched_epoch=fetched_epoch,
        )

    def get_quote(self) -> BtcUsdQuote:
        with self._lock:
            current = self._clock()
            current_monotonic = self._monotonic_clock()
            if self._cached_quote is not None and self._cached_at_monotonic is not None:
                residence = current_monotonic - self._cached_at_monotonic
                observed_epoch = datetime.fromisoformat(self._cached_quote.observed_at).timestamp()
                total_age = current - observed_epoch
                if (
                    residence >= 0
                    and total_age >= -300
                    and residence < self._cache_ttl_seconds
                    and total_age <= self._max_provider_age_seconds
                ):
                    return self._cached_quote
            for fetch in (self._fetch_coingecko, self._fetch_yadio):
                try:
                    quote = fetch(current)
                except Exception:
                    continue
                self._cached_quote = quote
                self._cached_at_monotonic = current_monotonic
                return quote
            if self._cached_quote is not None and self._cached_at_monotonic is not None:
                residence = current_monotonic - self._cached_at_monotonic
                observed_epoch = datetime.fromisoformat(self._cached_quote.observed_at).timestamp()
                total_age = current - observed_epoch
                if residence >= 0 and total_age >= -300 and total_age <= self._max_stale_seconds:
                    return replace(self._cached_quote, stale=True, age_seconds=max(0, int(total_age)))
            raise RateUnavailableError("live BTC/USD rate is unavailable")


def fiat_to_sats(amount: Decimal | str | int | float, quote: BtcUsdQuote) -> int:
    value = _positive_decimal(amount, "fiat amount")
    return int((value * quote.sats_per_usd).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


btc_usd_rates = BtcUsdRateService(
    timeout_seconds=_env_int("BTC_USD_RATE_TIMEOUT_SECONDS", 5),
    cache_ttl_seconds=_env_int("BTC_USD_RATE_CACHE_SECONDS", 60),
    max_stale_seconds=_env_int("BTC_USD_RATE_MAX_STALE_SECONDS", 900),
    max_provider_age_seconds=_env_int("BTC_USD_RATE_MAX_PROVIDER_AGE_SECONDS", 900),
    coingecko_url=os.getenv("BTC_USD_COINGECKO_URL", COINGECKO_URL),
    yadio_url=os.getenv("BTC_USD_YADIO_URL", YADIO_URL),
)
