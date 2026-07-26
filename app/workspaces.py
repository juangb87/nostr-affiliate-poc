from __future__ import annotations

from typing import Any

from sqlalchemy import bindparam, text


def short(value: Any, left: int = 12, right: int = 8) -> str:
    raw = str(value or "")
    return raw if len(raw) <= left + right + 1 else f"{raw[:left]}…{raw[-right:]}"


def _rows(connection: Any, statement: Any, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return [dict(row._mapping) for row in connection.execute(statement, params or {}).fetchall()]


def merchant_workspace_data(connection: Any, session: dict[str, Any], *, base_url: str, shopify_ready: bool, shopify_detail: str) -> dict[str, Any]:
    account_id = session["account_id"]
    identities = {session["nostr_pubkey_hex"]}
    identities.update(
        row._mapping["merchant_pubkey_hex"]
        for row in connection.execute(
            text("SELECT merchant_pubkey_hex FROM merchant_account_links WHERE account_id=:account_id"),
            {"account_id": account_id},
        ).fetchall()
    )
    identity_list = sorted(identities)
    campaign_stmt = text(
        """
        SELECT c.*,
          (SELECT COUNT(*) FROM enrollments e WHERE e.campaign_id=c.id) AS affiliates,
          (SELECT COUNT(*) FROM conversions v WHERE v.campaign_id=c.id) AS conversions
        FROM campaigns c
        WHERE c.merchant_pubkey_hex IN :identities OR c.merchant_pubkey IN :identities
        ORDER BY c.created_at DESC
        """
    ).bindparams(bindparam("identities", expanding=True))
    campaigns = _rows(connection, campaign_stmt, {"identities": identity_list})
    campaign_ids = [row["id"] for row in campaigns]

    conversions: list[dict[str, Any]] = []
    payouts: list[dict[str, Any]] = []
    totals = {"affiliates": 0, "conversions": 0, "commission_sats": 0, "actionable_payouts": 0}
    tracking_events = 0
    if campaign_ids:
        conversions_stmt = text(
            """
            SELECT v.id, v.campaign_id, v.order_total, v.currency, v.commission_sats,
                   v.status, v.created_at, c.name AS campaign_name
            FROM conversions v JOIN campaigns c ON c.id=v.campaign_id
            WHERE v.campaign_id IN :campaign_ids
            ORDER BY v.created_at DESC LIMIT 30
            """
        ).bindparams(bindparam("campaign_ids", expanding=True))
        conversions = _rows(connection, conversions_stmt, {"campaign_ids": campaign_ids})
        payouts_stmt = text(
            """
            SELECT p.id, p.affiliate_pubkey, p.amount_sats, p.status, p.state,
                   p.payment_hash, p.created_at, c.name AS campaign_name
            FROM payouts p
            JOIN conversions v ON v.id=p.conversion_id
            JOIN campaigns c ON c.id=v.campaign_id
            WHERE v.campaign_id IN :campaign_ids
            ORDER BY p.created_at DESC LIMIT 30
            """
        ).bindparams(bindparam("campaign_ids", expanding=True))
        payouts = _rows(connection, payouts_stmt, {"campaign_ids": campaign_ids})
        aggregate_stmt = text(
            """
            SELECT
              (SELECT COUNT(DISTINCT e.affiliate_pubkey_hex) FROM enrollments e WHERE e.campaign_id IN :campaign_ids) AS affiliates,
              (SELECT COUNT(*) FROM conversions v WHERE v.campaign_id IN :campaign_ids AND v.status='approved') AS conversions,
              (SELECT COALESCE(SUM(v.commission_sats),0) FROM conversions v WHERE v.campaign_id IN :campaign_ids AND v.status='approved') AS commission_sats,
              (SELECT COUNT(*) FROM payouts p JOIN conversions v ON v.id=p.conversion_id
                 WHERE v.campaign_id IN :campaign_ids AND p.state IN ('PAYABLE','ON_HOLD','FAILED','UNKNOWN')) AS actionable_payouts
            """
        ).bindparams(bindparam("campaign_ids", expanding=True))
        totals = dict(connection.execute(aggregate_stmt, {"campaign_ids": campaign_ids}).one()._mapping)
        tracking_stmt = text(
            """
            SELECT COUNT(*) FROM tracking_events t
            WHERE t.ref_code IN (SELECT e.ref_code FROM enrollments e WHERE e.campaign_id IN :campaign_ids)
               OR t.click_id IN (SELECT cl.id FROM clicks cl WHERE cl.campaign_id IN :campaign_ids)
            """
        ).bindparams(bindparam("campaign_ids", expanding=True))
        tracking_events = int(connection.execute(tracking_stmt, {"campaign_ids": campaign_ids}).scalar_one())

    for campaign in campaigns:
        campaign["commission_percent"] = f"{int(campaign['commission_bps']) / 100:g}"
    for payout in payouts:
        payout["affiliate_short"] = short(payout.get("affiliate_pubkey"))
        payout["user_state"] = payout_user_state(payout.get("state"))

    return {
        "campaigns": campaigns,
        "conversions": conversions,
        "payouts": payouts,
        "totals": {
            "campaigns": len(campaigns),
            "active_campaigns": sum(1 for row in campaigns if row.get("status") == "active"),
            "affiliates": int(totals.get("affiliates") or 0),
            "conversions": int(totals.get("conversions") or 0),
            "commission_sats": int(totals.get("commission_sats") or 0),
            "actionable_payouts": int(totals.get("actionable_payouts") or 0),
        },
        "integration": {
            "shopify_ready": shopify_ready,
            "shopify_detail": shopify_detail,
            "tracking_events": tracking_events,
        },
    }


def payout_user_state(state: str | None) -> str:
    states = {
        "ON_HOLD": "En espera",
        "PAYABLE": "Listo para pagar",
        "PAYING": "Procesando",
        "UNKNOWN": "Requiere revisión",
        "FAILED": "Fallido",
        "SETTLED": "Pagado",
        "PUBLISHED": "Pagado y publicado",
        "CANCELLED": "Cancelado",
    }
    return states.get(str(state or "").upper(), str(state or "Pendiente"))


def affiliate_workspace_data(connection: Any, session: dict[str, Any], *, base_url: str) -> dict[str, Any]:
    npub = session["npub"]
    pubkey_hex = session["nostr_pubkey_hex"]
    params = {"npub": npub, "hex": pubkey_hex}
    links = _rows(
        connection,
        text(
            """
            SELECT e.id, e.ref_code, e.status, e.created_at, c.id AS campaign_id,
                   c.name AS campaign_name, c.commission_bps, c.window_days, c.destination_url
            FROM enrollments e JOIN campaigns c ON c.id=e.campaign_id
            WHERE e.affiliate_pubkey=:npub OR e.affiliate_pubkey_hex=:hex OR e.affiliate_pubkey=:hex
            ORDER BY e.created_at DESC
            """
        ),
        params,
    )
    conversions = _rows(
        connection,
        text(
            """
            SELECT v.id, v.campaign_id, v.commission_sats, v.status, v.currency,
                   v.created_at, c.name AS campaign_name
            FROM conversions v JOIN campaigns c ON c.id=v.campaign_id
            WHERE v.affiliate_pubkey=:npub OR v.affiliate_pubkey=:hex
            ORDER BY v.created_at DESC LIMIT 30
            """
        ),
        params,
    )
    payouts = _rows(
        connection,
        text(
            """
            SELECT p.id, p.amount_sats, p.status, p.state, p.payment_hash, p.created_at,
                   c.name AS campaign_name
            FROM payouts p
            JOIN conversions v ON v.id=p.conversion_id
            JOIN campaigns c ON c.id=v.campaign_id
            WHERE p.affiliate_pubkey=:npub OR p.affiliate_pubkey=:hex
            ORDER BY p.created_at DESC LIMIT 30
            """
        ),
        params,
    )
    clicks = int(connection.execute(text("SELECT COUNT(*) FROM clicks WHERE affiliate_pubkey=:npub OR affiliate_pubkey=:hex"), params).scalar_one())
    for link in links:
        link["commission_percent"] = f"{int(link['commission_bps']) / 100:g}"
        link["ref_url"] = f"{base_url}/r/{link['ref_code']}"
    for payout in payouts:
        payout["user_state"] = payout_user_state(payout.get("state"))
        payout["payment_hash_short"] = short(payout.get("payment_hash"), 8, 6) if payout.get("payment_hash") else None
    affiliate_totals = dict(
        connection.execute(
            text(
                """
                SELECT
                  (SELECT COUNT(*) FROM conversions v WHERE (v.affiliate_pubkey=:npub OR v.affiliate_pubkey=:hex) AND v.status='approved') AS conversions,
                  (SELECT COALESCE(SUM(v.commission_sats),0) FROM conversions v WHERE (v.affiliate_pubkey=:npub OR v.affiliate_pubkey=:hex) AND v.status='approved') AS gross_sats,
                  (SELECT COALESCE(SUM(p.amount_sats),0) FROM payouts p
                     WHERE (p.affiliate_pubkey=:npub OR p.affiliate_pubkey=:hex)
                       AND p.state IN ('SETTLED','PUBLISHED')) AS paid_sats
                """
            ),
            params,
        ).one()._mapping
    )
    return {
        "links": links,
        "conversions": conversions,
        "payouts": payouts,
        "totals": {
            "active_links": sum(1 for row in links if row.get("status") == "approved"),
            "clicks": clicks,
            "conversions": int(affiliate_totals.get("conversions") or 0),
            "gross_sats": int(affiliate_totals.get("gross_sats") or 0),
            "paid_sats": int(affiliate_totals.get("paid_sats") or 0),
        },
    }
