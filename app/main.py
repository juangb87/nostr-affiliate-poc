from __future__ import annotations

import asyncio
import base64
import fcntl
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Optional

from app.nostr_kinds import (
    CAMPAIGN_KIND,
    CAMPAIGN_STATUSES,
    CONVERSION_KIND,
    ENROLLMENT_KIND,
    ENROLLMENT_STATUSES,
    PAYOUT_KIND,
    REVERSAL_KIND,
    REVERSAL_REASONS,
    SCHEMA_VERSION,
)
from fastapi import BackgroundTasks, Cookie, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse
from nostr_sdk import Client, Event, EventBuilder, Keys, Kind, PublicKey, RelayUrl, Tag
import qrcode
from qrcode.image.svg import SvgPathImage
from qrcode.exceptions import DataOverflowError
from app.account_auth import (
    SESSION_COOKIE,
    digest as auth_digest,
    normalize_role,
    parse_iso,
    parse_merchant_bindings,
    parse_pubkey_set,
    random_token,
    verify_auth_event,
)
from app.lightning import (
    LightningPaymentError,
    bolt11_expires_at,
    lightning_address_url,
    pay_nwc_invoice,
    prepare_lnurl_payment,
    probe_nwc_wallet,
    validate_lightning_address,
)
from app.payment_rails import (
    NwcPaymentRail,
    PaymentRailAmbiguousError,
    PaymentStatus,
    build_payment_rail,
)
from app.payment_state import calculate_fee_sats, payment_idempotency_key
from app.rates import BtcUsdQuote, RateUnavailableError, btc_usd_rates, fiat_to_sats
from app.workspaces import affiliate_workspace_data, merchant_workspace_data, short as workspace_short
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


APP_SECRET = os.getenv("APP_SECRET", "dev-secret-change-me")
logger = logging.getLogger(__name__)
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
SHORT_LINK_HOST = os.getenv("SHORT_LINK_HOST", "mrt.st").strip().lower().rstrip(".")
SHORT_LINK_BASE_URL = os.getenv("SHORT_LINK_BASE_URL", f"https://{SHORT_LINK_HOST}").rstrip("/")
SHORT_REF_PATH_RE = re.compile(r"^/([A-Za-z0-9][A-Za-z0-9_-]{0,127})/?$")
SHORT_LINK_RESERVED_PATHS = {
    "app", "health", "static", "r", "v1", "shopify", "campaigns", "affiliates",
    "flows", "payouts", "docs", "redoc", "openapi.json", "bb.js", "favicon.ico", "invite",
}
DEFAULT_DESTINATION = os.getenv("DEFAULT_DESTINATION_URL", "https://example.com/checkout")
DEFAULT_RELAYS = "wss://nos.lol,wss://relay.damus.io,wss://relay.primal.net"
DEFAULT_MERCHANT_NPUB = "npub1540rxhz9x7fpc73nu5q3qydykej7lceh5j4jej6mmpc6n3saw3cqv7s8js"
DEFAULT_AFFILIATE_NPUB = "npub16ghkhw9d4g9x6pxp6l6dtyjqaeuavwucrq8gpkt60x0kx9fzqwpszhtw0n"
_MERCHANT_ENROLLMENT_LOCK = threading.Lock()
_MERCHANT_BOOTSTRAP_LOCKS = tuple(threading.Lock() for _ in range(64))
_NOSTR_PUBLICATION_LOCKS = tuple(threading.Lock() for _ in range(64))
_MERCHANT_CONVERSION_LOCKS = tuple(threading.Lock() for _ in range(64))
_INIT_DB_LOCK = threading.RLock()
_INVITATION_ACCEPT_LOCK = threading.Lock()
_INVOICE_PREPARE_LOCK = threading.Lock()
_INVOICE_PREPARE_LAST: dict[str, float] = {}
_INVOICE_PREPARE_ACTIVE: set[str] = set()

app = FastAPI(
    title="Nostr Affiliate POC",
    description="MVP: Nostr affiliate proofs, durable ledger, and provider-independent Lightning payment rails.",
    version="1.0.0",
)

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


def tracking_cors_origins() -> list[str]:
    raw = os.getenv(
        "TRACKING_CORS_ORIGINS",
        "https://shapersfit.com,https://www.shapersfit.com,https://shapersfit.myshopify.com",
    )
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    shop = os.getenv("SHOPIFY_STORE_DOMAIN", "").strip().lower().rstrip("/")
    if "://" in shop:
        shop = shop.split("://", 1)[1]
    if re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,252}", shop):
        shop_origin = f"https://{shop}"
        if shop_origin not in origins:
            origins.append(shop_origin)
    # Shopify Custom Pixels run in a strict sandbox with an opaque origin,
    # serialized by browsers as the literal Origin header value "null".
    if "null" not in origins:
        origins.append("null")
    return origins


