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


class SimulateClickIn(BaseModel):
    ref_code: str


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


@app.post("/clicks/simulate")
def simulate_click(body: SimulateClickIn) -> dict[str, Any]:
    """Dashboard helper: create a click without following a browser redirect."""
    init_db()
    with engine().begin() as c:
        enr = asdict(c.execute(text("SELECT * FROM enrollments WHERE ref_code=:ref"), {"ref": body.ref_code}).fetchone())
        if not enr:
            raise HTTPException(404, "ref code not found")
        camp = asdict(c.execute(text("SELECT * FROM campaigns WHERE id=:id"), {"id": enr["campaign_id"]}).fetchone())
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
    sep = "&" if "?" in camp["destination_url"] else "?"
    redirect_url = f"{camp['destination_url']}{sep}bb_click_id={click_id}&bb_ref={body.ref_code}"
    return {"click_id": click_id, "ref_code": body.ref_code, "campaign_id": enr["campaign_id"], "affiliate_pubkey": enr["affiliate_pubkey"], "redirect_url": redirect_url}


@app.get("/dashboard/data")
def dashboard_data() -> dict[str, Any]:
    init_db()
    with engine().connect() as c:
        counts = {
            "campaigns": c.execute(text("SELECT COUNT(*) FROM campaigns")).scalar_one(),
            "enrollments": c.execute(text("SELECT COUNT(*) FROM enrollments")).scalar_one(),
            "clicks": c.execute(text("SELECT COUNT(*) FROM clicks")).scalar_one(),
            "conversions": c.execute(text("SELECT COUNT(*) FROM conversions")).scalar_one(),
            "pending_sats": c.execute(text("SELECT COALESCE(SUM(amount_sats), 0) FROM payouts WHERE status='pending'")).scalar_one(),
            "nostr_events": c.execute(text("SELECT COUNT(*) FROM nostr_events")).scalar_one(),
            "published_events": c.execute(text("SELECT COUNT(*) FROM nostr_events WHERE relay_status='published'")).scalar_one(),
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
    return {"health": health(), "counts": counts, "campaigns": campaigns, "enrollments": enrollments, "clicks": clicks, "conversions": conversions, "events": events}


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
  <section class="grid">
    <div class="card span-4">
      <h2>1. Create campaign</h2>
      <div class="row"><input id="merchant" value="merchant_pubkey_demo" placeholder="merchant pubkey"><input id="campaignName" value="Bumbei BTC Rewards" placeholder="campaign name"></div>
      <div class="row"><input id="commission" type="number" value="800" placeholder="bps"><input id="windowDays" type="number" value="30" placeholder="window days"></div>
      <input id="destination" value="https://example.com/checkout" placeholder="destination URL">
      <p><button onclick="createCampaign()">Create campaign + publish Nostr event</button></p>
    </div>
    <div class="card span-4">
      <h2>2. Enroll affiliate</h2>
      <input id="campaignId" placeholder="campaign_id from step 1">
      <div class="row"><input id="affiliate" value="affiliate_pubkey_demo" placeholder="affiliate pubkey"><input id="lightning" value="affiliate@getalby.com" placeholder="Lightning address"></div>
      <p><button onclick="createEnrollment()">Enroll + generate ref link</button></p>
      <p id="refBox" class="label"></p>
    </div>
    <div class="card span-4">
      <h2>3. Click + conversion</h2>
      <input id="refCode" placeholder="ref_code from enrollment">
      <p><button class="secondary" onclick="simulateClick()">Simulate click</button></p>
      <input id="clickId" placeholder="click_id">
      <div class="row"><input id="orderTotal" type="number" value="100"><input id="satsUsd" type="number" value="2500"></div>
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
function status(s){ return `<span class="status ${s}">${s}</span>`; }
function table(rows, cols){ if(!rows?.length) return '<p class="label">No rows yet.</p>'; return `<div class="table-wrap"><table><thead><tr>${cols.map(c=>`<th>${c[0]}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${cols.map(c=>`<td>${c[2]?c[2](r[c[1]],r):r[c[1]]??''}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`; }
async function refresh(){
  const data = await api('/dashboard/data');
  $('health-pill').textContent = `${data.health.db} · Nostr publish ${data.health.nostr_publish ? 'on' : 'off'}`;
  const metrics = [['Campaigns',data.counts.campaigns],['Enrollments',data.counts.enrollments],['Clicks',data.counts.clicks],['Conversions',data.counts.conversions],['Pending sats',data.counts.pending_sats],['Published events',data.counts.published_events]];
  $('metrics').innerHTML = metrics.map(m=>`<div class="card metric-card"><div class="label">${m[0]}</div><div class="metric">${m[1]}</div></div>`).join('');
  $('campaigns').innerHTML = table(data.campaigns, [['ID','id',v=>`<code>${v}</code>`],['Name','name'],['bps','commission_bps'],['Event','nostr_event_id',v=>`<a href="/nostr/events/${v}">${short(v)}</a>`]]);
  $('conversions').innerHTML = table(data.conversions, [['ID','id',v=>`<code>${v}</code>`],['Affiliate','affiliate_pubkey',short],['sats','commission_sats'],['Event','nostr_event_id',v=>`<a href="/nostr/events/${v}">${short(v)}</a>`]]);
  $('events').innerHTML = table(data.events, [['Kind','kind'],['Entity','entity_type',(v,r)=>`${v}<br><code>${r.entity_id}</code>`],['Relay','relay_status',status],['Event','event_id',v=>`<a href="/nostr/events/${v}">${short(v)}</a>`],['Relays','relays',(v)=>v.map(r=>`${status(r.status)} ${r.relay_url.replace('wss://','')}`).join('<br>')]]);
}
async function createCampaign(){
  const data = await api('/campaigns',{method:'POST',body:JSON.stringify({merchant_pubkey:$('merchant').value,name:$('campaignName').value,commission_bps:+$('commission').value,attribution_window_days:+$('windowDays').value,destination_url:$('destination').value})});
  $('campaignId').value=data.campaign_id; show(data); toast('Campaign created'); await refresh();
}
async function createEnrollment(){
  const data = await api('/enrollments',{method:'POST',body:JSON.stringify({campaign_id:$('campaignId').value,affiliate_pubkey:$('affiliate').value,lightning_address:$('lightning').value})});
  $('refCode').value=data.ref_code; $('refBox').innerHTML=`Ref URL: <a href="${data.ref_url}" target="_blank">${data.ref_url}</a>`; show(data); toast('Affiliate enrolled'); await refresh();
}
async function simulateClick(){
  const data = await api('/clicks/simulate',{method:'POST',body:JSON.stringify({ref_code:$('refCode').value})});
  $('clickId').value=data.click_id; show(data); toast('Click simulated'); await refresh();
}
async function createConversion(){
  const data = await api('/conversions',{method:'POST',body:JSON.stringify({order_id:'ord_'+crypto.randomUUID(),click_id:$('clickId').value,order_total:+$('orderTotal').value,currency:'USD',sats_per_usd:+$('satsUsd').value})});
  show(data); toast('Conversion proof published'); await refresh();
}
async function runDemo(){ const data = await api('/demo',{method:'POST'}); $('campaignId').value=data.campaign.campaign_id; $('refCode').value=data.enrollment.ref_code; $('clickId').value=data.click_id; show(data); toast('Full demo complete'); await refresh(); }
refresh().catch(e=>toast(e.message));
</script>
</body>
</html>
"""


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> str:
    return DASHBOARD_HTML


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return """
    <html><head><title>Nostr Affiliate POC</title><style>body{font-family:system-ui;margin:40px;max-width:900px}code,pre{background:#f4f4f4;padding:2px 5px;border-radius:4px}li{margin:8px 0}</style></head>
    <body><h1>Nostr Affiliate POC</h1><p>Minimal demo: campaign → enrollment → redirect click → conversion → real Nostr proof → pending Lightning payout.</p>
    <ul><li><a href='/dashboard'>Dashboard</a></li><li><a href='/docs'>API docs</a></li><li><form method='post' action='/demo'><button>Run demo flow</button></form></li><li><a href='/proofs'>View Nostr proof events</a></li><li><a href='/health'>Health</a></li></ul>
    <p>Events are real Nostr events signed with Schnorr keys. If NOSTR_PUBLISH=true, the app publishes them to configured public relays.</p></body></html>
    """
