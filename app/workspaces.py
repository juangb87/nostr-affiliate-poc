from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import bindparam, text


def short(value: Any, left: int = 12, right: int = 8) -> str:
    raw = str(value or "")
    return raw if len(raw) <= left + right + 1 else f"{raw[:left]}…{raw[-right:]}"


def _rows(connection: Any, statement: Any, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return [dict(row._mapping) for row in connection.execute(statement, params or {}).fetchall()]


def money_display(value: Any, currency: str) -> str:
    try:
        amount = Decimal(str(value or "0")).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        amount = Decimal("0.00")
    code = str(currency or "").upper()
    formatted = f"{amount:,.2f}"
    return f"${formatted}" if code == "USD" else f"{formatted} {code}".strip()


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
        SELECT c.*, mp.logo_url, mp.display_name, mp.tagline,
          (SELECT COUNT(*) FROM enrollments e WHERE e.campaign_id=c.id AND e.status='approved') AS affiliates,
          (SELECT COUNT(*) FROM enrollments e WHERE e.campaign_id=c.id AND e.status='pending') AS pending_affiliates,
          (SELECT COUNT(*) FROM conversions v WHERE v.campaign_id=c.id) AS conversions
        FROM campaigns c
        LEFT JOIN merchant_profiles mp ON mp.merchant_pubkey_hex=c.merchant_pubkey_hex
        WHERE c.archived_at IS NULL
          AND (c.merchant_pubkey_hex IN :identities OR c.merchant_pubkey IN :identities)
        ORDER BY c.created_at DESC
        """
    ).bindparams(bindparam("identities", expanding=True))
    campaigns = _rows(connection, campaign_stmt, {"identities": identity_list})
    campaign_ids = [row["id"] for row in campaigns]

    conversions: list[dict[str, Any]] = []
    payouts: list[dict[str, Any]] = []
    clicks: list[dict[str, Any]] = []
    enrollments: list[dict[str, Any]] = []
    shopify_sales: list[dict[str, Any]] = []
    totals = {"affiliates": 0, "clicks": 0, "conversions": 0, "commission_sats": 0, "actionable_payouts": 0}
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
            SELECT p.id, p.affiliate_pubkey, p.amount_sats, p.lightning_address,
                   p.status, p.state, p.payment_hash, p.payment_provider,
                   p.return_window_ends_at, p.created_at, c.name AS campaign_name
            FROM payouts p
            JOIN conversions v ON v.id=p.conversion_id
            JOIN campaigns c ON c.id=v.campaign_id
            WHERE v.campaign_id IN :campaign_ids
            ORDER BY p.created_at DESC LIMIT 30
            """
        ).bindparams(bindparam("campaign_ids", expanding=True))
        payouts = _rows(connection, payouts_stmt, {"campaign_ids": campaign_ids})
        clicks_stmt = text(
            """
            SELECT cl.id, cl.ref_code, cl.affiliate_pubkey, cl.landing_url, cl.created_at,
                   c.name AS campaign_name
            FROM clicks cl JOIN campaigns c ON c.id=cl.campaign_id
            WHERE cl.campaign_id IN :campaign_ids
            ORDER BY cl.created_at DESC LIMIT 50
            """
        ).bindparams(bindparam("campaign_ids", expanding=True))
        clicks = _rows(connection, clicks_stmt, {"campaign_ids": campaign_ids})
        enrollments_stmt = text(
            """
            SELECT e.id, e.campaign_id, e.affiliate_pubkey, e.affiliate_pubkey_hex, e.ref_code,
                   e.status, e.created_at, c.name AS campaign_name
            FROM enrollments e JOIN campaigns c ON c.id=e.campaign_id
            WHERE e.campaign_id IN :campaign_ids
            ORDER BY e.created_at DESC
            """
        ).bindparams(bindparam("campaign_ids", expanding=True))
        enrollments = _rows(connection, enrollments_stmt, {"campaign_ids": campaign_ids})
        shopify_sales_stmt = text(
            """
            SELECT d.currency, d.order_total_decimal, d.order_total
            FROM shopify_webhook_deliveries d
            JOIN conversions v ON v.id=d.conversion_id
            WHERE d.status='processed' AND v.status='approved' AND v.campaign_id IN :campaign_ids
            ORDER BY d.created_at
            """
        ).bindparams(bindparam("campaign_ids", expanding=True))
        shopify_sale_rows = _rows(connection, shopify_sales_stmt, {"campaign_ids": campaign_ids})
        totals_by_currency: dict[str, dict[str, Any]] = {}
        for row in shopify_sale_rows:
            currency = str(row.get("currency") or "").upper() or "USD"
            bucket = totals_by_currency.setdefault(currency, {"currency": currency, "orders": 0, "total": Decimal("0")})
            raw_total = row.get("order_total_decimal") or row.get("order_total") or "0"
            try:
                bucket["total"] += Decimal(str(raw_total))
            except InvalidOperation:
                continue
            bucket["orders"] += 1
        shopify_sales = [totals_by_currency[key] for key in sorted(totals_by_currency)]
        aggregate_stmt = text(
            """
            SELECT
              (SELECT COUNT(DISTINCT e.affiliate_pubkey_hex) FROM enrollments e WHERE e.campaign_id IN :campaign_ids AND e.status='approved') AS affiliates,
              (SELECT COUNT(*) FROM clicks cl WHERE cl.campaign_id IN :campaign_ids) AS clicks,
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
    for click in clicks:
        click["affiliate_short"] = short(click.get("affiliate_pubkey"))
        click["id_short"] = short(click.get("id"), 10, 6)
    for enrollment in enrollments:
        enrollment["affiliate_npub"] = str(enrollment.get("affiliate_pubkey") or enrollment.get("affiliate_pubkey_hex") or "")
        enrollment["affiliate_short"] = short(enrollment["affiliate_npub"])
    for sale in shopify_sales:
        sale["display"] = money_display(sale.get("total"), str(sale.get("currency") or ""))
        sale["orders"] = int(sale.get("orders") or 0)
    primary_sale = next(
        (sale for sale in shopify_sales if sale.get("currency") == "USD"),
        shopify_sales[0] if shopify_sales else None,
    )
    other_sales = [sale for sale in shopify_sales if sale is not primary_sale]
    shopify_orders = int(primary_sale["orders"]) if primary_sale else 0
    shopify_orders_total = sum(sale["orders"] for sale in shopify_sales)
    for payout in payouts:
        payout["affiliate_short"] = short(payout.get("affiliate_pubkey"))
        payout["user_state"] = payout_user_state(payout.get("state"))
        return_window_pending = False
        if payout.get("return_window_ends_at"):
            try:
                window_end = datetime.fromisoformat(str(payout["return_window_ends_at"]).replace("Z", "+00:00"))
                return_window_pending = window_end > datetime.now(timezone.utc)
            except ValueError:
                return_window_pending = True
        payout["return_window_pending"] = return_window_pending
        payout["manual_payable"] = bool(
            payout.get("state") == "PAYABLE" and payout.get("status") == "pending"
            and payout.get("lightning_address") and not payout.get("payment_provider")
            and not return_window_pending
        )

    return {
        "campaigns": campaigns,
        "clicks": clicks,
        "enrollments": enrollments,
        "conversions": conversions,
        "payouts": payouts,
        "totals": {
            "campaigns": len(campaigns),
            "active_campaigns": sum(1 for row in campaigns if row.get("status") == "active"),
            "affiliates": int(totals.get("affiliates") or 0),
            "clicks": int(totals.get("clicks") or 0),
            "conversions": int(totals.get("conversions") or 0),
            "commission_sats": int(totals.get("commission_sats") or 0),
            "actionable_payouts": int(totals.get("actionable_payouts") or 0),
        },
        "integration": {
            "shopify_ready": shopify_ready,
            "shopify_detail": shopify_detail,
            "shopify_sales": shopify_sales,
            "shopify_sales_primary_display": primary_sale["display"] if primary_sale else "$0.00",
            "shopify_sales_other": other_sales,
            "shopify_orders": shopify_orders,
            "shopify_orders_total": shopify_orders_total,
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


def affiliate_workspace_data(
    connection: Any,
    session: dict[str, Any],
    *,
    base_url: str,
    ref_base_url: str | None = None,
) -> dict[str, Any]:
    npub = session["npub"]
    pubkey_hex = session["nostr_pubkey_hex"]
    params = {"npub": npub, "hex": pubkey_hex}
    links = _rows(
        connection,
        text(
            """
            SELECT e.id, e.ref_code, e.status, e.lightning_address, e.destination_verified_at, e.created_at, c.id AS campaign_id,
                   c.name AS campaign_name, c.merchant_pubkey, c.status AS campaign_status,
                   c.commission_bps, c.window_days, c.destination_url
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
    profile_row = connection.execute(
        text(
            """
            SELECT lightning_address, verified_at, updated_at
            FROM affiliate_profiles
            WHERE affiliate_pubkey_hex=:hex OR affiliate_pubkey=:npub
            LIMIT 1
            """
        ),
        params,
    ).fetchone()
    profile = dict(profile_row._mapping) if profile_row else {}
    lightning_address = profile.get("lightning_address", "")
    link_base_url = ref_base_url.rstrip("/") if ref_base_url else None
    for link in links:
        link["commission_percent"] = f"{int(link['commission_bps']) / 100:g}"
        link["ref_url"] = (
            f"{link_base_url}/{link['ref_code']}"
            if link_base_url
            else f"{base_url.rstrip('/')}/r/{link['ref_code']}"
        )
        link["merchant_short"] = short(link.get("merchant_pubkey"))
        if profile:
            destination_ready = bool(
                profile.get("verified_at")
                and link.get("destination_verified_at")
                and link.get("lightning_address") == profile.get("lightning_address")
            )
        else:
            # Only enrollments marked during the schema upgrade retain temporary
            # grandfathered availability until the Affiliate verifies a profile.
            destination_ready = bool(
                link.get("lightning_address")
                and link.get("destination_verified_at") == "legacy"
            )
        link["available"] = bool(
            link.get("status") == "approved"
            and link.get("campaign_status") == "active"
            and destination_ready
        )
        if link["available"]:
            link["user_state"] = "Listo para compartir"
        elif link.get("campaign_status") != "active":
            link["user_state"] = "Programa pausado"
        elif not destination_ready:
            link["user_state"] = "Falta destino verificado"
        else:
            link["user_state"] = "Acceso pendiente"
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
        "lightning_address": lightning_address,
        "affiliate_profile": profile,
        "conversions": conversions,
        "payouts": payouts,
        "totals": {
            "active_links": sum(1 for row in links if row.get("available")),
            "clicks": clicks,
            "conversions": int(affiliate_totals.get("conversions") or 0),
            "gross_sats": int(affiliate_totals.get("gross_sats") or 0),
            "paid_sats": int(affiliate_totals.get("paid_sats") or 0),
        },
    }
