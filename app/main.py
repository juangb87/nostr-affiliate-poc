from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import Cookie, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

APP_SECRET = os.getenv("APP_SECRET", "dev-secret-change-me")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
DEFAULT_DESTINATION = os.getenv("DEFAULT_DESTINATION_URL", "https://example.com/checkout")

app = FastAPI(
    title="Nostr Affiliate POC",
    description="MVP: campaign → enrollment → redirect click → conversion → Nostr-style proof → pending Lightning payout.",
    version="0.2.0",
)

_ENGINE: Engine | None = None
_ENGINE_URL: str | None = None


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def database_url() -> str:
    url = os.getenv("DATABASE_URL", "sqlite:///./data/poc.db")
    # Railway/Postgres providers often expose postgres://; SQLAlchemy wants postgresql+psycopg://.
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


def sign_payload(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hmac.new(APP_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()


def nostr_event(kind: int, tags: list[list[str]], content: str = "") -> dict[str, Any]:
    payload = {"kind": kind, "tags": tags, "content": content, "created_at": now()}
    sig = sign_payload(payload)
    event_id = hashlib.sha256((json.dumps(payload, sort_keys=True) + sig).encode()).hexdigest()
    return {"id": event_id, "sig": sig, **payload, "relay_status": "mock_not_published"}


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
    """
    with engine().begin() as c:
        # SQLAlchemy/psycopg executes one statement at a time; split our simple DDL script.
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
def health() -> dict[str, str]:
    init_db()
    return {"ok": "true", "service": "nostr-affiliate-poc", "db": "postgres" if database_url().startswith("postgresql") else "sqlite"}


@app.post("/campaigns")
def create_campaign(body: CampaignIn) -> dict[str, Any]:
    init_db()
    campaign_id = hid("camp")
    terms_hash = sha(body.terms_url)
    event = nostr_event(
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
    with engine().begin() as c:
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
    return {"campaign_id": campaign_id, "nostr_event_id": event["id"], "nostr_event": event}


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
    event = nostr_event(
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
    with engine().begin() as c:
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
    return {"enrollment_id": enrollment_id, "ref_code": ref_code, "ref_url": f"{BASE_URL}/r/{ref_code}", "nostr_event_id": event["id"], "nostr_event": event}


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
        event = nostr_event(
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
    return {"conversion_id": conversion_id, "affiliate_pubkey": click["affiliate_pubkey"], "commission_sats": commission_sats, "status": "approved", "payout_status": "pending", "nostr_event_id": event["id"], "nostr_event": event}


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
        campaigns = [json.loads(r._mapping["nostr_event_json"]) for r in c.execute(text("SELECT nostr_event_json FROM campaigns ORDER BY created_at DESC")).fetchall()]
        enrollments = [json.loads(r._mapping["nostr_event_json"]) for r in c.execute(text("SELECT nostr_event_json FROM enrollments ORDER BY created_at DESC")).fetchall()]
        conversions = [json.loads(r._mapping["nostr_event_json"]) for r in c.execute(text("SELECT nostr_event_json FROM conversions ORDER BY created_at DESC")).fetchall()]
    return {"events": campaigns + enrollments + conversions}


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
    <body><h1>Nostr Affiliate POC</h1><p>Minimal demo: campaign → enrollment → redirect click → conversion → Nostr-style proof → pending Lightning payout.</p>
    <ul><li><a href='/docs'>API docs</a></li><li><form method='post' action='/demo'><button>Run demo flow</button></form></li><li><a href='/proofs'>View Nostr-style proof events</a></li><li><a href='/health'>Health</a></li></ul>
    <p>Events are HMAC-signed mock Nostr events for the MVP. Next step: publish to real relays and add Lightning payout integration.</p></body></html>
    """
