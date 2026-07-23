from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import Cookie, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from nostr_sdk import Client, EventBuilder, Keys, Kind, RelayUrl, Tag
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

APP_SECRET = os.getenv("APP_SECRET", "dev-secret-change-me")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
DEFAULT_DESTINATION = os.getenv("DEFAULT_DESTINATION_URL", "https://example.com/checkout")
DEFAULT_RELAYS = "wss://nos.lol,wss://relay.damus.io,wss://relay.primal.net"

app = FastAPI(
    title="Nostr Affiliate POC",
    description="MVP: campaign → enrollment → redirect click → conversion → real Nostr proof → pending Lightning payout.",
    version="0.3.0",
)

_ENGINE: Engine | None = None
_ENGINE_URL: str | None = None


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def hid(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(8).replace('-', '').replace('_', '')[:10]}"


def sha(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def nostr_relays() -> list[str]:
    raw = os.getenv("NOSTR_RELAYS", DEFAULT_RELAYS)
    return [r.strip() for r in raw.split(",") if r.strip()]


def nostr_publish_enabled() -> bool:
    explicit = os.getenv("NOSTR_PUBLISH")
    if explicit is not None:
        return explicit.lower() in {"1", "true", "yes", "on"}
    return bool(os.getenv("NOSTR_PRIVATE_KEY"))


def nostr_keys() -> Keys:
    secret = os.getenv("NOSTR_PRIVATE_KEY")
    if secret:
        return Keys.parse(secret)
    # Deterministic dev key for local tests only. Production should set NOSTR_PRIVATE_KEY.
    derived = hashlib.sha256((APP_SECRET + ":nostr-dev-key").encode()).hexdigest()
    return Keys.parse(derived)


def build_nostr_event(kind: int, tags: list[list[str]], content: str = "") -> dict[str, Any]:
    keys = nostr_keys()
    event = EventBuilder(Kind(kind), content).tags([Tag.parse(t) for t in tags]).sign_with_keys(keys)
    data = json.loads(event.as_json())
    data["relay_status"] = "pending_publication" if nostr_publish_enabled() else "signed_not_published"
    return data


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
        event = Event.from_json(json.dumps({k: v for k, v in event_json.items() if k != "relay_status"}))
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
    relay_status = "published" if published_count else relay_results[0]["status"] if relay_results else "unknown"
    event["relay_status"] = relay_status
    event["relay_results"] = relay_results
    c.execute(
        text(
            """
            INSERT INTO nostr_events (event_id, kind, pubkey, content, tags_json, event_json,
            entity_type, entity_id, relay_status, created_at, published_at)
            VALUES (:event_id, :kind, :pubkey, :content, :tags_json, :event_json,
            :entity_type, :entity_id, :relay_status, :created_at, :published_at)
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


def init_db() -> None:
    ddl = """
    CREATE TABLE IF NOT EXISTS campaigns (
        id TEXT PRIMARY KEY,
        merchant_pubkey TEXT NOT NULL,
        name TEXT NOT NULL,
        commission_bps INTEGER NOT NULL,
        window_days INTEGER NOT NULL,
        destination_url TEXT NOT NULL,
        terms_hash TEXT NOT NULL,
        nostr_event_id TEXT NOT NULL,
        nostr_event_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS enrollments (
        id TEXT PRIMARY KEY,
        campaign_id TEXT NOT NULL,
        affiliate_pubkey TEXT NOT NULL,
        lightning_address TEXT,
        ref_code TEXT UNIQUE NOT NULL,
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
    CREATE TABLE IF NOT EXISTS conversions (
        id TEXT PRIMARY KEY,
        order_id_hash TEXT NOT NULL,
        click_id TEXT NOT NULL,
        campaign_id TEXT NOT NULL,
        affiliate_pubkey TEXT NOT NULL,
        order_total REAL NOT NULL,
        currency TEXT NOT NULL,
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
        payment_hash TEXT,
        nostr_event_id TEXT,
        nostr_event_json TEXT,
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
    """
    if database_url().startswith("sqlite"):
        ddl = ddl.replace("id INTEGER PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY", "id INTEGER PRIMARY KEY AUTOINCREMENT")
    with engine().begin() as c:
        for stmt in [s.strip() for s in ddl.split(";") if s.strip()]:
            c.execute(text(stmt))


@app.on_event("startup")
def startup() -> None:
    init_db()


class CampaignIn(BaseModel):
    merchant_pubkey: str = Field(..., examples=["merchant_pubkey_demo"])
    name: str = "Bumbei BTC Rewards"
    commission_bps: int = 800
    attribution_window_days: int = 30
    destination_url: str = DEFAULT_DESTINATION
    terms_url: str = "https://bumbei.com/terms/affiliate"


class EnrollmentIn(BaseModel):
    campaign_id: str
    affiliate_pubkey: str = Field(..., examples=["affiliate_pubkey_demo"])
    lightning_address: Optional[str] = Field(None, examples=["seba@getalby.com"])


class ConversionIn(BaseModel):
    order_id: str
    click_id: str
    order_total: float
    currency: str = "USD"
    sats_per_usd: int = 2500


@app.get("/health")
def health() -> dict[str, Any]:
    init_db()
    return {
        "ok": "true",
        "service": "nostr-affiliate-poc",
        "db": "postgres" if database_url().startswith("postgresql") else "sqlite",
        "nostr_pubkey": nostr_keys().public_key().to_hex(),
        "nostr_publish": nostr_publish_enabled(),
        "relays": nostr_relays(),
    }


@app.post("/campaigns")
def create_campaign(body: CampaignIn) -> dict[str, Any]:
    init_db()
    campaign_id = hid("camp")
    terms_hash = sha(body.terms_url)
    event = build_nostr_event(
        39001,
        [
            ["d", campaign_id],
            ["type", "affiliate_campaign"],
            ["merchant", body.merchant_pubkey],
            ["commission_bps", str(body.commission_bps)],
            ["window_days", str(body.attribution_window_days)],
            ["payout", "sats"],
            ["terms", terms_hash],
            ["destination", body.destination_url],
        ],
        json.dumps({"name": body.name, "terms_url": body.terms_url}),
    )
    relay_results = publish_event(event)
    with engine().begin() as c:
        persist_nostr_event(c, event, "campaign", campaign_id, relay_results)
        c.execute(
            text(
                """
                INSERT INTO campaigns (id, merchant_pubkey, name, commission_bps, window_days,
                destination_url, terms_hash, nostr_event_id, nostr_event_json, created_at)
                VALUES (:id, :merchant_pubkey, :name, :commission_bps, :window_days,
                :destination_url, :terms_hash, :nostr_event_id, :nostr_event_json, :created_at)
                """
            ),
            {
                "id": campaign_id,
                "merchant_pubkey": body.merchant_pubkey,
                "name": body.name,
                "commission_bps": body.commission_bps,
                "window_days": body.attribution_window_days,
                "destination_url": body.destination_url,
                "terms_hash": terms_hash,
                "nostr_event_id": event["id"],
                "nostr_event_json": json.dumps(event),
                "created_at": now(),
            },
        )
    return {"campaign_id": campaign_id, "nostr_event_id": event["id"], "nostr_event": event, "relay_results": relay_results}


@app.get("/campaigns/{campaign_id}")
def get_campaign(campaign_id: str) -> dict[str, Any]:
    with engine().connect() as c:
        campaign = asdict(c.execute(text("SELECT * FROM campaigns WHERE id=:id"), {"id": campaign_id}).fetchone())
    if not campaign:
        raise HTTPException(404, "campaign not found")
    campaign["nostr_event"] = json.loads(campaign.pop("nostr_event_json"))
    return campaign


@app.post("/enrollments")
def create_enrollment(body: EnrollmentIn) -> dict[str, Any]:
    init_db()
    with engine().connect() as c:
        camp = asdict(c.execute(text("SELECT * FROM campaigns WHERE id=:id"), {"id": body.campaign_id}).fetchone())
    if not camp:
        raise HTTPException(404, "campaign not found")
    enrollment_id = hid("enr")
    ref_code = hid("ref")
    event = build_nostr_event(
        39002,
        [
            ["type", "affiliate_enrollment"],
            ["campaign", body.campaign_id],
            ["merchant", camp["merchant_pubkey"]],
            ["affiliate", body.affiliate_pubkey],
            ["terms", camp["terms_hash"]],
        ],
        "",
    )
    relay_results = publish_event(event)
    with engine().begin() as c:
        persist_nostr_event(c, event, "enrollment", enrollment_id, relay_results)
        c.execute(
            text(
                """
                INSERT INTO enrollments (id, campaign_id, affiliate_pubkey, lightning_address,
                ref_code, nostr_event_id, nostr_event_json, created_at)
                VALUES (:id, :campaign_id, :affiliate_pubkey, :lightning_address,
                :ref_code, :nostr_event_id, :nostr_event_json, :created_at)
                """
            ),
            {
                "id": enrollment_id,
                "campaign_id": body.campaign_id,
                "affiliate_pubkey": body.affiliate_pubkey,
                "lightning_address": body.lightning_address,
                "ref_code": ref_code,
                "nostr_event_id": event["id"],
                "nostr_event_json": json.dumps(event),
                "created_at": now(),
            },
        )
    return {"enrollment_id": enrollment_id, "ref_code": ref_code, "ref_url": f"{BASE_URL}/r/{ref_code}", "nostr_event_id": event["id"], "nostr_event": event, "relay_results": relay_results}


@app.get("/r/{ref_code}")
def redirect_click(ref_code: str, request: Request) -> RedirectResponse:
    init_db()
    with engine().begin() as c:
        enr = asdict(c.execute(text("SELECT * FROM enrollments WHERE ref_code=:ref"), {"ref": ref_code}).fetchone())
        if not enr:
            raise HTTPException(404, "ref code not found")
        camp = asdict(c.execute(text("SELECT * FROM campaigns WHERE id=:id"), {"id": enr["campaign_id"]}).fetchone())
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
    sep = "&" if "?" in camp["destination_url"] else "?"
    url = f"{camp['destination_url']}{sep}bb_click_id={click_id}&bb_ref={ref_code}"
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie("bb_click_id", click_id, max_age=60 * 60 * 24 * int(camp["window_days"]), httponly=True, samesite="lax")
    return resp


@app.post("/conversions")
def create_conversion(body: ConversionIn, bb_click_id: Optional[str] = Cookie(None)) -> dict[str, Any]:
    init_db()
    click_id = body.click_id or bb_click_id
    with engine().begin() as c:
        click = asdict(c.execute(text("SELECT * FROM clicks WHERE id=:id"), {"id": click_id}).fetchone())
        if not click:
            raise HTTPException(404, "click not found")
        camp = asdict(c.execute(text("SELECT * FROM campaigns WHERE id=:id"), {"id": click["campaign_id"]}).fetchone())
        enr = asdict(c.execute(text("SELECT * FROM enrollments WHERE ref_code=:ref"), {"ref": click["ref_code"]}).fetchone())
        commission_sats = round(body.order_total * body.sats_per_usd * int(camp["commission_bps"]) / 10000)
        conversion_id = hid("conv")
        event = build_nostr_event(
            39005,
            [
                ["type", "affiliate_conversion"],
                ["campaign", click["campaign_id"]],
                ["merchant", camp["merchant_pubkey"]],
                ["affiliate", click["affiliate_pubkey"]],
                ["click_hash", sha(click_id)],
                ["conversion_hash", sha(body.order_id)],
                ["commission_sats", str(commission_sats)],
                ["status", "approved"],
            ],
            "",
        )
        relay_results = publish_event(event)
        persist_nostr_event(c, event, "conversion", conversion_id, relay_results)
        c.execute(
            text(
                """
                INSERT INTO conversions (id, order_id_hash, click_id, campaign_id, affiliate_pubkey,
                order_total, currency, commission_sats, status, nostr_event_id, nostr_event_json, created_at)
                VALUES (:id, :order_id_hash, :click_id, :campaign_id, :affiliate_pubkey,
                :order_total, :currency, :commission_sats, :status, :nostr_event_id, :nostr_event_json, :created_at)
                """
            ),
            {
                "id": conversion_id,
                "order_id_hash": sha(body.order_id),
                "click_id": click_id,
                "campaign_id": click["campaign_id"],
                "affiliate_pubkey": click["affiliate_pubkey"],
                "order_total": body.order_total,
                "currency": body.currency,
                "commission_sats": commission_sats,
                "status": "approved",
                "nostr_event_id": event["id"],
                "nostr_event_json": json.dumps(event),
                "created_at": now(),
            },
        )
        payout_id = hid("pay")
        c.execute(
            text(
                """
                INSERT INTO payouts (id, conversion_id, affiliate_pubkey, amount_sats,
                lightning_address, status, payment_hash, nostr_event_id, nostr_event_json, created_at)
                VALUES (:id, :conversion_id, :affiliate_pubkey, :amount_sats,
                :lightning_address, :status, :payment_hash, :nostr_event_id, :nostr_event_json, :created_at)
                """
            ),
            {
                "id": payout_id,
                "conversion_id": conversion_id,
                "affiliate_pubkey": click["affiliate_pubkey"],
                "amount_sats": commission_sats,
                "lightning_address": enr["lightning_address"] if enr else None,
                "status": "pending",
                "payment_hash": None,
                "nostr_event_id": None,
                "nostr_event_json": None,
                "created_at": now(),
            },
        )
    return {"conversion_id": conversion_id, "affiliate_pubkey": click["affiliate_pubkey"], "commission_sats": commission_sats, "status": "approved", "payout_status": "pending", "nostr_event_id": event["id"], "nostr_event": event, "relay_results": relay_results}


@app.get("/affiliates/{affiliate_pubkey}")
def affiliate_summary(affiliate_pubkey: str) -> dict[str, Any]:
    with engine().connect() as c:
        enrollments = [dict(r._mapping) for r in c.execute(text("SELECT * FROM enrollments WHERE affiliate_pubkey=:a"), {"a": affiliate_pubkey}).fetchall()]
        clicks = [dict(r._mapping) for r in c.execute(text("SELECT * FROM clicks WHERE affiliate_pubkey=:a"), {"a": affiliate_pubkey}).fetchall()]
        conversions = [dict(r._mapping) for r in c.execute(text("SELECT * FROM conversions WHERE affiliate_pubkey=:a"), {"a": affiliate_pubkey}).fetchall()]
        payouts = [dict(r._mapping) for r in c.execute(text("SELECT * FROM payouts WHERE affiliate_pubkey=:a"), {"a": affiliate_pubkey}).fetchall()]
    return {
        "affiliate_pubkey": affiliate_pubkey,
        "enrollments": len(enrollments),
        "clicks": len(clicks),
        "conversions": len(conversions),
        "pending_sats": sum(p["amount_sats"] for p in payouts if p["status"] == "pending"),
        "conversion_rows": conversions,
        "payout_rows": payouts,
    }


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


@app.post("/demo")
def demo() -> dict[str, Any]:
    campaign = create_campaign(CampaignIn(merchant_pubkey="merchant_pubkey_demo", destination_url=f"{BASE_URL}/demo-checkout"))
    enrollment = create_enrollment(EnrollmentIn(campaign_id=campaign["campaign_id"], affiliate_pubkey="affiliate_pubkey_demo", lightning_address="affiliate@getalby.com"))
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
                "affiliate_pubkey": "affiliate_pubkey_demo",
                "ip_hash": sha("demo-ip"),
                "user_agent_hash": sha("demo-ua"),
                "landing_url": f"{BASE_URL}/demo-checkout",
                "created_at": now(),
            },
        )
    conversion = create_conversion(ConversionIn(order_id=hid("ord"), click_id=click_id, order_total=100.0, currency="USD"))
    return {"campaign": campaign, "enrollment": enrollment, "click_id": click_id, "conversion": conversion, "affiliate": affiliate_summary("affiliate_pubkey_demo")}


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return """
    <html><head><title>Nostr Affiliate POC</title><style>body{font-family:system-ui;margin:40px;max-width:900px}code,pre{background:#f4f4f4;padding:2px 5px;border-radius:4px}li{margin:8px 0}</style></head>
    <body><h1>Nostr Affiliate POC</h1><p>Minimal demo: campaign → enrollment → redirect click → conversion → real Nostr proof → pending Lightning payout.</p>
    <ul><li><a href='/docs'>API docs</a></li><li><form method='post' action='/demo'><button>Run demo flow</button></form></li><li><a href='/proofs'>View Nostr proof events</a></li><li><a href='/health'>Health</a></li></ul>
    <p>Events are real Nostr events signed with Schnorr keys. If NOSTR_PUBLISH=true, the app publishes them to configured public relays.</p></body></html>
    """