app.add_middleware(
    CORSMiddleware,
    allow_origins=tracking_cors_origins(),
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

LEGACY_DEMO_MUTATION_PATHS = {
    "/campaigns",
    "/enrollments",
    "/conversions",
    "/clicks/simulate",
    "/demo",
    "/demo-merchant/checkout",
}


def referral_url(ref_code: str) -> str:
    return f"{SHORT_LINK_BASE_URL}/{ref_code}"


def legacy_demo_mutations_enabled() -> bool:
    explicit = os.getenv("ENABLE_LEGACY_DEMO_MUTATIONS")
    return bool(explicit and explicit.lower() in {"1", "true", "yes", "on"})


@app.middleware("http")
async def redirect_short_link_host(request: Request, call_next: Any) -> Response:
    host = (request.url.hostname or "").lower().rstrip(".")
    if host != SHORT_LINK_HOST:
        return await call_next(request)

    canonical = urlparse(BASE_URL)
    scheme = canonical.scheme or "https"
    netloc = canonical.netloc
    match = SHORT_REF_PATH_RE.fullmatch(request.url.path)
    if (
        request.method in {"GET", "HEAD"}
        and match
        and match.group(1).lower() not in SHORT_LINK_RESERVED_PATHS
    ):
        target = request.url.replace(scheme=scheme, netloc=netloc, path=f"/r/{match.group(1)}")
        response = RedirectResponse(str(target), status_code=302)
        response.headers["Cache-Control"] = "no-store"
        return response

    target = request.url.replace(scheme=scheme, netloc=netloc)
    return RedirectResponse(str(target), status_code=308)


@app.middleware("http")
async def redirect_www_to_canonical_apex(request: Request, call_next: Any) -> Response:
    if request.url.hostname and request.url.hostname.lower() == "www.meerat.com":
        canonical_url = request.url.replace(scheme="https", netloc="meerat.com")
        return RedirectResponse(str(canonical_url), status_code=308)
    return await call_next(request)


@app.middleware("http")
async def protect_legacy_demo_mutations(request: Request, call_next: Any) -> Response:
    if request.method == "POST" and request.url.path in LEGACY_DEMO_MUTATION_PATHS and not legacy_demo_mutations_enabled():
        return JSONResponse({"detail": "legacy demo mutations are disabled"}, status_code=404)
    return await call_next(request)

_ENGINE: Engine | None = None
_ENGINE_URL: str | None = None


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def valid_payment_hash(value: Any) -> bool:
    candidate = str(value or "").lower()
    return len(candidate) == 64 and all(ch in "0123456789abcdef" for ch in candidate)


def database_url() -> str:
    url = os.getenv("DATABASE_URL", "sqlite:///./data/poc.db")
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://") :]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    elif url.startswith("sqlite:///"):
        path = url.replace("sqlite:///", "", 1)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    return url


def engine() -> Engine:
    global _ENGINE, _ENGINE_URL
    url = database_url()
    if _ENGINE is None or _ENGINE_URL != url:
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _ENGINE = create_engine(url, future=True, pool_pre_ping=True, connect_args=connect_args)
        _ENGINE_URL = url
    return _ENGINE


def asdict(row: Any) -> dict[str, Any] | None:
    return dict(row._mapping) if row else None


PUBLIC_PAYOUT_FIELDS = {
    "id",
    "conversion_id",
    "affiliate_pubkey",
    "amount_sats",
    "status",
    "state",
    "fee_sats",
    "fee_state",
    "return_window_ends_at",
    "payment_hash",
    "payment_provider",
    "fees_paid_msats",
    "paid_at",
    "settled_at",
    "nostr_event_id",
    "created_at",
}


def public_payout_record(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Expose only payout facts intended for unauthenticated HTTP responses."""
    if not row:
        return row
    return {key: row[key] for key in PUBLIC_PAYOUT_FIELDS if key in row}


def public_enrollment_record(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Keep Lightning payout destinations out of public enrollment representations."""
    if not row:
        return row
    enrollment = dict(row)
    enrollment.pop("lightning_address", None)
    return enrollment


def hid(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(8).replace('-', '').replace('_', '')[:10]}"


def sha(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def safe_text(value: Any, max_len: int = 1000) -> str:
    if value is None:
        return ""
    return str(value).replace("\x00", "").replace("\r", " ").replace("\n", " ").strip()[:max_len]


def absolute_url(url: str) -> str:
    value = safe_text(url, 3000)
    if not value.startswith(("http://", "https://")):
        value = "https://" + value.lstrip("/")
    return value


def add_query_params(url: str, params: dict[str, str]) -> str:
    parsed = urlparse(absolute_url(url))
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update({k: v for k, v in params.items() if v})
    return urlunparse(parsed._replace(query=urlencode(query)))


def rate_quote_for_currency(currency: str) -> BtcUsdQuote | None:
    normalized = currency.upper().strip()
    if normalized not in {"USD", "USDC"}:
        return None
    try:
        return btc_usd_rates.get_quote()
    except RateUnavailableError as exc:
        raise HTTPException(503, "live BTC/USD rate is unavailable; retry later") from exc


def decimal_text(value: Decimal | str | int | float) -> str:
    rendered = format(Decimal(str(value)), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def order_total_sats(order_total: Decimal | str | int | float, currency: str, quote: BtcUsdQuote | None = None) -> int:
    normalized = currency.upper().strip()
    try:
        amount = Decimal(str(order_total))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise HTTPException(400, "order_total must be a positive number") from exc
    if not amount.is_finite() or amount <= 0:
        raise HTTPException(400, "order_total must be a positive number")
    if normalized in {"SAT", "SATS", "MSAT"}:
        sats = int(
            (amount / Decimal(1000) if normalized == "MSAT" else amount).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
    elif normalized in {"BTC", "XBT"}:
        sats = int((amount * Decimal(100_000_000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    elif normalized in {"USD", "USDC"}:
        if quote is None:
            raise HTTPException(503, "live BTC/USD rate is unavailable; retry later")
        sats = fiat_to_sats(amount, quote)
    else:
        raise HTTPException(400, "unsupported currency; use USD, SATS, or BTC")
    if sats <= 0:
        raise HTTPException(422, "order total is too small to create a sat-denominated obligation")
    return sats


def normalize_pubkey(value: str, label: str = "pubkey") -> dict[str, str]:
    raw = value.strip()
    try:
        pk = PublicKey.parse(raw)
    except Exception as exc:
        raise HTTPException(400, f"invalid {label}; expected npub or 64-char hex pubkey") from exc
    return {"npub": pk.to_bech32(), "hex": pk.to_hex(), "input": raw}


def nostr_relays() -> list[str]:
    raw = os.getenv("NOSTR_RELAYS", DEFAULT_RELAYS)
    return [r.strip() for r in raw.split(",") if r.strip()]


def nostr_publish_enabled() -> bool:
    explicit = os.getenv("NOSTR_PUBLISH")
    if explicit is not None:
        return explicit.lower() in {"1", "true", "yes", "on"}
    return bool(os.getenv("NOSTR_PRIVATE_KEY"))


def merchant_api_keys() -> set[str]:
    raw = os.getenv("MERCHANT_API_KEYS", "")
    return {k.strip() for k in raw.split(",") if k.strip()}


def configured_merchant_pubkey_hex() -> str:
    raw = os.getenv("SHOPIFY_MERCHANT_PUBKEY", "").strip()
    if not raw:
        raise HTTPException(503, "merchant identity is not configured")
    try:
        return PublicKey.parse(raw).to_hex()
    except Exception as exc:
        raise HTTPException(503, "configured merchant identity is invalid") from exc


def require_merchant_api_key(authorization: Optional[str]) -> str:
    valid_keys = merchant_api_keys()
    if not valid_keys:
        raise HTTPException(503, "merchant API authentication is not configured")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing Bearer merchant API key")
    token = authorization.split(" ", 1)[1].strip()
    for valid in valid_keys:
        if secrets.compare_digest(token, valid):
            return configured_merchant_pubkey_hex()
    raise HTTPException(403, "invalid merchant API key")


def require_merchant_ownership(record: dict[str, Any], authorized_merchant_hex: str) -> None:
    candidate = record.get("merchant_pubkey_hex") or record.get("merchant_pubkey")
    try:
        record_merchant_hex = PublicKey.parse(str(candidate or "")).to_hex()
    except Exception as exc:
        raise HTTPException(500, "record has an invalid merchant identity") from exc
    if not secrets.compare_digest(record_merchant_hex, authorized_merchant_hex):
        raise HTTPException(403, "merchant API key cannot access this merchant")


def require_payout_admin_key(authorization: Optional[str]) -> str:
    expected = os.getenv("PAYOUT_ADMIN_KEY", "").strip()
    if not expected:
        raise HTTPException(503, "payout administration is not configured")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing Bearer payout admin key")
    token = authorization.split(" ", 1)[1].strip()
    if not secrets.compare_digest(token, expected):
        raise HTTPException(403, "invalid payout admin key")
    return token


def lightning_payouts_enabled() -> bool:
    return os.getenv("LIGHTNING_PAYOUTS_ENABLED", "false").lower() in {"1", "true", "yes", "on"}


def configured_payment_rail():
    """Resolve the explicitly selected rail without performing any provider operation."""
    if os.getenv("PAYMENT_RAIL", "nwc").strip().lower() == "nwc":
        # Keep the established NWC hooks injectable for tests while the worker uses the generic contract.
        return NwcPaymentRail(prepare=prepare_lnurl_payment, pay=pay_nwc_invoice)
    return build_payment_rail()


def lightning_max_payout_sats() -> int:
    return max(1, int(os.getenv("LIGHTNING_MAX_PAYOUT_SATS", "1000")))


def default_campaign_budget_sats() -> int:
    return max(0, int(os.getenv("DEFAULT_CAMPAIGN_BUDGET_SATS", "1000000")))


def meerat_fee_bps() -> int:
    return max(0, min(int(os.getenv("MEERAT_FEE_BPS", "1000")), 10_000))


def fee_min_sats() -> int:
    return max(0, int(os.getenv("FEE_MIN_SATS", "10")))


def default_return_window_days() -> int:
    return max(0, int(os.getenv("DEFAULT_RETURN_WINDOW_DAYS", "0")))


def validate_runtime_security() -> None:
    if not database_url().startswith("postgresql"):
        return
    if not BASE_URL.startswith("https://"):
        raise RuntimeError("BASE_URL must use https in production")
    if not os.getenv("NOSTR_PRIVATE_KEY") and (APP_SECRET == "dev-secret-change-me" or len(APP_SECRET) < 32):
        raise RuntimeError("production requires NOSTR_PRIVATE_KEY or a strong APP_SECRET")


def nostr_keys() -> Keys:
    secret = os.getenv("NOSTR_PRIVATE_KEY")
    if secret:
        return Keys.parse(secret)
    if database_url().startswith("postgresql") and (APP_SECRET == "dev-secret-change-me" or len(APP_SECRET) < 32):
        raise RuntimeError("refusing to derive a production Nostr key from an insecure APP_SECRET")
    # Deterministic fallback is limited to tests/development or a strong production secret.
    derived = hashlib.sha256((APP_SECRET + ":nostr-dev-key").encode()).hexdigest()
    return Keys.parse(derived)


def build_nostr_event(kind: int, tags: list[list[str]], content: str = "") -> dict[str, Any]:
    keys = nostr_keys()
    event = EventBuilder(Kind(kind), content).tags([Tag.parse(t) for t in tags]).sign_with_keys(keys)
    data = json.loads(event.as_json())
    data["relay_status"] = "pending_publication" if nostr_publish_enabled() else "signed_not_published"
    return data


def address_coordinate(kind: int, entity_id: str) -> str:
    return f"{kind}:{nostr_keys().public_key().to_hex()}:{entity_id}"


def fiat_order_tags(order_total: Decimal | str | int | float, currency: str) -> list[list[str]]:
    normalized = currency.upper().strip()
    if normalized in {"USD", "USDC"}:
        return [["order_fiat_amount", decimal_text(order_total)], ["order_fiat_currency", normalized]]
    return []


def build_campaign_event(campaign: dict[str, Any], terms_url: Optional[str] = None) -> dict[str, Any]:
    merchant_hex = campaign.get("merchant_pubkey_hex") or normalize_pubkey(campaign["merchant_pubkey"], "merchant_pubkey")["hex"]
    content = {"name": campaign["name"]}
    if terms_url:
        content["terms_url"] = terms_url
    return build_nostr_event(
        CAMPAIGN_KIND,
        [
            ["v", SCHEMA_VERSION],
            ["d", campaign["id"]],
            ["type", "affiliate_campaign"],
            ["p", merchant_hex, "", "merchant"],
            ["campaign", campaign["id"]],
            ["status", campaign.get("status") or "active"],
            ["state_revision", str(time.time_ns())],
            ["commission_bps", str(campaign["commission_bps"])],
            ["window_days", str(campaign["window_days"])],
            ["payout", "sats"],
            ["terms", campaign["terms_hash"]],
            ["destination", campaign["destination_url"]],
        ],
        json.dumps(content),
    )


def build_enrollment_event(enrollment: dict[str, Any], campaign: dict[str, Any]) -> dict[str, Any]:
    merchant_hex = campaign.get("merchant_pubkey_hex") or normalize_pubkey(campaign["merchant_pubkey"], "merchant_pubkey")["hex"]
    affiliate_hex = enrollment.get("affiliate_pubkey_hex") or normalize_pubkey(enrollment["affiliate_pubkey"], "affiliate_pubkey")["hex"]
    return build_nostr_event(
        ENROLLMENT_KIND,
        [
            ["v", SCHEMA_VERSION],
            ["d", enrollment["id"]],
            ["type", "affiliate_enrollment"],
            ["p", merchant_hex, "", "merchant"],
            ["p", affiliate_hex, "", "affiliate"],
            ["campaign", enrollment["campaign_id"]],
            ["status", enrollment.get("status") or "approved"],
            ["state_revision", str(time.time_ns())],
            ["terms", campaign["terms_hash"]],
        ],
        "",
    )


async def _publish_event(event_json: dict[str, Any], relays: list[str]) -> list[dict[str, str]]:
    from nostr_sdk import Event

    client = Client()
    relay_urls: list[RelayUrl] = []
    for relay in relays:
        try:
            relay_url = RelayUrl.parse(relay)
            relay_urls.append(relay_url)
            await client.add_relay(relay_url)
        except Exception as exc:  # pragma: no cover - depends on external input
            pass
    if not relay_urls:
        return [{"relay": relay, "status": "failed", "error": "invalid relay url"} for relay in relays]
    try:
        await client.connect()
        event = Event.from_json(json.dumps({k: v for k, v in event_json.items() if k not in {"relay_status", "relay_results"}}))
        output = await asyncio.wait_for(client.send_event_to(relay_urls, event), timeout=12)
        success = {str(r) for r in output.success}
        failed = {str(k): str(v) for k, v in output.failed.items()}
        results = []
        for relay in relays:
            if relay in success:
                results.append({"relay": relay, "status": "published"})
            else:
                results.append({"relay": relay, "status": "failed", "error": failed.get(relay, "not acknowledged")})
        return results
    except Exception as exc:  # External relays/network can fail; persist the error per relay.
        return [{"relay": relay, "status": "failed", "error": str(exc)} for relay in relays]
    finally:
        await client.shutdown()


def publish_event(event_json: dict[str, Any]) -> list[dict[str, str]]:
    relays = nostr_relays()
    if not nostr_publish_enabled():
        return [{"relay": relay, "status": "skipped", "error": "NOSTR_PUBLISH disabled or NOSTR_PRIVATE_KEY missing"} for relay in relays]
    try:
        return asyncio.run(_publish_event(event_json, relays))
    except RuntimeError:
        # FastAPI sync endpoints normally have no running loop, but keep a safe fallback for test harnesses.
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_publish_event(event_json, relays))
        finally:
            loop.close()


def persist_nostr_event(c: Any, event: dict[str, Any], entity_type: str, entity_id: str, relay_results: list[dict[str, str]]) -> None:
    published_count = sum(1 for r in relay_results if r["status"] == "published")
    retryable_failure = (
        entity_type in {"conversion", "campaign"}
        and not published_count
        and any(r["status"] == "failed" for r in relay_results)
    )
    relay_status = (
        "published" if published_count
        else "pending_publication" if retryable_failure
        else relay_results[0]["status"] if relay_results
        else "pending_publication" if entity_type in {"conversion", "campaign"}
        else "unknown"
    )
    event["relay_status"] = relay_status
    event["relay_results"] = relay_results
    c.execute(
        text(
            """
            INSERT INTO nostr_events (event_id, kind, pubkey, content, tags_json, event_json,
            entity_type, entity_id, relay_status, created_at, published_at)
            VALUES (:event_id, :kind, :pubkey, :content, :tags_json, :event_json,
            :entity_type, :entity_id, :relay_status, :created_at, :published_at)
            ON CONFLICT(event_id) DO UPDATE SET
                event_json=CASE
                    WHEN nostr_events.relay_status='published' AND excluded.relay_status<>'published'
                    THEN nostr_events.event_json ELSE excluded.event_json END,
                relay_status=CASE
                    WHEN nostr_events.relay_status='published' AND excluded.relay_status<>'published'
                    THEN nostr_events.relay_status ELSE excluded.relay_status END,
                published_at=COALESCE(excluded.published_at, nostr_events.published_at)
            """
        ),
        {
            "event_id": event["id"],
            "kind": event["kind"],
            "pubkey": event["pubkey"],
            "content": event["content"],
            "tags_json": json.dumps(event["tags"]),
            "event_json": json.dumps(event),
            "entity_type": entity_type,
            "entity_id": entity_id,
            "relay_status": relay_status,
            "created_at": now(),
            "published_at": now() if published_count else None,
        },
    )
    for r in relay_results:
        c.execute(
            text(
                """
                INSERT INTO nostr_event_relays (event_id, relay_url, status, error, created_at)
                VALUES (:event_id, :relay_url, :status, :error, :created_at)
                """
            ),
            {"event_id": event["id"], "relay_url": r["relay"], "status": r["status"], "error": r.get("error"), "created_at": now()},
        )


@contextmanager
def _nostr_publication_lock(event_id: str):
    """Serialize one event's relay publication across threads and worker processes."""
    thread_lock = _NOSTR_PUBLICATION_LOCKS[int(hashlib.sha256(event_id.encode()).hexdigest(), 16) % len(_NOSTR_PUBLICATION_LOCKS)]
    with thread_lock:
        current_engine = engine()
        if current_engine.dialect.name == "postgresql":
            connection = current_engine.connect()
            locked = False
            try:
                connection.execute(
                    text("SELECT pg_advisory_lock(hashtextextended(:lock_key, 0))"),
                    {"lock_key": f"nostr-publication:{event_id}"},
                )
                connection.commit()
                locked = True
                yield connection
            finally:
                if locked:
                    try:
                        connection.execute(
                            text("SELECT pg_advisory_unlock(hashtextextended(:lock_key, 0))"),
                            {"lock_key": f"nostr-publication:{event_id}"},
                        )
                        connection.commit()
                    except Exception:
                        connection.invalidate()
                connection.close()
        elif current_engine.dialect.name == "sqlite":
            database_identity = str(current_engine.url.database or database_url())
            lock_digest = hashlib.sha256(f"{database_identity}:{event_id}".encode()).hexdigest()
            lock_path = Path("/tmp") / f"meerat-nostr-{lock_digest}.lock"
            with lock_path.open("a+") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield None
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        else:
            yield None


def _finalize_committed_nostr_event_unlocked(
    event: dict[str, Any],
    entity_type: str,
    entity_id: str,
    publication_connection: Any | None = None,
) -> list[dict[str, str]]:
    """Publish only after commit; persist through the connection that owns any advisory lock."""
    try:
        relay_results = publish_event(event)
    except Exception as exc:
        logger.exception("failed to publish committed Nostr outbox event %s", event.get("id"))
        relay_results = [
            {"relay": relay, "status": "failed", "error": str(exc)}
            for relay in nostr_relays()
        ]

    def persist_result(c: Any) -> None:
        persist_nostr_event(c, event, entity_type, entity_id, relay_results)
        if entity_type == "conversion":
            c.execute(
                text("""
                    UPDATE conversions SET nostr_event_json=:event_json
                    WHERE id=:id AND EXISTS (
                        SELECT 1 FROM nostr_events
                        WHERE event_id=:event_id AND relay_status=:relay_status
                    )
                """),
                {
                    "id": entity_id,
                    "event_id": event["id"],
                    "relay_status": event["relay_status"],
                    "event_json": json.dumps(event),
                },
            )
        elif entity_type == "campaign":
            c.execute(
                text("""
                    UPDATE campaigns SET nostr_event_json=:event_json
                    WHERE id=:id AND EXISTS (
                        SELECT 1 FROM nostr_events
                        WHERE event_id=:event_id AND relay_status=:relay_status
                    )
                """),
                {
                    "id": entity_id,
                    "event_id": event["id"],
                    "relay_status": event["relay_status"],
                    "event_json": json.dumps(event),
                },
            )

    try:
        if publication_connection is not None:
            with publication_connection.begin():
                persist_result(publication_connection)
        else:
            with engine().begin() as c:
                persist_result(c)
    except Exception:
        logger.exception("failed to finalize committed Nostr outbox event %s", event.get("id"))
        event["relay_status"] = "pending_publication"
        event["relay_results"] = []
        return []
    return relay_results


def finalize_committed_nostr_event(event: dict[str, Any], entity_type: str, entity_id: str) -> list[dict[str, str]]:
    event_id = str(event.get("id", ""))
    if not event_id:
        raise ValueError("Nostr event id is required before publication")
    with _nostr_publication_lock(event_id) as publication_connection:
        if publication_connection is not None:
            stored = asdict(
                publication_connection.execute(
                    text("SELECT relay_status, event_json FROM nostr_events WHERE event_id=:event_id"),
                    {"event_id": event_id},
                ).fetchone()
            )
            publication_connection.commit()
        else:
            with engine().connect() as c:
                stored = asdict(
                    c.execute(
                        text("SELECT relay_status, event_json FROM nostr_events WHERE event_id=:event_id"),
                        {"event_id": event_id},
                    ).fetchone()
                )
        if stored:
            current_event = json.loads(stored["event_json"])
            current_event["relay_status"] = stored["relay_status"]
            relay_results = current_event.get("relay_results", [])
            if stored["relay_status"] == "published":
                return relay_results
            if stored["relay_status"] == "skipped" and not nostr_publish_enabled():
                return relay_results
            event = current_event
        return _finalize_committed_nostr_event_unlocked(
            event,
            entity_type,
            entity_id,
            publication_connection=publication_connection,
        )


def retry_conversion_outbox(conversion_id: str) -> None:
    """Retry durable conversion and related campaign events on an idempotent webhook replay."""
    with engine().connect() as c:
        rows = [dict(row._mapping) for row in c.execute(text("""
            SELECT event_json, entity_type, entity_id
            FROM nostr_events
            WHERE relay_status='pending_publication'
              AND (
                (entity_type='conversion' AND entity_id=:conversion_id)
                OR (
                    entity_type='campaign'
                    AND entity_id=(SELECT campaign_id FROM conversions WHERE id=:conversion_id)
                )
              )
            ORDER BY CASE WHEN entity_type='conversion' THEN 0 ELSE 1 END, created_at
        """), {"conversion_id": conversion_id}).fetchall()]
    for row in rows:
        try:
            pending_event = json.loads(row["event_json"])
            finalize_committed_nostr_event(pending_event, row["entity_type"], row["entity_id"])
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.exception("invalid pending Nostr outbox event for %s %s", row["entity_type"], row["entity_id"])


def _init_db_unlocked() -> None:
    ddl = """
    CREATE TABLE IF NOT EXISTS campaigns (
        id TEXT PRIMARY KEY,
        merchant_pubkey TEXT NOT NULL,
        merchant_pubkey_hex TEXT,
        name TEXT NOT NULL,
        commission_bps INTEGER NOT NULL,
        window_days INTEGER NOT NULL,
        destination_url TEXT NOT NULL,
        terms_url TEXT,
        terms_hash TEXT NOT NULL,
        invite_eyebrow TEXT,
        invite_headline TEXT,
        invite_description TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        archived_at TEXT,
        nostr_event_id TEXT NOT NULL,
        nostr_event_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS enrollments (
        id TEXT PRIMARY KEY,
        campaign_id TEXT NOT NULL,
        affiliate_pubkey TEXT NOT NULL,
        affiliate_pubkey_hex TEXT,
        lightning_address TEXT,
        ref_code TEXT UNIQUE NOT NULL,
        status TEXT NOT NULL DEFAULT 'approved',
        nostr_event_id TEXT NOT NULL,
        nostr_event_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS clicks (
        id TEXT PRIMARY KEY,
        ref_code TEXT NOT NULL,
        campaign_id TEXT NOT NULL,
        affiliate_pubkey TEXT NOT NULL,
        ip_hash TEXT,
        user_agent_hash TEXT,
        landing_url TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS tracking_events (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        event_type TEXT NOT NULL,
        ref_code TEXT,
        click_id TEXT,
        shop TEXT,
        url TEXT,
        path TEXT,
        referrer TEXT,
        order_id_hash TEXT,
        order_name TEXT,
        checkout_token_hash TEXT,
        order_total REAL,
        currency TEXT,
        user_agent_hash TEXT,
        ip_hash TEXT,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS shopify_webhook_deliveries (
        webhook_id TEXT PRIMARY KEY,
        order_key TEXT UNIQUE NOT NULL,
        shop_domain TEXT NOT NULL,
        topic TEXT NOT NULL,
        click_id TEXT NOT NULL,
        order_total REAL NOT NULL,
        order_total_decimal TEXT,
        currency TEXT NOT NULL,
        status TEXT NOT NULL,
        conversion_id TEXT,
        error TEXT,
        created_at TEXT NOT NULL,
        processed_at TEXT
    );
    CREATE TABLE IF NOT EXISTS shopify_webhook_receipts (
        webhook_id TEXT PRIMARY KEY,
        shop_domain TEXT NOT NULL,
        topic TEXT NOT NULL,
        status TEXT NOT NULL,
        reason TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS conversions (
        id TEXT PRIMARY KEY,
        order_id_hash TEXT NOT NULL,
        merchant_order_key TEXT UNIQUE,
        idempotency_payload_hash TEXT,
        click_id TEXT NOT NULL,
        campaign_id TEXT NOT NULL,
        affiliate_pubkey TEXT NOT NULL,
        order_total REAL NOT NULL,
        order_total_decimal TEXT,
        currency TEXT NOT NULL,
        order_total_sats INTEGER,
        btc_usd_rate TEXT,
        sats_per_usd TEXT,
        rate_source TEXT,
        rate_observed_at TEXT,
        rate_fetched_at TEXT,
        rate_stale INTEGER,
        commission_sats INTEGER NOT NULL,
        status TEXT NOT NULL,
        nostr_event_id TEXT NOT NULL,
        nostr_event_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS payouts (
        id TEXT PRIMARY KEY,
        conversion_id TEXT NOT NULL,
        affiliate_pubkey TEXT NOT NULL,
        amount_sats INTEGER NOT NULL,
        lightning_address TEXT,
        status TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'PAYABLE',
        fee_sats INTEGER NOT NULL DEFAULT 0,
        fee_state TEXT NOT NULL DEFAULT 'FEE_PENDING',
        reserved_sats INTEGER NOT NULL DEFAULT 0,
        return_window_ends_at TEXT,
        settled_at TEXT,
        payment_hash TEXT,
        bolt11_invoice TEXT,
        payment_provider TEXT,
        fees_paid_msats INTEGER,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        processing_started_at TEXT,
        paid_at TEXT,
        nostr_event_id TEXT,
        nostr_event_json TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS campaign_budgets (
        campaign_id TEXT PRIMARY KEY,
        budget_sats INTEGER NOT NULL,
        committed_sats INTEGER NOT NULL DEFAULT 0,
        settled_sats INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS payment_attempts (
        id TEXT PRIMARY KEY,
        payout_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        rail TEXT NOT NULL,
        idempotency_key TEXT UNIQUE NOT NULL,
        destination TEXT NOT NULL,
        amount_sats INTEGER NOT NULL,
        status TEXT NOT NULL,
        payment_hash TEXT,
        preimage TEXT,
        provider_reference TEXT,
        error_code TEXT,
        retryable INTEGER,
        routing_fee_sats INTEGER,
        error TEXT,
        attempt_number INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        settled_at TEXT
    );
    CREATE TABLE IF NOT EXISTS ledger_entries (
        id TEXT PRIMARY KEY,
        transaction_id TEXT NOT NULL,
        campaign_id TEXT NOT NULL,
        payout_id TEXT NOT NULL,
        account TEXT NOT NULL,
        direction TEXT NOT NULL,
        amount_sats INTEGER NOT NULL,
        entry_type TEXT NOT NULL,
        idempotency_key TEXT UNIQUE NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_payment_attempts_payout ON payment_attempts(payout_id, kind, attempt_number);
    CREATE INDEX IF NOT EXISTS idx_payment_attempts_recovery ON payment_attempts(status, updated_at);
    CREATE UNIQUE INDEX IF NOT EXISTS uq_payment_attempts_one_live
        ON payment_attempts(payout_id, kind) WHERE status IN ('PAYING','UNKNOWN');
    CREATE INDEX IF NOT EXISTS idx_ledger_entries_payout ON ledger_entries(payout_id, created_at);
    CREATE TABLE IF NOT EXISTS reversals (
        id TEXT PRIMARY KEY,
        conversion_id TEXT UNIQUE NOT NULL,
        reason TEXT NOT NULL,
        refund_sats INTEGER,
        nostr_event_id TEXT NOT NULL,
        nostr_event_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS nostr_events (
        event_id TEXT PRIMARY KEY,
        kind INTEGER NOT NULL,
        pubkey TEXT NOT NULL,
        content TEXT NOT NULL,
        tags_json TEXT NOT NULL,
        event_json TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        relay_status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        published_at TEXT
    );
    CREATE TABLE IF NOT EXISTS nostr_event_relays (
        id INTEGER PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY,
        event_id TEXT NOT NULL,
        relay_url TEXT NOT NULL,
        status TEXT NOT NULL,
        error TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS accounts (
        id TEXT PRIMARY KEY,
        nostr_pubkey_hex TEXT UNIQUE NOT NULL,
        npub TEXT UNIQUE NOT NULL,
        display_name TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_login_at TEXT
    );
    CREATE TABLE IF NOT EXISTS account_roles (
        account_id TEXT NOT NULL,
        role TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (account_id, role)
    );
    CREATE TABLE IF NOT EXISTS auth_challenges (
        id TEXT PRIMARY KEY,
        challenge_hash TEXT UNIQUE NOT NULL,
        client_hash TEXT,
        role TEXT NOT NULL,
        relay TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        consumed_at TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS account_sessions (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        role TEXT NOT NULL,
        token_hash TEXT UNIQUE NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        revoked_at TEXT
    );
    CREATE TABLE IF NOT EXISTS merchant_account_links (
        account_id TEXT NOT NULL,
        merchant_pubkey_hex TEXT NOT NULL,
        source TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (account_id, merchant_pubkey_hex)
    );
    CREATE TABLE IF NOT EXISTS merchant_profiles (
        merchant_pubkey_hex TEXT PRIMARY KEY,
        merchant_pubkey TEXT NOT NULL,
        display_name TEXT,
        tagline TEXT,
        logo_url TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS affiliate_invitations (
        id TEXT PRIMARY KEY,
        token_hash TEXT UNIQUE NOT NULL,
        token_prefix TEXT NOT NULL,
        campaign_id TEXT NOT NULL,
        created_by_account_id TEXT NOT NULL,
        status TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        accepted_at TEXT,
        accepted_by_hex TEXT,
        enrollment_id TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_account_sessions_token ON account_sessions(token_hash, revoked_at, expires_at);
    CREATE INDEX IF NOT EXISTS idx_merchant_account_links_account ON merchant_account_links(account_id);
    CREATE INDEX IF NOT EXISTS idx_affiliate_invitations_campaign ON affiliate_invitations(campaign_id, status, created_at);

    """
    if database_url().startswith("sqlite"):
        ddl = ddl.replace("id INTEGER PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY", "id INTEGER PRIMARY KEY AUTOINCREMENT")
    with engine().begin() as c:
        for stmt in [s.strip() for s in ddl.split(";") if s.strip()]:
            c.execute(text(stmt))
        if database_url().startswith("postgresql"):
            c.execute(text("ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS merchant_pubkey_hex TEXT"))
            c.execute(text("ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS terms_url TEXT"))
            c.execute(text("ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS invite_eyebrow TEXT"))
            c.execute(text("ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS invite_headline TEXT"))
            c.execute(text("ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS invite_description TEXT"))
            c.execute(text("ALTER TABLE merchant_profiles ADD COLUMN IF NOT EXISTS display_name TEXT"))
            c.execute(text("ALTER TABLE merchant_profiles ADD COLUMN IF NOT EXISTS tagline TEXT"))
            c.execute(text("ALTER TABLE enrollments ADD COLUMN IF NOT EXISTS affiliate_pubkey_hex TEXT"))
            c.execute(text("ALTER TABLE conversions ADD COLUMN IF NOT EXISTS merchant_order_key TEXT"))
            c.execute(text("ALTER TABLE conversions ADD COLUMN IF NOT EXISTS idempotency_payload_hash TEXT"))
            c.execute(text("ALTER TABLE conversions ADD COLUMN IF NOT EXISTS order_total_decimal TEXT"))
            c.execute(text("ALTER TABLE shopify_webhook_deliveries ADD COLUMN IF NOT EXISTS order_total_decimal TEXT"))
            c.execute(text("ALTER TABLE conversions ADD COLUMN IF NOT EXISTS order_total_sats INTEGER"))
            c.execute(text("ALTER TABLE conversions ADD COLUMN IF NOT EXISTS btc_usd_rate TEXT"))
            c.execute(text("ALTER TABLE conversions ADD COLUMN IF NOT EXISTS sats_per_usd TEXT"))
            c.execute(text("ALTER TABLE conversions ADD COLUMN IF NOT EXISTS rate_source TEXT"))
            c.execute(text("ALTER TABLE conversions ADD COLUMN IF NOT EXISTS rate_observed_at TEXT"))
            c.execute(text("ALTER TABLE conversions ADD COLUMN IF NOT EXISTS rate_fetched_at TEXT"))
            c.execute(text("ALTER TABLE conversions ADD COLUMN IF NOT EXISTS rate_stale INTEGER"))
            c.execute(text("ALTER TABLE payouts ADD COLUMN IF NOT EXISTS bolt11_invoice TEXT"))
            c.execute(text("ALTER TABLE payouts ADD COLUMN IF NOT EXISTS payment_provider TEXT"))
            c.execute(text("ALTER TABLE payouts ADD COLUMN IF NOT EXISTS fees_paid_msats INTEGER"))
            c.execute(text("ALTER TABLE payouts ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0"))
            c.execute(text("ALTER TABLE payouts ADD COLUMN IF NOT EXISTS last_error TEXT"))
            c.execute(text("ALTER TABLE payouts ADD COLUMN IF NOT EXISTS processing_started_at TEXT"))
            c.execute(text("ALTER TABLE payouts ADD COLUMN IF NOT EXISTS paid_at TEXT"))
            c.execute(text("ALTER TABLE payouts ADD COLUMN IF NOT EXISTS state TEXT NOT NULL DEFAULT 'PAYABLE'"))
            c.execute(text("ALTER TABLE payouts ADD COLUMN IF NOT EXISTS fee_sats INTEGER NOT NULL DEFAULT 0"))
            c.execute(text("ALTER TABLE payouts ADD COLUMN IF NOT EXISTS fee_state TEXT NOT NULL DEFAULT 'FEE_PENDING'"))
            c.execute(text("ALTER TABLE payouts ADD COLUMN IF NOT EXISTS reserved_sats INTEGER NOT NULL DEFAULT 0"))
            c.execute(text("ALTER TABLE payouts ADD COLUMN IF NOT EXISTS return_window_ends_at TEXT"))
            c.execute(text("ALTER TABLE payouts ADD COLUMN IF NOT EXISTS settled_at TEXT"))
            c.execute(text("ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active'"))
            c.execute(text("ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS archived_at TEXT"))
            c.execute(text("ALTER TABLE enrollments ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'approved'"))
            c.execute(text("ALTER TABLE payment_attempts ADD COLUMN IF NOT EXISTS provider_reference TEXT"))
            c.execute(text("ALTER TABLE payment_attempts ADD COLUMN IF NOT EXISTS error_code TEXT"))
            c.execute(text("ALTER TABLE payment_attempts ADD COLUMN IF NOT EXISTS retryable INTEGER"))
            c.execute(text("ALTER TABLE auth_challenges ADD COLUMN IF NOT EXISTS client_hash TEXT"))
        else:
            campaign_cols = {r._mapping["name"] for r in c.execute(text("PRAGMA table_info(campaigns)")).fetchall()}
            enrollment_cols = {r._mapping["name"] for r in c.execute(text("PRAGMA table_info(enrollments)")).fetchall()}
            conversion_cols = {r._mapping["name"] for r in c.execute(text("PRAGMA table_info(conversions)")).fetchall()}
            shopify_delivery_cols = {r._mapping["name"] for r in c.execute(text("PRAGMA table_info(shopify_webhook_deliveries)")).fetchall()}
            payout_cols = {r._mapping["name"] for r in c.execute(text("PRAGMA table_info(payouts)")).fetchall()}
            attempt_cols = {r._mapping["name"] for r in c.execute(text("PRAGMA table_info(payment_attempts)")).fetchall()}
            challenge_cols = {r._mapping["name"] for r in c.execute(text("PRAGMA table_info(auth_challenges)")).fetchall()}
            merchant_profile_cols = {r._mapping["name"] for r in c.execute(text("PRAGMA table_info(merchant_profiles)")).fetchall()}
            for column in ("invite_eyebrow", "invite_headline", "invite_description"):
                if column not in campaign_cols:
                    c.execute(text(f"ALTER TABLE campaigns ADD COLUMN {column} TEXT"))
            for column in ("display_name", "tagline"):
                if column not in merchant_profile_cols:
                    c.execute(text(f"ALTER TABLE merchant_profiles ADD COLUMN {column} TEXT"))
            if "merchant_pubkey_hex" not in campaign_cols:
                c.execute(text("ALTER TABLE campaigns ADD COLUMN merchant_pubkey_hex TEXT"))
            if "terms_url" not in campaign_cols:
                c.execute(text("ALTER TABLE campaigns ADD COLUMN terms_url TEXT"))
            if "affiliate_pubkey_hex" not in enrollment_cols:
                c.execute(text("ALTER TABLE enrollments ADD COLUMN affiliate_pubkey_hex TEXT"))
            conversion_column_ddl = {
                "merchant_order_key": "TEXT",
                "idempotency_payload_hash": "TEXT",
                "order_total_decimal": "TEXT",
                "order_total_sats": "INTEGER",
                "btc_usd_rate": "TEXT",
                "sats_per_usd": "TEXT",
                "rate_source": "TEXT",
                "rate_observed_at": "TEXT",
                "rate_fetched_at": "TEXT",
                "rate_stale": "INTEGER",
            }
            for column, column_type in conversion_column_ddl.items():
                if column not in conversion_cols:
                    c.execute(text(f"ALTER TABLE conversions ADD COLUMN {column} {column_type}"))
            if "order_total_decimal" not in shopify_delivery_cols:
                c.execute(text("ALTER TABLE shopify_webhook_deliveries ADD COLUMN order_total_decimal TEXT"))
            payout_column_ddl = {
                "bolt11_invoice": "TEXT",
                "payment_provider": "TEXT",
                "fees_paid_msats": "INTEGER",
                "attempt_count": "INTEGER NOT NULL DEFAULT 0",
                "last_error": "TEXT",
                "processing_started_at": "TEXT",
                "paid_at": "TEXT",
                "state": "TEXT NOT NULL DEFAULT 'PAYABLE'",
                "fee_sats": "INTEGER NOT NULL DEFAULT 0",
                "fee_state": "TEXT NOT NULL DEFAULT 'FEE_PENDING'",
                "reserved_sats": "INTEGER NOT NULL DEFAULT 0",
                "return_window_ends_at": "TEXT",
                "settled_at": "TEXT",
            }
            for column, column_type in payout_column_ddl.items():
                if column not in payout_cols:
                    c.execute(text(f"ALTER TABLE payouts ADD COLUMN {column} {column_type}"))
            attempt_column_ddl = {"provider_reference": "TEXT", "error_code": "TEXT", "retryable": "INTEGER"}
            for column, column_type in attempt_column_ddl.items():
                if column not in attempt_cols:
                    c.execute(text(f"ALTER TABLE payment_attempts ADD COLUMN {column} {column_type}"))
            if "status" not in campaign_cols:
                c.execute(text("ALTER TABLE campaigns ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"))
            if "archived_at" not in campaign_cols:
                c.execute(text("ALTER TABLE campaigns ADD COLUMN archived_at TEXT"))
            if "status" not in enrollment_cols:
                c.execute(text("ALTER TABLE enrollments ADD COLUMN status TEXT NOT NULL DEFAULT 'approved'"))
            if "client_hash" not in challenge_cols:
                c.execute(text("ALTER TABLE auth_challenges ADD COLUMN client_hash TEXT"))
        legacy_campaigns = c.execute(
            text(
                """
                SELECT id, merchant_pubkey, merchant_pubkey_hex, terms_url, nostr_event_json
                FROM campaigns
                WHERE merchant_pubkey_hex IS NULL OR terms_url IS NULL
                """
            )
        ).fetchall()
        for legacy_row in legacy_campaigns:
            legacy = legacy_row._mapping
            updates: dict[str, Any] = {"id": legacy["id"]}
            if not legacy.get("merchant_pubkey_hex"):
                try:
                    updates["merchant_pubkey_hex"] = normalize_pubkey(legacy["merchant_pubkey"], "merchant_pubkey")["hex"]
                except HTTPException:
                    pass
            if not legacy.get("terms_url"):
                try:
                    event_payload = json.loads(legacy["nostr_event_json"])
                    content = json.loads(event_payload.get("content") or "{}")
                    terms_url = safe_text(content.get("terms_url"), 3000)
                    parsed_terms = urlparse(terms_url)
                    if parsed_terms.scheme in {"http", "https"} and parsed_terms.netloc:
                        updates["terms_url"] = terms_url
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            assignments = [key for key in ("merchant_pubkey_hex", "terms_url") if key in updates]
            if assignments:
                c.execute(
                    text("UPDATE campaigns SET " + ", ".join(f"{key}=:{key}" for key in assignments) + " WHERE id=:id"),
                    updates,
                )
        c.execute(text("CREATE INDEX IF NOT EXISTS idx_auth_challenges_rate ON auth_challenges(client_hash, created_at)"))
        c.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS idx_conversions_merchant_order_key ON conversions(merchant_order_key) WHERE merchant_order_key IS NOT NULL"))
        c.execute(text("""
            UPDATE payouts SET state=CASE
                WHEN status='paid' AND nostr_event_id IS NOT NULL THEN 'PUBLISHED'
                WHEN status='paid' THEN 'SETTLED'
                WHEN status IN ('processing','sandbox_processing','payment_unknown') THEN 'PAYING'
                WHEN status='failed' THEN 'FAILED'
                WHEN status='on_hold' THEN 'ON_HOLD'
                WHEN status='reversed' THEN 'CANCELLED'
                ELSE state END
            WHERE state='PAYABLE'
        """))
        c.execute(text("""
            UPDATE payouts SET state='SETTLED'
            WHERE status='paid' AND nostr_event_id IS NULL AND state='PUBLISHED'
        """))
        c.execute(text("""
            UPDATE payouts
            SET state='ON_HOLD', status='on_hold', last_error='budget reservation required after ledger migration'
            WHERE status IN ('pending','failed')
              AND state IN ('PAYABLE','FAILED')
              AND reserved_sats=0
              AND NOT EXISTS (
                  SELECT 1 FROM ledger_entries
                  WHERE ledger_entries.payout_id=payouts.id
                    AND ledger_entries.idempotency_key=payouts.id || ':reserve:debit'
              )
        """))


def init_db() -> None:
    """Run idempotent schema initialization once at a time within this process."""
    with _INIT_DB_LOCK:
        _init_db_unlocked()


def archive_campaign_preserving_history(
    campaign_id: str,
    *,
    expected_merchant_hex: str,
    expected_name: str,
) -> bool:
    """End and hide a campaign while retaining its immutable financial history."""
    init_db()
    with _INVITATION_ACCEPT_LOCK:
        with engine().begin() as c:
            if database_url().startswith("postgresql"):
                c.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                    {"lock_key": f"campaign-archive:{campaign_id}"},
                )
            campaign = asdict(
                c.execute(text("SELECT * FROM campaigns WHERE id=:id"), {"id": campaign_id}).fetchone()
            )
            if not campaign:
                return False
            if campaign.get("merchant_pubkey_hex") != expected_merchant_hex or campaign.get("name") != expected_name:
                raise RuntimeError(f"refusing to archive unexpected campaign {campaign_id}")
            if campaign.get("archived_at"):
                return False
            campaign["status"] = "ended"
            event = build_campaign_event(campaign)
            relay_results = publish_event(event)
            persist_nostr_event(c, event, "campaign", campaign_id, relay_results)
            c.execute(
                text(
                    """
                    UPDATE campaigns
                    SET status='ended', archived_at=:archived_at,
                        nostr_event_id=:event_id, nostr_event_json=:event_json
                    WHERE id=:id
                    """
                ),
                {
                    "id": campaign_id,
                    "archived_at": now(),
                    "event_id": event["id"],
                    "event_json": json.dumps(event),
                },
            )
    return True


@app.post("/internal/migrations/archive-lightning-koffee-canary")
def apply_requested_campaign_archive(
    x_migration_token: Optional[str] = Header(None),
) -> dict[str, Any]:
    token_hash = hashlib.sha256((x_migration_token or "").encode()).hexdigest()
    if not hmac.compare_digest(token_hash, "00aabb8156fe02de4986412704b3347a28d1406233850a2657da13d54e5d8434"):
        raise HTTPException(404, "not found")
    changed = archive_campaign_preserving_history(
        "camp_aowrZDPrmp",
        expected_merchant_hex="c19621bcad2c9d502618dfaf25a6be0fde23bd730e51889dc883376c91cca6c4",
        expected_name="Meerat NWC Canary 21 sats",
    )
    return {"ok": True, "changed": changed, "campaign_id": "camp_aowrZDPrmp"}


def record_ledger_transaction(
    c: Any,
    *,
    campaign_id: str,
    payout_id: str,
    entry_type: str,
    debit_account: str,
    credit_account: str,
    amount_sats: int,
    idempotency_base: str,
) -> bool:
    """Append one balanced debit/credit pair exactly once."""
    if amount_sats <= 0:
        return False
    debit_key = f"{idempotency_base}:debit"
    if c.execute(text("SELECT 1 FROM ledger_entries WHERE idempotency_key=:key"), {"key": debit_key}).fetchone():
        return False
    transaction_id = hid("ledtx")
    created_at = now()
    for direction, account in (("debit", debit_account), ("credit", credit_account)):
        c.execute(
            text("""
                INSERT INTO ledger_entries
                (id, transaction_id, campaign_id, payout_id, account, direction, amount_sats, entry_type, idempotency_key, created_at)
                VALUES (:id, :transaction_id, :campaign_id, :payout_id, :account, :direction, :amount_sats, :entry_type, :idempotency_key, :created_at)
            """),
            {
                "id": hid("led"),
                "transaction_id": transaction_id,
                "campaign_id": campaign_id,
                "payout_id": payout_id,
                "account": account,
                "direction": direction,
                "amount_sats": amount_sats,
                "entry_type": entry_type,
                "idempotency_key": f"{idempotency_base}:{direction}",
                "created_at": created_at,
            },
        )
    return True


def ensure_campaign_budget(c: Any, campaign_id: str) -> dict[str, Any]:
    c.execute(
        text("""
            INSERT INTO campaign_budgets (campaign_id, budget_sats, committed_sats, settled_sats, updated_at)
            VALUES (:campaign_id, :budget_sats, 0, 0, :updated_at)
            ON CONFLICT(campaign_id) DO NOTHING
        """),
        {"campaign_id": campaign_id, "budget_sats": default_campaign_budget_sats(), "updated_at": now()},
    )
    return asdict(c.execute(text("SELECT * FROM campaign_budgets WHERE campaign_id=:id"), {"id": campaign_id}).fetchone())


def locked_campaign_budget(c: Any, campaign_id: str) -> dict[str, Any]:
    ensure_campaign_budget(c, campaign_id)
    suffix = " FOR UPDATE" if c.engine.dialect.name == "postgresql" else ""
    return asdict(c.execute(text(f"SELECT * FROM campaign_budgets WHERE campaign_id=:id{suffix}"), {"id": campaign_id}).fetchone())


def reserve_campaign_budget(c: Any, campaign_id: str, payout_id: str, amount_sats: int) -> bool:
    budget = locked_campaign_budget(c, campaign_id)
    if c.execute(text("SELECT 1 FROM ledger_entries WHERE idempotency_key=:key"), {"key": f"{payout_id}:reserve:debit"}).fetchone():
        return True
    if int(budget["committed_sats"]) + int(budget["settled_sats"]) + amount_sats > int(budget["budget_sats"]):
        return False
    reserved = c.execute(
        text("""
            UPDATE campaign_budgets
            SET committed_sats=committed_sats+:amount, updated_at=:updated_at
            WHERE campaign_id=:campaign_id
              AND committed_sats + settled_sats + :amount <= budget_sats
        """),
        {"amount": amount_sats, "updated_at": now(), "campaign_id": campaign_id},
    )
    if reserved.rowcount != 1:
        return False
    record_ledger_transaction(
        c,
        campaign_id=campaign_id,
        payout_id=payout_id,
        entry_type="reserve",
        debit_account="merchant_budget_available",
        credit_account="payout_reserved",
        amount_sats=amount_sats,
        idempotency_base=f"{payout_id}:reserve",
    )
    return True


def settle_campaign_budget(c: Any, campaign_id: str, payout_id: str, amount_sats: int) -> None:
    locked_campaign_budget(c, campaign_id)
    if c.execute(text("SELECT 1 FROM ledger_entries WHERE idempotency_key=:key"), {"key": f"{payout_id}:commission_settled:debit"}).fetchone():
        return
    settled = c.execute(
        text("""
            UPDATE campaign_budgets
            SET committed_sats=committed_sats-:amount,
                settled_sats=settled_sats+:amount, updated_at=:updated_at
            WHERE campaign_id=:campaign_id AND committed_sats>=:amount
        """),
        {"amount": amount_sats, "updated_at": now(), "campaign_id": campaign_id},
    )
    if settled.rowcount != 1:
        raise HTTPException(409, "commission settlement exceeds committed campaign budget")
    record_ledger_transaction(
        c,
        campaign_id=campaign_id,
        payout_id=payout_id,
        entry_type="commission_settled",
        debit_account="payout_reserved",
        credit_account="affiliate_paid",
        amount_sats=amount_sats,
        idempotency_base=f"{payout_id}:commission_settled",
    )


def release_campaign_budget(
    c: Any,
    campaign_id: str,
    payout_id: str,
    amount_sats: int,
    *,
    movement: str = "release",
) -> None:
    if amount_sats <= 0:
        return
    locked_campaign_budget(c, campaign_id)
    idempotency_base = f"{payout_id}:{movement}"
    if c.execute(text("SELECT 1 FROM ledger_entries WHERE idempotency_key=:key"), {"key": f"{idempotency_base}:debit"}).fetchone():
        return
    released = c.execute(
        text("""
            UPDATE campaign_budgets
            SET committed_sats=committed_sats-:amount,
                updated_at=:updated_at
            WHERE campaign_id=:campaign_id AND committed_sats>=:amount
        """),
        {"amount": amount_sats, "updated_at": now(), "campaign_id": campaign_id},
    )
    if released.rowcount != 1:
        raise HTTPException(409, "budget release exceeds committed campaign budget")
    record_ledger_transaction(
        c,
        campaign_id=campaign_id,
        payout_id=payout_id,
        entry_type=movement,
        debit_account="payout_reserved",
        credit_account="merchant_budget_available",
        amount_sats=amount_sats,
        idempotency_base=idempotency_base,
    )


@app.on_event("startup")
def startup() -> None:
    validate_runtime_security()
    init_db()


class CampaignIn(BaseModel):
    merchant_pubkey: str = Field(..., examples=[DEFAULT_MERCHANT_NPUB])
    name: str = "Bumbei BTC Rewards"
    commission_bps: int = 800
    attribution_window_days: int = 30
    destination_url: str = DEFAULT_DESTINATION
    terms_url: str = "https://bumbei.com/terms/affiliate"


class MerchantBootstrapIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    merchant_pubkey: str = Field(..., min_length=32, max_length=128)
    program_name: str | None = Field(default=None, max_length=160)
    commission_percent: Decimal | None = Field(
        default=None, gt=0, le=100, max_digits=5, decimal_places=2
    )
    attribution_window_days: int | None = Field(default=None, ge=1, le=365)
    destination_url: str | None = Field(default=None, max_length=3000)
    terms_url: str | None = Field(default=None, max_length=3000)
    logo_url: str | None = Field(default=None, max_length=2048)


class MerchantProfileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    merchant_pubkey: str = Field(..., min_length=32, max_length=128)
    display_name: str | None = Field(default=None, max_length=120)
    tagline: str | None = Field(default=None, max_length=180)
    logo_url: str | None = Field(default=None, max_length=2048)


class CampaignInviteBrandingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    campaign_id: str = Field(..., min_length=1, max_length=80)
    invite_eyebrow: str | None = Field(default=None, max_length=100)
    invite_headline: str | None = Field(default=None, max_length=120)
    invite_description: str | None = Field(default=None, max_length=360)


class MerchantOnboardingIn(MerchantBootstrapIn):
    display_name: str = Field(..., min_length=1, max_length=120)
    tagline: str | None = Field(default=None, max_length=180)
    invite_eyebrow: str | None = Field(default=None, max_length=100)
    invite_headline: str | None = Field(default=None, max_length=120)
    invite_description: str | None = Field(default=None, max_length=360)


class EnrollmentIn(BaseModel):
    campaign_id: str = Field(..., min_length=1, max_length=80)
    affiliate_pubkey: str = Field(..., min_length=1, max_length=128, examples=[DEFAULT_AFFILIATE_NPUB])
    lightning_address: Optional[str] = Field(
        None,
        max_length=254,
        pattern=r"^[^@\s]{1,64}@[A-Za-z0-9.-]{1,189}$",
        examples=["seba@getalby.com"],
    )


class ConversionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str
    click_id: str
    order_total: Decimal = Field(..., gt=0, max_digits=20, decimal_places=8)
    currency: str = "USD"


class SimulateClickIn(BaseModel):
    ref_code: str


class BrowserEventIn(BaseModel):
    type: str = Field("page_view", description="Browser event type, e.g. page_view")
    shop: Optional[str] = None
    bb_ref: Optional[str] = None
    bumbei_ref: Optional[str] = None
    affiliate: Optional[str] = None
    ref: Optional[str] = None
    bb_click_id: Optional[str] = None
    click_id: Optional[str] = None
    url: Optional[str] = None
    path: Optional[str] = None
    query: Optional[str] = None
    referrer: Optional[str] = None
    user_agent: Optional[str] = None
    ts: Optional[str] = None


class BrowserConversionIn(BaseModel):
    type: str = Field("checkout_completed", description="Browser/pixel conversion event type")
    shop: Optional[str] = None
    bb_ref: Optional[str] = None
    bumbei_ref: Optional[str] = None
    affiliate: Optional[str] = None
    ref: Optional[str] = None
    bb_click_id: Optional[str] = None
    click_id: Optional[str] = None
    order_id: Optional[str] = None
    order_name: Optional[str] = None
    checkout_token: Optional[str] = None
    total_price: Optional[float | str] = None
    currency: Optional[str] = None
    url: Optional[str] = None
    path: Optional[str] = None
    referrer: Optional[str] = None
    user_agent: Optional[str] = None
    ts: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MerchantConversionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str = Field(..., min_length=3)
    bb_click_id: str = Field(..., min_length=4)
    order_total: Decimal = Field(..., gt=0, max_digits=20, decimal_places=8)
    currency: str = Field("USD", description="USD, SATS, or BTC. USD is converted to sats server-side.")
    customer_hash: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DemoMerchantCheckoutIn(BaseModel):
    bb_click_id: str = Field(..., min_length=4)
    bb_ref: Optional[str] = None
    order_total: Decimal = Field(Decimal("250000"), gt=0, max_digits=20, decimal_places=8)
    currency: str = "SATS"


class PayoutMarkPaidIn(BaseModel):
    payment_hash: Optional[str] = Field(None, description="Sandbox Lightning payment hash. Generated if omitted.")
    note: Optional[str] = "sandbox payout paid"


class AffiliateLightningAddressIn(BaseModel):
    lightning_address: str = Field(..., min_length=3, max_length=320)


class MerchantManualSettlementIn(BaseModel):
    payment_hash: str = Field(..., min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")


class CampaignBudgetIn(BaseModel):
    budget_sats: int = Field(..., ge=0)


class AttemptReconcileIn(BaseModel):
    outcome: str
    payment_hash: Optional[str] = None
    routing_fee_sats: Optional[int] = Field(None, ge=0)
    error: Optional[str] = Field(None, description="Required operator audit reason for manual settlement")


class CampaignStatusIn(BaseModel):
    status: str


class EnrollmentStatusIn(BaseModel):
    status: str


class ReversalIn(BaseModel):
    reason: str
    refund_sats: Optional[int] = Field(None, ge=0)
    note: Optional[str] = None


class AuthChallengeIn(BaseModel):
    role: str


class AuthVerifyIn(BaseModel):
    event: dict[str, Any]


class MerchantInvitationIn(BaseModel):
    campaign_id: str = Field(..., min_length=1, max_length=80)
    expires_days: int = Field(7, ge=1, le=30)


class AffiliateInvitationTokenIn(BaseModel):
    token: str = Field(..., min_length=32, max_length=128)


class AffiliateInvitationAcceptIn(AffiliateInvitationTokenIn):
    event: dict[str, Any]


@app.get("/health")
def health() -> dict[str, Any]:
    init_db()
    return {
        "ok": "true",
        "service": "nostr-affiliate-poc",
        "db": "postgres" if database_url().startswith("postgresql") else "sqlite",
        "nostr_pubkey": nostr_keys().public_key().to_hex(),
        "nostr_publish": nostr_publish_enabled(),
        "dynamic_campaign_invites": True,
        "relays": nostr_relays(),
        "btc_usd_rates": {"mode": "live", "providers": ["coingecko", "yadio"]},
        "nostr_schema_version": SCHEMA_VERSION,
        "nostr_kinds": {
            "campaign": CAMPAIGN_KIND,
            "enrollment": ENROLLMENT_KIND,
            "conversion": CONVERSION_KIND,
            "payout": PAYOUT_KIND,
            "reversal": REVERSAL_KIND,
        },
    }


def _auth_event_challenge(event_json: dict[str, Any]) -> str:
    tags = event_json.get("tags") if isinstance(event_json, dict) else None
    values = [tag[1] for tag in (tags or []) if isinstance(tag, list) and len(tag) >= 2 and tag[0] == "challenge"]
    if len(values) != 1 or not isinstance(values[0], str) or not values[0]:
        raise HTTPException(400, "authentication event requires exactly one challenge tag")
    return values[0]


def _grant_role_if_authorized(c: Any, account_id: str, pubkey_hex: str, role: str) -> bool:
    authorized = False
    if role == "affiliate":
        affiliate_npub = PublicKey.parse(pubkey_hex).to_bech32()
        authorized = bool(
            c.execute(
                text(
                    """
                    SELECT 1 FROM enrollments
                    WHERE (affiliate_pubkey_hex=:hex OR affiliate_pubkey=:hex OR affiliate_pubkey=:npub)
                      AND status='approved'
                    LIMIT 1
                    """
                ),
                {"hex": pubkey_hex, "npub": affiliate_npub},
            ).fetchone()
        )
    elif role == "merchant":
        direct = c.execute(text("SELECT 1 FROM campaigns WHERE merchant_pubkey_hex=:hex OR merchant_pubkey=:hex LIMIT 1"), {"hex": pubkey_hex}).fetchone()
        authorized = bool(direct)
        try:
            bindings = parse_merchant_bindings(os.getenv("MERCHANT_ACCOUNT_BINDINGS", ""))
        except ValueError as exc:
            if not authorized:
                raise HTTPException(503, "merchant account binding configuration is invalid") from exc
            bindings = []
        c.execute(
            text("DELETE FROM merchant_account_links WHERE account_id=:account_id AND source='environment_binding'"),
            {"account_id": account_id},
        )
        for owner_hex, merchant_hex in bindings:
            if owner_hex != pubkey_hex or merchant_hex == owner_hex:
                continue
            authorized = True
            c.execute(
                text("""
                    INSERT INTO merchant_account_links (account_id, merchant_pubkey_hex, source, created_at)
                    VALUES (:account_id, :merchant_hex, 'environment_binding', :created_at)
                    ON CONFLICT (account_id, merchant_pubkey_hex) DO NOTHING
                """),
                {"account_id": account_id, "merchant_hex": merchant_hex, "created_at": now()},
            )
    elif role == "ops":
        try:
            authorized = pubkey_hex in parse_pubkey_set(os.getenv("OPS_NOSTR_PUBKEYS", ""))
        except ValueError as exc:
            raise HTTPException(503, "operator allowlist configuration is invalid") from exc
    if authorized:
        c.execute(
            text("""
                INSERT INTO account_roles (account_id, role, created_at)
                VALUES (:account_id, :role, :created_at)
                ON CONFLICT (account_id, role) DO NOTHING
            """),
            {"account_id": account_id, "role": role, "created_at": now()},
        )
    return authorized


def _session_account(request: Request, required_role: str | None = None) -> dict[str, Any] | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    init_db()
    with engine().begin() as c:
        row = c.execute(
            text("""
                SELECT s.id AS session_id, s.account_id, s.role, s.expires_at,
                       a.nostr_pubkey_hex, a.npub, a.display_name, a.status
                FROM account_sessions s JOIN accounts a ON a.id=s.account_id
                WHERE s.token_hash=:token_hash AND s.revoked_at IS NULL
                LIMIT 1
            """),
            {"token_hash": auth_digest(token)},
        ).fetchone()
        session = asdict(row)
        if not session or session["status"] != "active" or parse_iso(session["expires_at"]) <= datetime.now(timezone.utc):
            return None
        if required_role and session["role"] != required_role:
            return None
        if required_role and not _grant_role_if_authorized(c, session["account_id"], session["nostr_pubkey_hex"], required_role):
            c.execute(text("UPDATE account_sessions SET revoked_at=:now WHERE id=:id"), {"now": now(), "id": session["session_id"]})
            return None
        c.execute(text("UPDATE account_sessions SET last_seen_at=:now WHERE id=:id"), {"now": now(), "id": session["session_id"]})
        return session


def require_account_session(request: Request, role: str) -> dict[str, Any]:
    session = _session_account(request, role)
    if not session:
        raise HTTPException(401, f"{role} sign-in required")
    return session


@app.post("/auth/nostr/challenge", tags=["Accounts"])
def create_auth_challenge(body: AuthChallengeIn, request: Request) -> dict[str, Any]:
    try:
        role = normalize_role(body.role)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    init_db()
    challenge = random_token(32)
    challenge_id = hid("chl")
    created_at = datetime.now(timezone.utc)
    expires_at = created_at + timedelta(minutes=3)
    client_hash = hmac.new(APP_SECRET.encode(), _request_ip(request).encode(), hashlib.sha256).hexdigest()
    cutoff = (created_at - timedelta(minutes=1)).isoformat()
    with engine().begin() as c:
        c.execute(text("DELETE FROM auth_challenges WHERE expires_at<:now"), {"now": created_at.isoformat()})
        recent_client = int(
            c.execute(
                text("SELECT COUNT(*) FROM auth_challenges WHERE client_hash=:client_hash AND created_at>=:cutoff"),
                {"client_hash": client_hash, "cutoff": cutoff},
            ).scalar_one()
        )
        if recent_client >= 20:
            raise HTTPException(429, "too many authentication challenges; retry shortly")
        recent_global = int(
            c.execute(
                text("SELECT COUNT(*) FROM auth_challenges WHERE created_at>=:cutoff"),
                {"cutoff": cutoff},
            ).scalar_one()
        )
        if recent_global >= 3000:
            raise HTTPException(429, "authentication service is busy; retry shortly")
        c.execute(
            text("""
                INSERT INTO auth_challenges
                (id, challenge_hash, client_hash, role, relay, expires_at, consumed_at, created_at)
                VALUES (:id, :challenge_hash, :client_hash, :role, :relay, :expires_at, NULL, :created_at)
            """),
            {
                "id": challenge_id,
                "challenge_hash": auth_digest(challenge),
                "client_hash": client_hash,
                "role": role,
                "relay": BASE_URL,
                "expires_at": expires_at.isoformat(),
                "created_at": created_at.isoformat(),
            },
        )
    return {
        "challenge": challenge,
        "role": role,
        "relay": BASE_URL,
        "kind": 22242,
        "expires_at": expires_at.isoformat(),
    }


@app.post("/auth/nostr/verify", tags=["Accounts"])
def verify_auth_login(body: AuthVerifyIn, response: Response) -> dict[str, Any]:
    challenge = _auth_event_challenge(body.event)
    init_db()
    session_token = random_token(32)
    with engine().begin() as c:
        challenge_row = asdict(c.execute(text("SELECT * FROM auth_challenges WHERE challenge_hash=:hash LIMIT 1"), {"hash": auth_digest(challenge)}).fetchone())
        if not challenge_row:
            raise HTTPException(404, "authentication challenge not found")
        if challenge_row["consumed_at"]:
            raise HTTPException(409, "authentication challenge was already used")
        if parse_iso(challenge_row["expires_at"]) <= datetime.now(timezone.utc):
            raise HTTPException(410, "authentication challenge expired")
        try:
            identity = verify_auth_event(
                body.event,
                expected_challenge=challenge,
                expected_role=challenge_row["role"],
                expected_relay=challenge_row["relay"],
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        consumed = c.execute(
            text("UPDATE auth_challenges SET consumed_at=:now WHERE id=:id AND consumed_at IS NULL"),
            {"now": now(), "id": challenge_row["id"]},
        )
        if consumed.rowcount != 1:
            raise HTTPException(409, "authentication challenge was already used")

        existing = asdict(c.execute(text("SELECT * FROM accounts WHERE nostr_pubkey_hex=:hex"), {"hex": identity["hex"]}).fetchone())
        account_id = existing["id"] if existing else hid("acct")
        timestamp = now()
        if existing:
            c.execute(text("UPDATE accounts SET npub=:npub, updated_at=:now, last_login_at=:now WHERE id=:id"), {"npub": identity["npub"], "now": timestamp, "id": account_id})
        else:
            c.execute(
                text("""
                    INSERT INTO accounts (id, nostr_pubkey_hex, npub, status, created_at, updated_at, last_login_at)
                    VALUES (:id, :hex, :npub, 'active', :now, :now, :now)
                """),
                {"id": account_id, "hex": identity["hex"], "npub": identity["npub"], "now": timestamp},
            )
        role = challenge_row["role"]
        if not _grant_role_if_authorized(c, account_id, identity["hex"], role):
            raise HTTPException(403, f"this Nostr identity is not authorized as {role}")
        session_id = hid("ses")
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
        c.execute(
            text("""
                INSERT INTO account_sessions (id, account_id, role, token_hash, created_at, expires_at, last_seen_at, revoked_at)
                VALUES (:id, :account_id, :role, :token_hash, :now, :expires_at, :now, NULL)
            """),
            {
                "id": session_id,
                "account_id": account_id,
                "role": role,
                "token_hash": auth_digest(session_token),
                "now": timestamp,
                "expires_at": expires_at,
            },
        )
    response.set_cookie(
        SESSION_COOKIE,
        session_token,
        max_age=12 * 60 * 60,
        httponly=True,
        secure=BASE_URL.startswith("https://"),
        samesite="lax",
        path="/",
    )
    return {"ok": True, "account": {"npub": identity["npub"], "role": role}, "redirect": f"/app/{role}" if role != "ops" else "/ops"}


@app.get("/auth/me", tags=["Accounts"])
def auth_me(request: Request) -> dict[str, Any]:
    session = _session_account(request)
    if not session:
        raise HTTPException(401, "not authenticated")
    return {"authenticated": True, "account": {"npub": session["npub"], "role": session["role"], "display_name": session.get("display_name")}}


@app.post("/auth/logout", tags=["Accounts"])
def auth_logout(request: Request, response: Response) -> dict[str, Any]:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        init_db()
        with engine().begin() as c:
            c.execute(text("UPDATE account_sessions SET revoked_at=:now WHERE token_hash=:hash AND revoked_at IS NULL"), {"now": now(), "hash": auth_digest(token)})
    response.delete_cookie(SESSION_COOKIE, path="/", secure=BASE_URL.startswith("https://"), httponly=True, samesite="lax")
    return {"ok": True}


@app.post("/campaigns")
def create_campaign(body: CampaignIn) -> dict[str, Any]:
    init_db()
    campaign_id = hid("camp")
    merchant = normalize_pubkey(body.merchant_pubkey, "merchant_pubkey")
    terms_hash = sha(body.terms_url)
    campaign_row = {
        "id": campaign_id,
        "merchant_pubkey": merchant["npub"],
        "merchant_pubkey_hex": merchant["hex"],
        "name": body.name,
        "commission_bps": body.commission_bps,
        "window_days": body.attribution_window_days,
        "destination_url": body.destination_url,
        "terms_hash": terms_hash,
        "status": "active",
    }
    event = build_campaign_event(campaign_row, body.terms_url)
    relay_results = publish_event(event)
    with engine().begin() as c:
        persist_nostr_event(c, event, "campaign", campaign_id, relay_results)
        c.execute(
            text(
                """
                INSERT INTO campaigns (id, merchant_pubkey, merchant_pubkey_hex, name, commission_bps, window_days,
                destination_url, terms_url, terms_hash, status, nostr_event_id, nostr_event_json, created_at)
                VALUES (:id, :merchant_pubkey, :merchant_pubkey_hex, :name, :commission_bps, :window_days,
                :destination_url, :terms_url, :terms_hash, :status, :nostr_event_id, :nostr_event_json, :created_at)
                """
            ),
            {
                "id": campaign_id,
                "merchant_pubkey": merchant["npub"],
                "merchant_pubkey_hex": merchant["hex"],
                "name": body.name,
                "commission_bps": body.commission_bps,
                "window_days": body.attribution_window_days,
                "destination_url": body.destination_url,
                "terms_url": body.terms_url,
                "terms_hash": terms_hash,
                "status": "active",
                "nostr_event_id": event["id"],
                "nostr_event_json": json.dumps(event),
                "created_at": now(),
            },
        )
    return {"campaign_id": campaign_id, "merchant_pubkey": merchant["npub"], "merchant_pubkey_hex": merchant["hex"], "nostr_event_id": event["id"], "nostr_event": event, "relay_results": relay_results}


@app.get("/campaigns/{campaign_id}")
def get_campaign(campaign_id: str) -> dict[str, Any]:
    with engine().connect() as c:
        campaign = asdict(c.execute(text("SELECT * FROM campaigns WHERE id=:id"), {"id": campaign_id}).fetchone())
    if not campaign:
        raise HTTPException(404, "campaign not found")
    campaign["nostr_event"] = json.loads(campaign.pop("nostr_event_json"))
    return campaign


@app.post("/campaigns/{campaign_id}/status")
def update_campaign_status(
    campaign_id: str,
    body: CampaignStatusIn,
    authorization: Optional[str] = Header(None),
) -> dict[str, Any]:
    authorized_merchant_hex = require_merchant_api_key(authorization)
    init_db()
    status = body.status.strip().lower()
    if status not in CAMPAIGN_STATUSES:
        raise HTTPException(400, f"invalid campaign status; use {', '.join(sorted(CAMPAIGN_STATUSES))}")
    with _INVITATION_ACCEPT_LOCK:
        with engine().begin() as c:
            if database_url().startswith("postgresql"):
                c.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                    {"lock_key": f"campaign-enrollment:{campaign_id}"},
                )
            campaign = asdict(c.execute(text("SELECT * FROM campaigns WHERE id=:id"), {"id": campaign_id}).fetchone())
            if not campaign:
                raise HTTPException(404, "campaign not found")
            require_merchant_ownership(campaign, authorized_merchant_hex)
            if campaign.get("status") == status:
                return {"ok": True, "duplicate": True, "campaign_id": campaign_id, "status": status, "nostr_event_id": campaign["nostr_event_id"], "nostr_event": json.loads(campaign["nostr_event_json"])}
            campaign["status"] = status
            event = build_campaign_event(campaign)
            relay_results = publish_event(event)
            persist_nostr_event(c, event, "campaign", campaign_id, relay_results)
            c.execute(text("UPDATE campaigns SET status=:status, nostr_event_id=:event_id, nostr_event_json=:event_json WHERE id=:id"), {"id": campaign_id, "status": status, "event_id": event["id"], "event_json": json.dumps(event)})
    return {"ok": True, "duplicate": False, "campaign_id": campaign_id, "status": status, "nostr_event_id": event["id"], "nostr_event": event, "relay_results": relay_results}


def _create_enrollment_record(body: EnrollmentIn) -> dict[str, Any]:
    init_db()
    with engine().connect() as c:
        camp = asdict(c.execute(text("SELECT * FROM campaigns WHERE id=:id"), {"id": body.campaign_id}).fetchone())
    if not camp:
        raise HTTPException(404, "campaign not found")
    enrollment_id = hid("enr")
    ref_code = hid("ref")
    affiliate = normalize_pubkey(body.affiliate_pubkey, "affiliate_pubkey")
    enrollment_row = {
        "id": enrollment_id,
        "campaign_id": body.campaign_id,
        "affiliate_pubkey": affiliate["npub"],
        "affiliate_pubkey_hex": affiliate["hex"],
        "status": "approved",
    }
    event = build_enrollment_event(enrollment_row, camp)
    relay_results = publish_event(event)
    with engine().begin() as c:
        persist_nostr_event(c, event, "enrollment", enrollment_id, relay_results)
        c.execute(
            text(
                """
                INSERT INTO enrollments (id, campaign_id, affiliate_pubkey, affiliate_pubkey_hex, lightning_address,
                ref_code, status, nostr_event_id, nostr_event_json, created_at)
                VALUES (:id, :campaign_id, :affiliate_pubkey, :affiliate_pubkey_hex, :lightning_address,
                :ref_code, :status, :nostr_event_id, :nostr_event_json, :created_at)
                """
            ),
            {
                "id": enrollment_id,
                "campaign_id": body.campaign_id,
                "affiliate_pubkey": affiliate["npub"],
                "affiliate_pubkey_hex": affiliate["hex"],
                "lightning_address": body.lightning_address,
                "ref_code": ref_code,
                "status": "approved",
                "nostr_event_id": event["id"],
                "nostr_event_json": json.dumps(event),
                "created_at": now(),
            },
        )
    return {"enrollment_id": enrollment_id, "affiliate_pubkey": affiliate["npub"], "affiliate_pubkey_hex": affiliate["hex"], "ref_code": ref_code, "ref_url": referral_url(ref_code), "nostr_event_id": event["id"], "nostr_event": event, "relay_results": relay_results}


@app.post("/enrollments/{enrollment_id}/status")
def update_enrollment_status(
    enrollment_id: str,
    body: EnrollmentStatusIn,
    authorization: Optional[str] = Header(None),
) -> dict[str, Any]:
    authorized_merchant_hex = require_merchant_api_key(authorization)
    init_db()
    status = body.status.strip().lower()
    if status not in ENROLLMENT_STATUSES:
        raise HTTPException(400, f"invalid enrollment status; use {', '.join(sorted(ENROLLMENT_STATUSES))}")
    with engine().begin() as c:
        enrollment = asdict(c.execute(text("SELECT * FROM enrollments WHERE id=:id"), {"id": enrollment_id}).fetchone())
        if not enrollment:
            raise HTTPException(404, "enrollment not found")
        campaign = asdict(c.execute(text("SELECT * FROM campaigns WHERE id=:id"), {"id": enrollment["campaign_id"]}).fetchone())
        if not campaign:
            raise HTTPException(404, "campaign not found")
        require_merchant_ownership(campaign, authorized_merchant_hex)
        if enrollment.get("status") == status:
            return {"ok": True, "duplicate": True, "enrollment_id": enrollment_id, "status": status, "nostr_event_id": enrollment["nostr_event_id"], "nostr_event": json.loads(enrollment["nostr_event_json"])}
        enrollment["status"] = status
        event = build_enrollment_event(enrollment, campaign)
        relay_results = publish_event(event)
        persist_nostr_event(c, event, "enrollment", enrollment_id, relay_results)
        c.execute(text("UPDATE enrollments SET status=:status, nostr_event_id=:event_id, nostr_event_json=:event_json WHERE id=:id"), {"id": enrollment_id, "status": status, "event_id": event["id"], "event_json": json.dumps(event)})
    return {"ok": True, "duplicate": False, "enrollment_id": enrollment_id, "status": status, "nostr_event_id": event["id"], "nostr_event": event, "relay_results": relay_results}


@app.get("/r/{ref_code}")
def redirect_click(ref_code: str, request: Request) -> RedirectResponse:
    init_db()
    with engine().begin() as c:
        enr = asdict(c.execute(text("SELECT * FROM enrollments WHERE ref_code=:ref"), {"ref": ref_code}).fetchone())
        if not enr:
            raise HTTPException(404, "ref code not found")
        camp = asdict(c.execute(text("SELECT * FROM campaigns WHERE id=:id"), {"id": enr["campaign_id"]}).fetchone())
        if enr.get("status") != "approved":
            raise HTTPException(409, "enrollment is not approved")
        if not camp or camp.get("status") != "active":
            raise HTTPException(409, "campaign is not active")
        click_id = hid("clk")
        ip = request.client.host if request.client else "unknown"
        ua = request.headers.get("user-agent", "")
        c.execute(
            text(
                """
                INSERT INTO clicks (id, ref_code, campaign_id, affiliate_pubkey, ip_hash,
                user_agent_hash, landing_url, created_at)
                VALUES (:id, :ref_code, :campaign_id, :affiliate_pubkey, :ip_hash,
                :user_agent_hash, :landing_url, :created_at)
                """
            ),
            {
                "id": click_id,
                "ref_code": ref_code,
                "campaign_id": enr["campaign_id"],
                "affiliate_pubkey": enr["affiliate_pubkey"],
                "ip_hash": sha(ip),
                "user_agent_hash": sha(ua),
                "landing_url": camp["destination_url"],
                "created_at": now(),
            },
        )
    url = add_query_params(camp["destination_url"], {"bb_click_id": click_id, "bb_ref": ref_code})
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie("bb_click_id", click_id, max_age=60 * 60 * 24 * int(camp["window_days"]), httponly=True, samesite="lax")
    return resp


def _create_conversion(
    body: ConversionIn,
    bb_click_id: Optional[str] = None,
    *,
    merchant_order_key: str | None = None,
    idempotency_payload_hash: str | None = None,
) -> dict[str, Any]:
    quote = rate_quote_for_currency(body.currency)
    rate_snapshot = quote.snapshot() if quote else {
        "btc_usd_rate": None,
        "sats_per_usd": None,
        "rate_source": "not_required",
        "rate_observed_at": None,
        "rate_fetched_at": None,
        "rate_stale": False,
    }
    init_db()
    click_id = body.click_id or bb_click_id
    pause_event: dict[str, Any] | None = None
    with engine().begin() as c:
        click = asdict(c.execute(text("SELECT * FROM clicks WHERE id=:id"), {"id": click_id}).fetchone())
        if not click:
            raise HTTPException(404, "click not found")
        affiliate_lock_hex = normalize_pubkey(click["affiliate_pubkey"], "affiliate_pubkey")["hex"]
        if c.engine.dialect.name == "postgresql":
            c.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": f"affiliate-destination:{affiliate_lock_hex}"},
            )
        camp = asdict(c.execute(text("SELECT * FROM campaigns WHERE id=:id"), {"id": click["campaign_id"]}).fetchone())
        enr = asdict(c.execute(text("SELECT * FROM enrollments WHERE ref_code=:ref"), {"ref": click["ref_code"]}).fetchone())
        if not camp or camp.get("status") != "active":
            raise HTTPException(409, "campaign is not active")
        if not enr or enr.get("status") != "approved":
            raise HTTPException(409, "enrollment is not approved")
        total_sats = order_total_sats(body.order_total, body.currency, quote)
        commission_sats = total_sats * int(camp["commission_bps"]) // 10000
        conversion_id = hid("conv")
        merchant_hex = camp.get("merchant_pubkey_hex") or normalize_pubkey(camp["merchant_pubkey"], "merchant_pubkey")["hex"]
        affiliate_hex = (enr.get("affiliate_pubkey_hex") if enr else None) or normalize_pubkey(click["affiliate_pubkey"], "affiliate_pubkey")["hex"]
        conversion_tags = [
            ["v", SCHEMA_VERSION],
            ["type", "affiliate_conversion"],
            ["p", merchant_hex, "", "merchant"],
            ["p", affiliate_hex, "", "affiliate"],
            ["campaign", click["campaign_id"]],
            ["a", address_coordinate(CAMPAIGN_KIND, click["campaign_id"])],
            ["a", address_coordinate(ENROLLMENT_KIND, enr["id"])],
            ["click_hash", sha(click_id)],
            ["order_hash", sha(body.order_id)],
            ["order_total_sats", str(total_sats)],
            ["commission_sats", str(commission_sats)],
            ["commission_bps", str(camp["commission_bps"])],
            ["status", "approved"],
        ] + fiat_order_tags(body.order_total, body.currency)
        if quote:
            conversion_tags.extend([
                ["btc_usd_rate", rate_snapshot["btc_usd_rate"]],
                ["sats_per_usd", rate_snapshot["sats_per_usd"]],
                ["rate_source", rate_snapshot["rate_source"]],
                ["rate_observed_at", rate_snapshot["rate_observed_at"]],
                ["rate_fetched_at", rate_snapshot["rate_fetched_at"]],
                ["rate_stale", "true" if rate_snapshot["rate_stale"] else "false"],
            ])
        event = build_nostr_event(CONVERSION_KIND, conversion_tags, "")
        relay_results: list[dict[str, str]] = []
        persist_nostr_event(c, event, "conversion", conversion_id, relay_results)
        c.execute(
            text(
                """
                INSERT INTO conversions (id, order_id_hash, merchant_order_key, idempotency_payload_hash, click_id, campaign_id, affiliate_pubkey,
                order_total, order_total_decimal, currency, order_total_sats, btc_usd_rate, sats_per_usd, rate_source,
                rate_observed_at, rate_fetched_at, rate_stale, commission_sats, status,
                nostr_event_id, nostr_event_json, created_at)
                VALUES (:id, :order_id_hash, :merchant_order_key, :idempotency_payload_hash, :click_id, :campaign_id, :affiliate_pubkey,
                :order_total, :order_total_decimal, :currency, :order_total_sats, :btc_usd_rate, :sats_per_usd, :rate_source,
                :rate_observed_at, :rate_fetched_at, :rate_stale, :commission_sats, :status,
                :nostr_event_id, :nostr_event_json, :created_at)
                """
            ),
            {
                "id": conversion_id,
                "order_id_hash": sha(body.order_id),
                "merchant_order_key": merchant_order_key,
                "idempotency_payload_hash": idempotency_payload_hash,
                "click_id": click_id,
                "campaign_id": click["campaign_id"],
                "affiliate_pubkey": click["affiliate_pubkey"],
                "order_total": float(body.order_total),
                "order_total_decimal": decimal_text(body.order_total),
                "currency": body.currency,
                "order_total_sats": total_sats,
                "btc_usd_rate": rate_snapshot["btc_usd_rate"],
                "sats_per_usd": rate_snapshot["sats_per_usd"],
                "rate_source": rate_snapshot["rate_source"],
                "rate_observed_at": rate_snapshot["rate_observed_at"],
                "rate_fetched_at": rate_snapshot["rate_fetched_at"],
                "rate_stale": 1 if rate_snapshot["rate_stale"] else 0,
                "commission_sats": commission_sats,
                "status": "approved",
                "nostr_event_id": event["id"],
                "nostr_event_json": json.dumps(event),
                "created_at": now(),
            },
        )
        payout_id = hid("pay")
        fee_sats = calculate_fee_sats(commission_sats, meerat_fee_bps(), fee_min_sats())
        obligation_sats = commission_sats + fee_sats
        budget_reserved = reserve_campaign_budget(c, click["campaign_id"], payout_id, obligation_sats)
        payout_state = "PAYABLE" if budget_reserved else "ON_HOLD"
        payout_status = "pending" if budget_reserved else "on_hold"
        return_window_ends_at = (datetime.now(timezone.utc) + timedelta(days=default_return_window_days())).isoformat()
        c.execute(
            text(
                """
                INSERT INTO payouts (id, conversion_id, affiliate_pubkey, amount_sats,
                lightning_address, status, state, fee_sats, fee_state, reserved_sats,
                return_window_ends_at, payment_hash, nostr_event_id, nostr_event_json, created_at)
                VALUES (:id, :conversion_id, :affiliate_pubkey, :amount_sats,
                :lightning_address, :status, :state, :fee_sats, :fee_state, :reserved_sats,
                :return_window_ends_at, :payment_hash, :nostr_event_id, :nostr_event_json, :created_at)
                """
            ),
            {
                "id": payout_id,
                "conversion_id": conversion_id,
                "affiliate_pubkey": click["affiliate_pubkey"],
                "amount_sats": commission_sats,
                "lightning_address": enr["lightning_address"] if enr else None,
                "status": payout_status,
                "state": payout_state,
                "fee_sats": fee_sats,
                "fee_state": "FEE_PENDING",
                "reserved_sats": obligation_sats if budget_reserved else 0,
                "return_window_ends_at": return_window_ends_at,
                "payment_hash": None,
                "nostr_event_id": None,
                "nostr_event_json": None,
                "created_at": now(),
            },
        )
        if not budget_reserved:
            camp["status"] = "paused"
            pause_event = build_campaign_event(camp)
            persist_nostr_event(c, pause_event, "campaign", camp["id"], [])
            c.execute(
                text("UPDATE campaigns SET status='paused', nostr_event_id=:event_id, nostr_event_json=:event_json WHERE id=:id"),
                {"id": camp["id"], "event_id": pause_event["id"], "event_json": json.dumps(pause_event)},
            )
    relay_results = finalize_committed_nostr_event(event, "conversion", conversion_id)
    if pause_event is not None:
        finalize_committed_nostr_event(pause_event, "campaign", camp["id"])
    return {
        "conversion_id": conversion_id,
        "affiliate_pubkey": click["affiliate_pubkey"],
        "order_total_sats": total_sats,
        **rate_snapshot,
        "commission_sats": commission_sats,
        "fee_sats": fee_sats,
        "status": "approved",
        "payout_status": payout_status,
        "payout_state": payout_state,
        "nostr_event_id": event["id"],
        "nostr_event": event,
        "relay_results": relay_results,
    }


@app.post("/conversions")
def create_conversion(body: ConversionIn, bb_click_id: Optional[str] = Cookie(None)) -> dict[str, Any]:
    return _create_conversion(body, bb_click_id)


def apply_reversal_to_payout(c: Any, conversion: dict[str, Any]) -> None:
    suffix = " FOR UPDATE" if c.engine.dialect.name == "postgresql" else ""
    payout = asdict(c.execute(
        text(f"SELECT * FROM payouts WHERE conversion_id=:id ORDER BY created_at DESC LIMIT 1{suffix}"),
        {"id": conversion["id"]},
    ).fetchone())
    if not payout:
        return
    state = payout.get("state")
    reserved_sats = int(payout.get("reserved_sats") or 0)
    if state in {"PAYABLE", "FAILED", "ON_HOLD"}:
        release_campaign_budget(c, conversion["campaign_id"], payout["id"], reserved_sats, movement="reversal_release")
        c.execute(
            text("UPDATE payouts SET status='reversed', state='CANCELLED', fee_state='CANCELLED', reserved_sats=0 WHERE id=:id"),
            {"id": payout["id"]},
        )
    elif state == "PAYING":
        # The provider may already have paid. Keep the reservation until reconciliation.
        c.execute(
            text("UPDATE payouts SET state='CANCEL_PENDING', fee_state='CANCELLED' WHERE id=:id AND state='PAYING'"),
            {"id": payout["id"]},
        )
    elif state in {"SETTLED", "PUBLISHED"} and reserved_sats > 0:
        # The commission is settled; reverse only the still-unpaid Meerat fee.
        release_campaign_budget(c, conversion["campaign_id"], payout["id"], reserved_sats, movement="reversal_fee_release")
        c.execute(
            text("UPDATE payouts SET fee_state='CANCELLED', reserved_sats=0 WHERE id=:id"),
            {"id": payout["id"]},
        )


def apply_existing_reversal_for_payout(payout_id: str) -> None:
    with engine().begin() as c:
        conversion = asdict(c.execute(text("""
            SELECT v.* FROM conversions v JOIN payouts p ON p.conversion_id=v.id WHERE p.id=:id
        """), {"id": payout_id}).fetchone())
        if conversion and conversion.get("status") == "reversed":
            apply_reversal_to_payout(c, conversion)


@app.post("/conversions/{conversion_id}/reverse")
def reverse_conversion(
    conversion_id: str,
    body: ReversalIn,
    authorization: Optional[str] = Header(None),
) -> dict[str, Any]:
    authorized_merchant_hex = require_merchant_api_key(authorization)
    init_db()
    reason = body.reason.strip().lower()
    if reason not in REVERSAL_REASONS:
        raise HTTPException(400, f"invalid reversal reason; use {', '.join(sorted(REVERSAL_REASONS))}")
    with engine().begin() as c:
        conversion = asdict(c.execute(text("SELECT * FROM conversions WHERE id=:id"), {"id": conversion_id}).fetchone())
        if not conversion:
            raise HTTPException(404, "conversion not found")
        campaign = asdict(c.execute(text("SELECT * FROM campaigns WHERE id=:id"), {"id": conversion["campaign_id"]}).fetchone())
        if not campaign:
            raise HTTPException(400, "conversion is missing campaign context")
        require_merchant_ownership(campaign, authorized_merchant_hex)
        existing = asdict(c.execute(text("SELECT * FROM reversals WHERE conversion_id=:id"), {"id": conversion_id}).fetchone())
        if existing:
            apply_reversal_to_payout(c, conversion)
            return {
                "ok": True,
                "duplicate": True,
                "reversal_id": existing["id"],
                "conversion_id": conversion_id,
                "nostr_event_id": existing["nostr_event_id"],
                "nostr_event": json.loads(existing["nostr_event_json"]),
            }
        click = asdict(c.execute(text("SELECT * FROM clicks WHERE id=:id"), {"id": conversion["click_id"]}).fetchone())
        enrollment = asdict(c.execute(text("SELECT * FROM enrollments WHERE ref_code=:ref"), {"ref": click["ref_code"] if click else None}).fetchone()) if click else None
        if not campaign or not enrollment:
            raise HTTPException(400, "conversion is missing campaign/enrollment context")
        merchant_hex = campaign.get("merchant_pubkey_hex") or normalize_pubkey(campaign["merchant_pubkey"], "merchant_pubkey")["hex"]
        affiliate_hex = enrollment.get("affiliate_pubkey_hex") or normalize_pubkey(conversion["affiliate_pubkey"], "affiliate_pubkey")["hex"]
        tags = [
            ["v", SCHEMA_VERSION],
            ["type", "affiliate_reversal"],
            ["e", conversion["nostr_event_id"]],
            ["p", merchant_hex, "", "merchant"],
            ["p", affiliate_hex, "", "affiliate"],
            ["campaign", conversion["campaign_id"]],
            ["reason", reason],
            ["reversed_at", str(int(time.time()))],
        ]
        if body.refund_sats is not None:
            tags.append(["refund_sats", str(body.refund_sats)])
        reversal_id = hid("rev")
        event = build_nostr_event(REVERSAL_KIND, tags, "")
        relay_results = publish_event(event)
        persist_nostr_event(c, event, "reversal", reversal_id, relay_results)
        c.execute(
            text("""
                INSERT INTO reversals (id, conversion_id, reason, refund_sats, nostr_event_id, nostr_event_json, created_at)
                VALUES (:id, :conversion_id, :reason, :refund_sats, :nostr_event_id, :nostr_event_json, :created_at)
            """),
            {"id": reversal_id, "conversion_id": conversion_id, "reason": reason, "refund_sats": body.refund_sats, "nostr_event_id": event["id"], "nostr_event_json": json.dumps(event), "created_at": now()},
        )
        c.execute(text("UPDATE conversions SET status='reversed' WHERE id=:id"), {"id": conversion_id})
        apply_reversal_to_payout(c, conversion)
    return {
        "ok": True,
        "duplicate": False,
        "reversal_id": reversal_id,
        "conversion_id": conversion_id,
        "reason": reason,
        "refund_sats": body.refund_sats,
        "nostr_event_id": event["id"],
        "nostr_event": event,
        "relay_results": relay_results,
    }


@app.post("/clicks/simulate")
def simulate_click(body: SimulateClickIn) -> dict[str, Any]:
    """Dashboard helper: create a click without following a browser redirect."""
    init_db()
    with engine().begin() as c:
        enr = asdict(c.execute(text("SELECT * FROM enrollments WHERE ref_code=:ref"), {"ref": body.ref_code}).fetchone())
        if not enr:
            raise HTTPException(404, "ref code not found")
        camp = asdict(c.execute(text("SELECT * FROM campaigns WHERE id=:id"), {"id": enr["campaign_id"]}).fetchone())
        if enr.get("status") != "approved":
            raise HTTPException(409, "enrollment is not approved")
        if not camp or camp.get("status") != "active":
            raise HTTPException(409, "campaign is not active")
        click_id = hid("clk")
        c.execute(
            text(
                """
                INSERT INTO clicks (id, ref_code, campaign_id, affiliate_pubkey, ip_hash,
                user_agent_hash, landing_url, created_at)
                VALUES (:id, :ref_code, :campaign_id, :affiliate_pubkey, :ip_hash,
                :user_agent_hash, :landing_url, :created_at)
                """
            ),
            {
                "id": click_id,
                "ref_code": body.ref_code,
                "campaign_id": enr["campaign_id"],
                "affiliate_pubkey": enr["affiliate_pubkey"],
                "ip_hash": sha("dashboard-demo-ip"),
                "user_agent_hash": sha("dashboard-demo-ua"),
                "landing_url": camp["destination_url"],
                "created_at": now(),
            },
        )
    redirect_url = add_query_params(camp["destination_url"], {"bb_click_id": click_id, "bb_ref": body.ref_code})
    return {"click_id": click_id, "ref_code": body.ref_code, "campaign_id": enr["campaign_id"], "affiliate_pubkey": enr["affiliate_pubkey"], "redirect_url": redirect_url}


def _tracking_ref(payload: Any) -> str:
    return safe_text(
        getattr(payload, "bb_ref", None)
        or getattr(payload, "bumbei_ref", None)
        or getattr(payload, "affiliate", None)
        or getattr(payload, "ref", None),
        200,
    )


def _tracking_click_id(payload: Any) -> str:
    return safe_text(getattr(payload, "bb_click_id", None) or getattr(payload, "click_id", None), 120)


def _request_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    return forwarded or (request.client.host if request.client else "unknown")


def _tracking_safe_url(value: Any, max_length: int) -> str | None:
    raw = safe_text(value, max_length)
    if not raw:
        return None
    try:
        parsed = urlparse(raw)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))


def store_tracking_event(kind: str, body: BrowserEventIn | BrowserConversionIn, request: Request) -> dict[str, Any]:
    init_db()
    ref_code = _tracking_ref(body)
    click_id = _tracking_click_id(body)
    if not ref_code and not click_id:
        raise HTTPException(400, "missing bb_ref or bb_click_id")

    event_id = hid("evt")
    user_agent = safe_text(getattr(body, "user_agent", None) or request.headers.get("user-agent", ""), 500)
    order_id = safe_text(getattr(body, "order_id", None), 300)
    checkout_token = safe_text(getattr(body, "checkout_token", None), 300)
    total_price = getattr(body, "total_price", None)
    try:
        order_total = float(total_price) if total_price not in (None, "") else None
    except (TypeError, ValueError):
        order_total = None

    with engine().begin() as c:
        click = asdict(c.execute(text("SELECT * FROM clicks WHERE id=:id"), {"id": click_id}).fetchone()) if click_id else None
        if click:
            if ref_code and ref_code != click["ref_code"]:
                raise HTTPException(400, "bb_ref does not match bb_click_id")
            ref_code = click["ref_code"]
        raw_metadata = getattr(body, "metadata", None) or {}
        safe_metadata = {
            key: safe_text(raw_metadata.get(key), 200)
            for key in ("event_id", "source")
            if safe_text(raw_metadata.get(key), 200)
        }
        payload = {
            "type": safe_text(getattr(body, "type", None) or kind, 80),
            "shop": safe_text(getattr(body, "shop", None), 120) or None,
            "bb_ref": ref_code or None,
            "bb_click_id": click_id or None,
            "path": safe_text(getattr(body, "path", None), 1000) or None,
            "order_total": order_total,
            "currency": safe_text(getattr(body, "currency", None), 20).upper() or None,
            "ts": safe_text(getattr(body, "ts", None), 80) or None,
            "metadata": safe_metadata,
        }
        payload = {key: value for key, value in payload.items() if value not in (None, {}, "")}
        c.execute(
            text(
                """
                INSERT INTO tracking_events (id, kind, event_type, ref_code, click_id, shop, url, path,
                referrer, order_id_hash, order_name, checkout_token_hash, order_total, currency,
                user_agent_hash, ip_hash, payload_json, created_at)
                VALUES (:id, :kind, :event_type, :ref_code, :click_id, :shop, :url, :path,
                :referrer, :order_id_hash, :order_name, :checkout_token_hash, :order_total, :currency,
                :user_agent_hash, :ip_hash, :payload_json, :created_at)
                """
            ),
            {
                "id": event_id,
                "kind": kind,
                "event_type": safe_text(getattr(body, "type", None) or kind, 80),
                "ref_code": ref_code or None,
                "click_id": click_id or None,
                "shop": safe_text(getattr(body, "shop", None), 120) or None,
                "url": _tracking_safe_url(getattr(body, "url", None), 3000),
                "path": safe_text(getattr(body, "path", None), 1000) or None,
                "referrer": _tracking_safe_url(getattr(body, "referrer", None), 2000),
                "order_id_hash": sha(order_id) if order_id else None,
                "order_name": safe_text(getattr(body, "order_name", None), 200) or None,
                "checkout_token_hash": sha(checkout_token) if checkout_token else None,
                "order_total": order_total,
                "currency": safe_text(getattr(body, "currency", None), 20).upper() or None,
                "user_agent_hash": sha(user_agent) if user_agent else None,
                "ip_hash": sha(_request_ip(request)),
                "payload_json": json.dumps(payload, sort_keys=True, default=str),
                "created_at": now(),
            },
        )
    return {
        "ok": True,
        "event_id": event_id,
        "kind": kind,
        "type": safe_text(getattr(body, "type", None) or kind, 80),
        "bb_ref": ref_code or None,
        "bb_click_id": click_id or None,
    }


@app.post("/v1/events", tags=["Tracking"])
def create_browser_event(body: BrowserEventIn, request: Request) -> dict[str, Any]:
    """Record a browser-side landing/page-view event from a merchant storefront."""
    return store_tracking_event("track", body, request)


@app.post("/v1/conversions", tags=["Tracking"])
def create_browser_conversion(body: BrowserConversionIn, request: Request) -> dict[str, Any]:
    """Record a browser/pixel conversion signal. Use /merchant/conversions for payout-grade server-side proofs."""
    result = store_tracking_event("conversion", body, request)
    result["note"] = "browser conversion stored; use /merchant/conversions for payout-grade Nostr proof"
    return result


@app.get("/v1/tracking/status", tags=["Tracking"])
def tracking_status(limit: int = 10) -> dict[str, Any]:
    """Safe aggregate debug status for browser tracking events."""
    init_db()
    limit = max(0, min(limit, 50))
    with engine().connect() as c:
        total = c.execute(text("SELECT COUNT(*) FROM tracking_events")).scalar_one()
        counts = {r._mapping["kind"]: r._mapping["count"] for r in c.execute(text("SELECT kind, COUNT(*) AS count FROM tracking_events GROUP BY kind")).fetchall()}
        recent = [dict(r._mapping) for r in c.execute(
            text("""
                SELECT id, kind, event_type, ref_code AS bb_ref, click_id AS bb_click_id, shop, path, created_at
                FROM tracking_events
                ORDER BY created_at DESC
                LIMIT :limit
            """),
            {"limit": limit},
        ).fetchall()]
    return {"ok": True, "total_events": total, "counts": counts, "recent": recent, "cors_origins": tracking_cors_origins()}


@app.post("/bumbei/track", include_in_schema=False)
def legacy_bumbei_track(body: BrowserEventIn, request: Request) -> dict[str, Any]:
    """Backward-compatible alias for storefronts still using the original POC route."""
    return create_browser_event(body, request)


@app.post("/bumbei/conversion", include_in_schema=False)
def legacy_bumbei_conversion(body: BrowserConversionIn, request: Request) -> dict[str, Any]:
    """Backward-compatible alias for pixels still using the original POC route."""
    return create_browser_conversion(body, request)


@app.get("/bumbei/status", include_in_schema=False)
def legacy_bumbei_status(limit: int = 10) -> dict[str, Any]:
    """Backward-compatible alias for the original POC debug route."""
    return tracking_status(limit)


def dashboard_data() -> dict[str, Any]:
    init_db()
    with engine().connect() as c:
        counts = {
            "campaigns": c.execute(text("SELECT COUNT(*) FROM campaigns")).scalar_one(),
            "enrollments": c.execute(text("SELECT COUNT(*) FROM enrollments")).scalar_one(),
            "clicks": c.execute(text("SELECT COUNT(*) FROM clicks")).scalar_one(),
            "conversions": c.execute(text("SELECT COUNT(*) FROM conversions")).scalar_one(),
            "pending_sats": c.execute(
                text("SELECT COALESCE(SUM(amount_sats), 0) FROM payouts WHERE state NOT IN ('SETTLED','PUBLISHED','CANCELLED')")
            ).scalar_one(),
            "nostr_events": c.execute(text("SELECT COUNT(*) FROM nostr_events")).scalar_one(),
            "published_events": c.execute(text("SELECT COUNT(*) FROM nostr_events WHERE relay_status='published'")).scalar_one(),
            "pending_events": c.execute(text("SELECT COUNT(*) FROM nostr_events WHERE relay_status<>'published'")).scalar_one(),
            "failed_relays": c.execute(text("SELECT COUNT(*) FROM nostr_event_relays WHERE status='failed'")).scalar_one(),
            "actionable_payouts": c.execute(
                text("SELECT COUNT(*) FROM payouts WHERE state IN ('PAYABLE','FAILED','UNKNOWN')")
            ).scalar_one(),
        }
        campaigns = [dict(r._mapping) for r in c.execute(text("SELECT id, merchant_pubkey, name, commission_bps, window_days, destination_url, nostr_event_id, created_at FROM campaigns ORDER BY created_at DESC LIMIT 10")).fetchall()]
        enrollments = [dict(r._mapping) for r in c.execute(text("SELECT id, campaign_id, affiliate_pubkey, lightning_address, ref_code, nostr_event_id, created_at FROM enrollments ORDER BY created_at DESC LIMIT 10")).fetchall()]
        clicks = [dict(r._mapping) for r in c.execute(text("SELECT id, ref_code, campaign_id, affiliate_pubkey, landing_url, created_at FROM clicks ORDER BY created_at DESC LIMIT 10")).fetchall()]
        conversions = [dict(r._mapping) for r in c.execute(text("SELECT id, click_id, campaign_id, affiliate_pubkey, order_total, currency, commission_sats, status, nostr_event_id, created_at FROM conversions ORDER BY created_at DESC LIMIT 10")).fetchall()]
        events = [dict(r._mapping) for r in c.execute(text("SELECT event_id, kind, pubkey, entity_type, entity_id, relay_status, created_at, published_at FROM nostr_events ORDER BY created_at DESC LIMIT 12")).fetchall()]
        relay_rows = [dict(r._mapping) for r in c.execute(text("SELECT event_id, relay_url, status, error, created_at FROM nostr_event_relays ORDER BY created_at DESC LIMIT 60")).fetchall()]
    relays_by_event: dict[str, list[dict[str, Any]]] = {}
    for row in relay_rows:
        relays_by_event.setdefault(row["event_id"], []).append(row)
    for event in events:
        event["relays"] = relays_by_event.get(event["event_id"], [])
    service_health = health()
    attention: list[dict[str, Any]] = []
    if not service_health["nostr_publish"]:
        attention.append({
            "severity": "warning",
            "title": "Publicación Nostr desactivada",
            "detail": "Las pruebas se firman localmente, pero no se envían a relays.",
        })
    if counts["pending_events"]:
        attention.append({
            "severity": "warning",
            "title": "Publicación pendiente",
            "detail": f"{counts['pending_events']} evento(s) todavía no figuran como publicados.",
        })
    if counts["failed_relays"]:
        attention.append({
            "severity": "danger",
            "title": "Entregas a relays fallidas",
            "detail": f"{counts['failed_relays']} intento(s) requieren revisión.",
        })
    if counts["actionable_payouts"]:
        attention.append({
            "severity": "warning",
            "title": "Payouts que requieren atención",
            "detail": f"{counts['actionable_payouts']} payout(s) están listos, fallidos o con estado desconocido.",
        })
    return {
        "health": service_health,
        "counts": counts,
        "attention": attention,
        "snapshot_at": now(),
        "campaigns": campaigns,
        "enrollments": enrollments,
        "clicks": clicks,
        "conversions": conversions,
        "events": events,
    }


@contextmanager
def merchant_conversion_lock(order_key: str):
    """Serialize one merchant order locally and across PostgreSQL workers."""
    lock_digest = order_key.rsplit(":", 1)[-1]
    lock = _MERCHANT_CONVERSION_LOCKS[int(lock_digest[:8], 16) % len(_MERCHANT_CONVERSION_LOCKS)]
    with lock:
        if not database_url().startswith("postgresql"):
            yield
            return
        with engine().connect().execution_options(isolation_level="AUTOCOMMIT") as c:
            c.execute(text("SELECT pg_advisory_lock(hashtextextended(:key, 0))"), {"key": order_key})
            try:
                yield
            finally:
                c.execute(text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"), {"key": order_key})


def process_merchant_conversion(body: MerchantConversionIn, authorized_merchant_hex: str) -> dict[str, Any]:
    merchant_order_key = sha(f"{authorized_merchant_hex}:{body.order_id}")
    with merchant_conversion_lock(merchant_order_key):
        return _process_merchant_conversion_locked(body, authorized_merchant_hex, merchant_order_key)


def _process_merchant_conversion_locked(
    body: MerchantConversionIn,
    authorized_merchant_hex: str,
    merchant_order_key: str,
) -> dict[str, Any]:
    """Create an idempotent, payout-grade conversion from a trusted merchant signal."""
    init_db()
    order_id_hash = sha(body.order_id)
    payload_material = {
        "click_id": body.bb_click_id,
        "order_total": decimal_text(body.order_total),
        "currency": body.currency.upper().strip(),
    }
    payload_hash = sha(json.dumps(payload_material, sort_keys=True, separators=(",", ":")))
    select_existing = """
        SELECT v.id, v.merchant_order_key, v.idempotency_payload_hash, v.click_id,
               v.order_total, v.order_total_decimal, v.currency, v.nostr_event_id,
               v.nostr_event_json, v.order_total_sats, v.commission_sats,
               v.btc_usd_rate, v.sats_per_usd, v.rate_source, v.rate_observed_at,
               v.rate_fetched_at, v.rate_stale, c.merchant_pubkey, c.merchant_pubkey_hex,
               ne.relay_status
        FROM conversions v JOIN campaigns c ON c.id=v.campaign_id
        LEFT JOIN nostr_events ne ON ne.event_id=v.nostr_event_id
    """
    with engine().connect() as c:
        existing = asdict(c.execute(
            text(select_existing + " WHERE v.merchant_order_key=:key LIMIT 1"),
            {"key": merchant_order_key},
        ).fetchone())
        if not existing:
            legacy_candidates = [dict(row._mapping) for row in c.execute(
                text(select_existing + " WHERE v.merchant_order_key IS NULL AND v.order_id_hash=:h"),
                {"h": order_id_hash},
            ).fetchall()]
            for candidate in legacy_candidates:
                try:
                    require_merchant_ownership(candidate, authorized_merchant_hex)
                except HTTPException:
                    continue
                existing = candidate
                break
    if existing:
        require_merchant_ownership(existing, authorized_merchant_hex)
        stored_payload_hash = existing.get("idempotency_payload_hash")
        if not stored_payload_hash:
            stored_material = {
                "click_id": existing["click_id"],
                "order_total": decimal_text(existing.get("order_total_decimal") or existing["order_total"]),
                "currency": str(existing["currency"]).upper().strip(),
            }
            stored_payload_hash = sha(json.dumps(stored_material, sort_keys=True, separators=(",", ":")))
        if not hmac.compare_digest(stored_payload_hash, payload_hash):
            raise HTTPException(409, "idempotency key reused with different conversion payload")
        rate_source = existing.get("rate_source") or "legacy_unknown"
        if not existing.get("merchant_order_key") or not existing.get("idempotency_payload_hash"):
            with engine().begin() as c:
                c.execute(
                    text("""
                        UPDATE conversions
                        SET merchant_order_key=COALESCE(merchant_order_key, :key),
                            idempotency_payload_hash=COALESCE(idempotency_payload_hash, :payload_hash)
                        WHERE id=:id
                    """),
                    {"key": merchant_order_key, "payload_hash": stored_payload_hash, "id": existing["id"]},
                )
        retry_conversion_outbox(existing["id"])
        return {
            "ok": True,
            "duplicate": True,
            "conversion_id": existing["id"],
            "nostr_event_id": existing["nostr_event_id"],
            "order_total_sats": existing.get("order_total_sats"),
            "commission_sats": existing.get("commission_sats"),
            "sats_per_usd_source": "server" if rate_source not in {"not_required", "legacy_unknown"} else rate_source,
            "btc_usd_rate": existing.get("btc_usd_rate"),
            "sats_per_usd": existing.get("sats_per_usd"),
            "rate_source": rate_source,
            "rate_observed_at": existing.get("rate_observed_at"),
            "rate_fetched_at": existing.get("rate_fetched_at"),
            "rate_stale": bool(existing.get("rate_stale")),
            "receipt_url": f"{BASE_URL}/flows/{existing['id']}/receipt",
            "json_receipt_url": f"{BASE_URL}/flows/{existing['id']}",
            "payload_hash": stored_payload_hash,
        }
    with engine().connect() as c:
        click_campaign = asdict(c.execute(text("""
            SELECT c.merchant_pubkey, c.merchant_pubkey_hex
            FROM clicks cl JOIN campaigns c ON c.id=cl.campaign_id
            WHERE cl.id=:click_id
        """), {"click_id": body.bb_click_id}).fetchone())
    if not click_campaign:
        raise HTTPException(404, "click not found")
    require_merchant_ownership(click_campaign, authorized_merchant_hex)
    conversion = _create_conversion(
        ConversionIn(
            order_id=body.order_id,
            click_id=body.bb_click_id,
            order_total=body.order_total,
            currency=body.currency,
        ),
        merchant_order_key=merchant_order_key,
        idempotency_payload_hash=payload_hash,
    )
    return {
        "ok": True,
        "duplicate": False,
        "conversion_id": conversion["conversion_id"],
        "nostr_event_id": conversion["nostr_event_id"],
        "order_total_sats": conversion["order_total_sats"],
        "sats_per_usd_source": "server" if body.currency.upper() in {"USD", "USDC"} else "not_required",
        "btc_usd_rate": conversion["btc_usd_rate"],
        "sats_per_usd": conversion["sats_per_usd"],
        "rate_source": conversion["rate_source"],
        "rate_observed_at": conversion["rate_observed_at"],
        "rate_fetched_at": conversion["rate_fetched_at"],
        "rate_stale": conversion["rate_stale"],
        "commission_sats": conversion["commission_sats"],
        "payout_status": conversion["payout_status"],
        "receipt_url": f"{BASE_URL}/flows/{conversion['conversion_id']}/receipt",
        "json_receipt_url": f"{BASE_URL}/flows/{conversion['conversion_id']}",
        "payload_hash": payload_hash,
        "relay_results": conversion["relay_results"],
    }


@app.post("/merchant/conversions")
def merchant_conversion_webhook(body: MerchantConversionIn, authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    """Merchant-facing server-to-server conversion webhook.

    The merchant sends back the bb_click_id captured from the referral redirect. Raw
    order/customer data is not published to Nostr; the conversion proof stores hashes.
    """
    authorized_merchant_hex = require_merchant_api_key(authorization)
    return process_merchant_conversion(body, authorized_merchant_hex)


def shopify_webhook_secret() -> str:
    return os.getenv("SHOPIFY_WEBHOOK_SECRET") or os.getenv("SHOPIFY_SECRET", "")


def normalized_shopify_store_domain() -> str:
    raw = os.getenv("SHOPIFY_STORE_DOMAIN", "").strip().lower().rstrip("/")
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    return raw


def shopify_installation_snippets(base_url: str, shop_domain: str) -> dict[str, str]:
    endpoint = base_url.rstrip("/")
    parsed_endpoint = urlparse(endpoint)
    if parsed_endpoint.scheme not in {"http", "https"} or not parsed_endpoint.hostname:
        endpoint = "http://localhost:8000"
    safe_shop = shop_domain if re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,252}", shop_domain or "") else "<store>.myshopify.com"
    events_url_literal = json.dumps(f"{endpoint}/v1/events")
    conversions_url_literal = json.dumps(f"{endpoint}/v1/conversions")
    shop_literal = json.dumps(safe_shop)
    theme_script = """<script>
(function () {
  function param(name) { return new URLSearchParams(window.location.search).get(name); }
  function setCookie(name, value) {
    if (!value) return;
    var expires = new Date(Date.now() + 30 * 24 * 60 * 60 * 1000).toUTCString();
    document.cookie = name + "=" + encodeURIComponent(value) + "; expires=" + expires + "; path=/; SameSite=Lax";
  }
  function getCookie(name) {
    var match = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
    return match ? decodeURIComponent(match[1]) : "";
  }

  var ref = param("bb_ref") || param("bumbei_ref") || param("ref") || param("affiliate");
  var clickId = param("bb_click_id") || param("click_id");
  if (ref) { setCookie("bb_ref", ref); setCookie("bumbei_ref", ref); }
  if (clickId) setCookie("bb_click_id", clickId);

  ref = ref || getCookie("bb_ref") || getCookie("bumbei_ref");
  clickId = clickId || getCookie("bb_click_id");
  if (!ref && !clickId) return;

  fetch("/cart/update.js", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ attributes: { bb_ref: ref || "", bb_click_id: clickId || "" } })
  }).catch(function () {});

  fetch(__EVENTS_URL__, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    keepalive: true,
    body: JSON.stringify({
      type: "page_view",
      shop: __SHOP_LITERAL__,
      bb_ref: ref || null,
      bb_click_id: clickId || null,
      path: window.location.pathname,
      ts: new Date().toISOString()
    })
  }).catch(function () {});
})();
</script>""".replace("__EVENTS_URL__", events_url_literal).replace("__SHOP_LITERAL__", shop_literal)
    custom_pixel = """analytics.subscribe("checkout_completed", async (event) => {
  const checkout = event.data && event.data.checkout ? event.data.checkout : {};
  async function cookie(names) {
    for (const name of names) {
      const value = await browser.cookie.get(name);
      if (value) return value;
    }
    return null;
  }

  const ref = await cookie(["bb_ref", "bumbei_ref"]);
  const clickId = await cookie(["bb_click_id", "click_id"]);
  if (!ref && !clickId) return;

  await fetch(__CONVERSIONS_URL__, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      type: "checkout_completed",
      shop: __SHOP_LITERAL__,
      bb_ref: ref,
      bb_click_id: clickId,
      order_id: checkout.order && checkout.order.id ? String(checkout.order.id) : null,
      total_price: checkout.totalPrice && checkout.totalPrice.amount ? checkout.totalPrice.amount : null,
      currency: checkout.currencyCode || null,
      ts: event.timestamp || new Date().toISOString(),
      metadata: { event_id: event.id || null, source: "shopify_custom_pixel" }
    })
  });
});""".replace("__CONVERSIONS_URL__", conversions_url_literal).replace("__SHOP_LITERAL__", shop_literal)
    return {
        "theme_script": theme_script,
        "custom_pixel": custom_pixel,
        "webhook_url": f"{endpoint}/shopify/webhooks/orders-paid",
        "shop_domain": safe_shop,
    }


def verify_shopify_webhook(raw_body: bytes, signature: str) -> None:
    secret = shopify_webhook_secret()
    if not secret:
        raise HTTPException(503, "Shopify webhook secret is not configured")
    expected = base64.b64encode(hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()).decode()
    if not signature or not hmac.compare_digest(expected, signature):
        raise HTTPException(401, "invalid Shopify webhook signature")


def shopify_note_attributes(payload: dict[str, Any]) -> dict[str, str]:
    attributes: dict[str, str] = {}
    for item in payload.get("note_attributes") or []:
        if not isinstance(item, dict):
            continue
        name = safe_text(item.get("name"), 120)
        value = safe_text(item.get("value"), 500)
        if name and value:
            attributes[name] = value
    return attributes


def record_shopify_webhook_receipt(
    webhook_id: str, shop: str, topic: str, status: str, reason: Optional[str] = None
) -> None:
    """Store minimal signed-delivery telemetry without retaining Shopify payload or order data."""
    init_db()
    timestamp = now()
    with engine().begin() as c:
        c.execute(
            text(
                """
                INSERT INTO shopify_webhook_receipts
                (webhook_id, shop_domain, topic, status, reason, created_at, updated_at)
                VALUES (:webhook_id, :shop, :topic, :status, :reason, :timestamp, :timestamp)
                ON CONFLICT(webhook_id) DO UPDATE SET
                    status=:status, reason=:reason, updated_at=:timestamp
                """
            ),
            {
                "webhook_id": webhook_id,
                "shop": shop,
                "topic": topic,
                "status": status,
                "reason": safe_text(reason, 300) if reason else None,
                "timestamp": timestamp,
            },
        )


def enqueue_shopify_paid_order(
    *, webhook_id: str, order_key: str, shop: str, topic: str, click_id: str, order_total: Decimal, currency: str
) -> tuple[dict[str, Any], bool, bool]:
    """Persist a minimal webhook inbox row and atomically claim a new Shopify order."""
    init_db()
    created_at = now()
    with engine().begin() as c:
        inserted = c.execute(
            text(
                """
                INSERT INTO shopify_webhook_deliveries
                (webhook_id, order_key, shop_domain, topic, click_id, order_total, order_total_decimal, currency,
                 status, conversion_id, error, created_at, processed_at)
                VALUES (:webhook_id, :order_key, :shop_domain, :topic, :click_id, :order_total, :order_total_decimal,
                        :currency, 'pending', NULL, NULL, :created_at, NULL)
                ON CONFLICT(order_key) DO NOTHING
                """
            ),
            {
                "webhook_id": webhook_id,
                "order_key": order_key,
                "shop_domain": shop,
                "topic": topic,
                "click_id": click_id,
                "order_total": float(order_total),
                "order_total_decimal": decimal_text(order_total),
                "currency": currency,
                "created_at": created_at,
            },
        ).rowcount == 1
        row = asdict(c.execute(text("SELECT * FROM shopify_webhook_deliveries WHERE order_key=:key"), {"key": order_key}).fetchone())
        conflict = bool(
            row
            and (
                row["click_id"] != click_id
                or Decimal(row.get("order_total_decimal") or str(row["order_total"])) != order_total
                or row["currency"] != currency
                or row["shop_domain"] != shop
            )
        )
        should_process = inserted
        if row and row["status"] == "failed" and not conflict:
            c.execute(
                text("UPDATE shopify_webhook_deliveries SET status='pending', error=NULL WHERE order_key=:key"),
                {"key": order_key},
            )
            row["status"] = "pending"
            should_process = True
    return row, should_process, conflict


def process_shopify_delivery(order_key: str) -> None:
    """Process one claimed Shopify order after the HTTP response has been sent."""
    with engine().begin() as c:
        claimed = c.execute(
            text(
                """
                UPDATE shopify_webhook_deliveries
                SET status='processing', error=NULL
                WHERE order_key=:key AND status='pending'
                """
            ),
            {"key": order_key},
        ).rowcount == 1
        row = asdict(c.execute(text("SELECT * FROM shopify_webhook_deliveries WHERE order_key=:key"), {"key": order_key}).fetchone())
    if not claimed or not row:
        return

    try:
        result = process_merchant_conversion(
            MerchantConversionIn(
                order_id=order_key,
                bb_click_id=row["click_id"],
                order_total=Decimal(row.get("order_total_decimal") or str(row["order_total"])),
                currency=row["currency"],
                metadata={"platform": "shopify", "shop": row["shop_domain"], "topic": row["topic"]},
            ),
            configured_merchant_pubkey_hex(),
        )
    except Exception as exc:
        with engine().begin() as c:
            c.execute(
                text(
                    """
                    UPDATE shopify_webhook_deliveries
                    SET status='failed', error=:error, processed_at=:processed_at
                    WHERE order_key=:key
                    """
                ),
                {"key": order_key, "error": safe_text(str(exc), 500), "processed_at": now()},
            )
        record_shopify_webhook_receipt(row["webhook_id"], row["shop_domain"], row["topic"], "failed", str(exc))
        return

    with engine().begin() as c:
        c.execute(
            text(
                """
                UPDATE shopify_webhook_deliveries
                SET status='processed', conversion_id=:conversion_id, error=NULL, processed_at=:processed_at
                WHERE order_key=:key
                """
            ),
            {"key": order_key, "conversion_id": result["conversion_id"], "processed_at": now()},
        )
    record_shopify_webhook_receipt(row["webhook_id"], row["shop_domain"], row["topic"], "processed")


@app.post("/shopify/webhooks/orders-paid", tags=["Shopify"])
async def shopify_orders_paid_webhook(request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    """Authenticate and enqueue Shopify's orders/paid webhook for authoritative processing."""
    max_body_bytes = 5 * 1024 * 1024
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_body_bytes:
                raise HTTPException(413, "Shopify webhook body is too large")
        except ValueError:
            raise HTTPException(400, "invalid Content-Length")
    raw_body = await request.body()
    if len(raw_body) > max_body_bytes:
        raise HTTPException(413, "Shopify webhook body is too large")
    verify_shopify_webhook(raw_body, request.headers.get("x-shopify-hmac-sha256", ""))

    topic = request.headers.get("x-shopify-topic", "").strip().lower()
    shop = request.headers.get("x-shopify-shop-domain", "").strip().lower()
    webhook_id = request.headers.get("x-shopify-webhook-id", "").strip()
    if topic != "orders/paid":
        raise HTTPException(400, "unexpected Shopify webhook topic")
    if not webhook_id:
        raise HTTPException(400, "missing Shopify webhook id")

    configured_shop = normalized_shopify_store_domain()
    if not configured_shop:
        raise HTTPException(503, "Shopify store domain is not configured")
    if shop != configured_shop:
        raise HTTPException(403, "unexpected Shopify store domain")

    try:
        payload = json.loads(raw_body)
    except (TypeError, ValueError):
        raise HTTPException(400, "invalid Shopify webhook JSON")
    if not isinstance(payload, dict):
        raise HTTPException(400, "invalid Shopify webhook payload")

    record_shopify_webhook_receipt(webhook_id, shop, topic, "received")
    attributes = shopify_note_attributes(payload)
    click_id = attributes.get("bb_click_id") or attributes.get("click_id")
    if not click_id:
        record_shopify_webhook_receipt(webhook_id, shop, topic, "ignored", "missing affiliate attribution")
        return {
            "ok": True,
            "ignored": True,
            "reason": "missing affiliate attribution",
            "shop": shop,
            "topic": topic,
            "webhook_id": webhook_id,
        }

    order_id = safe_text(payload.get("id"), 300)
    currency = safe_text(payload.get("currency"), 20).upper()
    try:
        order_total = Decimal(str(payload.get("total_price")))
    except (InvalidOperation, TypeError, ValueError):
        raise HTTPException(422, "invalid Shopify order total")
    if not order_id or not currency or not order_total.is_finite() or order_total <= 0:
        raise HTTPException(422, "incomplete Shopify paid order")

    order_key = sha(f"shopify:{shop}:{order_id}")
    delivery, should_process, conflict = enqueue_shopify_paid_order(
        webhook_id=webhook_id,
        order_key=order_key,
        shop=shop,
        topic=topic,
        click_id=click_id,
        order_total=order_total,
        currency=currency,
    )
    if conflict:
        record_shopify_webhook_receipt(webhook_id, shop, topic, "conflict", "duplicate order payload mismatch")
        return {
            "ok": True,
            "ignored": False,
            "duplicate": True,
            "conflict": True,
            "status": delivery["status"],
            "conversion_id": delivery.get("conversion_id"),
            "shop": shop,
            "topic": topic,
        }
    record_shopify_webhook_receipt(
        webhook_id,
        shop,
        topic,
        "queued" if should_process else "duplicate",
    )
    if should_process:
        background_tasks.add_task(process_shopify_delivery, order_key)
    return {
        "ok": True,
        "ignored": False,
        "duplicate": not should_process,
        "conflict": False,
        "queued": delivery["status"] != "processed",
        "status": delivery["status"],
        "conversion_id": delivery.get("conversion_id"),
        "shop": shop,
        "topic": topic,
        "webhook_id": webhook_id,
    }


@app.get("/shopify/webhooks/status", tags=["Shopify"])
def shopify_webhook_status() -> dict[str, Any]:
    """Expose safe webhook readiness and aggregate inbox state without credentials or order data."""
    init_db()
    with engine().connect() as c:
        counts = {
            row._mapping["status"]: row._mapping["count"]
            for row in c.execute(
                text("SELECT status, COUNT(*) AS count FROM shopify_webhook_deliveries GROUP BY status")
            ).fetchall()
        }
        receipt_counts = {
            row._mapping["status"]: row._mapping["count"]
            for row in c.execute(
                text("SELECT status, COUNT(*) AS count FROM shopify_webhook_receipts GROUP BY status")
            ).fetchall()
        }
        latest_row = c.execute(
            text(
                """
                SELECT webhook_id, topic, status, reason, created_at, updated_at
                FROM shopify_webhook_receipts
                ORDER BY updated_at DESC LIMIT 1
                """
            )
        ).fetchone()
        latest_receipt = asdict(latest_row) if latest_row else None
    return {
        "ok": True,
        "secret_configured": bool(shopify_webhook_secret()),
        "store_configured": bool(normalized_shopify_store_domain()),
        "topic": "orders/paid",
        "callback_url": f"{BASE_URL}/shopify/webhooks/orders-paid",
        "deliveries": counts,
        "receipts": receipt_counts,
        "latest_receipt": latest_receipt,
    }


@app.get("/affiliates/{affiliate_pubkey}")
def affiliate_summary(affiliate_pubkey: str) -> dict[str, Any]:
    return affiliate_public_data(affiliate_pubkey)


@app.get("/affiliates/{affiliate_pubkey}/summary")
def affiliate_public_summary(affiliate_pubkey: str) -> dict[str, Any]:
    return affiliate_public_data(affiliate_pubkey)


def affiliate_public_data(affiliate_pubkey: str) -> dict[str, Any]:
    init_db()
    identity = normalize_pubkey(affiliate_pubkey, "affiliate_pubkey")
    with engine().connect() as c:
        enrollments = [dict(r._mapping) for r in c.execute(
            text("""
                SELECT e.*, c.name AS campaign_name, c.merchant_pubkey, c.merchant_pubkey_hex, c.commission_bps, c.window_days, c.destination_url
                FROM enrollments e
                JOIN campaigns c ON c.id=e.campaign_id
                WHERE e.affiliate_pubkey=:npub OR e.affiliate_pubkey_hex=:hex OR e.affiliate_pubkey=:hex
                ORDER BY e.created_at DESC
            """),
            {"npub": identity["npub"], "hex": identity["hex"]},
        ).fetchall()]
        clicks = [dict(r._mapping) for r in c.execute(text("SELECT * FROM clicks WHERE affiliate_pubkey=:npub OR affiliate_pubkey=:hex ORDER BY created_at DESC"), {"npub": identity["npub"], "hex": identity["hex"]}).fetchall()]
        conversions = [dict(r._mapping) for r in c.execute(
            text("""
                SELECT v.*, c.name AS campaign_name, e.ref_code
                FROM conversions v
                LEFT JOIN campaigns c ON c.id=v.campaign_id
                LEFT JOIN clicks k ON k.id=v.click_id
                LEFT JOIN enrollments e ON e.ref_code=k.ref_code
                WHERE v.affiliate_pubkey=:npub OR v.affiliate_pubkey=:hex
                ORDER BY v.created_at DESC
            """),
            {"npub": identity["npub"], "hex": identity["hex"]},
        ).fetchall()]
        payouts = [dict(r._mapping) for r in c.execute(text("SELECT * FROM payouts WHERE affiliate_pubkey=:npub OR affiliate_pubkey=:hex ORDER BY created_at DESC"), {"npub": identity["npub"], "hex": identity["hex"]}).fetchall()]
        reversals = [dict(r._mapping) for r in c.execute(text("SELECT r.* FROM reversals r JOIN conversions v ON r.conversion_id=v.id WHERE v.affiliate_pubkey=:npub OR v.affiliate_pubkey=:hex ORDER BY r.created_at DESC"), {"npub": identity["npub"], "hex": identity["hex"]}).fetchall()]
        entity_ids = [e["id"] for e in enrollments] + [v["id"] for v in conversions] + [p["id"] for p in payouts] + [r["id"] for r in reversals]
        events: list[dict[str, Any]] = []
        relays_by_event: dict[str, list[dict[str, Any]]] = {}
        for entity_id in entity_ids:
            for ev in c.execute(text("SELECT event_id, kind, pubkey, entity_type, entity_id, relay_status, event_json, created_at, published_at FROM nostr_events WHERE entity_id=:id ORDER BY created_at DESC"), {"id": entity_id}).fetchall():
                evd = dict(ev._mapping)
                evd["event_json"] = json.loads(evd["event_json"])
                events.append(evd)
                relays_by_event[evd["event_id"]] = [dict(r._mapping) for r in c.execute(text("SELECT relay_url, status, error, created_at FROM nostr_event_relays WHERE event_id=:event_id"), {"event_id": evd["event_id"]}).fetchall()]
    for ev in events:
        ev["relays"] = relays_by_event.get(ev["event_id"], [])
    unique_campaigns = {e["campaign_id"] for e in enrollments}
    totals = {
        "campaigns": len(unique_campaigns),
        "enrollments": len(enrollments),
        "clicks": len(clicks),
        "conversions": len(conversions),
        "reversals": len(reversals),
        "commission_sats": sum(int(v["commission_sats"]) for v in conversions),
        "pending_sats": sum(int(p["amount_sats"]) for p in payouts if p["status"] == "pending"),
        "published_events": sum(1 for ev in events if ev["relay_status"] == "published"),
    }
    payouts = [public_payout_record(p) for p in payouts]
    enrollments = [public_enrollment_record(e) for e in enrollments]
    return {
        "identity": {"npub": identity["npub"], "hex": identity["hex"]},
        "affiliate_pubkey": identity["npub"],
        "enrollments": totals["enrollments"],
        "clicks": totals["clicks"],
        "conversions": totals["conversions"],
        "pending_sats": totals["pending_sats"],
        "conversion_rows": conversions,
        "payout_rows": payouts,
        "totals": totals,
        "enrollments": enrollments,
        "clicks": clicks,
        "conversions": conversions,
        "payouts": payouts,
        "reversals": reversals,
        "events": events,
        "links": {
            "profile": f"{BASE_URL}/affiliates/{identity['npub']}/profile",
            "summary": f"{BASE_URL}/affiliates/{identity['npub']}/summary",
        },
    }


@app.get("/affiliates/{affiliate_pubkey}/profile", response_class=HTMLResponse)
def affiliate_public_profile(request: Request, affiliate_pubkey: str) -> Response:
    data = affiliate_public_data(affiliate_pubkey)
    return templates.TemplateResponse(
        request=request,
        name="affiliate_public.html",
        context={**data, "short": _short, "status": _receipt_status},
    )

@app.get("/admin/campaigns/{campaign_id}/budget")
def campaign_budget_detail(campaign_id: str, authorization: Optional[str] = Header(None)) -> dict[str, Any]:
    require_payout_admin_key(authorization)
    init_db()
    with engine().begin() as c:
        campaign = c.execute(text("SELECT 1 FROM campaigns WHERE id=:id"), {"id": campaign_id}).fetchone()
        if not campaign:
            raise HTTPException(404, "campaign not found")
        budget = ensure_campaign_budget(c, campaign_id)
    return {"budget": budget, "available_sats": max(0, budget["budget_sats"] - budget["committed_sats"] - budget["settled_sats"])}


@app.put("/admin/campaigns/{campaign_id}/budget")
def update_campaign_budget(
    campaign_id: str,
    body: CampaignBudgetIn,
    authorization: Optional[str] = Header(None),
) -> dict[str, Any]:
    require_payout_admin_key(authorization)
    init_db()
    with engine().begin() as c:
        if not c.execute(text("SELECT 1 FROM campaigns WHERE id=:id"), {"id": campaign_id}).fetchone():
            raise HTTPException(404, "campaign not found")
        locked_campaign_budget(c, campaign_id)
        updated = c.execute(
            text("""
                UPDATE campaign_budgets SET budget_sats=:budget, updated_at=:updated_at
                WHERE campaign_id=:id AND :budget >= committed_sats + settled_sats
            """),
            {"id": campaign_id, "budget": body.budget_sats, "updated_at": now()},
        )
        if updated.rowcount != 1:
            raise HTTPException(409, "budget cannot be lower than committed plus settled sats")
        budget = asdict(c.execute(text("SELECT * FROM campaign_budgets WHERE campaign_id=:id"), {"id": campaign_id}).fetchone())
    return {"ok": True, "budget": budget}


@app.post("/admin/payouts/{payout_id}/release-hold")
def release_payout_hold(payout_id: str, authorization: Optional[str] = Header(None)) -> dict[str, Any]:
    require_payout_admin_key(authorization)
    init_db()
    with engine().begin() as c:
        suffix = " FOR UPDATE" if c.engine.dialect.name == "postgresql" else ""
        payout = asdict(c.execute(text(f"SELECT * FROM payouts WHERE id=:id{suffix}"), {"id": payout_id}).fetchone())
        if not payout:
            raise HTTPException(404, "payout not found")
        if payout["state"] == "PAYABLE":
            return {"ok": True, "duplicate": True, "payout_id": payout_id, "payout_state": "PAYABLE"}
        if payout["state"] != "ON_HOLD":
            raise HTTPException(409, f"payout state {payout['state']} is not ON_HOLD")
        conversion = asdict(c.execute(text("SELECT campaign_id, status FROM conversions WHERE id=:id"), {"id": payout["conversion_id"]}).fetchone())
        if conversion["status"] == "reversed":
            raise HTTPException(409, "reversed conversion cannot release a payout hold")
        obligation_sats = int(payout["amount_sats"]) + int(payout.get("fee_sats") or 0)
        claimed = c.execute(
            text("UPDATE payouts SET state='PAYABLE', status='pending', reserved_sats=:reserved_sats, last_error=NULL WHERE id=:id AND state='ON_HOLD'"),
            {"id": payout_id, "reserved_sats": obligation_sats},
        )
        if claimed.rowcount != 1:
            raise HTTPException(409, "payout hold was already released")
        if not reserve_campaign_budget(c, conversion["campaign_id"], payout_id, obligation_sats):
            raise HTTPException(409, "campaign budget is still insufficient")
    return {"ok": True, "duplicate": False, "payout_id": payout_id, "payout_state": "PAYABLE", "reserved_sats": obligation_sats}


@app.get("/admin/payouts/{payout_id}/attempts")
def payout_attempts(payout_id: str, authorization: Optional[str] = Header(None)) -> dict[str, Any]:
    require_payout_admin_key(authorization)
    init_db()
    with engine().connect() as c:
        if not c.execute(text("SELECT 1 FROM payouts WHERE id=:id"), {"id": payout_id}).fetchone():
            raise HTTPException(404, "payout not found")
        attempts = [dict(row._mapping) for row in c.execute(
            text("SELECT * FROM payment_attempts WHERE payout_id=:id ORDER BY attempt_number, created_at"),
            {"id": payout_id},
        ).fetchall()]
    for attempt in attempts:
        attempt.pop("preimage", None)
    return {"payout_id": payout_id, "attempts": attempts}


@app.get("/admin/payouts/{payout_id}/ledger")
def payout_ledger(payout_id: str, authorization: Optional[str] = Header(None)) -> dict[str, Any]:
    require_payout_admin_key(authorization)
    init_db()
    with engine().connect() as c:
        if not c.execute(text("SELECT 1 FROM payouts WHERE id=:id"), {"id": payout_id}).fetchone():
            raise HTTPException(404, "payout not found")
        entries = [dict(row._mapping) for row in c.execute(
            text("SELECT * FROM ledger_entries WHERE payout_id=:id ORDER BY created_at, id"),
            {"id": payout_id},
        ).fetchall()]
    debits = sum(row["amount_sats"] for row in entries if row["direction"] == "debit")
    credits = sum(row["amount_sats"] for row in entries if row["direction"] == "credit")
    return {"payout_id": payout_id, "balanced": debits == credits, "debits_sats": debits, "credits_sats": credits, "entries": entries}


@app.get("/admin/payment-rail/balance")
def payment_rail_balance(authorization: Optional[str] = Header(None)) -> dict[str, Any]:
    """Read-only provider balance check; never initiates a payment."""
    require_payout_admin_key(authorization)
    try:
        rail = configured_payment_rail()
        balance_sats = asyncio.run(rail.get_balance())
    except RuntimeError as exc:
        raise HTTPException(501, safe_text(str(exc), 300)) from exc
    except Exception as exc:
        raise HTTPException(502, "payment rail balance lookup failed") from exc
    return {"rail": safe_text(getattr(rail, "name", "unknown"), 50), "balance_sats": int(balance_sats)}


@app.get("/admin/payment-attempts/recovery")
def recoverable_payment_attempts(
    older_than_seconds: int = 60,
    authorization: Optional[str] = Header(None),
) -> dict[str, Any]:
    require_payout_admin_key(authorization)
    init_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max(1, older_than_seconds))).isoformat()
    with engine().connect() as c:
        attempts = [dict(row._mapping) for row in c.execute(
            text("""
                SELECT * FROM payment_attempts
                WHERE status IN ('PAYING','UNKNOWN') AND updated_at<=:cutoff
                ORDER BY updated_at
            """),
            {"cutoff": cutoff},
        ).fetchall()]
    for attempt in attempts:
        attempt.pop("preimage", None)
    return {"cutoff": cutoff, "attempts": attempts, "action": "reconcile before any retry"}


def _apply_provider_refresh_result(attempt_id: str, result: Any) -> dict[str, Any]:
    """Apply read-only provider evidence with an attempt+payout CAS; never sends payment."""
    with engine().begin() as c:
        suffix = " FOR UPDATE" if c.engine.dialect.name == "postgresql" else ""
        attempt = asdict(c.execute(text(f"SELECT * FROM payment_attempts WHERE id=:id{suffix}"), {"id": attempt_id}).fetchone())
        if not attempt or attempt["status"] != "UNKNOWN":
            raise HTTPException(409, "payment attempt is no longer UNKNOWN")
        payout = asdict(c.execute(text(f"SELECT * FROM payouts WHERE id=:id{suffix}"), {"id": attempt["payout_id"]}).fetchone())
        latest_id = c.execute(text("""
            SELECT id FROM payment_attempts WHERE payout_id=:payout_id AND kind=:kind
            ORDER BY attempt_number DESC, created_at DESC LIMIT 1
        """), {"payout_id": attempt["payout_id"], "kind": attempt["kind"]}).scalar_one()
        if (not payout or latest_id != attempt_id or payout.get("payment_provider") != attempt["rail"]
                or payout.get("state") not in {"PAYING", "CANCEL_PENDING"}):
            raise HTTPException(409, "stale payment attempt cannot be refreshed")
        if result.provider_reference and result.provider_reference != attempt.get("provider_reference"):
            raise HTTPException(409, "provider reference does not match the current attempt")
        if result.status == PaymentStatus.FAILURE:
            if result.retryable:
                raise HTTPException(409, "provider failure is retryable and not definitive")
            error = safe_text(result.error or "provider confirmed payment failure", 500)
            changed = c.execute(text("""
                UPDATE payment_attempts SET status='FAILED', error=:error, error_code=:code,
                    retryable=0, updated_at=:updated_at WHERE id=:id AND status='UNKNOWN'
            """), {"id": attempt_id, "error": error, "code": result.error_code, "updated_at": now()})
            if changed.rowcount != 1:
                raise HTTPException(409, "payment attempt was concurrently refreshed")
            conversion = asdict(c.execute(text("SELECT campaign_id,status FROM conversions WHERE id=:id"), {"id": payout["conversion_id"]}).fetchone())
            if payout["state"] == "CANCEL_PENDING" and conversion.get("status") == "reversed":
                release_campaign_budget(c, conversion["campaign_id"], payout["id"], int(payout.get("reserved_sats") or 0), movement="provider_reconciled_reversal_release")
                target_state, target_status = "CANCELLED", "reversed"
                changed_payout = c.execute(text("""
                    UPDATE payouts SET state='CANCELLED', status='reversed', fee_state='CANCELLED',
                        reserved_sats=0, last_error=:error
                    WHERE id=:id AND state='CANCEL_PENDING' AND payment_provider=:rail
                """), {"id": payout["id"], "rail": attempt["rail"], "error": error})
            else:
                target_state, target_status = "FAILED", "failed"
                changed_payout = c.execute(text("""
                    UPDATE payouts SET state='FAILED', status='failed', last_error=:error
                    WHERE id=:id AND state='PAYING' AND payment_provider=:rail
                """), {"id": payout["id"], "rail": attempt["rail"], "error": error})
            if changed_payout.rowcount != 1:
                raise HTTPException(409, "payout was concurrently refreshed")
            return {"ok": True, "resolved": True, "attempt_id": attempt_id, "payout_id": payout["id"], "payout_state": target_state, "status": target_status.upper()}

        payment_hash = str(result.payment_hash or "").lower()
        if result.status != PaymentStatus.SUCCESS or not valid_payment_hash(payment_hash):
            raise HTTPException(409, "provider settlement evidence is incomplete")
        existing_hash = str(attempt.get("payment_hash") or "").lower()
        if existing_hash and (not valid_payment_hash(existing_hash) or not hmac.compare_digest(existing_hash, payment_hash)):
            raise HTTPException(409, "provider payment hash does not match prepared evidence")
        changed = c.execute(text("""
            UPDATE payment_attempts SET status='SETTLED', payment_hash=:payment_hash,
                routing_fee_sats=:fee, error=NULL, retryable=0, settled_at=:settled_at, updated_at=:updated_at
            WHERE id=:id AND status='UNKNOWN'
        """), {"id": attempt_id, "payment_hash": payment_hash, "fee": result.fee_paid_sats, "settled_at": now(), "updated_at": now()})
        changed_payout = c.execute(text("""
            UPDATE payouts SET state='SETTLED', status='paid', payment_hash=:payment_hash, settled_at=:settled_at
            WHERE id=:id AND state IN ('PAYING','CANCEL_PENDING') AND payment_provider=:rail
        """), {"id": payout["id"], "rail": attempt["rail"], "payment_hash": payment_hash, "settled_at": now()})
        if changed.rowcount != 1 or changed_payout.rowcount != 1:
            raise HTTPException(409, "attempt and payout were concurrently refreshed")
    finalized = finalize_payout_paid(
        payout["id"], payment_hash, "Provider-confirmed Lightning payout",
        sandbox=False, provider=attempt["rail"],
        fees_paid_msats=(result.fee_paid_sats * 1000 if result.fee_paid_sats is not None else None),
    )
    apply_existing_reversal_for_payout(payout["id"])
    finalized["attempt_id"] = attempt_id
    finalized["resolved"] = True
    return finalized


@app.post("/admin/payment-attempts/{attempt_id}/refresh")
def refresh_payment_attempt(
    attempt_id: str,
    authorization: Optional[str] = Header(None),
) -> dict[str, Any]:
    """Query the configured provider for an existing attempt; never sends a payment."""
    require_payout_admin_key(authorization)
    init_db()
    with engine().connect() as c:
        attempt = asdict(c.execute(text("SELECT * FROM payment_attempts WHERE id=:id"), {"id": attempt_id}).fetchone())
    if not attempt:
        raise HTTPException(404, "payment attempt not found")
    if attempt["status"] != "UNKNOWN":
        raise HTTPException(409, f"attempt status {attempt['status']} does not require refresh")
    try:
        rail = configured_payment_rail()
    except RuntimeError as exc:
        raise HTTPException(503, safe_text(str(exc), 300)) from exc
    if safe_text(getattr(rail, "name", ""), 50).lower() != attempt["rail"]:
        raise HTTPException(409, "configured payment rail does not match attempt rail")
    reference = attempt.get("provider_reference")
    used_prepared_hash = False
    if not reference and attempt["rail"] == "nwc":
        prepared_hash = str(attempt.get("payment_hash") or "").lower()
        if valid_payment_hash(prepared_hash):
            reference = prepared_hash
            used_prepared_hash = True
    if not reference:
        raise HTTPException(409, "attempt has no provider reference for read-only refresh")
    if used_prepared_hash:
        with engine().begin() as c:
            c.execute(text("""
                UPDATE payment_attempts
                SET provider_reference=:reference, updated_at=:updated_at
                WHERE id=:id AND status='UNKNOWN' AND provider_reference IS NULL
                  AND payment_hash=:reference
            """), {"reference": reference, "updated_at": now(), "id": attempt_id})
    try:
        result = asyncio.run(rail.lookup_payment(reference))
    except Exception as exc:
        raise HTTPException(502, "payment lookup failed; attempt remains unresolved") from exc
    if result is None or result.status == PaymentStatus.PENDING:
        return {
            "ok": True,
            "resolved": False,
            "attempt_id": attempt_id,
            "payout_id": attempt["payout_id"],
            "status": "UNKNOWN",
            "action": "manual reconciliation required",
        }
    if result.status in {PaymentStatus.FAILURE, PaymentStatus.SUCCESS}:
        return _apply_provider_refresh_result(attempt_id, result)
    return {
        "ok": True,
        "resolved": False,
        "attempt_id": attempt_id,
        "payout_id": attempt["payout_id"],
        "status": "UNKNOWN",
        "action": "provider evidence incomplete; manual reconciliation required",
    }


@app.post("/admin/payment-attempts/{attempt_id}/reconcile")
def reconcile_payment_attempt(
    attempt_id: str,
    body: AttemptReconcileIn,
    authorization: Optional[str] = Header(None),
) -> dict[str, Any]:
    require_payout_admin_key(authorization)
    outcome = body.outcome.strip().upper()
    if outcome != "SETTLED":
        raise HTTPException(400, "manual reconciliation only supports SETTLED")
    audit_reason = safe_text(body.error, 500)
    if not audit_reason:
        raise HTTPException(400, "an operator audit reason is required")
    init_db()
    with engine().connect() as c:
        snapshot_attempt = asdict(c.execute(text("SELECT * FROM payment_attempts WHERE id=:id"), {"id": attempt_id}).fetchone())
        snapshot_payout = asdict(c.execute(text("SELECT * FROM payouts WHERE id=:id"), {"id": snapshot_attempt["payout_id"]}).fetchone()) if snapshot_attempt else None
    if not snapshot_attempt or not snapshot_payout:
        raise HTTPException(404, "payment attempt not found")
    if snapshot_attempt["status"] != "UNKNOWN":
        raise HTTPException(409, f"attempt status {snapshot_attempt['status']} cannot be manually reconciled")
    if snapshot_attempt["status"] == outcome:
        if outcome == "SETTLED" and not snapshot_payout.get("nostr_event_id"):
            result = finalize_payout_paid(
                snapshot_payout["id"],
                snapshot_attempt.get("payment_hash") or snapshot_payout.get("payment_hash"),
                "Retrying Nostr proof for a reconciled Lightning payout",
                sandbox=snapshot_attempt["rail"] == "sandbox",
                provider=snapshot_attempt["rail"],
                fees_paid_msats=(snapshot_attempt["routing_fee_sats"] * 1000 if snapshot_attempt.get("routing_fee_sats") is not None else None),
            )
            result["attempt_id"] = attempt_id
            apply_existing_reversal_for_payout(snapshot_payout["id"])
            return result
        apply_existing_reversal_for_payout(snapshot_payout["id"])
        return {"ok": True, "duplicate": True, "attempt_id": attempt_id, "payout_id": snapshot_payout["id"], "payout_state": snapshot_payout["state"]}
    with engine().begin() as c:
        suffix = " FOR UPDATE" if c.engine.dialect.name == "postgresql" else ""
        attempt = asdict(c.execute(text(f"SELECT * FROM payment_attempts WHERE id=:id{suffix}"), {"id": attempt_id}).fetchone())
        if not attempt:
            raise HTTPException(404, "payment attempt not found")
        payout = asdict(c.execute(text(f"SELECT * FROM payouts WHERE id=:id{suffix}"), {"id": attempt["payout_id"]}).fetchone())
        conversion = asdict(c.execute(text("SELECT * FROM conversions WHERE id=:id"), {"id": payout["conversion_id"]}).fetchone())
        if attempt["status"] != "UNKNOWN":
            raise HTTPException(409, f"attempt status {attempt['status']} cannot be manually reconciled")
        latest_id = c.execute(text("""
            SELECT id FROM payment_attempts WHERE payout_id=:payout_id AND kind=:kind
            ORDER BY attempt_number DESC, created_at DESC LIMIT 1
        """), {"payout_id": attempt["payout_id"], "kind": attempt["kind"]}).scalar_one()
        if latest_id != attempt_id or payout.get("payment_provider") != attempt["rail"]:
            raise HTTPException(409, "stale payment attempt cannot be reconciled")
        payment_hash = str(body.payment_hash or "").lower()
        existing_hash = str(attempt.get("payment_hash") or "").lower()
        if not valid_payment_hash(payment_hash):
            raise HTTPException(400, "a 64-character hexadecimal payment_hash is required")
        if not valid_payment_hash(existing_hash) or not hmac.compare_digest(payment_hash, existing_hash):
            raise HTTPException(409, "manual payment hash does not match prepared attempt evidence")
        if attempt["rail"] in {"blink", "fake"} and not attempt.get("provider_reference"):
            raise HTTPException(409, "attempt has no provider reference evidence")
        reconciled = c.execute(
            text("""
                UPDATE payment_attempts SET status='SETTLED', payment_hash=:payment_hash,
                    routing_fee_sats=:routing_fee_sats, error=:audit, settled_at=:settled_at, updated_at=:updated_at
                WHERE id=:id AND status='UNKNOWN'
            """),
            {"id": attempt_id, "payment_hash": payment_hash, "routing_fee_sats": body.routing_fee_sats,
             "audit": f"manual settlement: {audit_reason}", "settled_at": now(), "updated_at": now()},
        )
        if reconciled.rowcount != 1:
            raise HTTPException(409, "payment attempt was concurrently reconciled")
        updated = c.execute(
            text("""
                UPDATE payouts SET state='SETTLED', status='paid', payment_hash=:payment_hash, settled_at=:settled_at
                WHERE id=:id AND state IN ('PAYING','CANCEL_PENDING') AND payment_provider=:rail
            """),
            {"id": payout["id"], "rail": attempt["rail"], "payment_hash": payment_hash, "settled_at": now()},
        )
        if updated.rowcount != 1:
            raise HTTPException(409, "payout was concurrently reconciled")
    result = finalize_payout_paid(
        payout["id"],
        payment_hash,
        "Manually reconciled Lightning payout",
        sandbox=False,
        provider=attempt["rail"],
        fees_paid_msats=(body.routing_fee_sats * 1000 if body.routing_fee_sats is not None else None),
    )
    if conversion.get("status") == "reversed":
        apply_existing_reversal_for_payout(payout["id"])
    result["attempt_id"] = attempt_id
    return result


def payout_data(payout_id: str) -> dict[str, Any]:
    init_db()
    with engine().connect() as c:
        payout = asdict(c.execute(text("SELECT * FROM payouts WHERE id=:id"), {"id": payout_id}).fetchone())
        if not payout:
            raise HTTPException(404, "payout not found")
        conversion = asdict(c.execute(text("SELECT * FROM conversions WHERE id=:id"), {"id": payout["conversion_id"]}).fetchone())
        campaign = asdict(c.execute(text("SELECT * FROM campaigns WHERE id=:id"), {"id": conversion["campaign_id"] if conversion else None}).fetchone()) if conversion else None
        click = asdict(c.execute(text("SELECT * FROM clicks WHERE id=:id"), {"id": conversion["click_id"] if conversion else None}).fetchone()) if conversion else None
        enrollment = asdict(c.execute(text("SELECT * FROM enrollments WHERE ref_code=:ref"), {"ref": click["ref_code"] if click else None}).fetchone()) if click else None
        event = asdict(c.execute(text("SELECT event_id, kind, pubkey, entity_type, entity_id, relay_status, event_json, created_at, published_at FROM nostr_events WHERE event_id=:id"), {"id": payout.get("nostr_event_id")}).fetchone()) if payout.get("nostr_event_id") else None
        relays = [dict(r._mapping) for r in c.execute(text("SELECT relay_url, status, error, created_at FROM nostr_event_relays WHERE event_id=:id"), {"id": payout.get("nostr_event_id")}).fetchall()] if payout.get("nostr_event_id") else []
    if event:
        event["event_json"] = json.loads(event["event_json"])
        event["relays"] = relays
    payout = public_payout_record(payout)
    enrollment = public_enrollment_record(enrollment)
    return {"payout": payout, "conversion": conversion, "campaign": campaign, "click": click, "enrollment": enrollment, "event": event}


@app.get("/payouts/{payout_id}")
def payout_detail(payout_id: str) -> dict[str, Any]:
    return payout_data(payout_id)


def finalize_payout_paid(
    payout_id: str,
    payment_hash: str,
    note: str | None,
    *,
    sandbox: bool,
    provider: str,
    fees_paid_msats: int | None = None,
) -> dict[str, Any]:
    init_db()
    duplicate = False
    event: dict[str, Any] | None = None
    with engine().begin() as c:
        suffix = " FOR UPDATE" if c.engine.dialect.name == "postgresql" else ""
        payout = asdict(c.execute(text(f"SELECT * FROM payouts WHERE id=:id{suffix}"), {"id": payout_id}).fetchone())
        if not payout:
            raise HTTPException(404, "payout not found")
        if c.engine.dialect.name == "postgresql":
            c.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": f"manual-payment-hash:{payment_hash}"},
            )
        hash_used_elsewhere = c.execute(text("""
            SELECT 1 FROM payouts WHERE payment_hash=:payment_hash AND id!=:id
            UNION ALL
            SELECT 1 FROM payment_attempts WHERE payment_hash=:payment_hash AND payout_id!=:id
            LIMIT 1
        """), {"payment_hash": payment_hash, "id": payout_id}).fetchone()
        if hash_used_elsewhere:
            raise HTTPException(409, "payment hash is already assigned to another payout")
        conversion = asdict(c.execute(text("SELECT * FROM conversions WHERE id=:id"), {"id": payout["conversion_id"]}).fetchone())
        campaign = asdict(c.execute(text("SELECT * FROM campaigns WHERE id=:id"), {"id": conversion["campaign_id"] if conversion else None}).fetchone()) if conversion else None
        if payout["status"] == "paid" and payout.get("nostr_event_id"):
            if payout.get("payment_hash") != payment_hash:
                raise HTTPException(409, "payout was settled with different payment evidence")
            event = json.loads(payout["nostr_event_json"]) if payout.get("nostr_event_json") else None
            if not event:
                stored = c.execute(text("SELECT event_json FROM nostr_events WHERE event_id=:id"), {"id": payout["nostr_event_id"]}).scalar_one_or_none()
                event = json.loads(stored) if stored else None
            if not event:
                raise HTTPException(500, "payout proof outbox is missing")
            if payout.get("state") == "PUBLISHED":
                return {
                    "ok": True, "duplicate": True, "payout_id": payout_id,
                    "payout_status": "paid", "payout_state": "PUBLISHED",
                    "payment_hash": payout.get("payment_hash"),
                    "nostr_event_id": payout.get("nostr_event_id"), "nostr_event": event,
                    "receipt_url": f"{BASE_URL}/payouts/{payout_id}/receipt",
                    "flow_receipt_url": f"{BASE_URL}/flows/{payout['conversion_id']}/receipt",
                }
            duplicate = True
        else:
            if not conversion or not campaign:
                raise HTTPException(400, "payout is missing conversion/campaign context")
            reserved_sats = int(payout.get("reserved_sats") or 0)
            if reserved_sats >= int(payout["amount_sats"]):
                settle_campaign_budget(c, conversion["campaign_id"], payout_id, int(payout["amount_sats"]))
                reserved_sats -= int(payout["amount_sats"])
            affiliate_identity = normalize_pubkey(payout["affiliate_pubkey"], "affiliate_pubkey")
            merchant_hex = campaign.get("merchant_pubkey_hex") or normalize_pubkey(campaign["merchant_pubkey"], "merchant_pubkey")["hex"]
            settled_at = str(int(time.time()))
            payout_tags = [
                ["v", SCHEMA_VERSION], ["type", "affiliate_payout"],
                ["e", conversion["nostr_event_id"]],
                ["p", merchant_hex, "", "merchant"],
                ["p", affiliate_identity["hex"], "", "affiliate"],
                ["campaign", conversion["campaign_id"]], ["status", "paid"],
                ["amount_sats", str(payout["amount_sats"])],
                ["fee_sats", str(payout.get("fee_sats") or 0)],
                ["payment_hash", payment_hash], ["settled_at", settled_at],
                ["rail", provider], ["payment_provider", provider],
                ["sandbox", "true" if sandbox else "false"],
            ]
            event_content: dict[str, Any] = {"sandbox": sandbox}
            if provider == "manual":
                payout_tags.extend([["settlement_mode", "manual"], ["evidence", "merchant_attestation"]])
                event_content.update({"settlement_mode": "manual", "evidence": "merchant_attestation"})
            event = build_nostr_event(PAYOUT_KIND, payout_tags, json.dumps(event_content))
            # Durable outbox and financial state commit before any relay network call.
            persist_nostr_event(c, dict(event), "payout", payout_id, [])
            c.execute(text("""
                UPDATE payouts
                SET status='paid', state='SETTLED', payment_hash=:payment_hash,
                    payment_provider=:payment_provider, fees_paid_msats=:fees_paid_msats,
                    paid_at=:paid_at, settled_at=:settled_at, reserved_sats=:reserved_sats,
                    last_error=NULL, nostr_event_id=:nostr_event_id,
                    nostr_event_json=:nostr_event_json
                WHERE id=:id
            """), {
                "id": payout_id, "payment_hash": payment_hash,
                "payment_provider": provider, "fees_paid_msats": fees_paid_msats,
                "paid_at": now(), "settled_at": now(), "reserved_sats": reserved_sats,
                "nostr_event_id": event["id"], "nostr_event_json": json.dumps(event),
            })

    assert event is not None
    # Publishing the same signed event again is safe: its Nostr event id is stable.
    relay_results = publish_event(event)
    with engine().begin() as c:
        persist_nostr_event(c, dict(event), "payout", payout_id, relay_results)
        c.execute(text("""
            UPDATE payouts SET state='PUBLISHED'
            WHERE id=:id AND status='paid' AND state='SETTLED'
              AND nostr_event_id=:nostr_event_id AND payment_hash=:payment_hash
        """), {"id": payout_id, "nostr_event_id": event["id"], "payment_hash": payment_hash})
    return {
        "ok": True, "duplicate": duplicate, "payout_id": payout_id,
        "conversion_id": payout["conversion_id"], "payout_status": "paid",
        "payout_state": "PUBLISHED", "amount_sats": payout["amount_sats"],
        "payment_hash": payment_hash, "nostr_event_id": event["id"],
        "nostr_event": event, "relay_results": relay_results,
        "receipt_url": f"{BASE_URL}/payouts/{payout_id}/receipt",
        "flow_receipt_url": f"{BASE_URL}/flows/{payout['conversion_id']}/receipt",
    }
@app.post("/payouts/{payout_id}/mark-paid")
def mark_payout_paid(
    payout_id: str,
    body: PayoutMarkPaidIn,
    authorization: Optional[str] = Header(None),
) -> dict[str, Any]:
    require_payout_admin_key(authorization)
    if os.getenv("SANDBOX_PAYOUT_MARK_PAID_ENABLED", "false").lower() not in {"1", "true", "yes", "on"}:
        raise HTTPException(404, "sandbox payout route is disabled")
    init_db()
    payment_hash = body.payment_hash or hashlib.sha256(f"{payout_id}:{time.time()}".encode()).hexdigest()
    attempt_id: str | None = None
    with engine().begin() as c:
        payout = asdict(c.execute(text("SELECT * FROM payouts WHERE id=:id"), {"id": payout_id}).fetchone())
        if not payout:
            raise HTTPException(404, "payout not found")
        if payout.get("payment_provider") == "nwc":
            raise HTTPException(409, "sandbox cannot modify a real NWC payout")
        if payout["status"] != "paid":
            claimed = c.execute(
                text(
                    """
                    UPDATE payouts SET status='sandbox_processing', state='PAYING', payment_provider='sandbox'
                    WHERE id=:id AND status IN ('pending', 'failed')
                      AND state IN ('PAYABLE', 'FAILED')
                      AND reserved_sats >= amount_sats + fee_sats
                      AND EXISTS (
                          SELECT 1 FROM conversions
                          WHERE conversions.id=payouts.conversion_id AND conversions.status!='reversed'
                      )
                      AND (payment_provider IS NULL OR payment_provider='sandbox')
                    """
                ),
                {"id": payout_id},
            )
            if claimed.rowcount != 1:
                raise HTTPException(409, "sandbox cannot override this payout state")
            attempt_number = int(c.execute(text("SELECT COUNT(*) FROM payment_attempts WHERE payout_id=:id AND kind='commission'"), {"id": payout_id}).scalar_one()) + 1
            attempt_id = hid("att")
            created_at = now()
            c.execute(
                text("""
                    INSERT INTO payment_attempts
                    (id, payout_id, kind, rail, idempotency_key, destination, amount_sats,
                     status, payment_hash, routing_fee_sats, attempt_number, created_at, updated_at, settled_at)
                    VALUES (:id, :payout_id, 'commission', 'sandbox', :idempotency_key, :destination,
                            :amount_sats, 'SETTLED', :payment_hash, 0, :attempt_number, :created_at, :updated_at, :settled_at)
                """),
                {
                    "id": attempt_id,
                    "payout_id": payout_id,
                    "idempotency_key": payment_idempotency_key(payout_id, "commission", attempt_number),
                    "destination": payout.get("lightning_address") or "sandbox",
                    "amount_sats": payout["amount_sats"],
                    "payment_hash": payment_hash,
                    "attempt_number": attempt_number,
                    "created_at": created_at,
                    "updated_at": created_at,
                    "settled_at": created_at,
                },
            )
            c.execute(text("UPDATE payouts SET status='paid', state='SETTLED', payment_hash=:payment_hash, settled_at=:settled_at WHERE id=:id"), {"id": payout_id, "payment_hash": payment_hash, "settled_at": created_at})
    result = finalize_payout_paid(
        payout_id,
        payment_hash,
        body.note,
        sandbox=True,
        provider="sandbox",
    )
    if attempt_id:
        result["attempt_id"] = attempt_id
    return result


@app.get("/admin/payments/nwc/readiness")
def nwc_payment_readiness(authorization: Optional[str] = Header(None)) -> dict[str, Any]:
    """Probe NWC read-only capabilities; never initiates or enables a payment."""
    require_payout_admin_key(authorization)
    configured = bool(os.getenv("NWC_CONNECTION_URI", "").strip())
    if not configured:
        raise HTTPException(503, "NWC connection is not configured")
    if os.getenv("PAYMENT_RAIL", "nwc").strip().lower() != "nwc":
        raise HTTPException(409, "NWC is not the configured payment rail")
    try:
        readiness = asyncio.run(probe_nwc_wallet())
    except LightningPaymentError as exc:
        raise HTTPException(502, safe_text(str(exc), 300)) from exc
    ready = bool(
        readiness.get("authenticated")
        and readiness.get("supports_pay_invoice")
        and readiness.get("supports_lookup_invoice")
    )
    return {
        "ok": ready,
        "configured": True,
        **readiness,
        "payment_rail": "nwc",
        "payment_execution_enabled": lightning_payouts_enabled(),
        "max_payout_sats": lightning_max_payout_sats(),
    }


@app.post("/admin/payouts/{payout_id}/execute")
def execute_payout(
    payout_id: str,
    authorization: Optional[str] = Header(None),
) -> dict[str, Any]:
    require_payout_admin_key(authorization)
    if not lightning_payouts_enabled():
        raise HTTPException(503, "Lightning payouts are disabled")
    try:
        rail = configured_payment_rail()
    except RuntimeError as exc:
        raise HTTPException(503, safe_text(str(exc), 300)) from exc
    rail_name = safe_text(getattr(rail, "name", "unknown"), 50).lower()
    if rail_name not in {"nwc", "blink", "fake"}:
        raise HTTPException(503, "configured payment rail is not supported")
    if rail_name == "blink":
        raise HTTPException(503, "Blink payment execution is disabled pending provider idempotency guarantees")
    init_db()
    with engine().connect() as c:
        payout = asdict(c.execute(text("""
            SELECT p.*, v.status AS conversion_status
            FROM payouts p JOIN conversions v ON v.id=p.conversion_id
            WHERE p.id=:id
        """), {"id": payout_id}).fetchone())
    if not payout:
        raise HTTPException(404, "payout not found")
    if payout.get("conversion_status") == "reversed":
        raise HTTPException(409, "reversed conversion payout is not executable")
    if payout["status"] == "paid":
        if payout.get("nostr_event_id"):
            existing_event = json.loads(payout["nostr_event_json"]) if payout.get("nostr_event_json") else None
            return {
                "ok": True,
                "duplicate": True,
                "payout_id": payout_id,
                "payout_status": "paid",
                "payout_state": payout.get("state") or "PUBLISHED",
                "payment_hash": payout.get("payment_hash"),
                "nostr_event_id": payout.get("nostr_event_id"),
                "nostr_event": existing_event,
                "receipt_url": f"{BASE_URL}/payouts/{payout_id}/receipt",
                "flow_receipt_url": f"{BASE_URL}/flows/{payout['conversion_id']}/receipt",
            }
        # Payment evidence survived but proof publication did not; retry proof only, never the payment.
        return finalize_payout_paid(
            payout_id,
            payout["payment_hash"],
            "Retrying Nostr proof for an already paid Lightning payout",
            sandbox=False,
            provider=payout.get("payment_provider") or "nwc",
            fees_paid_msats=payout.get("fees_paid_msats"),
        )
    if payout.get("state") in {"PAYING", "SETTLED"} or payout["status"] in {"processing", "payment_unknown"}:
        raise HTTPException(409, f"payout state is {payout.get('state')}; manual reconciliation required")
    if payout.get("state") in {"ON_HOLD", "CANCEL_PENDING", "CANCELLED"}:
        raise HTTPException(409, f"payout state {payout['state']} is not executable")
    obligation_sats = int(payout["amount_sats"]) + int(payout.get("fee_sats") or 0)
    if int(payout.get("reserved_sats") or 0) < obligation_sats:
        raise HTTPException(409, "payout has no complete campaign budget reservation")
    if payout.get("return_window_ends_at"):
        try:
            return_window_ends_at = datetime.fromisoformat(str(payout["return_window_ends_at"]).replace("Z", "+00:00"))
            if return_window_ends_at > datetime.now(timezone.utc):
                raise HTTPException(409, "payout return window has not ended")
        except ValueError as exc:
            raise HTTPException(500, "payout has an invalid return window") from exc
    if not payout.get("lightning_address"):
        raise HTTPException(400, "payout has no Lightning Address")
    if payout["amount_sats"] > lightning_max_payout_sats():
        raise HTTPException(400, "payout exceeds LIGHTNING_MAX_PAYOUT_SATS")
    with engine().begin() as c:
        attempt_number = int(c.execute(
            text("SELECT COUNT(*) FROM payment_attempts WHERE payout_id=:id AND kind='commission'"),
            {"id": payout_id},
        ).scalar_one()) + 1
        attempt_id = hid("att")
        idempotency_key = payment_idempotency_key(payout_id, "commission", attempt_number)
        claimed = c.execute(
            text(
                """
                UPDATE payouts
                SET status='processing', state='PAYING', payment_provider=:rail, attempt_count=attempt_count+1,
                    processing_started_at=:started_at, last_error=NULL
                WHERE id=:id AND state IN ('PAYABLE', 'FAILED')
                  AND reserved_sats >= amount_sats + fee_sats
                  AND EXISTS (
                      SELECT 1 FROM conversions
                      WHERE conversions.id=payouts.conversion_id AND conversions.status!='reversed'
                  )
                """
            ),
            {"id": payout_id, "rail": rail_name, "started_at": now()},
        )
        if claimed.rowcount != 1:
            raise HTTPException(409, "payout could not be claimed for processing")
        c.execute(
            text("""
                INSERT INTO payment_attempts
                (id, payout_id, kind, rail, idempotency_key, destination, amount_sats,
                 status, attempt_number, created_at, updated_at)
                VALUES (:id, :payout_id, 'commission', :rail, :idempotency_key, :destination,
                        :amount_sats, 'PAYING', :attempt_number, :created_at, :updated_at)
            """),
            {
                "id": attempt_id,
                "payout_id": payout_id,
                "rail": rail_name,
                "idempotency_key": idempotency_key,
                "destination": payout["lightning_address"],
                "amount_sats": payout["amount_sats"],
                "attempt_number": attempt_number,
                "created_at": now(),
                "updated_at": now(),
            },
        )
    if isinstance(rail, NwcPaymentRail):
        async def record_prepared_evidence(invoice: str, payment_hash: str) -> None:
            if not invoice or not valid_payment_hash(payment_hash):
                raise LightningPaymentError("prepared NWC evidence is invalid")
            with engine().begin() as c:
                latest_id = c.execute(text("""
                    SELECT id FROM payment_attempts WHERE payout_id=:payout_id AND kind='commission'
                    ORDER BY attempt_number DESC, created_at DESC LIMIT 1
                """), {"payout_id": payout_id}).scalar_one_or_none()
                if latest_id != attempt_id:
                    raise LightningPaymentError("NWC attempt ownership was lost before payment")
                attempt_recorded = c.execute(text("""
                    UPDATE payment_attempts SET payment_hash=:payment_hash, updated_at=:updated_at
                    WHERE id=:attempt_id AND payout_id=:payout_id AND rail='nwc' AND status='PAYING'
                """), {"attempt_id": attempt_id, "payout_id": payout_id, "payment_hash": payment_hash.lower(), "updated_at": now()})
                payout_recorded = c.execute(text("""
                    UPDATE payouts SET bolt11_invoice=:invoice, payment_hash=:payment_hash
                    WHERE id=:payout_id AND state='PAYING' AND status='processing'
                      AND payment_provider='nwc' AND reserved_sats >= amount_sats + fee_sats
                      AND EXISTS (
                        SELECT 1 FROM conversions
                        WHERE conversions.id=payouts.conversion_id AND conversions.status!='reversed'
                      )
                """), {"payout_id": payout_id, "invoice": invoice, "payment_hash": payment_hash.lower()})
                if attempt_recorded.rowcount != 1 or payout_recorded.rowcount != 1:
                    raise LightningPaymentError("NWC attempt ownership or reservation was lost before payment")
        rail.set_prepared_evidence_recorder(record_prepared_evidence)
    try:
        result = asyncio.run(rail.pay_to_lightning_address(
            payout["lightning_address"],
            int(payout["amount_sats"]),
            f"Meerat affiliate reward {payout_id}",
            idempotency_key,
        ))
    except PaymentRailAmbiguousError as exc:
        error = safe_text(str(exc), 500)
        with engine().begin() as c:
            c.execute(text("""
                UPDATE payment_attempts
                SET status='UNKNOWN', payment_hash=:payment_hash, provider_reference=:provider_reference,
                    error=:error, retryable=0, updated_at=:updated_at
                WHERE id=:id AND status='PAYING'
            """), {
                "id": attempt_id,
                "payment_hash": exc.payment_hash,
                "provider_reference": exc.provider_reference,
                "error": error,
                "updated_at": now(),
            })
            c.execute(text("""
                UPDATE payouts SET status='payment_unknown', state='PAYING',
                    payment_hash=COALESCE(:payment_hash, payment_hash), last_error=:error
                WHERE id=:id AND state='PAYING'
            """), {"id": payout_id, "payment_hash": exc.payment_hash, "error": error})
        raise HTTPException(502, error) from exc
    except Exception as exc:
        error = "payment rail failed unexpectedly; manual reconciliation required"
        with engine().begin() as c:
            c.execute(text("""
                UPDATE payment_attempts SET status='UNKNOWN', error=:error, retryable=0, updated_at=:updated_at
                WHERE id=:id AND status='PAYING'
            """), {"id": attempt_id, "error": error, "updated_at": now()})
            c.execute(text("""
                UPDATE payouts SET status='payment_unknown', state='PAYING', last_error=:error
                WHERE id=:id AND state='PAYING'
            """), {"id": payout_id, "error": error})
        raise HTTPException(502, error) from exc

    if result.status == PaymentStatus.PENDING:
        with engine().begin() as c:
            c.execute(text("""
                UPDATE payment_attempts
                SET status='UNKNOWN', payment_hash=:payment_hash, provider_reference=:provider_reference,
                    routing_fee_sats=:routing_fee_sats, error='provider payment pending reconciliation',
                    retryable=0, updated_at=:updated_at
                WHERE id=:id AND status='PAYING'
            """), {
                "id": attempt_id,
                "payment_hash": result.payment_hash,
                "provider_reference": result.provider_reference,
                "routing_fee_sats": result.fee_paid_sats,
                "updated_at": now(),
            })
            c.execute(text("""
                UPDATE payouts SET status='payment_unknown', state='PAYING',
                    payment_hash=COALESCE(:payment_hash, payment_hash), last_error='provider payment pending reconciliation'
                WHERE id=:id AND state='PAYING'
            """), {"id": payout_id, "payment_hash": result.payment_hash})
        raise HTTPException(202, "payment pending; reconciliation required")

    if result.status == PaymentStatus.FAILURE:
        error = safe_text(result.error or "payment rail rejected the payment", 500)
        with engine().begin() as c:
            c.execute(text("""
                UPDATE payment_attempts
                SET status='FAILED', provider_reference=:provider_reference, error=:error,
                    error_code=:error_code, retryable=:retryable, updated_at=:updated_at
                WHERE id=:id AND status='PAYING'
            """), {
                "id": attempt_id,
                "provider_reference": result.provider_reference,
                "error": error,
                "error_code": result.error_code,
                "retryable": 1 if result.retryable else 0,
                "updated_at": now(),
            })
            payout_updated = c.execute(text("""
                UPDATE payouts SET status='failed', state='FAILED', last_error=:error
                WHERE id=:id AND state='PAYING'
            """), {"id": payout_id, "error": error})
            if payout_updated.rowcount == 0:
                current = asdict(c.execute(text("""
                    SELECT p.*, v.campaign_id, v.status AS conversion_status
                    FROM payouts p JOIN conversions v ON v.id=p.conversion_id WHERE p.id=:id
                """), {"id": payout_id}).fetchone())
                if current and current.get("state") == "CANCEL_PENDING" and current.get("conversion_status") == "reversed":
                    release_campaign_budget(
                        c,
                        current["campaign_id"],
                        payout_id,
                        int(current.get("reserved_sats") or 0),
                        movement="inflight_reversal_release",
                    )
                    c.execute(text("""
                        UPDATE payouts SET status='reversed', state='CANCELLED', fee_state='CANCELLED',
                            reserved_sats=0, last_error=:error WHERE id=:id AND state='CANCEL_PENDING'
                    """), {"id": payout_id, "error": error})
        raise HTTPException(502, error)

    payment_hash = str(result.payment_hash or "").lower()
    prepared_hash = None
    with engine().connect() as c:
        prepared_hash = c.execute(text("SELECT payment_hash FROM payment_attempts WHERE id=:id"), {"id": attempt_id}).scalar_one_or_none()
    hash_mismatch = bool(prepared_hash and valid_payment_hash(prepared_hash) and not hmac.compare_digest(str(prepared_hash).lower(), payment_hash))
    if result.status != PaymentStatus.SUCCESS or not valid_payment_hash(payment_hash) or hash_mismatch:
        error = "payment rail returned invalid or mismatched success evidence; manual reconciliation required"
        with engine().begin() as c:
            attempt_changed = c.execute(text("""
                UPDATE payment_attempts SET status='UNKNOWN', error=:error, retryable=0, updated_at=:updated_at
                WHERE id=:id AND status='PAYING'
            """), {"id": attempt_id, "error": error, "updated_at": now()})
            payout_changed = c.execute(text("""
                UPDATE payouts SET status='payment_unknown', state='PAYING', last_error=:error
                WHERE id=:id AND state='PAYING' AND payment_provider=:rail
            """), {"id": payout_id, "rail": rail_name, "error": error})
            if attempt_changed.rowcount != 1 or payout_changed.rowcount != 1:
                raise HTTPException(409, "invalid provider result raced with reconciliation")
        raise HTTPException(502, error)
    result_payment_hash = payment_hash
    # Payment evidence is committed before Nostr proof construction/publication.
    fees_paid_msats = result.fee_paid_msats
    if fees_paid_msats is None and result.fee_paid_sats is not None:
        fees_paid_msats = int(result.fee_paid_sats) * 1000
    with engine().begin() as c:
        routing_fee_sats = result.fee_paid_sats
        attempt_recorded = c.execute(
            text("""
                UPDATE payment_attempts
                SET status='SETTLED', payment_hash=:payment_hash, provider_reference=:provider_reference,
                    routing_fee_sats=:routing_fee_sats, error=NULL, error_code=NULL, retryable=0,
                    settled_at=:settled_at, updated_at=:updated_at
                WHERE id=:id AND status='PAYING'
            """),
            {
                "id": attempt_id,
                "payment_hash": result_payment_hash,
                "provider_reference": result.provider_reference,
                "routing_fee_sats": routing_fee_sats,
                "settled_at": now(),
                "updated_at": now(),
            },
        )
        recorded = c.execute(
            text(
                """
                UPDATE payouts
                SET status='paid', state='SETTLED', payment_hash=:payment_hash, fees_paid_msats=:fees_paid_msats,
                    paid_at=:paid_at, settled_at=:settled_at, last_error=NULL
                WHERE id=:id AND state IN ('PAYING','CANCEL_PENDING') AND payment_provider=:rail
                """
            ),
            {
                "id": payout_id,
                "rail": rail_name,
                "payment_hash": result_payment_hash,
                "fees_paid_msats": fees_paid_msats,
                "paid_at": now(),
                "settled_at": now(),
            },
        )
        if attempt_recorded.rowcount != 1 or recorded.rowcount != 1:
            raise HTTPException(409, "payment succeeded but current attempt ownership was lost; manual reconciliation required")
    finalized = finalize_payout_paid(
        payout_id,
        result_payment_hash,
        f"Lightning payout paid via {rail_name}",
        sandbox=False,
        provider=rail_name,
        fees_paid_msats=fees_paid_msats,
    )
    apply_existing_reversal_for_payout(payout_id)
    finalized["attempt_id"] = attempt_id
    finalized["idempotency_key"] = idempotency_key
    return finalized


def _signed_tag(event_json: dict[str, Any], name: str) -> str | None:
    for tag in event_json.get("tags", []):
        if isinstance(tag, list) and len(tag) >= 2 and tag[0] == name and isinstance(tag[1], str):
            return tag[1]
    return None


def _signed_role_pubkey(event_json: dict[str, Any], role: str) -> str | None:
    for tag in event_json.get("tags", []):
        if isinstance(tag, list) and len(tag) >= 4 and tag[0] == "p" and tag[3] == role and isinstance(tag[1], str):
            return tag[1]
    return None


def _signed_role_matches(event_json: dict[str, Any], role: str, expected_pubkey: str) -> bool:
    tagged_pubkey = _signed_role_pubkey(event_json, role)
    if tagged_pubkey is not None:
        return tagged_pubkey == expected_pubkey
    # nostr-sdk intentionally omits self-referencing `p` tags while signing. When
    # the participant is also the event author, the signed author pubkey is the
    # equivalent cryptographic identity claim. Never use this fallback when a
    # conflicting explicit role tag is present.
    return event_json.get("pubkey") == expected_pubkey


def _payout_event_matches(data: dict[str, Any], event_json: dict[str, Any]) -> bool:
    payout = data["payout"]
    conversion = data.get("conversion") or {}
    campaign = data.get("campaign") or {}
    event_record = data.get("event") or {}
    try:
        expected_affiliate = normalize_pubkey(payout["affiliate_pubkey"], "affiliate_pubkey")["hex"]
        expected_merchant = campaign.get("merchant_pubkey_hex") or normalize_pubkey(campaign["merchant_pubkey"], "merchant_pubkey")["hex"]
    except (HTTPException, KeyError, TypeError, ValueError):
        return False
    return all((
        event_json.get("kind") == PAYOUT_KIND,
        event_json.get("pubkey") == nostr_keys().public_key().to_hex(),
        event_record.get("entity_type") == "payout",
        event_record.get("entity_id") == payout.get("id"),
        _signed_tag(event_json, "type") == "affiliate_payout",
        _signed_tag(event_json, "status") == "paid",
        _signed_tag(event_json, "amount_sats") == str(payout.get("amount_sats")),
        _signed_tag(event_json, "payment_hash") == payout.get("payment_hash"),
        _signed_tag(event_json, "campaign") == conversion.get("campaign_id"),
        _signed_tag(event_json, "e") == conversion.get("nostr_event_id"),
        _signed_role_matches(event_json, "affiliate", expected_affiliate),
        _signed_role_matches(event_json, "merchant", expected_merchant),
    ))


def _receipt_status(status_value: Any) -> dict[str, str]:
    raw = str(status_value or "unknown").lower()
    classes = {
        "paid": "success", "settled": "success", "published": "success",
        "active": "success", "approved": "success", "verified": "success",
        "pending": "pending", "paying": "pending", "sandbox_processing": "pending",
        "pending_publication": "pending", "retrying": "pending",
        "failed": "danger", "payment_unknown": "danger", "unknown": "danger",
        "skipped": "neutral",
    }
    labels = {"pending_publication": "retrying / pending publication"}
    return {"value": raw, "label": labels.get(raw, raw.replace("_", " ")), "class": classes.get(raw, "neutral")}


def _receipt_timestamp(value: Any) -> str:
    if not value:
        return "not recorded"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
        return parsed.strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError):
        return str(value)


def payout_receipt_context(data: dict[str, Any]) -> dict[str, Any]:
    payout = data["payout"]
    event_record = data.get("event") or {}
    stored_event = event_record.get("event_json") if isinstance(event_record.get("event_json"), dict) else {}
    allowed_fields = {key: stored_event.get(key) for key in ("id", "pubkey", "created_at", "kind", "tags", "content", "sig")}
    event_verified = False
    event_note: str | None = None
    canonical_event: dict[str, Any] = {}
    if all(allowed_fields.get(key) is not None for key in allowed_fields):
        try:
            parsed_event = Event.from_json(json.dumps(allowed_fields, ensure_ascii=False, separators=(",", ":")))
            crypto_verified = bool(parsed_event.verify())
            canonical_event = json.loads(parsed_event.as_json())
            event_verified = all((
                crypto_verified,
                parsed_event.id().to_hex() == event_record.get("event_id") == payout.get("nostr_event_id"),
                _payout_event_matches(data, canonical_event),
            ))
            if event_verified:
                event_note = parsed_event.id().to_bech32()
        except Exception:
            event_verified = False
            canonical_event = allowed_fields

    verified_claims = canonical_event if event_verified else {}
    signed_provider = _signed_tag(verified_claims, "payment_provider") or _signed_tag(verified_claims, "rail")
    local_provider = payout.get("payment_provider")
    provider = signed_provider or local_provider or "not specified"
    sandbox_tag = _signed_tag(verified_claims, "sandbox")
    if sandbox_tag in {"true", "false"}:
        sandbox_state = "sandbox" if sandbox_tag == "true" else "non-sandbox"
    elif local_provider in {"sandbox", "fake"}:
        sandbox_state = "sandbox"
    elif local_provider in {"nwc", "manual"}:
        sandbox_state = "non-sandbox"
    else:
        sandbox_state = "unknown"
    signed_evidence = _signed_tag(verified_claims, "evidence_type") or _signed_tag(verified_claims, "evidence")
    if signed_evidence == "merchant_attestation" or provider == "manual":
        evidence_type = "merchant_attestation"
        evidence_explanation = "Merchant attestation is not trustless settlement evidence."
    elif sandbox_state == "sandbox":
        evidence_type = "sandbox_test"
        evidence_explanation = "This is test evidence and does not prove that real sats moved."
    elif provider != "not specified":
        evidence_type = "provider_reported_payment"
        evidence_explanation = "Provider-reported payment evidence is not an independently trustless proof."
    else:
        evidence_type = "legacy_unspecified"
        evidence_explanation = "This legacy receipt does not specify an independently verifiable payment evidence type."

    status = _receipt_status(payout.get("status"))
    relays = []
    for relay in event_record.get("relays", []):
        relays.append({**relay, "display_status": _receipt_status(relay.get("status"))})
    aggregate_relay_status = _receipt_status(event_record.get("relay_status") or "unknown")
    amount = payout.get("amount_sats")
    headline = f"{amount} sats recorded paid." if status["value"] == "paid" else f"{amount} sats · {status['label']}."
    titles = {
        "sandbox": "Sandbox payout receipt",
        "non-sandbox": "Non-sandbox payout receipt",
        "unknown": "Payment mode not established",
    }
    if event_verified and signed_provider and sandbox_tag in {"true", "false"}:
        claim_source = "verified signed Nostr event"
    elif event_verified:
        claim_source = "mixed: verified signed Nostr event + local payout record"
    else:
        claim_source = "local payout record; signed-event verification unavailable"
    return {
        **data,
        "receipt": {
            "title": titles[sandbox_state],
            "headline": headline,
            "sandbox_state": sandbox_state,
            "is_sandbox": sandbox_state == "sandbox",
            "provider": provider,
            "claim_source": claim_source,
            "evidence_type": evidence_type,
            "evidence_explanation": evidence_explanation,
            "preimage_disclaimer": "No payment preimage is disclosed by this receipt.",
            "paid_at": _receipt_timestamp(payout.get("paid_at") or payout.get("settled_at")),
            "status": status,
        },
        "proof": {
            "event": canonical_event,
            "verified": event_verified,
            "note": event_note,
            "njump_url": f"https://njump.me/{event_note}" if event_note else None,
            "internal_url": f"/nostr/events/{event_record.get('event_id')}" if event_verified and event_record.get("event_id") else None,
            "relay_status": aggregate_relay_status,
            "relays": relays,
        },
    }


@app.get("/payouts/{payout_id}/receipt", response_class=HTMLResponse)
def payout_receipt_page(request: Request, payout_id: str) -> Response:
    context = payout_receipt_context(payout_data(payout_id))
    return templates.TemplateResponse(request=request, name="payout_receipt.html", context=context)


@app.get("/proofs")
def proofs() -> dict[str, Any]:
    with engine().connect() as c:
        events = [json.loads(r._mapping["event_json"]) for r in c.execute(text("SELECT event_json FROM nostr_events ORDER BY created_at DESC")).fetchall()]
    if events:
        return {"events": events}
    # Backward-compatible fallback for rows created before nostr_events existed.
    with engine().connect() as c:
        campaigns = [json.loads(r._mapping["nostr_event_json"]) for r in c.execute(text("SELECT nostr_event_json FROM campaigns ORDER BY created_at DESC")).fetchall()]
        enrollments = [json.loads(r._mapping["nostr_event_json"]) for r in c.execute(text("SELECT nostr_event_json FROM enrollments ORDER BY created_at DESC")).fetchall()]
        conversions = [json.loads(r._mapping["nostr_event_json"]) for r in c.execute(text("SELECT nostr_event_json FROM conversions ORDER BY created_at DESC")).fetchall()]
    return {"events": campaigns + enrollments + conversions}


@app.get("/nostr/events/{event_id}")
def get_nostr_event(event_id: str) -> dict[str, Any]:
    with engine().connect() as c:
        event = asdict(c.execute(text("SELECT * FROM nostr_events WHERE event_id=:id"), {"id": event_id}).fetchone())
        relays = [dict(r._mapping) for r in c.execute(text("SELECT relay_url, status, error, created_at FROM nostr_event_relays WHERE event_id=:id"), {"id": event_id}).fetchall()]
    if not event:
        raise HTTPException(404, "nostr event not found")
    event["event_json"] = json.loads(event["event_json"])
    event["tags"] = json.loads(event.pop("tags_json"))
    event["relays"] = relays
    return event



def campaign_public_data(campaign_id: str) -> dict[str, Any]:
    init_db()
    with engine().connect() as c:
        campaign = asdict(c.execute(text("SELECT * FROM campaigns WHERE id=:id"), {"id": campaign_id}).fetchone())
        if not campaign:
            raise HTTPException(404, "campaign not found")
        merchant_profile = asdict(
            c.execute(
                text("SELECT merchant_pubkey, merchant_pubkey_hex, logo_url FROM merchant_profiles WHERE merchant_pubkey_hex=:hex"),
                {"hex": campaign.get("merchant_pubkey_hex")},
            ).fetchone()
        ) or {
            "merchant_pubkey": campaign["merchant_pubkey"],
            "merchant_pubkey_hex": campaign.get("merchant_pubkey_hex"),
            "logo_url": None,
        }
        enrollments = [dict(r._mapping) for r in c.execute(text("SELECT * FROM enrollments WHERE campaign_id=:id ORDER BY created_at DESC"), {"id": campaign_id}).fetchall()]
        clicks = [dict(r._mapping) for r in c.execute(text("SELECT * FROM clicks WHERE campaign_id=:id ORDER BY created_at DESC"), {"id": campaign_id}).fetchall()]
        conversions = [dict(r._mapping) for r in c.execute(text("SELECT * FROM conversions WHERE campaign_id=:id ORDER BY created_at DESC"), {"id": campaign_id}).fetchall()]
        payouts = [dict(r._mapping) for r in c.execute(text("SELECT p.* FROM payouts p JOIN conversions v ON p.conversion_id=v.id WHERE v.campaign_id=:id ORDER BY p.created_at DESC"), {"id": campaign_id}).fetchall()]
        reversals = [dict(r._mapping) for r in c.execute(text("SELECT r.* FROM reversals r JOIN conversions v ON r.conversion_id=v.id WHERE v.campaign_id=:id ORDER BY r.created_at DESC"), {"id": campaign_id}).fetchall()]
        entity_ids = [campaign_id] + [e["id"] for e in enrollments] + [v["id"] for v in conversions] + [p["id"] for p in payouts] + [r["id"] for r in reversals]
        events: list[dict[str, Any]] = []
        relays_by_event: dict[str, list[dict[str, Any]]] = {}
        for entity_id in entity_ids:
            for ev in c.execute(text("SELECT event_id, kind, pubkey, entity_type, entity_id, relay_status, event_json, created_at, published_at FROM nostr_events WHERE entity_id=:id ORDER BY created_at DESC"), {"id": entity_id}).fetchall():
                evd = dict(ev._mapping)
                evd["event_json"] = json.loads(evd["event_json"])
                events.append(evd)
                relays_by_event[evd["event_id"]] = [dict(r._mapping) for r in c.execute(text("SELECT relay_url, status, error, created_at FROM nostr_event_relays WHERE event_id=:event_id"), {"event_id": evd["event_id"]}).fetchall()]
    for ev in events:
        ev["relays"] = relays_by_event.get(ev["event_id"], [])
    totals = {
        "enrollments": len(enrollments),
        "clicks": len(clicks),
        "conversions": len(conversions),
        "reversals": len(reversals),
        "commission_sats": sum(int(v["commission_sats"]) for v in conversions),
        "pending_sats": sum(int(p["amount_sats"]) for p in payouts if p["status"] == "pending"),
        "published_events": sum(1 for ev in events if ev["relay_status"] == "published"),
    }
    payouts = [public_payout_record(p) for p in payouts]
    enrollments = [public_enrollment_record(e) for e in enrollments]
    campaign["nostr_event"] = json.loads(campaign.pop("nostr_event_json"))
    return {
        "campaign_id": campaign_id,
        "campaign": campaign,
        "merchant_profile": merchant_profile,
        "totals": totals,
        "enrollments": enrollments,
        "clicks": clicks,
        "conversions": conversions,
        "payouts": payouts,
        "reversals": reversals,
        "events": events,
        "links": {
            "page": f"{BASE_URL}/campaigns/{campaign_id}/page",
            "json": f"{BASE_URL}/campaigns/{campaign_id}/summary",
            "campaign_event": f"{BASE_URL}/nostr/events/{campaign['nostr_event_id']}",
        },
    }


@app.get("/campaigns/{campaign_id}/summary")
def campaign_summary(campaign_id: str) -> dict[str, Any]:
    return campaign_public_data(campaign_id)


def flow_receipt_data(conversion_id: str) -> dict[str, Any]:
    init_db()
    with engine().connect() as c:
        conversion = asdict(c.execute(text("SELECT * FROM conversions WHERE id=:id"), {"id": conversion_id}).fetchone())
        if not conversion:
            raise HTTPException(404, "conversion not found")
        click = asdict(c.execute(text("SELECT * FROM clicks WHERE id=:id"), {"id": conversion["click_id"]}).fetchone())
        campaign = asdict(c.execute(text("SELECT * FROM campaigns WHERE id=:id"), {"id": conversion["campaign_id"]}).fetchone())
        enrollment = asdict(c.execute(text("SELECT * FROM enrollments WHERE ref_code=:ref"), {"ref": click["ref_code"] if click else None}).fetchone()) if click else None
        payout = asdict(c.execute(text("SELECT * FROM payouts WHERE conversion_id=:id ORDER BY created_at DESC LIMIT 1"), {"id": conversion_id}).fetchone())
        reversal = asdict(c.execute(text("SELECT * FROM reversals WHERE conversion_id=:id ORDER BY created_at DESC LIMIT 1"), {"id": conversion_id}).fetchone())
        event_ids = [eid for eid in [campaign.get("nostr_event_id") if campaign else None, enrollment.get("nostr_event_id") if enrollment else None, conversion.get("nostr_event_id"), payout.get("nostr_event_id") if payout else None, reversal.get("nostr_event_id") if reversal else None] if eid]
        events: list[dict[str, Any]] = []
        relays_by_event: dict[str, list[dict[str, Any]]] = {}
        if event_ids:
            for eid in event_ids:
                ev = asdict(c.execute(text("SELECT event_id, kind, pubkey, entity_type, entity_id, relay_status, event_json, created_at, published_at FROM nostr_events WHERE event_id=:id"), {"id": eid}).fetchone())
                if ev:
                    ev["event_json"] = json.loads(ev["event_json"])
                    events.append(ev)
                relays_by_event[eid] = [dict(r._mapping) for r in c.execute(text("SELECT relay_url, status, error, created_at FROM nostr_event_relays WHERE event_id=:id"), {"id": eid}).fetchall()]
    for ev in events:
        ev["relays"] = relays_by_event.get(ev["event_id"], [])
    payout = public_payout_record(payout)
    enrollment = public_enrollment_record(enrollment)
    return {
        "conversion_id": conversion_id,
        "merchant_pubkey": campaign["merchant_pubkey"] if campaign else None,
        "affiliate_pubkey": conversion["affiliate_pubkey"],
        "campaign": campaign,
        "enrollment": enrollment,
        "click": click,
        "conversion": conversion,
        "payout": payout,
        "reversal": reversal,
        "events": events,
        "links": {
            "campaign_event": f"/nostr/events/{campaign['nostr_event_id']}" if campaign else None,
            "enrollment_event": f"/nostr/events/{enrollment['nostr_event_id']}" if enrollment else None,
            "conversion_event": f"/nostr/events/{conversion['nostr_event_id']}",
        },
    }


@app.get("/flows/{conversion_id}")
def get_flow_receipt(conversion_id: str) -> dict[str, Any]:
    return flow_receipt_data(conversion_id)


def _short(value: Any, front: int = 12, back: int = 8) -> str:
    raw = str(value or "")
    return raw if len(raw) <= front + back + 1 else f"{raw[:front]}…{raw[-back:]}"


@app.get("/campaigns/{campaign_id}/page", response_class=HTMLResponse)
def campaign_public_page(request: Request, campaign_id: str) -> Response:
    data = campaign_public_data(campaign_id)
    return templates.TemplateResponse(
        request=request,
        name="campaign_public.html",
        context={**data, "short": _short, "status": _receipt_status},
    )

@app.get("/flows/{conversion_id}/receipt", response_class=HTMLResponse)
def flow_receipt_page(request: Request, conversion_id: str) -> Response:
    data = flow_receipt_data(conversion_id)
    return templates.TemplateResponse(
        request=request,
        name="flow_receipt.html",
        context={**data, "short": _short, "status": _receipt_status},
    )

@app.post("/demo")
def demo() -> dict[str, Any]:
    campaign = create_campaign(CampaignIn(merchant_pubkey=DEFAULT_MERCHANT_NPUB, destination_url=f"{BASE_URL}/demo-checkout"))
    enrollment = _create_enrollment_record(EnrollmentIn(campaign_id=campaign["campaign_id"], affiliate_pubkey=DEFAULT_AFFILIATE_NPUB, lightning_address="affiliate@getalby.com"))
    click_id = hid("clk")
    with engine().begin() as c:
        c.execute(
            text(
                """
                INSERT INTO clicks (id, ref_code, campaign_id, affiliate_pubkey, ip_hash,
                user_agent_hash, landing_url, created_at)
                VALUES (:id, :ref_code, :campaign_id, :affiliate_pubkey, :ip_hash,
                :user_agent_hash, :landing_url, :created_at)
                """
            ),
            {
                "id": click_id,
                "ref_code": enrollment["ref_code"],
                "campaign_id": campaign["campaign_id"],
                "affiliate_pubkey": DEFAULT_AFFILIATE_NPUB,
                "ip_hash": sha("demo-ip"),
                "user_agent_hash": sha("demo-ua"),
                "landing_url": f"{BASE_URL}/demo-checkout",
                "created_at": now(),
            },
        )
    conversion = create_conversion(ConversionIn(order_id=hid("ord"), click_id=click_id, order_total=100.0, currency="USD"))
    return {"campaign": campaign, "enrollment": enrollment, "click_id": click_id, "conversion": conversion, "affiliate": affiliate_summary(DEFAULT_AFFILIATE_NPUB)}



BB_JS = r"""
(function(){
  var COOKIE_DAYS = 30;
  function readParams(){
    var p = new URLSearchParams(window.location.search || '');
    return { bb_click_id: p.get('bb_click_id'), bb_ref: p.get('bb_ref') };
  }
  function setCookie(name, value){
    if(!value) return;
    var maxAge = COOKIE_DAYS * 24 * 60 * 60;
    document.cookie = name + '=' + encodeURIComponent(value) + '; path=/; max-age=' + maxAge + '; SameSite=Lax';
  }
  function getCookie(name){
    var match = document.cookie.match(new RegExp('(?:^|; )' + name.replace(/[.$?*|{}()\[\]\\\/\+^]/g, '\\$&') + '=([^;]*)'));
    return match ? decodeURIComponent(match[1]) : null;
  }
  function save(attribution){
    if(attribution.bb_click_id) { localStorage.setItem('bb_click_id', attribution.bb_click_id); setCookie('bb_click_id', attribution.bb_click_id); }
    if(attribution.bb_ref) { localStorage.setItem('bb_ref', attribution.bb_ref); setCookie('bb_ref', attribution.bb_ref); }
  }
  function get(){
    return {
      bb_click_id: localStorage.getItem('bb_click_id') || getCookie('bb_click_id'),
      bb_ref: localStorage.getItem('bb_ref') || getCookie('bb_ref')
    };
  }
  function injectHiddenInputs(root){
    var attr = get();
    root = root || document;
    Array.prototype.forEach.call(root.querySelectorAll('form'), function(form){
      ['bb_click_id','bb_ref'].forEach(function(name){
        var val = attr[name];
        if(!val) return;
        var input = form.querySelector('input[name="'+name+'"]');
        if(!input){
          input = document.createElement('input');
          input.type = 'hidden';
          input.name = name;
          form.appendChild(input);
        }
        input.value = val;
      });
    });
  }
  function init(){
    var params = readParams();
    if(params.bb_click_id || params.bb_ref) save(params);
    injectHiddenInputs(document);
    window.dispatchEvent(new CustomEvent('bumbei:attribution', { detail: get() }));
  }
  window.BumbeiAttribution = {
    init: init,
    get: get,
    save: save,
    injectHiddenInputs: injectHiddenInputs,
    debug: function(){ console.table(get()); return get(); }
  };
  if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
"""


@app.get("/bb.js")
def bb_js() -> Response:
    return Response(BB_JS, media_type="application/javascript", headers={"Cache-Control": "public, max-age=300"})


@app.get("/demo-merchant", response_class=HTMLResponse)
def demo_merchant_page() -> str:
    return f"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Demo merchant checkout · Bumbei</title>
  <style>
    :root {{ --black:#151615; --orange:#FC6A42; --gray:#E3E3D7; --yellow:#F9C441; --muted:#a8aa9e; --ok:#75d68a; }}
    * {{ box-sizing:border-box; }} body {{ margin:0; font-family:Inter,system-ui,sans-serif; background:radial-gradient(circle at top left, rgba(252,106,66,.24), transparent 30rem), var(--black); color:#fff; }}
    main {{ width:min(980px,100%); margin:0 auto; padding:42px clamp(16px,4vw,52px); }}
    .card {{ background:linear-gradient(180deg, rgba(255,255,255,.08), rgba(255,255,255,.035)); border:1px solid rgba(227,227,215,.14); border-radius:26px; padding:24px; box-shadow:0 24px 70px rgba(0,0,0,.28); }}
    h1 {{ font-size:clamp(36px,6vw,70px); line-height:.92; letter-spacing:-.06em; margin:0 0 16px; }} p {{ color:var(--muted); line-height:1.6; }}
    code,pre {{ background:#0b0c0b; color:#fff; border-radius:12px; padding:4px 7px; word-break:break-all; }} pre {{ padding:16px; overflow:auto; }}
    label {{ display:block; margin:12px 0 6px; color:var(--gray); }} input,select,button {{ width:100%; padding:13px; border-radius:14px; border:1px solid rgba(227,227,215,.18); background:#111210; color:#fff; font:inherit; }}
    button {{ margin-top:16px; background:var(--orange); color:var(--black); font-weight:900; cursor:pointer; }} .grid {{ display:grid; grid-template-columns:1fr 1fr; gap:18px; }}
    .pill {{ display:inline-flex; border:1px solid rgba(227,227,215,.14); border-radius:999px; padding:8px 12px; color:var(--gray); background:rgba(227,227,215,.06); }}
    .ok {{ color:var(--ok); }} @media(max-width:760px){{.grid{{grid-template-columns:1fr}}}}
  </style>
</head>
<body>
<main>
  <span class="pill">Demo merchant landing / checkout</span>
  <h1>Simulate an Oshigoods-style sats order.</h1>
  <p>This page loads <code>/bb.js</code>, captures <code>bb_click_id</code>/<code>bb_ref</code> from the URL, injects hidden checkout fields, and posts to a demo merchant backend trigger.</p>
  <div class="grid">
    <section class="card">
      <h2>Attribution captured by snippet</h2>
      <pre id="debug">Loading attribution…</pre>
      <p>Try visiting this page with <code>?bb_click_id=clk_y8DrWEwJ8R&amp;bb_ref=ref_I6al7223jL</code>.</p>
    </section>
    <section class="card">
      <h2>Checkout action</h2>
      <form id="checkout-form">
        <label>Order total</label><input name="order_total" type="number" value="250000" />
        <label>Currency</label><select name="currency"><option>SATS</option><option>BTC</option><option>USD</option></select>
        <button type="submit">Simulate paid order → trigger conversion</button>
      </form>
      <pre id="result">Submit to create conversion proof.</pre>
    </section>
  </div>
</main>
<script src="/bb.js"></script>
<script>
function showAttr() {{ document.getElementById('debug').textContent = JSON.stringify(window.BumbeiAttribution.get(), null, 2); }}
window.addEventListener('bumbei:attribution', showAttr); setTimeout(showAttr, 150);
document.getElementById('checkout-form').addEventListener('submit', async function(e) {{
  e.preventDefault();
  window.BumbeiAttribution.injectHiddenInputs(document);
  const fd = new FormData(e.currentTarget);
  const payload = Object.fromEntries(fd.entries());
  payload.order_total = Number(payload.order_total);
  const res = await fetch('/demo-merchant/checkout', {{ method:'POST', headers:{{'content-type':'application/json'}}, body:JSON.stringify(payload) }});
  const json = await res.json();
  document.getElementById('result').textContent = JSON.stringify(json, null, 2);
  if(json.receipt_url) window.open(json.receipt_url, '_blank');
}});
</script>
</body>
</html>
"""


@app.post("/demo-merchant/checkout")
def demo_merchant_checkout(body: DemoMerchantCheckoutIn) -> dict[str, Any]:
    """Demo-only merchant backend trigger. Real merchants should use /merchant/conversions server-side."""
    conversion = create_conversion(
        ConversionIn(
            order_id=hid("demo_order"),
            click_id=body.bb_click_id,
            order_total=body.order_total,
            currency=body.currency,
        )
    )
    return {
        "ok": True,
        "demo": True,
        "bb_click_id": body.bb_click_id,
        "bb_ref": body.bb_ref,
        "order_total_sats": conversion["order_total_sats"],
        "btc_usd_rate": conversion["btc_usd_rate"],
        "sats_per_usd": conversion["sats_per_usd"],
        "rate_source": conversion["rate_source"],
        "rate_observed_at": conversion["rate_observed_at"],
        "rate_stale": conversion["rate_stale"],
        "conversion_id": conversion["conversion_id"],
        "nostr_event_id": conversion["nostr_event_id"],
        "commission_sats": conversion["commission_sats"],
        "receipt_url": f"{BASE_URL}/flows/{conversion['conversion_id']}/receipt",
        "json_receipt_url": f"{BASE_URL}/flows/{conversion['conversion_id']}",
        "relay_results": conversion["relay_results"],
    }


DASHBOARD_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Nostr Affiliate POC Dashboard</title>
  <style>
    :root { --black:#151615; --orange:#FC6A42; --gray:#E3E3D7; --blue:#6082DB; --yellow:#F9C441; --card:#20211f; --muted:#a8aa9e; --ok:#75d68a; --bad:#ff8585; }
    * { box-sizing:border-box; }
    body { margin:0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif; background: radial-gradient(circle at top left, rgba(252,106,66,.25), transparent 32rem), var(--black); color:#fff; }
    header { padding:32px clamp(18px,4vw,56px); border-bottom:1px solid rgba(227,227,215,.12); display:flex; justify-content:space-between; gap:20px; align-items:flex-start; }
    h1,h2,h3 { font-family: Unbounded, Inter, ui-sans-serif, system-ui, sans-serif; letter-spacing:-.04em; margin:0; }
    h1 { font-size:clamp(32px,5vw,64px); line-height:.95; max-width:820px; }
    h2 { font-size:22px; margin-bottom:14px; }
    p { color:var(--muted); line-height:1.55; }
    a { color:var(--yellow); }
    main { width:min(1440px,100%); margin:0 auto; padding:28px clamp(18px,4vw,56px) 60px; display:grid; gap:22px; }
    .pill { display:inline-flex; align-items:center; gap:8px; border:1px solid rgba(227,227,215,.15); background:rgba(227,227,215,.06); border-radius:999px; padding:8px 12px; color:var(--gray); font-size:13px; white-space:nowrap; }
    .grid { display:grid; grid-template-columns:repeat(12,minmax(0,1fr)); gap:18px; width:100%; }
    .metrics-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:16px; width:100%; }
    .card { min-width:0; background:linear-gradient(180deg, rgba(255,255,255,.07), rgba(255,255,255,.035)); border:1px solid rgba(227,227,215,.12); border-radius:22px; padding:20px; box-shadow:0 20px 60px rgba(0,0,0,.24); overflow:hidden; }
    .span-3{grid-column:span 3 / span 3}.span-4{grid-column:span 4 / span 4}.span-5{grid-column:span 5 / span 5}.span-6{grid-column:span 6 / span 6}.span-7{grid-column:span 7 / span 7}.span-8{grid-column:span 8 / span 8}.span-12{grid-column:1 / -1}
    .metric { font-size:34px; font-weight:800; margin-top:8px; line-height:1; overflow-wrap:anywhere; }
    .label { color:var(--muted); font-size:13px; overflow-wrap:anywhere; }
    input, button, select { width:100%; min-width:0; border:1px solid rgba(227,227,215,.18); border-radius:14px; padding:12px 13px; background:#111210; color:#fff; font:inherit; }
    button { cursor:pointer; background:var(--orange); border-color:var(--orange); color:#151615; font-weight:800; transition:.15s transform ease; line-height:1.2; }
    button:hover { transform:translateY(-1px); }
    button.secondary { background:rgba(227,227,215,.08); color:var(--gray); border-color:rgba(227,227,215,.18); }
    .row { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; margin:10px 0; }
    pre { max-height:360px; overflow:auto; background:#0b0c0b; border:1px solid rgba(227,227,215,.12); border-radius:16px; padding:14px; color:#dfe2d1; font-size:12px; white-space:pre-wrap; word-break:break-word; }
    .table-wrap { width:100%; overflow-x:auto; }
    table { width:100%; min-width:560px; border-collapse:collapse; font-size:13px; table-layout:fixed; }
    th,td { text-align:left; padding:10px 8px; border-bottom:1px solid rgba(227,227,215,.09); vertical-align:top; overflow-wrap:anywhere; word-break:break-word; }
    th { color:var(--muted); font-weight:600; }
    code { color:#fff; background:rgba(227,227,215,.09); padding:2px 5px; border-radius:6px; overflow-wrap:anywhere; word-break:break-all; }
    .status { display:inline-block; padding:4px 8px; border-radius:999px; font-size:12px; background:rgba(227,227,215,.1); }
    .published { background:rgba(117,214,138,.18); color:var(--ok); }
    .failed { background:rgba(255,133,133,.18); color:var(--bad); }
    .skipped { background:rgba(249,196,65,.18); color:var(--yellow); }
    .flow { display:flex; flex-wrap:wrap; gap:10px; align-items:center; }
    .flow span { padding:10px 12px; background:rgba(96,130,219,.16); border:1px solid rgba(96,130,219,.28); border-radius:14px; }
    .toast { position:fixed; right:20px; bottom:20px; max-width:420px; padding:14px 16px; border-radius:16px; background:#fff; color:#151615; box-shadow:0 20px 60px rgba(0,0,0,.35); display:none; }
    @media (max-width: 1100px){ .span-4,.span-5,.span-6,.span-7,.span-8{grid-column:1 / -1} }
    @media (max-width: 900px){ header{display:block} h1{font-size:42px}.row{grid-template-columns:1fr}.metrics-grid{grid-template-columns:repeat(2,minmax(0,1fr))} }
    @media (max-width: 560px){ main,header{padding-left:16px;padding-right:16px}.metrics-grid{grid-template-columns:1fr}.flow{align-items:flex-start}.flow span{width:100%} }
  </style>
</head>
<body>
<header>
  <div>
    <div class="pill">⚡ Bumbei x Nostr affiliate proof POC</div>
    <h1>Affiliate identity, attribution proofs & Lightning-ready payouts.</h1>
    <p>Demo dashboard para crear campañas, enrolar afiliados, simular clicks/conversiones y ver eventos Nostr publicados en relays públicos.</p>
  </div>
  <div class="pill" id="health-pill">Loading…</div>
</header>
<main>
  <section class="metrics-grid" id="metrics"></section>
  <section class="card span-12">
    <h2>End-to-end flow</h2>
    <div class="flow"><span>Campaign</span>→<span>Enrollment</span>→<span>Click</span>→<span>Conversion</span>→<span>Nostr proof</span>→<span>Pending sats</span></div>
  </section>
  <section class="card span-12">
    <h2>Merchant webhook</h2>
    <p>Para integraciones server-to-server tipo Shopify/WooCommerce/custom checkout: <code>POST /merchant/conversions</code>. El merchant devuelve <code>bb_click_id</code>; si reporta <code>USD</code>, Bumbei calcula sats con su rate server-side. También acepta <code>SATS</code> o <code>BTC</code> para merchants Nostr-native como Oshigoods.</p>
    <pre>{
  "order_id": "order_123",
  "bb_click_id": "clk_y8DrWEwJ8R",
  "order_total": 250000,
  "currency": "SATS",
  "customer_hash": "sha256:...",
  "metadata": {"platform": "oshigoods"}
}</pre>
  </section>
  <section class="grid">
    <div class="card span-4">
      <h2>1. Create campaign</h2>
      <div class="row"><input id="merchant" value="npub1540rxhz9x7fpc73nu5q3qydykej7lceh5j4jej6mmpc6n3saw3cqv7s8js" placeholder="merchant pubkey"><input id="campaignName" value="Bumbei BTC Rewards" placeholder="campaign name"></div>
      <div class="row"><input id="commission" type="number" value="800" placeholder="bps"><input id="windowDays" type="number" value="30" placeholder="window days"></div>
      <input id="destination" value="https://example.com/checkout" placeholder="destination URL">
      <p><button onclick="createCampaign()">Create campaign + publish Nostr event</button></p>
    </div>
    <div class="card span-4">
      <h2>2. Enroll affiliate</h2>
      <input id="campaignId" placeholder="campaign_id from step 1">
      <div class="row"><input id="affiliate" value="npub16ghkhw9d4g9x6pxp6l6dtyjqaeuavwucrq8gpkt60x0kx9fzqwpszhtw0n" placeholder="affiliate pubkey"><input id="lightning" value="affiliate@getalby.com" placeholder="Lightning address"></div>
      <p><button onclick="createEnrollment()">Enroll + generate ref link</button></p>
      <p id="refBox" class="label"></p>
    </div>
    <div class="card span-4">
      <h2>3. Click + conversion</h2>
      <input id="refCode" placeholder="ref_code from enrollment">
      <p><button class="secondary" onclick="simulateClick()">Simulate click</button></p>
      <input id="clickId" placeholder="click_id">
      <input id="orderTotal" type="number" min="0.01" step="0.01" value="100" aria-label="Order total in USD">
      <p class="label">BTC/USD is resolved server-side from live providers.</p>
      <p><button onclick="createConversion()">Create conversion proof</button></p>
    </div>
  </section>
  <section class="grid">
    <div class="card span-7"><h2>Recent Nostr events</h2><div id="events"></div></div>
    <div class="card span-5"><h2>Latest result</h2><pre id="result">Run a flow or click “Run full demo”.</pre><button class="secondary" onclick="runDemo()">Run full demo</button></div>
  </section>
  <section class="grid">
    <div class="card span-6"><h2>Campaigns</h2><div id="campaigns"></div></div>
    <div class="card span-6"><h2>Conversions</h2><div id="conversions"></div></div>
  </section>
</main>
<div id="toast" class="toast"></div>
<script>
const $ = id => document.getElementById(id);
function toast(msg){ const t=$('toast'); t.textContent=msg; t.style.display='block'; setTimeout(()=>t.style.display='none',3500); }
function show(obj){ $('result').textContent = JSON.stringify(obj, null, 2); }
async function api(path, opts={}){
  const res = await fetch(path, {headers:{'content-type':'application/json'}, ...opts});
  const data = await res.json().catch(()=>({error:'non-json response'}));
  if(!res.ok) throw new Error(data.detail || data.error || res.statusText);
  return data;
}
function short(x){ return x ? String(x).slice(0,10)+'…'+String(x).slice(-6) : ''; }
function esc(value){ return String(value ?? '').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function safePath(value){ return encodeURIComponent(String(value ?? '')); }
function status(s){ const label=String(s ?? 'unknown'); const cls=['published','failed','skipped','pending','success','error'].includes(label.toLowerCase())?label.toLowerCase():'unknown'; return `<span class="status ${cls}">${esc(label)}</span>`; }
function table(rows, cols){ if(!rows?.length) return '<p class="label">No rows yet.</p>'; return `<div class="table-wrap"><table><thead><tr>${cols.map(c=>`<th>${esc(c[0])}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${cols.map(c=>`<td>${c[2]?c[2](r[c[1]],r):esc(r[c[1]])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`; }
async function refresh(){
  const data = await api('/ops/data');
  $('health-pill').textContent = `${data.health.db} · Nostr publish ${data.health.nostr_publish ? 'on' : 'off'}`;
  const metrics = [['Campaigns',data.counts.campaigns],['Enrollments',data.counts.enrollments],['Clicks',data.counts.clicks],['Conversions',data.counts.conversions],['Pending sats',data.counts.pending_sats],['Published events',data.counts.published_events]];
  $('metrics').innerHTML = metrics.map(m=>`<div class="card metric-card"><div class="label">${esc(m[0])}</div><div class="metric">${esc(m[1])}</div></div>`).join('');
  $('campaigns').innerHTML = table(data.campaigns, [['ID','id',v=>`<code>${esc(v)}</code>`],['Name','name'],['bps','commission_bps'],['Event','nostr_event_id',v=>`<a href="/nostr/events/${safePath(v)}">${esc(short(v))}</a>`],['Page','id',v=>`<a href="/campaigns/${safePath(v)}/page">open</a>`]]);
  $('conversions').innerHTML = table(data.conversions, [['ID','id',v=>`<code>${esc(v)}</code>`],['Affiliate','affiliate_pubkey',v=>`<a href="/affiliates/${safePath(v)}/profile">${esc(short(v))}</a>`],['sats','commission_sats'],['Event','nostr_event_id',v=>`<a href="/nostr/events/${safePath(v)}">${esc(short(v))}</a>`],['Receipt','id',v=>`<a href="/flows/${safePath(v)}/receipt">open</a>`]]);
  $('events').innerHTML = table(data.events, [['Kind','kind'],['Entity','entity_type',(v,r)=>`${esc(v)}<br><code>${esc(r.entity_id)}</code>`],['Relay','relay_status',status],['Event','event_id',v=>`<a href="/nostr/events/${safePath(v)}">${esc(short(v))}</a>`],['Relays','relays',(v)=>(Array.isArray(v)?v:[]).map(r=>`${status(r.status)} ${esc(String(r.relay_url ?? '').replace('wss://',''))}`).join('<br>')]]);
}
async function createCampaign(){
  const data = await api('/campaigns',{method:'POST',body:JSON.stringify({merchant_pubkey:$('merchant').value,name:$('campaignName').value,commission_bps:+$('commission').value,attribution_window_days:+$('windowDays').value,destination_url:$('destination').value})});
  $('campaignId').value=data.campaign_id; show(data); toast('Campaign created'); await refresh();
}
async function createEnrollment(){
  const data = await api('/enrollments',{method:'POST',body:JSON.stringify({campaign_id:$('campaignId').value,affiliate_pubkey:$('affiliate').value,lightning_address:$('lightning').value})});
  $('refCode').value=data.ref_code; $('refBox').innerHTML=`Ref URL: <a href="${esc(data.ref_url)}" target="_blank" rel="noopener">${esc(data.ref_url)}</a>`; show(data); toast('Affiliate enrolled'); await refresh();
}
async function simulateClick(){
  const data = await api('/clicks/simulate',{method:'POST',body:JSON.stringify({ref_code:$('refCode').value})});
  $('clickId').value=data.click_id; show(data); toast('Click simulated'); await refresh();
}
async function createConversion(){
  const data = await api('/conversions',{method:'POST',body:JSON.stringify({order_id:'ord_'+crypto.randomUUID(),click_id:$('clickId').value,order_total:$('orderTotal').value,currency:'USD'})});
  show(data); toast('Conversion proof published'); await refresh();
}
async function runDemo(){ const data = await api('/demo',{method:'POST'}); $('campaignId').value=data.campaign.campaign_id; $('refCode').value=data.enrollment.ref_code; $('clickId').value=data.click_id; show(data); toast('Full demo complete'); await refresh(); }
refresh().catch(e=>toast(e.message));
</script>
</body>
</html>
"""


@app.get("/app", response_class=HTMLResponse)
def account_entry(request: Request, role: str | None = None) -> Response:
    session = _session_account(request)
    if session:
        destination = "/ops" if session["role"] == "ops" else f"/app/{session['role']}"
        return RedirectResponse(destination, status_code=303)
    requested_role = role if role in {"merchant", "affiliate", "ops"} else None
    return templates.TemplateResponse(request=request, name="login.html", context={"requested_role": requested_role})


def _account_shell(session: dict[str, Any], role: str) -> dict[str, Any]:
    return {
        "npub": session["npub"],
        "npub_short": workspace_short(session["npub"]),
        "role": role,
    }


@app.get("/app/merchant", response_class=HTMLResponse)
def merchant_account_page(request: Request, view: str = "overview") -> Response:
    session = _session_account(request, "merchant")
    if not session:
        return RedirectResponse("/app?role=merchant", status_code=303)
    valid_views = {"overview", "campaigns", "affiliates", "activity", "payouts", "integration", "settings"}
    if view not in valid_views:
        raise HTTPException(404, "merchant workspace view not found")
    configured_shopify_merchant = os.getenv("SHOPIFY_MERCHANT_PUBKEY", DEFAULT_MERCHANT_NPUB)
    try:
        shopify_merchant_hex = normalize_pubkey(configured_shopify_merchant, "SHOPIFY_MERCHANT_PUBKEY")["hex"]
    except HTTPException:
        shopify_merchant_hex = ""
    with engine().connect() as c:
        owns_shopify_store = session["nostr_pubkey_hex"] == shopify_merchant_hex or bool(
            c.execute(
                text("SELECT 1 FROM merchant_account_links WHERE account_id=:account_id AND merchant_pubkey_hex=:hex LIMIT 1"),
                {"account_id": session["account_id"], "hex": shopify_merchant_hex},
            ).fetchone()
        )
        bootstrap_rows = c.execute(
            text(
                """
                SELECT merchant_pubkey_hex FROM merchant_account_links
                WHERE account_id=:account_id AND source='environment_binding'
                  AND merchant_pubkey_hex<>:owner_hex
                ORDER BY merchant_pubkey_hex
                """
            ),
            {"account_id": session["account_id"], "owner_hex": session["nostr_pubkey_hex"]},
        ).fetchall()
    bootstrap_tenants = [
        {
            "merchant_pubkey": PublicKey.parse(row._mapping["merchant_pubkey_hex"]).to_bech32(),
            "merchant_short": workspace_short(PublicKey.parse(row._mapping["merchant_pubkey_hex"]).to_bech32()),
        }
        for row in bootstrap_rows
    ]
    webhook = shopify_webhook_status() if owns_shopify_store else {"secret_configured": False, "store_configured": False, "receipts": {}}
    configured = bool(webhook.get("secret_configured") and webhook.get("store_configured"))
    processed = int(webhook.get("receipts", {}).get("processed", 0))
    shopify_ready = configured and processed > 0
    if shopify_ready:
        detail = f"{processed} webhook{'s' if processed != 1 else ''} orders/paid procesado{'s' if processed != 1 else ''}."
    elif configured:
        detail = "Configurado; esperando el primer webhook orders/paid válido."
    else:
        detail = "La integración de Shopify todavía no está configurada para este merchant."
    with engine().connect() as c:
        data = merchant_workspace_data(c, session, base_url=BASE_URL, shopify_ready=shopify_ready, shopify_detail=detail)
    if not data["totals"]["campaigns"]:
        return RedirectResponse("/app/merchant/onboarding", status_code=303)
    view_meta = {
        "overview": {"eyebrow": "Merchant account", "title": "Tu programa, bajo control.", "lede": "El estado de tu programa, las próximas acciones y sus resultados en un solo lugar."},
        "campaigns": {"eyebrow": "Programa", "title": "Campañas", "lede": "Condiciones públicas, estado y rendimiento de tus programas de afiliados."},
        "affiliates": {"eyebrow": "Comunidad", "title": "Affiliates", "lede": "Invitá personas y administrá las identidades enroladas en tus campañas."},
        "activity": {"eyebrow": "Analytics", "title": "Actividad", "lede": "Clicks, conversiones y comisiones confirmadas desde tus enlaces."},
        "payouts": {"eyebrow": "Lightning", "title": "Pagos", "lede": "Obligaciones de pago sin custodia y evidencia verificable de liquidación."},
        "integration": {"eyebrow": "Commerce", "title": "Integración Shopify", "lede": "Tracking, Pixel y webhook firmado para atribución autoritativa."},
        "settings": {"eyebrow": "Configuración", "title": "Marca e invitación", "lede": "Actualizá la identidad pública del Merchant y el mensaje de cada campaña."},
    }[view]
    nav_items = [
        ("Resumen", "overview"),
        ("Programa", "campaigns"),
        ("Affiliates", "affiliates"),
        ("Actividad", "activity"),
        ("Pagos", "payouts"),
        ("Shopify", "integration"),
        ("Configuración", "settings"),
    ]
    return templates.TemplateResponse(
        request=request,
        name="merchant.html",
        context={
            **data,
            "bootstrap_tenants": bootstrap_tenants,
            "shopify_installation": shopify_installation_snippets(
                BASE_URL, normalized_shopify_store_domain()
            ) if owns_shopify_store else None,
            "short_link_base_url": SHORT_LINK_BASE_URL,
            "program_defaults": _merchant_default_program(),
            "account": _account_shell(session, "merchant"),
            "role_label": "Merchant account",
            "view": view,
            "view_meta": view_meta,
            "nav": [
                {
                    "label": label,
                    "href": "/app/merchant" if key == "overview" else f"/app/merchant?view={key}",
                    "active": view == key,
                }
                for label, key in nav_items
            ],
        },
    )


@app.get("/app/merchant/onboarding", response_class=HTMLResponse)
def merchant_onboarding_page(request: Request) -> Response:
    session = _session_account(request, "merchant")
    if not session:
        return RedirectResponse("/app?role=merchant", status_code=303)
    with engine().connect() as c:
        bootstrap_rows = c.execute(
            text(
                """
                SELECT link.merchant_pubkey_hex FROM merchant_account_links link
                WHERE link.account_id=:account_id AND link.source='environment_binding'
                  AND link.merchant_pubkey_hex<>:owner_hex
                  AND NOT EXISTS (
                    SELECT 1 FROM campaigns campaign
                    WHERE campaign.merchant_pubkey_hex=link.merchant_pubkey_hex
                  )
                ORDER BY link.merchant_pubkey_hex
                """
            ),
            {"account_id": session["account_id"], "owner_hex": session["nostr_pubkey_hex"]},
        ).fetchall()
    bootstrap_tenants = [
        {
            "merchant_pubkey": PublicKey.parse(row._mapping["merchant_pubkey_hex"]).to_bech32(),
            "merchant_short": workspace_short(PublicKey.parse(row._mapping["merchant_pubkey_hex"]).to_bech32()),
        }
        for row in bootstrap_rows
    ]
    if not bootstrap_tenants:
        return RedirectResponse("/app/merchant?view=settings", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="merchant_onboarding.html",
        context={
            "bootstrap_tenants": bootstrap_tenants,
            "program_defaults": _merchant_default_program(),
            "account": _account_shell(session, "merchant"),
            "role_label": "Merchant onboarding",
            "nav": [{"label": "Onboarding", "href": "/app/merchant/onboarding", "active": True}],
        },
    )


def _require_same_origin(request: Request) -> None:
    expected = urlparse(BASE_URL)
    supplied = urlparse(request.headers.get("origin", ""))
    if supplied.scheme != expected.scheme or supplied.netloc != expected.netloc:
        raise HTTPException(403, "same-origin request required")


def _normalized_merchant_url(value: str, field_name: str, *, logo: bool = False) -> str:
    normalized = safe_text(value, 3000 if not logo else 2048)
    try:
        parsed = urlparse(normalized)
        hostname = parsed.hostname
        port = parsed.port
    except (UnicodeError, ValueError) as exc:
        raise HTTPException(422, f"{field_name} must be a valid URL") from exc
    allowed_schemes = {"https"} if logo else {"http", "https"}
    if parsed.scheme.lower() not in allowed_schemes or not parsed.netloc or not hostname:
        raise HTTPException(422, f"{field_name} must be a valid {'HTTPS' if logo else 'HTTP(S)'} URL")
    if parsed.username or parsed.password:
        raise HTTPException(422, f"{field_name} must not contain credentials")
    try:
        hostname = hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as exc:
        raise HTTPException(422, f"{field_name} has an invalid host") from exc
    if len(hostname) > 253 or any(
        not label or len(label) > 63 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
        for label in hostname.split(".")
    ):
        try:
            ipaddress.ip_address(hostname)
        except ValueError as exc:
            raise HTTPException(422, f"{field_name} has an invalid host") from exc
    if logo:
        if port not in {None, 443}:
            raise HTTPException(422, "logo_url must use the standard HTTPS port")
        if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
            raise HTTPException(422, "logo_url must use a public host")
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            if re.fullmatch(r"(?:0x[0-9a-f]+|[0-9.]+)", hostname):
                raise HTTPException(422, "logo_url must use a public host")
        else:
            if not address.is_global:
                raise HTTPException(422, "logo_url must use a public host")
        if unquote(parsed.path).lower().endswith(".svg"):
            raise HTTPException(422, "logo_url SVG images are not supported")
    return urlunparse(parsed._replace(fragment=""))


def _merchant_requested_program(body: MerchantBootstrapIn) -> dict[str, Any]:
    defaults = _merchant_default_program()
    name = safe_text(body.program_name if body.program_name is not None else defaults["name"], 160)
    if not name:
        raise HTTPException(422, "program_name is required")
    commission_percent = (
        body.commission_percent
        if body.commission_percent is not None
        else Decimal(defaults["commission_bps"]) / Decimal(100)
    )
    commission_bps = int(commission_percent * Decimal(100))
    window_days = body.attribution_window_days or defaults["window_days"]
    destination_url = _normalized_merchant_url(
        body.destination_url if body.destination_url is not None else defaults["destination_url"],
        "destination_url",
    )
    terms_url = _normalized_merchant_url(
        body.terms_url if body.terms_url is not None else defaults["terms_url"],
        "terms_url",
    )
    logo_url = None
    if body.logo_url is not None and safe_text(body.logo_url, 2048):
        logo_url = _normalized_merchant_url(body.logo_url, "logo_url", logo=True)
    return {
        "name": name,
        "commission_bps": commission_bps,
        "window_days": window_days,
        "destination_url": destination_url,
        "terms_url": terms_url,
        "logo_url": logo_url,
    }


def _merchant_default_program() -> dict[str, Any]:
    try:
        commission_bps = int(os.getenv("MERCHANT_DEFAULT_COMMISSION_BPS", "800"))
        window_days = int(os.getenv("MERCHANT_DEFAULT_WINDOW_DAYS", "30"))
    except ValueError as exc:
        raise HTTPException(503, "merchant default program configuration is invalid") from exc
    if not 1 <= commission_bps <= 10_000 or not 1 <= window_days <= 365:
        raise HTTPException(503, "merchant default program configuration is invalid")

    name = safe_text(os.getenv("MERCHANT_DEFAULT_PROGRAM_NAME", "Meerat Affiliate Program"), 160)
    if not name:
        raise HTTPException(503, "merchant default program configuration is invalid")

    def configured_url(env_name: str, fallback: str) -> str:
        value = safe_text(os.getenv(env_name, fallback), 3000)
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise HTTPException(503, "merchant default program configuration is invalid")
        return value

    return {
        "name": name,
        "commission_bps": commission_bps,
        "window_days": window_days,
        "destination_url": configured_url("MERCHANT_DEFAULT_DESTINATION_URL", DEFAULT_DESTINATION),
        "terms_url": configured_url("MERCHANT_DEFAULT_TERMS_URL", "https://bumbei.com/terms/affiliate"),
    }


def _merchant_campaign_publication_needed(event: dict[str, Any]) -> bool:
    relay_status = event.get("relay_status")
    if relay_status == "published":
        return False
    if relay_status == "skipped":
        return nostr_publish_enabled()
    return True


def _merchant_profile_target(c: Any, session: dict[str, Any], merchant_hex: str) -> bool:
    return bool(
        c.execute(
            text(
                """
                SELECT 1
                WHERE :merchant_hex=:owner_hex
                   OR EXISTS (
                     SELECT 1 FROM merchant_account_links link
                     WHERE link.account_id=:account_id
                       AND link.merchant_pubkey_hex=:merchant_hex
                   )
                """
            ),
            {
                "merchant_hex": merchant_hex,
                "owner_hex": session["nostr_pubkey_hex"],
                "account_id": session["account_id"],
            },
        ).fetchone()
    )


@app.put("/app/merchant/profile", tags=["Accounts"])
def merchant_update_profile(body: MerchantProfileIn, request: Request) -> dict[str, Any]:
    session = require_account_session(request, "merchant")
    _require_same_origin(request)
    merchant = normalize_pubkey(body.merchant_pubkey, "merchant_pubkey")
    raw_logo_url = safe_text(body.logo_url, 2048)
    logo_url = _normalized_merchant_url(raw_logo_url, "logo_url", logo=True) if raw_logo_url else None
    display_name = safe_text(body.display_name, 120)
    tagline = safe_text(body.tagline, 180)
    init_db()
    timestamp = now()
    with engine().begin() as c:
        if not _merchant_profile_target(c, session, merchant["hex"]):
            raise HTTPException(404, "merchant profile target not found")
        c.execute(
            text(
                """
                INSERT INTO merchant_profiles
                  (merchant_pubkey_hex, merchant_pubkey, display_name, tagline, logo_url, created_at, updated_at)
                VALUES (:hex, :npub, :display_name, :tagline, :logo_url, :created_at, :updated_at)
                ON CONFLICT(merchant_pubkey_hex) DO UPDATE SET
                  merchant_pubkey=:npub, display_name=:display_name, tagline=:tagline,
                  logo_url=:logo_url, updated_at=:updated_at
                """
            ),
            {
                "hex": merchant["hex"],
                "npub": merchant["npub"],
                "display_name": display_name or None,
                "tagline": tagline or None,
                "logo_url": logo_url,
                "created_at": timestamp,
                "updated_at": timestamp,
            },
        )
    return {
        "ok": True,
        "merchant_pubkey": merchant["npub"],
        "display_name": display_name or None,
        "tagline": tagline or None,
        "logo_url": logo_url,
    }


def _persist_merchant_campaign_invite(
    c: Any,
    session: dict[str, Any],
    campaign_id: str,
    invite_eyebrow: str,
    invite_headline: str,
    invite_description: str,
) -> dict[str, Any]:
    campaign = asdict(
        c.execute(
            text("SELECT id, merchant_pubkey_hex FROM campaigns WHERE id=:id LIMIT 1"),
            {"id": campaign_id},
        ).fetchone()
    )
    if not campaign or not _merchant_profile_target(c, session, campaign["merchant_pubkey_hex"]):
        raise HTTPException(404, "campaign not found")
    c.execute(
        text(
            """
            UPDATE campaigns
            SET invite_eyebrow=:invite_eyebrow,
                invite_headline=:invite_headline,
                invite_description=:invite_description
            WHERE id=:id
            """
        ),
        {
            "id": campaign_id,
            "invite_eyebrow": invite_eyebrow or None,
            "invite_headline": invite_headline or None,
            "invite_description": invite_description or None,
        },
    )
    return {
        "ok": True,
        "campaign_id": campaign_id,
        "invite_eyebrow": invite_eyebrow or None,
        "invite_headline": invite_headline or None,
        "invite_description": invite_description or None,
    }


@app.put("/app/merchant/campaign-invite", tags=["Accounts"])
def merchant_update_campaign_invite(body: CampaignInviteBrandingIn, request: Request) -> dict[str, Any]:
    session = require_account_session(request, "merchant")
    _require_same_origin(request)
    invite_eyebrow = safe_text(body.invite_eyebrow, 100)
    invite_headline = safe_text(body.invite_headline, 120)
    invite_description = safe_text(body.invite_description, 360)
    init_db()
    with engine().begin() as c:
        return _persist_merchant_campaign_invite(
            c, session, body.campaign_id, invite_eyebrow, invite_headline, invite_description
        )


@app.post("/app/merchant/bootstrap", tags=["Accounts"])
def merchant_bootstrap(body: MerchantBootstrapIn, request: Request) -> dict[str, Any]:
    session = require_account_session(request, "merchant")
    _require_same_origin(request)
    merchant = normalize_pubkey(body.merchant_pubkey, "merchant_pubkey")
    program = _merchant_requested_program(body)
    campaign_id = f"camp_default_{merchant['hex']}"
    event: dict[str, Any] | None = None
    duplicate = False
    publish_needed = True
    relay_results: list[dict[str, str]] = []
    status = "active"

    lock = _MERCHANT_BOOTSTRAP_LOCKS[int(merchant["hex"][:8], 16) % len(_MERCHANT_BOOTSTRAP_LOCKS)]
    with lock:
        with engine().begin() as c:
            if c.engine.dialect.name == "postgresql":
                c.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                    {"lock_key": f"merchant-bootstrap:{merchant['hex']}"},
                )
            elif c.engine.dialect.name == "sqlite":
                c.exec_driver_sql("BEGIN IMMEDIATE")
            linked = c.execute(
                text(
                    """
                    SELECT 1 FROM merchant_account_links
                    WHERE account_id=:account_id AND merchant_pubkey_hex=:merchant_hex
                      AND source='environment_binding'
                    LIMIT 1
                    """
                ),
                {"account_id": session["account_id"], "merchant_hex": merchant["hex"]},
            ).fetchone()
            if not linked or merchant["hex"] == session["nostr_pubkey_hex"]:
                raise HTTPException(404, "merchant bootstrap target not found")

            existing = asdict(c.execute(text("SELECT * FROM campaigns WHERE id=:id"), {"id": campaign_id}).fetchone())
            if existing:
                if existing.get("merchant_pubkey_hex") != merchant["hex"]:
                    raise HTTPException(409, "default program identity conflict")
                requested_fields = {
                    "name": body.program_name,
                    "commission_bps": body.commission_percent,
                    "window_days": body.attribution_window_days,
                    "destination_url": body.destination_url,
                    "terms_url": body.terms_url,
                }
                conflicts = (
                    (requested_fields["name"] is not None and existing["name"] != program["name"])
                    or (requested_fields["commission_bps"] is not None and int(existing["commission_bps"]) != program["commission_bps"])
                    or (requested_fields["window_days"] is not None and int(existing["window_days"]) != program["window_days"])
                    or (requested_fields["destination_url"] is not None and existing["destination_url"] != program["destination_url"])
                    or (requested_fields["terms_url"] is not None and existing.get("terms_url") != program["terms_url"])
                )
                if conflicts:
                    raise HTTPException(409, "program already exists with different settings")
                event = json.loads(existing["nostr_event_json"])
                duplicate = True
                status = existing["status"]
                relay_results = event.get("relay_results", [])
                publish_needed = _merchant_campaign_publication_needed(event)
            else:
                terms_hash = sha(program["terms_url"])
                campaign = {
                    "id": campaign_id,
                    "merchant_pubkey": merchant["npub"],
                    "merchant_pubkey_hex": merchant["hex"],
                    "name": program["name"],
                    "commission_bps": program["commission_bps"],
                    "window_days": program["window_days"],
                    "destination_url": program["destination_url"],
                    "terms_url": program["terms_url"],
                    "terms_hash": terms_hash,
                    "status": "active",
                }
                event = build_campaign_event(campaign, program["terms_url"])
                inserted = c.execute(
                    text(
                        """
                        INSERT INTO campaigns
                          (id, merchant_pubkey, merchant_pubkey_hex, name, commission_bps, window_days,
                           destination_url, terms_url, terms_hash, status, nostr_event_id, nostr_event_json, created_at)
                        VALUES
                          (:id, :merchant_pubkey, :merchant_pubkey_hex, :name, :commission_bps, :window_days,
                           :destination_url, :terms_url, :terms_hash, :status, :nostr_event_id, :nostr_event_json, :created_at)
                        ON CONFLICT(id) DO NOTHING
                        """
                    ),
                    {
                        **campaign,
                        "nostr_event_id": event["id"],
                        "nostr_event_json": json.dumps(event),
                        "created_at": now(),
                    },
                )
                if inserted.rowcount == 1:
                    ensure_campaign_budget(c, campaign_id)
                    persist_nostr_event(c, event, "campaign", campaign_id, [])
                else:
                    existing = asdict(c.execute(text("SELECT * FROM campaigns WHERE id=:id"), {"id": campaign_id}).fetchone())
                    if not existing or existing.get("merchant_pubkey_hex") != merchant["hex"]:
                        raise HTTPException(409, "default program identity conflict")
                    event = json.loads(existing["nostr_event_json"])
                    duplicate = True
                    status = existing["status"]
                    relay_results = event.get("relay_results", [])
                    publish_needed = _merchant_campaign_publication_needed(event)

            profile = c.execute(
                text("SELECT 1 FROM merchant_profiles WHERE merchant_pubkey_hex=:hex"),
                {"hex": merchant["hex"]},
            ).fetchone()
            if not profile:
                c.execute(
                    text(
                        """
                        INSERT INTO merchant_profiles
                          (merchant_pubkey_hex, merchant_pubkey, logo_url, created_at, updated_at)
                        VALUES (:hex, :npub, :logo_url, :created_at, :updated_at)
                        """
                    ),
                    {"hex": merchant["hex"], "npub": merchant["npub"], "logo_url": program["logo_url"] if not duplicate else None, "created_at": now(), "updated_at": now()},
                )

    assert event is not None
    if publish_needed:
        relay_results = finalize_committed_nostr_event(event, "campaign", campaign_id)
    return {
        "ok": True,
        "duplicate": duplicate,
        "campaign_id": campaign_id,
        "status": status,
        "merchant_pubkey": merchant["npub"],
        "nostr_event_id": event["id"],
        "relay_results": relay_results,
    }


@app.post("/app/merchant/onboarding", tags=["Accounts"])
def merchant_complete_onboarding(body: MerchantOnboardingIn, request: Request) -> dict[str, Any]:
    """Atomically complete the three onboarding persistence steps."""
    session = require_account_session(request, "merchant")
    _require_same_origin(request)
    merchant = normalize_pubkey(body.merchant_pubkey, "merchant_pubkey")
    program = _merchant_requested_program(body)
    campaign_id = f"camp_default_{merchant['hex']}"
    display_name = safe_text(body.display_name, 120)
    tagline = safe_text(body.tagline, 180)
    raw_logo_url = safe_text(body.logo_url, 2048)
    logo_url = _normalized_merchant_url(raw_logo_url, "logo_url", logo=True) if raw_logo_url else None
    invite_eyebrow = safe_text(body.invite_eyebrow, 100)
    invite_headline = safe_text(body.invite_headline, 120)
    invite_description = safe_text(body.invite_description, 360)
    duplicate = False
    publish_needed = True
    relay_results: list[dict[str, str]] = []
    status = "active"
    event: dict[str, Any] | None = None
    lock = _MERCHANT_BOOTSTRAP_LOCKS[int(merchant["hex"][:8], 16) % len(_MERCHANT_BOOTSTRAP_LOCKS)]
    init_db()

    with lock:
        with engine().begin() as c:
            if c.engine.dialect.name == "postgresql":
                c.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                    {"lock_key": f"merchant-bootstrap:{merchant['hex']}"},
                )
            elif c.engine.dialect.name == "sqlite":
                c.exec_driver_sql("BEGIN IMMEDIATE")
            linked = c.execute(
                text(
                    """
                    SELECT 1 FROM merchant_account_links
                    WHERE account_id=:account_id AND merchant_pubkey_hex=:merchant_hex
                      AND source='environment_binding'
                    LIMIT 1
                    """
                ),
                {"account_id": session["account_id"], "merchant_hex": merchant["hex"]},
            ).fetchone()
            if not linked or merchant["hex"] == session["nostr_pubkey_hex"]:
                raise HTTPException(404, "merchant bootstrap target not found")

            existing = asdict(c.execute(text("SELECT * FROM campaigns WHERE id=:id"), {"id": campaign_id}).fetchone())
            if existing:
                conflicts = (
                    (body.program_name is not None and existing["name"] != program["name"])
                    or (body.commission_percent is not None and int(existing["commission_bps"]) != program["commission_bps"])
                    or (body.attribution_window_days is not None and int(existing["window_days"]) != program["window_days"])
                    or (body.destination_url is not None and existing["destination_url"] != program["destination_url"])
                    or (body.terms_url is not None and existing.get("terms_url") != program["terms_url"])
                )
                if existing.get("merchant_pubkey_hex") != merchant["hex"]:
                    raise HTTPException(409, "default program identity conflict")
                if conflicts:
                    raise HTTPException(409, "program already exists with different settings")
                event = json.loads(existing["nostr_event_json"])
                duplicate = True
                status = existing["status"]
                relay_results = event.get("relay_results", [])
                publish_needed = _merchant_campaign_publication_needed(event)
            else:
                campaign = {
                    "id": campaign_id,
                    "merchant_pubkey": merchant["npub"],
                    "merchant_pubkey_hex": merchant["hex"],
                    "name": program["name"],
                    "commission_bps": program["commission_bps"],
                    "window_days": program["window_days"],
                    "destination_url": program["destination_url"],
                    "terms_url": program["terms_url"],
                    "terms_hash": sha(program["terms_url"]),
                    "status": "active",
                }
                event = build_campaign_event(campaign, program["terms_url"])
                c.execute(
                    text(
                        """
                        INSERT INTO campaigns
                          (id, merchant_pubkey, merchant_pubkey_hex, name, commission_bps, window_days,
                           destination_url, terms_url, terms_hash, status, nostr_event_id, nostr_event_json, created_at)
                        VALUES
                          (:id, :merchant_pubkey, :merchant_pubkey_hex, :name, :commission_bps, :window_days,
                           :destination_url, :terms_url, :terms_hash, :status, :nostr_event_id, :nostr_event_json, :created_at)
                        """
                    ),
                    {
                        **campaign,
                        "nostr_event_id": event["id"],
                        "nostr_event_json": json.dumps(event),
                        "created_at": now(),
                    },
                )
                ensure_campaign_budget(c, campaign_id)
                persist_nostr_event(c, event, "campaign", campaign_id, [])

            timestamp = now()
            c.execute(
                text(
                    """
                    INSERT INTO merchant_profiles
                      (merchant_pubkey_hex, merchant_pubkey, display_name, tagline, logo_url, created_at, updated_at)
                    VALUES (:hex, :npub, :display_name, :tagline, :logo_url, :created_at, :updated_at)
                    ON CONFLICT(merchant_pubkey_hex) DO UPDATE SET
                      merchant_pubkey=:npub, display_name=:display_name, tagline=:tagline,
                      logo_url=:logo_url, updated_at=:updated_at
                    """
                ),
                {
                    "hex": merchant["hex"], "npub": merchant["npub"],
                    "display_name": display_name, "tagline": tagline or None, "logo_url": logo_url,
                    "created_at": timestamp, "updated_at": timestamp,
                },
            )
            invitation = _persist_merchant_campaign_invite(
                c, session, campaign_id, invite_eyebrow, invite_headline, invite_description
            )

    assert event is not None
    if publish_needed:
        relay_results = finalize_committed_nostr_event(event, "campaign", campaign_id)
    profile = {
        "ok": True,
        "merchant_pubkey": merchant["npub"],
        "display_name": display_name,
        "tagline": tagline or None,
        "logo_url": logo_url,
    }
    return {
        "ok": True,
        "duplicate": duplicate,
        "campaign_id": campaign_id,
        "profile": profile,
        "invitation": invitation,
    }


def _normalize_lightning_address(value: str) -> str:
    address = safe_text(value, 320)
    try:
        lightning_address_url(address)
        name, domain = address.rsplit("@", 1)
        if (
            not re.fullmatch(r"[A-Za-z0-9_+.-]+", name)
            or name.startswith(".") or name.endswith(".") or ".." in name
        ):
            raise ValueError("invalid Lightning Address local-part")
        return f"{name}@{domain.encode('idna').decode('ascii').lower()}"
    except (LightningPaymentError, UnicodeError, ValueError) as exc:
        raise HTTPException(422, "invalid Lightning Address") from exc


@app.put("/app/affiliate/lightning-address", tags=["Accounts"])
def affiliate_update_lightning_address(request: Request, body: AffiliateLightningAddressIn) -> dict[str, Any]:
    session = require_account_session(request, "affiliate")
    _require_same_origin(request)
    address = _normalize_lightning_address(body.lightning_address)
    try:
        validate_lightning_address(address)
    except LightningPaymentError as exc:
        logger.info("Lightning Address verification rejected %s: %s", address, exc)
        raise HTTPException(422, "La Lightning Address no existe o no ofrece LNURL-pay.") from exc
    init_db()
    params = {"npub": session["npub"], "hex": session["nostr_pubkey_hex"], "address": address}
    with engine().begin() as c:
        if c.engine.dialect.name == "postgresql":
            c.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": f"affiliate-destination:{session['nostr_pubkey_hex']}"},
            )
        enrollments = c.execute(text("""
            UPDATE enrollments SET lightning_address=:address
            WHERE status='approved' AND (
              affiliate_pubkey=:npub OR affiliate_pubkey_hex=:hex OR affiliate_pubkey=:hex
            )
        """), params)
        payouts = c.execute(text("""
            UPDATE payouts SET lightning_address=:address
            WHERE state='PAYABLE' AND status='pending' AND payment_provider IS NULL
              AND (affiliate_pubkey=:npub OR affiliate_pubkey=:hex)
              AND NOT EXISTS (SELECT 1 FROM payment_attempts a WHERE a.payout_id=payouts.id)
        """), params)
    return {"ok": True, "lightning_address": address, "updated_enrollments": enrollments.rowcount, "updated_payouts": payouts.rowcount}


def _owned_merchant_payout(c: Any, session: dict[str, Any], payout_id: str, *, lock: bool = False) -> dict[str, Any] | None:
    suffix = " FOR UPDATE" if lock and c.engine.dialect.name == "postgresql" else ""
    return asdict(c.execute(text(f"""
        SELECT p.*, v.status AS conversion_status, v.campaign_id
        FROM payouts p
        JOIN conversions v ON v.id=p.conversion_id
        JOIN campaigns campaign ON campaign.id=v.campaign_id
        WHERE p.id=:payout_id AND (
          campaign.merchant_pubkey_hex=:owner_hex OR campaign.merchant_pubkey=:owner_hex OR EXISTS (
            SELECT 1 FROM merchant_account_links link
            WHERE link.account_id=:account_id AND link.merchant_pubkey_hex=campaign.merchant_pubkey_hex
          )
        )
        LIMIT 1{suffix}
    """), {"payout_id": payout_id, "owner_hex": session["nostr_pubkey_hex"], "account_id": session["account_id"]}).fetchone())


def _require_unsettled_manual_payout(c: Any, payout: dict[str, Any]) -> None:
    if payout.get("state") != "PAYABLE" or payout.get("status") != "pending":
        raise HTTPException(409, f"payout state {payout.get('state')} is not manually payable")
    if payout.get("conversion_status") == "reversed" or c.execute(
        text("SELECT 1 FROM reversals WHERE conversion_id=:id LIMIT 1"),
        {"id": payout["conversion_id"]},
    ).fetchone():
        raise HTTPException(409, "reversed conversion payout is not payable")
    if payout.get("return_window_ends_at"):
        try:
            return_window = datetime.fromisoformat(str(payout["return_window_ends_at"]).replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(500, "payout has an invalid return window") from exc
        if return_window > datetime.now(timezone.utc):
            raise HTTPException(409, "payout return window has not ended")
    if not payout.get("lightning_address"):
        raise HTTPException(409, "affiliate has not configured a Lightning Address")
    if payout.get("payment_provider") or payout.get("bolt11_invoice"):
        raise HTTPException(409, "payout already belongs to a payment provider")
    if int(payout.get("reserved_sats") or 0) < int(payout["amount_sats"]) + int(payout.get("fee_sats") or 0):
        raise HTTPException(409, "payout has no complete campaign budget reservation")
    if c.execute(text("SELECT 1 FROM payment_attempts WHERE payout_id=:id LIMIT 1"), {"id": payout["id"]}).fetchone():
        raise HTTPException(409, "payout already has payment evidence or an attempt")


def _bolt11_qr_data_uri(invoice: str) -> str:
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=4)
    qr.add_data(invoice.upper())
    qr.make(fit=True)
    image = qr.make_image(image_factory=SvgPathImage)
    output = io.BytesIO()
    image.save(output)
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


@app.post("/app/merchant/payouts/{payout_id}/prepare-invoice", tags=["Accounts"])
async def merchant_prepare_payout_invoice(payout_id: str, request: Request, response: Response) -> dict[str, Any]:
    session = require_account_session(request, "merchant")
    _require_same_origin(request)
    init_db()
    with engine().connect() as c:
        payout = _owned_merchant_payout(c, session, payout_id)
        if not payout:
            raise HTTPException(404, "payout not found")
        _require_unsettled_manual_payout(c, payout)
        lightning_address = str(payout["lightning_address"])
        amount_sats = int(payout["amount_sats"])
    rate_key = f"{session['account_id']}:{payout_id}"
    started = time.monotonic()
    with _INVOICE_PREPARE_LOCK:
        for key, timestamp in list(_INVOICE_PREPARE_LAST.items()):
            if started - timestamp > 60:
                _INVOICE_PREPARE_LAST.pop(key, None)
        if rate_key in _INVOICE_PREPARE_ACTIVE or started - _INVOICE_PREPARE_LAST.get(rate_key, 0) < 5:
            raise HTTPException(429, "invoice preparation is already running or was requested too recently")
        _INVOICE_PREPARE_ACTIVE.add(rate_key)
        _INVOICE_PREPARE_LAST[rate_key] = started
    try:
        try:
            invoice, payment_hash = await prepare_lnurl_payment(lightning_address, amount_sats)
        except LightningPaymentError as exc:
            raise HTTPException(502, str(exc)) from exc
        with engine().connect() as c:
            latest = _owned_merchant_payout(c, session, payout_id)
            if not latest:
                raise HTTPException(409, "payout changed while preparing the invoice")
            _require_unsettled_manual_payout(c, latest)
            if str(latest["lightning_address"]) != lightning_address or int(latest["amount_sats"]) != amount_sats:
                raise HTTPException(409, "payout destination or amount changed while preparing the invoice")
        expires_at = bolt11_expires_at(invoice)
        try:
            qr_data_uri = await asyncio.to_thread(_bolt11_qr_data_uri, invoice)
        except (DataOverflowError, ValueError) as exc:
            raise HTTPException(502, "BOLT11 invoice is too large to render safely") from exc
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
        return {
            "ok": True,
            "payout_id": payout_id,
            "amount_sats": amount_sats,
            "lightning_address": lightning_address,
            "invoice": invoice,
            "payment_hash": payment_hash,
            "expires_at": expires_at,
            "qr_data_uri": qr_data_uri,
        }
    finally:
        with _INVOICE_PREPARE_LOCK:
            _INVOICE_PREPARE_ACTIVE.discard(rate_key)


@app.post("/app/merchant/payouts/{payout_id}/manual-settlement", tags=["Accounts"])
def merchant_manual_settlement(payout_id: str, request: Request, body: MerchantManualSettlementIn) -> dict[str, Any]:
    session = require_account_session(request, "merchant")
    _require_same_origin(request)
    payment_hash = body.payment_hash.lower()
    init_db()
    with engine().begin() as c:
        payout = _owned_merchant_payout(c, session, payout_id, lock=True)
        if not payout:
            raise HTTPException(404, "payout not found")
        if payout["status"] == "paid" or payout.get("state") in {"SETTLED", "PUBLISHED"}:
            if payout.get("payment_provider") != "manual" or payout.get("payment_hash") != payment_hash:
                raise HTTPException(409, "payout was settled with different evidence")
        else:
            _require_unsettled_manual_payout(c, payout)
            if c.engine.dialect.name == "postgresql":
                c.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                    {"lock_key": f"manual-payment-hash:{payment_hash}"},
                )
            hash_used = c.execute(
                text(
                    """
                    SELECT 1 FROM payouts WHERE payment_hash=:payment_hash AND id!=:id
                    UNION ALL
                    SELECT 1 FROM payment_attempts WHERE payment_hash=:payment_hash AND payout_id!=:id
                    LIMIT 1
                    """
                ),
                {"payment_hash": payment_hash, "id": payout_id},
            ).fetchone()
            if hash_used:
                raise HTTPException(409, "payment hash is already assigned to another payout")
            timestamp = now()
            c.execute(text("""
                INSERT INTO payment_attempts
                  (id, payout_id, kind, rail, idempotency_key, destination, amount_sats,
                   status, payment_hash, provider_reference, routing_fee_sats,
                   attempt_number, created_at, updated_at, settled_at)
                VALUES (:id, :payout_id, 'commission', 'manual', :idempotency_key, :destination,
                        :amount_sats, 'SETTLED', :payment_hash, :attestor, 0, 1,
                        :created_at, :updated_at, :settled_at)
            """), {
                "id": hid("att"), "payout_id": payout_id,
                "idempotency_key": payment_idempotency_key(payout_id, "commission", 1),
                "destination": payout["lightning_address"], "amount_sats": payout["amount_sats"],
                "payment_hash": payment_hash, "attestor": f"merchant_account:{session['account_id']}",
                "created_at": timestamp, "updated_at": timestamp, "settled_at": timestamp,
            })
            updated = c.execute(text("""
                UPDATE payouts SET status='paid', state='SETTLED', payment_hash=:payment_hash,
                    payment_provider='manual', settled_at=:settled_at
                WHERE id=:id AND status='pending' AND state='PAYABLE' AND payment_provider IS NULL
            """), {"id": payout_id, "payment_hash": payment_hash, "settled_at": timestamp})
            if updated.rowcount != 1:
                raise HTTPException(409, "payout was concurrently modified")
    return finalize_payout_paid(payout_id, payment_hash, "Merchant-attested manual Lightning settlement", sandbox=False, provider="manual")



def _owned_merchant_campaign(c: Any, session: dict[str, Any], campaign_id: str) -> dict[str, Any]:
    return asdict(
        c.execute(
            text(
                """
                SELECT c.* FROM campaigns c
                WHERE c.id=:campaign_id AND (
                  c.merchant_pubkey_hex=:owner_hex OR c.merchant_pubkey=:owner_hex OR EXISTS (
                    SELECT 1 FROM merchant_account_links link
                    WHERE link.account_id=:account_id
                      AND link.merchant_pubkey_hex=c.merchant_pubkey_hex
                  )
                )
                LIMIT 1
                """
            ),
            {
                "campaign_id": campaign_id,
                "owner_hex": session["nostr_pubkey_hex"],
                "account_id": session["account_id"],
            },
        ).fetchone()
    )


@app.post("/app/merchant/invitations", tags=["Accounts"])
def merchant_create_invitation(request: Request, body: MerchantInvitationIn) -> dict[str, Any]:
    session = require_account_session(request, "merchant")
    _require_same_origin(request)
    init_db()
    token = random_token(32)
    invitation_id = hid("inv")
    created_at = datetime.now(timezone.utc)
    expires_at = created_at + timedelta(days=body.expires_days)
    with engine().begin() as c:
        campaign = _owned_merchant_campaign(c, session, body.campaign_id)
        if not campaign:
            raise HTTPException(404, "campaign not found")
        if campaign.get("status") != "active":
            raise HTTPException(409, "campaign is not active")
        c.execute(
            text(
                """
                INSERT INTO affiliate_invitations
                (id, token_hash, token_prefix, campaign_id, created_by_account_id, status,
                 expires_at, accepted_at, accepted_by_hex, enrollment_id, created_at)
                VALUES (:id, :token_hash, :token_prefix, :campaign_id, :account_id, 'pending',
                        :expires_at, NULL, NULL, NULL, :created_at)
                """
            ),
            {
                "id": invitation_id,
                "token_hash": auth_digest(token),
                "token_prefix": token[:8],
                "campaign_id": body.campaign_id,
                "account_id": session["account_id"],
                "expires_at": expires_at.isoformat(),
                "created_at": created_at.isoformat(),
            },
        )
    return {
        "ok": True,
        "invitation_id": invitation_id,
        "campaign_id": body.campaign_id,
        "campaign_name": campaign["name"],
        "status": "pending",
        "expires_at": expires_at.isoformat(),
        "invite_url": f"{SHORT_LINK_BASE_URL}/invite#token={token}",
    }


@app.get("/invite", response_class=HTMLResponse, tags=["Accounts"])
def affiliate_invitation_page(request: Request) -> Response:
    return templates.TemplateResponse(
        request=request,
        name="invite.html",
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        context={},
    )


@app.post("/invite/resolve", tags=["Accounts"])
def resolve_affiliate_invitation(request: Request, response: Response, body: AffiliateInvitationTokenIn) -> dict[str, Any]:
    _require_same_origin(request)
    init_db()
    with engine().connect() as c:
        invitation = asdict(
            c.execute(
                text(
                    """
                    SELECT i.*, c.name AS campaign_name, c.commission_bps, c.window_days,
                           c.status AS campaign_status, c.invite_eyebrow, c.invite_headline,
                           c.invite_description, mp.display_name, mp.tagline, mp.logo_url
                    FROM affiliate_invitations i
                    JOIN campaigns c ON c.id=i.campaign_id
                    LEFT JOIN merchant_profiles mp ON mp.merchant_pubkey_hex=c.merchant_pubkey_hex
                    WHERE i.token_hash=:token_hash LIMIT 1
                    """
                ),
                {"token_hash": auth_digest(body.token)},
            ).fetchone()
        )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    if not invitation:
        raise HTTPException(404, "invitation not found")
    if parse_iso(invitation["expires_at"]) <= datetime.now(timezone.utc):
        raise HTTPException(410, "invitation expired")
    if invitation["status"] != "pending":
        raise HTTPException(409, "invitation was already used or revoked")
    if invitation["campaign_status"] != "active":
        raise HTTPException(409, "campaign is not active")
    profile_name = safe_text(invitation.get("display_name"), 120)
    fallback_name = re.sub(
        r"\s+(?:affiliate\s+program|affiliate\s+programme|programa\s+de\s+afiliados|programa\s+affiliate)$",
        "",
        invitation["campaign_name"],
        flags=re.IGNORECASE,
    ).strip()
    display_name = profile_name or fallback_name or invitation["campaign_name"]
    tagline = safe_text(invitation.get("tagline"), 180) or "Comunidad, recomendaciones y sats"
    words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9]+", display_name)
    initials = "".join(word[0] for word in words[:2]).upper() or "₿"
    commission_percent = f"{int(invitation['commission_bps']) / 100:g}"
    invite_eyebrow = safe_text(invitation.get("invite_eyebrow"), 100) or "Programa de afiliados · Value for value"
    invite_headline = safe_text(invitation.get("invite_headline"), 120) or f"Recomendá {display_name}. Ganá sats."
    invite_description = safe_text(invitation.get("invite_description"), 360) or (
        f"Sumate al programa de afiliados de {display_name}. Compartí tu link con tu comunidad "
        "y recibí sats cuando tu recomendación termina en una compra."
    )
    return {
        "ok": True,
        "campaign_name": invitation["campaign_name"],
        "commission_percent": commission_percent,
        "window_days": invitation["window_days"],
        "merchant": {
            "display_name": display_name,
            "tagline": tagline,
            "logo_url": invitation.get("logo_url"),
            "initials": initials,
        },
        "campaign": {
            "name": invitation["campaign_name"],
            "commission_percent": commission_percent,
            "invite_eyebrow": invite_eyebrow,
            "invite_headline": invite_headline,
            "invite_description": invite_description,
        },
        "expires_at": invitation["expires_at"],
    }


def _create_affiliate_session(c: Any, identity: dict[str, str]) -> tuple[str, str]:
    existing = asdict(c.execute(text("SELECT * FROM accounts WHERE nostr_pubkey_hex=:hex"), {"hex": identity["hex"]}).fetchone())
    account_id = existing["id"] if existing else hid("acct")
    if existing and existing.get("status") != "active":
        raise HTTPException(403, "affiliate account is not active")
    timestamp = now()
    if existing:
        c.execute(
            text("UPDATE accounts SET npub=:npub, updated_at=:now, last_login_at=:now WHERE id=:id"),
            {"npub": identity["npub"], "now": timestamp, "id": account_id},
        )
    else:
        c.execute(
            text(
                """
                INSERT INTO accounts (id, nostr_pubkey_hex, npub, status, created_at, updated_at, last_login_at)
                VALUES (:id, :hex, :npub, 'active', :now, :now, :now)
                """
            ),
            {"id": account_id, "hex": identity["hex"], "npub": identity["npub"], "now": timestamp},
        )
    c.execute(
        text(
            """
            INSERT INTO account_roles (account_id, role, created_at)
            VALUES (:account_id, 'affiliate', :created_at)
            ON CONFLICT (account_id, role) DO NOTHING
            """
        ),
        {"account_id": account_id, "created_at": timestamp},
    )
    session_token = random_token(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
    c.execute(
        text(
            """
            INSERT INTO account_sessions (id, account_id, role, token_hash, created_at, expires_at, last_seen_at, revoked_at)
            VALUES (:id, :account_id, 'affiliate', :token_hash, :now, :expires_at, :now, NULL)
            """
        ),
        {
            "id": hid("ses"),
            "account_id": account_id,
            "token_hash": auth_digest(session_token),
            "now": timestamp,
            "expires_at": expires_at,
        },
    )
    return session_token, expires_at


def _set_account_session_cookie(response: Response, session_token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        session_token,
        max_age=12 * 60 * 60,
        httponly=True,
        secure=BASE_URL.startswith("https://"),
        samesite="lax",
        path="/",
    )


@app.post("/invite/accept", tags=["Accounts"])
def accept_affiliate_invitation(request: Request, response: Response, body: AffiliateInvitationAcceptIn) -> dict[str, Any]:
    _require_same_origin(request)
    token = body.token
    try:
        identity = verify_auth_event(
            body.event,
            expected_challenge=token,
            expected_role="affiliate_invite",
            expected_relay=BASE_URL,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    init_db()
    token_hash = auth_digest(token)
    event: dict[str, Any] | None = None
    relay_results: list[dict[str, str]] = []
    duplicate = False
    with _INVITATION_ACCEPT_LOCK:
        with engine().begin() as c:
            if database_url().startswith("postgresql"):
                c.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                    {"lock_key": f"affiliate-invitation:{token_hash}"},
                )
            invitation = asdict(
                c.execute(text("SELECT * FROM affiliate_invitations WHERE token_hash=:token_hash LIMIT 1"), {"token_hash": token_hash}).fetchone()
            )
            if not invitation:
                raise HTTPException(404, "invitation not found")
            if invitation["status"] == "accepted":
                if invitation.get("accepted_by_hex") != identity["hex"]:
                    raise HTTPException(409, "invitation was already used by another identity")
                enrollment = asdict(
                    c.execute(text("SELECT * FROM enrollments WHERE id=:id LIMIT 1"), {"id": invitation.get("enrollment_id")}).fetchone()
                )
                if not enrollment or enrollment.get("status") != "approved":
                    raise HTTPException(409, "accepted invitation has no active enrollment")
                session_token, session_expires_at = _create_affiliate_session(c, identity)
                recovery_nostr_status = "existing"
                proof_status = c.execute(
                    text("SELECT relay_status FROM nostr_events WHERE event_id=:event_id LIMIT 1"),
                    {"event_id": enrollment.get("nostr_event_id")},
                ).scalar()
                if proof_status != "published" and enrollment.get("nostr_event_json"):
                    try:
                        recovered_event = json.loads(enrollment["nostr_event_json"])
                        recovery_results = publish_event(recovered_event)
                        persist_nostr_event(c, recovered_event, "enrollment", enrollment["id"], recovery_results)
                        recovery_nostr_status = "published" if any(result["status"] == "published" for result in recovery_results) else "pending"
                    except Exception:
                        recovery_nostr_status = "pending"
                _set_account_session_cookie(response, session_token)
                return {
                    "ok": True,
                    "duplicate": True,
                    "recovered": True,
                    "invitation_id": invitation["id"],
                    "enrollment_id": enrollment["id"],
                    "affiliate_pubkey": identity["npub"],
                    "ref_url": referral_url(enrollment["ref_code"]),
                    "nostr_status": recovery_nostr_status,
                    "session_expires_at": session_expires_at,
                    "redirect": "/app/affiliate#links",
                }
            if invitation["status"] != "pending":
                raise HTTPException(409, "invitation was revoked")
            if parse_iso(invitation["expires_at"]) <= datetime.now(timezone.utc):
                raise HTTPException(410, "invitation expired")
            if database_url().startswith("postgresql"):
                c.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                    {"lock_key": f"campaign-enrollment:{invitation['campaign_id']}"},
                )
            campaign = asdict(c.execute(text("SELECT * FROM campaigns WHERE id=:id"), {"id": invitation["campaign_id"]}).fetchone())
            if not campaign or campaign.get("status") != "active":
                raise HTTPException(409, "campaign is not active")
            if database_url().startswith("postgresql"):
                c.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                    {"lock_key": f"affiliate-enrollment:{campaign['id']}:{identity['hex']}"},
                )
            existing = asdict(
                c.execute(
                    text(
                        """
                        SELECT * FROM enrollments
                        WHERE campaign_id=:campaign_id AND (
                          affiliate_pubkey_hex=:affiliate_hex OR affiliate_pubkey=:affiliate_npub OR affiliate_pubkey=:affiliate_hex
                        )
                        LIMIT 1
                        """
                    ),
                    {
                        "campaign_id": campaign["id"],
                        "affiliate_hex": identity["hex"],
                        "affiliate_npub": identity["npub"],
                    },
                ).fetchone()
            )
            if existing:
                if existing["status"] != "approved":
                    raise HTTPException(409, f"affiliate enrollment is {existing['status']}")
                enrollment = existing
                duplicate = True
            else:
                enrollment = {
                    "id": hid("enr"),
                    "campaign_id": campaign["id"],
                    "affiliate_pubkey": identity["npub"],
                    "affiliate_pubkey_hex": identity["hex"],
                    "lightning_address": None,
                    "ref_code": hid("ref"),
                    "status": "approved",
                    "created_at": now(),
                }
                event = build_enrollment_event(enrollment, campaign)
                enrollment["nostr_event_id"] = event["id"]
                enrollment["nostr_event_json"] = json.dumps(event)
                c.execute(
                    text(
                        """
                        INSERT INTO enrollments (id, campaign_id, affiliate_pubkey, affiliate_pubkey_hex, lightning_address,
                        ref_code, status, nostr_event_id, nostr_event_json, created_at)
                        VALUES (:id, :campaign_id, :affiliate_pubkey, :affiliate_pubkey_hex, :lightning_address,
                        :ref_code, :status, :nostr_event_id, :nostr_event_json, :created_at)
                        """
                    ),
                    enrollment,
                )
                persist_nostr_event(c, event, "enrollment", enrollment["id"], [])
            accepted_at = now()
            updated = c.execute(
                text(
                    """
                    UPDATE affiliate_invitations
                    SET status='accepted', accepted_at=:accepted_at, accepted_by_hex=:accepted_by_hex, enrollment_id=:enrollment_id
                    WHERE id=:id AND status='pending'
                    """
                ),
                {
                    "accepted_at": accepted_at,
                    "accepted_by_hex": identity["hex"],
                    "enrollment_id": enrollment["id"],
                    "id": invitation["id"],
                },
            )
            if updated.rowcount != 1:
                raise HTTPException(409, "invitation was already used or revoked")
            session_token, session_expires_at = _create_affiliate_session(c, identity)

    if event is not None:
        try:
            relay_results = publish_event(event)
            with engine().begin() as c:
                persist_nostr_event(c, event, "enrollment", enrollment["id"], relay_results)
        except Exception:
            # Enrollment and session are already durable; relay retry remains pending.
            relay_results = []
    _set_account_session_cookie(response, session_token)
    nostr_status = "existing" if duplicate else ("published" if any(result["status"] == "published" for result in relay_results) else "pending")
    return {
        "ok": True,
        "duplicate": duplicate,
        "invitation_id": invitation["id"],
        "enrollment_id": enrollment["id"],
        "affiliate_pubkey": identity["npub"],
        "ref_url": referral_url(enrollment["ref_code"]),
        "nostr_status": nostr_status,
        "session_expires_at": session_expires_at,
        "redirect": "/app/affiliate#links",
    }


def _merchant_enrollment_result(
    enrollment: dict[str, Any],
    *,
    duplicate: bool,
    nostr_status: str,
    relay_results: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "duplicate": duplicate,
        "enrollment_id": enrollment["id"],
        "affiliate_pubkey": enrollment["affiliate_pubkey"],
        "affiliate_pubkey_hex": enrollment["affiliate_pubkey_hex"],
        "ref_code": enrollment["ref_code"],
        "ref_url": referral_url(enrollment["ref_code"]),
        "status": enrollment["status"],
        "nostr_event_id": enrollment["nostr_event_id"],
        "nostr_status": nostr_status,
        "relay_results": relay_results or [],
    }



@app.get("/app/affiliate", response_class=HTMLResponse)
def affiliate_account_page(request: Request) -> Response:
    session = _session_account(request, "affiliate")
    if not session:
        return RedirectResponse("/app?role=affiliate", status_code=303)
    with engine().connect() as c:
        data = affiliate_workspace_data(c, session, base_url=BASE_URL, ref_base_url=SHORT_LINK_BASE_URL)
    return templates.TemplateResponse(
        request=request,
        name="affiliate.html",
        context={
            **data,
            "account": _account_shell(session, "affiliate"),
            "role_label": "Affiliate account",
            "nav": [
                {"label": "Resumen", "href": "/app/affiliate", "active": True},
                {"label": "Mis links", "href": "#links", "active": False},
                {"label": "Ganancias", "href": "#earnings", "active": False},
                {"label": "Conversiones", "href": "#activity", "active": False},
            ],
        },
    )


@app.get("/ops/data", tags=["Operations"])
def ops_dashboard_data(request: Request, response: Response) -> dict[str, Any]:
    require_account_session(request, "ops")
    response.headers["Cache-Control"] = "no-store"
    return dashboard_data()


@app.get("/dashboard/data", include_in_schema=False)
def legacy_dashboard_data(request: Request) -> Response:
    return RedirectResponse("/ops/data", status_code=307)


@app.get("/ops", response_class=HTMLResponse)
def operations_dashboard(request: Request) -> Response:
    session = _session_account(request, "ops")
    if not session:
        return RedirectResponse("/app?role=ops", status_code=303)
    data = dashboard_data()
    healthy = bool(data["health"]["nostr_publish"] and not data["counts"]["failed_relays"])
    return templates.TemplateResponse(
        request=request,
        name="ops.html",
        headers={"Cache-Control": "no-store"},
        context={
            "data": data,
            "account": _account_shell(session, "ops"),
            "role_label": "Operations",
            "workspace_status": {
                "label": "Operativo" if healthy else "Revisar estado",
                "class": "is-healthy" if healthy else "is-degraded",
            },
            "nav": [
                {"label": "Resumen", "href": "/ops", "active": True},
                {"label": "Conversiones", "href": "#conversions", "active": False},
                {"label": "Nostr", "href": "#nostr", "active": False},
            ],
        },
    )


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> Response:
    return RedirectResponse("/ops", status_code=303)


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> Response:
    return templates.TemplateResponse(request=request, name="home.html", context={})
