# Nostr Affiliate POC

Minimal proof-of-concept for a Nostr-powered affiliate network:

`campaign → enrollment → redirect click → conversion → real Nostr proof → pending Lightning payout`

## What this MVP proves

- Portable merchant/affiliate identity via validated Nostr pubkeys (`npub` or 64-char hex)
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
- `GET /bb.js` — lightweight tracking snippet that captures `bb_click_id`/`bb_ref`
- `GET /demo-merchant` — demo landing/checkout page using the snippet
- `POST /demo-merchant/checkout` — demo-only checkout trigger
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
- `SATS_PER_USD`: server-side USD→sats conversion rate used only when merchant reports `currency: "USD"`. Default: `2500`.

## Merchant tracking snippet

Real merchants can add:

```html
<script src="https://nostr-affiliate-poc-production.up.railway.app/bb.js"></script>
```

The snippet reads `bb_click_id` and `bb_ref` from URL params, stores them in first-party cookie + localStorage, injects hidden checkout form inputs, and exposes:

```js
window.BumbeiAttribution.get()
window.BumbeiAttribution.debug()
```

The demo merchant page is available at `/demo-merchant`. Visit it with params like:

```text
/demo-merchant?bb_click_id=clk_y8DrWEwJ8R&bb_ref=ref_I6al7223jL
```

Then submit the checkout form to simulate a paid order and trigger the conversion proof.

## Merchant webhook

```bash
curl -X POST "$BASE_URL/merchant/conversions" \
  -H "Authorization: Bearer bumbei-demo-key" \
  -H "Content-Type: application/json" \
  -d '{
    "order_id": "order_123",
    "bb_click_id": "clk_from_redirect",
    "order_total": 250000,
    "currency": "SATS",
    "customer_hash": "sha256:optional_customer_hash",
    "metadata": {"platform": "oshigoods"}
  }'
```

Supported currencies:

- `SATS`: `order_total` is already sats, ideal for Nostr-native merchants.
- `BTC`: `order_total` is BTC and the app converts to sats.
- `USD`: the merchant sends fiat amount; Bumbei/this service converts to sats with server-side `SATS_PER_USD`.

Merchants do **not** send `sats_per_usd`; exchange-rate policy stays server-side.

The response includes `order_total_sats`, `receipt_url`, `json_receipt_url`, `nostr_event_id`, payout status, and relay results. Duplicate `order_id` submissions are idempotent and return the original conversion.

## Privacy note

Clicks and order IDs are not published raw. Conversion proof events include only hashes such as `click_hash` and `conversion_hash`.
