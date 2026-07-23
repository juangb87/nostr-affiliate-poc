# Nostr Affiliate POC

Minimal proof-of-concept for a Nostr-powered affiliate network:

`campaign → enrollment → redirect click → conversion → real Nostr proof → pending Lightning payout`

## What this MVP proves

- Portable merchant/affiliate identity via pubkeys/npubs
- Campaign terms as signed, timestamped Nostr events
- Last-click attribution using `click_id`
- Conversion proof events with hashed click/order IDs
- Relay publication status stored in Postgres
- Pending Lightning payout rows for future settlement

Events are now real Nostr events signed with Schnorr keys via `nostr-sdk`. If `NOSTR_PUBLISH=true`, the app publishes campaign, enrollment, and conversion proof events to configured public relays.

## Local run

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://localhost:8000/dashboard for the interactive dashboard, or use:

```bash
python scripts/e2e.py
```

## API

- `GET /dashboard`
- `GET /dashboard/data`
- `POST /clicks/simulate`
- `POST /merchant/conversions` — merchant webhook with `Authorization: Bearer <merchant_api_key>`
- `POST /campaigns`
- `POST /enrollments`
- `GET /r/{ref_code}`
- `POST /conversions`
- `GET /affiliates/{affiliate_pubkey}`
- `GET /proofs`
- `GET /nostr/events/{event_id}`
- `POST /demo`

## Railway

Railway can run this via the included `Procfile`:

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Recommended environment variables:

- `BASE_URL`: public Railway URL
- `DEFAULT_DESTINATION_URL`: merchant checkout URL used for redirect links
- `DATABASE_URL`: defaults to `sqlite:///./data/poc.db`; supports Railway Postgres URLs (`postgres://...`) for persistence
- `NOSTR_PRIVATE_KEY`: hex or `nsec...` private key used to sign events
- `NOSTR_PUBLISH`: set to `true` to publish to relays
- `NOSTR_RELAYS`: comma-separated relay URLs. Default: `wss://nos.lol,wss://relay.damus.io,wss://relay.primal.net`
- `MERCHANT_API_KEYS`: comma-separated bearer tokens accepted by `/merchant/conversions`.

## Merchant webhook

```bash
curl -X POST "$BASE_URL/merchant/conversions" \
  -H "Authorization: Bearer bumbei-demo-key" \
  -H "Content-Type: application/json" \
  -d '{
    "order_id": "order_123",
    "bb_click_id": "clk_from_redirect",
    "order_total": 100,
    "currency": "USD",
    "customer_hash": "sha256:optional_customer_hash",
    "metadata": {"platform": "shopify"}
  }'
```

The response includes `receipt_url`, `json_receipt_url`, `nostr_event_id`, payout status, and relay results. Duplicate `order_id` submissions are idempotent and return the original conversion.

## Privacy note

Clicks and order IDs are not published raw. Conversion proof events include only hashes such as `click_hash` and `conversion_hash`.
