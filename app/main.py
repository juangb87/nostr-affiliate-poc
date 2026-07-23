from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import Cookie, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

APP_SECRET = os.getenv("APP_SECRET", "dev-secret-change-me")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
DEFAULT_DESTINATION = os.getenv("DEFAULT_DESTINATION_URL", "https://example.com/checkout")

app = FastAPI(
    title="Nostr Affiliate POC",
    description="MVP: campaign → enrollment → redirect click → conversion → Nostr-style proof → pending Lightning payout.",
    version="0.1.0",
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_path() -> Path:
    database_url = os.getenv("DATABASE_URL", "sqlite:///./data/poc.db")
    if not database_url.startswith("sqlite:///"):
        raise RuntimeError("Only sqlite:/// DATABASE_URL is supported in this POC")
    p = Path(database_url.replace("sqlite:///", "", 1))
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(db_path())
    c.row_factory = sqlite3.Row
    return c


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


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
    with conn() as c:
        c.executescript(
            """
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
                created_at TEXT NOT NULL,
                FOREIGN KEY(campaign_id) REFERENCES campaigns(id)
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
        )


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
    return {"ok": "true", "service": "nostr-affiliate-poc"}


@app.post("/campaigns")
def create_campaign(body: CampaignIn) -> dict[str, Any]:
    init_db()
    campaign_id = hid("camp")
    terms_hash = sha(body.terms_url)
    event = nostr_event(39001, [
        ["d", campaign_id], ["type", "affiliate_campaign"], ["merchant", body.merchant_pubkey],
        ["commission_bps", str(body.commission_bps)], ["window_days", str(body.attribution_window_days)],
        ["payout", "sats"], ["terms", terms_hash], ["destination", body.destination_url],
    ], json.dumps({"name": body.name, "terms_url": body.terms_url}))
    with conn() as c:
        c.execute(
            "INSERT INTO campaigns VALUES (?,?,?,?,?,?,?,?,?,?)",
            (campaign_id, body.merchant_pubkey, body.name, body.commission_bps, body.attribution_window_days,
             body.destination_url, terms_hash, event["id"], json.dumps(event), now()),
        )
    return {"campaign_id": campaign_id, "nostr_event_id": event["id"], "nostr_event": event}


@app.get("/campaigns/{campaign_id}")
def get_campaign(campaign_id: str) -> dict[str, Any]:
    with conn() as c:
        campaign = row_to_dict(c.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone())
    if not campaign:
        raise HTTPException(404, "campaign not found")
    campaign["nostr_event"] = json.loads(campaign.pop("nostr_event_json"))
    return campaign


@app.post("/enrollments")
def create_enrollment(body: EnrollmentIn) -> dict[str, Any]:
    init_db()
    with conn() as c:
        camp = c.execute("SELECT * FROM campaigns WHERE id=?", (body.campaign_id,)).fetchone()
    if not camp:
        raise HTTPException(404, "campaign not found")
    enrollment_id = hid("enr")
    ref_code = hid("ref")
    event = nostr_event(39002, [
        ["type", "affiliate_enrollment"], ["campaign", body.campaign_id],
        ["merchant", camp["merchant_pubkey"]], ["affiliate", body.affiliate_pubkey],
        ["terms", camp["terms_hash"]],
    ], "")
    with conn() as c:
        c.execute(
            "INSERT INTO enrollments VALUES (?,?,?,?,?,?,?,?)",
            (enrollment_id, body.campaign_id, body.affiliate_pubkey, body.lightning_address, ref_code,
             event["id"], json.dumps(event), now()),
        )
    return {"enrollment_id": enrollment_id, "ref_code": ref_code, "ref_url": f"{BASE_URL}/r/{ref_code}", "nostr_event_id": event["id"], "nostr_event": event}


@app.get("/r/{ref_code}")
def redirect_click(ref_code: str, request: Request) -> RedirectResponse:
    init_db()
    with conn() as c:
        enr = c.execute("SELECT * FROM enrollments WHERE ref_code=?", (ref_code,)).fetchone()
        if not enr:
            raise HTTPException(404, "ref code not found")
        camp = c.execute("SELECT * FROM campaigns WHERE id=?", (enr["campaign_id"],)).fetchone()
        click_id = hid("clk")
        ip = request.client.host if request.client else "unknown"
        ua = request.headers.get("user-agent", "")
        c.execute(
            "INSERT INTO clicks VALUES (?,?,?,?,?,?,?,?)",
            (click_id, ref_code, enr["campaign_id"], enr["affiliate_pubkey"], sha(ip), sha(ua), camp["destination_url"], now()),
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
    with conn() as c:
        click = c.execute("SELECT * FROM clicks WHERE id=?", (click_id,)).fetchone()
        if not click:
            raise HTTPException(404, "click not found")
        camp = c.execute("SELECT * FROM campaigns WHERE id=?", (click["campaign_id"],)).fetchone()
        enr = c.execute("SELECT * FROM enrollments WHERE ref_code=?", (click["ref_code"],)).fetchone()
        commission_sats = round(body.order_total * body.sats_per_usd * int(camp["commission_bps"]) / 10000)
        conversion_id = hid("conv")
        event = nostr_event(39005, [
            ["type", "affiliate_conversion"], ["campaign", click["campaign_id"]],
            ["merchant", camp["merchant_pubkey"]], ["affiliate", click["affiliate_pubkey"]],
            ["click_hash", sha(click_id)], ["conversion_hash", sha(body.order_id)],
            ["commission_sats", str(commission_sats)], ["status", "approved"],
        ], "")
        c.execute(
            "INSERT INTO conversions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (conversion_id, sha(body.order_id), click_id, click["campaign_id"], click["affiliate_pubkey"],
             body.order_total, body.currency, commission_sats, "approved", event["id"], json.dumps(event), now()),
        )
        payout_id = hid("pay")
        c.execute(
            "INSERT INTO payouts VALUES (?,?,?,?,?,?,?,?,?,?)",
            (payout_id, conversion_id, click["affiliate_pubkey"], commission_sats, enr["lightning_address"],
             "pending", None, None, None, now()),
        )
    return {"conversion_id": conversion_id, "affiliate_pubkey": click["affiliate_pubkey"], "commission_sats": commission_sats, "status": "approved", "payout_status": "pending", "nostr_event_id": event["id"], "nostr_event": event}


@app.get("/affiliates/{affiliate_pubkey}")
def affiliate_summary(affiliate_pubkey: str) -> dict[str, Any]:
    with conn() as c:
        enrollments = [dict(r) for r in c.execute("SELECT * FROM enrollments WHERE affiliate_pubkey=?", (affiliate_pubkey,)).fetchall()]
        clicks = [dict(r) for r in c.execute("SELECT * FROM clicks WHERE affiliate_pubkey=?", (affiliate_pubkey,)).fetchall()]
        conversions = [dict(r) for r in c.execute("SELECT * FROM conversions WHERE affiliate_pubkey=?", (affiliate_pubkey,)).fetchall()]
        payouts = [dict(r) for r in c.execute("SELECT * FROM payouts WHERE affiliate_pubkey=?", (affiliate_pubkey,)).fetchall()]
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
    with conn() as c:
        campaigns = [json.loads(r["nostr_event_json"]) for r in c.execute("SELECT nostr_event_json FROM campaigns ORDER BY created_at DESC").fetchall()]
        enrollments = [json.loads(r["nostr_event_json"]) for r in c.execute("SELECT nostr_event_json FROM enrollments ORDER BY created_at DESC").fetchall()]
        conversions = [json.loads(r["nostr_event_json"]) for r in c.execute("SELECT nostr_event_json FROM conversions ORDER BY created_at DESC").fetchall()]
    return {"events": campaigns + enrollments + conversions}


@app.post("/demo")
def demo() -> dict[str, Any]:
    campaign = create_campaign(CampaignIn(merchant_pubkey="merchant_pubkey_demo", destination_url=f"{BASE_URL}/demo-checkout"))
    enrollment = create_enrollment(EnrollmentIn(campaign_id=campaign["campaign_id"], affiliate_pubkey="affiliate_pubkey_demo", lightning_address="affiliate@getalby.com"))
    # Insert click directly for deterministic API demo without following redirects.
    click_id = hid("clk")
    with conn() as c:
        c.execute("INSERT INTO clicks VALUES (?,?,?,?,?,?,?,?)", (click_id, enrollment["ref_code"], campaign["campaign_id"], "affiliate_pubkey_demo", sha("demo-ip"), sha("demo-ua"), f"{BASE_URL}/demo-checkout", now()))
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
