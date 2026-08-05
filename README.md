# Nostr Affiliate POC

Minimal proof-of-concept for a Nostr-powered affiliate network:

`campaign → enrollment → attribution → conversion proof → budgeted ledger → durable Lightning payout`

## What this MVP proves

- Portable merchant/affiliate identity via validated Nostr pubkeys (`npub` or 64-char hex)
- Campaign terms as signed, timestamped Nostr events
- Last-click attribution using `click_id`
- Conversion proof events with hashed click/order IDs
- Relay publication status stored in Postgres
- Lightning payout settlement proofs and reversal proofs
- Merchant-direct campaign budgets, balanced ledger entries, crash-safe payment attempts, and provider-independent payment rails

Events are now real Nostr events signed with Schnorr keys via `nostr-sdk`. If `NOSTR_PUBLISH=true`, the app publishes campaign, enrollment, and conversion proof events to configured public relays.

## Local run

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://localhost:8000/app for the account experience, or use:

```bash
python scripts/e2e.py
```

## API

- `GET /app` — Nostr account entry and role selection
- `GET /app/merchant` — authenticated, merchant-scoped workspace
- `GET /app/affiliate` — authenticated, affiliate-scoped workspace
- `GET /ops` — operator-only technical dashboard
- `GET /ops/data` — operator-only global dashboard data
- `GET /dashboard` — compatibility redirect to `/ops`
- `GET /dashboard/data` — compatibility redirect to `/ops/data`
- `POST /auth/nostr/challenge` — one-use Nostr login challenge
- `POST /auth/nostr/verify` — verify a signed challenge and create an opaque session
- Browser signing adapters: NIP-07 extensions plus temporary NIP-46 `nostrconnect://` deep links/QR for mobile and cross-device login. Both adapters sign the same server challenge; Meerat never receives an `nsec`.
- `GET /auth/me` — safe current-account summary
- `POST /auth/logout` — revoke the current session
- `GET /mrt.js` — canonical tracking snippet that captures `mrt_click_id`/`mrt_ref`
- `POST /v1/events` — public browser-side landing/page-view tracker for merchant storefronts
- `POST /v1/conversions` — public browser/pixel conversion signal logger; use `/merchant/conversions` for payout-grade server-side proofs
- `GET /v1/tracking/status` — safe aggregate tracking debug status
- `POST /shopify/webhooks/orders-paid` — Shopify-signed authoritative paid-order webhook; validates the raw-body HMAC and reads `mrt_click_id`/`mrt_campaign` from order `note_attributes`
- `GET /shopify/webhooks/status` — safe readiness check for the Shopify webhook configuration
- Legacy `/bumbei/*` routes remain working as hidden compatibility aliases during migration
- `GET /demo-merchant` — demo landing/checkout page using the snippet
- `POST /demo-merchant/checkout` — demo-only checkout trigger
- `POST /clicks/simulate`
- `POST /merchant/conversions` — merchant webhook with `Authorization: Bearer <merchant_api_key>`
- `POST /campaigns`
- `POST /campaigns/{campaign_id}/status` — republishes the addressable campaign event; merchant Bearer API key required
- `GET /campaigns/{campaign_id}`
- `GET /campaigns/{campaign_id}/summary`
- `GET /campaigns/{campaign_id}/page`
- `POST /enrollments`
- `POST /enrollments/{enrollment_id}/status` — republishes the addressable enrollment event; merchant Bearer API key required
- `GET /r/{ref_code}`
- `POST /conversions`
- `POST /conversions/{conversion_id}/reverse` — immutable refund/fraud/chargeback proof; merchant Bearer API key required
- `GET /proofs`
- `GET /affiliates/{npub_or_hex}`
- `GET /affiliates/{npub_or_hex}/summary`
- `GET /affiliates/{npub_or_hex}/profile`
- `GET /payouts/{payout_id}`
- `POST /payouts/{payout_id}/mark-paid` — sandbox Lightning payout settlement + kind `2802` proof
- `GET /payouts/{payout_id}/receipt`
- `GET /admin/campaigns/{campaign_id}/budget` — payout-admin budget counters
- `PUT /admin/campaigns/{campaign_id}/budget` — configure a campaign cap
- `POST /admin/payouts/{payout_id}/release-hold` — reserve a held obligation after top-up
- `GET /admin/payouts/{payout_id}/attempts` — private attempt history without preimages
- `GET /admin/payouts/{payout_id}/ledger` — balanced accounting entries
- `POST /admin/payouts/{payout_id}/execute` — atomically claim and execute via the explicitly configured payment rail
- `GET /admin/payment-rail/balance` — read-only balance for rails that support it
- `GET /admin/payment-attempts/recovery` — stale `PAYING`/`UNKNOWN` attempts requiring reconciliation
- `POST /admin/payment-attempts/{attempt_id}/refresh` — provider lookup for an existing attempt; never sends a payment
- `POST /admin/payment-attempts/{attempt_id}/reconcile` — settle or fail an ambiguous attempt without repaying
- `GET /nostr/events/{event_id}`
- `POST /demo`

## Nostr event schema v2

New events use a unified `v=2` schema:

- `39001` campaign — addressable, stable `d=campaign_id`, status can be republished.
- `39002` enrollment — addressable, stable `d=enrollment_id`, status can be republished.
- `2801` conversion — immutable fact.
- `2802` payout — immutable fact referencing the conversion Nostr event with `e`.
- `2803` reversal — immutable fact referencing the conversion Nostr event with `e`.

Conversion events reference the campaign and enrollment with NIP-01 `a` coordinates. `p` tags always use 64-character hex pubkeys with the role in the fourth element. Fiat fields are separated from `order_total_sats`; raw click/order IDs and customer data remain private.

See [`docs/nostr-schema-v2.md`](docs/nostr-schema-v2.md) for the complete tag schema and cutover policy.

## Payment ledger and state machine

New conversion obligations reserve the full affiliate commission plus a separate Meerat fee against `campaign_budgets`. Payouts follow `PAYABLE → PAYING → SETTLED → PUBLISHED`; insufficient budget produces `ON_HOLD`, while an ambiguous wallet response remains `PAYING` with an `UNKNOWN` attempt until an administrator reconciles it. A reversal during that ambiguity changes the payout to `CANCEL_PENDING`: `FAILED` reconciliation cancels and releases it, while `SETTLED` records the paid commission and cancels only the unpaid Meerat fee. It is never retried automatically.

Ledger movements are append-only balanced debit/credit pairs. Affiliate settlement moves only the promised commission to `settled_sats`; the separate Meerat fee remains committed with `fee_state=FEE_PENDING` for the provider-adapter sprint.

Public payout responses use a strict allowlist. Wallet destinations, BOLT11 invoices, provider errors, internal processing timestamps, attempt counters and reserved-budget values are available only through authenticated operational endpoints.

See [`docs/payment-ledger-sprint2.md`](docs/payment-ledger-sprint2.md) for state transitions, recovery invariants, tables, and admin operations.

## PaymentRail and adapters

Sprint 3 routes execution through a provider-independent `PaymentRail`. `FakePaymentRail` gives deterministic success, failure, pending and recovery scenarios without network access; `NwcPaymentRail` preserves the existing LNURL + Alby Hub NWC path; and `BlinkAdapter` provides an opt-in GraphQL boundary with staging as its default endpoint. A provider `PENDING` result or ambiguous exception remains `UNKNOWN` and is never automatically repaid.

Real rails remain disabled globally unless `LIGHTNING_PAYOUTS_ENABLED=true`, and `/execute` still requires payout-admin authorization plus every budget, return-window, reversal and amount guard. Merely configuring Blink credentials cannot trigger a payment. Sprint 3 does not execute the separate Meerat fee obligation.

See [`docs/payment-rails-sprint3.md`](docs/payment-rails-sprint3.md) for the contract, adapter configuration, recovery behavior and safety invariants.

## Railway

Railway can run this via the included `Procfile`:

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Recommended environment variables:

- `BASE_URL`: canonical public URL (production: `https://meerat.com`)
- `SHORT_LINK_HOST`: host that only serves branded redirects (production: `mrt.st`)
- `SHORT_LINK_BASE_URL`: base URL emitted for referral links (production: `https://mrt.st`)
- `DEFAULT_DESTINATION_URL`: merchant checkout URL used for redirect links
- `DATABASE_URL`: defaults to `sqlite:///./data/poc.db`; supports Railway Postgres URLs (`postgres://...`) for persistence
- `NOSTR_PRIVATE_KEY`: dedicated backend hex or `nsec...` private key used to sign events
- `APP_SECRET`: stable random secret of at least 32 characters; never use the development default in production
- `NOSTR_PUBLISH`: set to `true` to publish to relays
- `NOSTR_RELAYS`: comma-separated relay URLs. Default: `wss://nos.lol,wss://relay.damus.io,wss://relay.primal.net`
- `MERCHANT_API_KEYS`: comma-separated bearer tokens accepted by `/merchant/conversions`.
- `SHOPIFY_SECRET`: Shopify app client secret used to verify webhook HMAC signatures.
- `SHOPIFY_WEBHOOK_SECRET`: optional dedicated override for webhook verification; when omitted, `SHOPIFY_SECRET` is used.
- `SHOPIFY_STORE_DOMAIN`: permanent `*.myshopify.com` domain accepted in signed webhook headers.
- `SHOPIFY_MERCHANT_PUBKEY`: merchant identity assigned to authoritative Shopify conversions.
- `OPS_NOSTR_PUBKEYS`: comma-separated Nostr pubkeys authorized for `/ops`.
- `NIP46_RELAYS`: comma-separated secure `wss://` relays used only for temporary browser-to-signer NIP-46 login connections. Invalid, duplicate, credential-bearing, and non-TLS URLs are ignored; at most three are exposed to the browser.
- `MERCHANT_ACCOUNT_BINDINGS`: comma-separated `owner_pubkey:merchant_pubkey` administrative ownership pairs. For pilot bootstrap these identities must be distinct: the owner signs in through NIP-07 and the merchant pubkey identifies the tenant. Removing the pair revokes that delegated workspace access. Meerat never requests the owner's `nsec`.
- `MERCHANT_DEFAULT_PROGRAM_NAME`: server-controlled name for a bootstrapped Merchant's first Affiliate Program. Default: `Meerat Affiliate Program`.
- `MERCHANT_DEFAULT_COMMISSION_BPS`: commission for that first paused program. Default: `800` (8%).
- `MERCHANT_DEFAULT_WINDOW_DAYS`: attribution window for that first paused program. Default: `30`.
- `MERCHANT_DEFAULT_DESTINATION_URL`: storefront destination for that first paused program; must be HTTP(S). Falls back to `DEFAULT_DESTINATION_URL`.
- `MERCHANT_DEFAULT_TERMS_URL`: terms URL hashed into the first campaign event; must be HTTP(S).
- `ENABLE_LEGACY_DEMO_MUTATIONS`: explicit opt-in for legacy setup/demo mutations; keep `false` in production.
- `SATS_PER_USD`: server-side USD→sats conversion rate used only when merchant reports `currency: "USD"`. Default: `2500`.
- `PAYOUT_ADMIN_KEY`: bearer secret required by budget, attempt, ledger, recovery, reconciliation, and payout execution endpoints.
- `DEFAULT_CAMPAIGN_BUDGET_SATS`: initial internal cap for campaigns without an explicit admin budget. Default: `1000000`.
- `MEERAT_FEE_BPS`: separate merchant-paid Meerat fee in basis points. Default: `1000` (10%).
- `FEE_MIN_SATS`: minimum Meerat fee when the basis-point fee is non-zero. Default: `10`.
- `DEFAULT_RETURN_WINDOW_DAYS`: delay metadata before a payout is eligible. Default: `0` for the POC.
- `LIGHTNING_PAYOUTS_ENABLED`: global real/fake rail execution gate. Defaults to `false`.
- `LIGHTNING_MAX_PAYOUT_SATS`: hard maximum for one admin-triggered affiliate payment.
- `PAYMENT_RAIL`: `nwc` (default), `fake`, or `blink`.
- `ALLOW_FAKE_PAYMENT_RAIL`: must also be `true` before `PAYMENT_RAIL=fake` can execute.
- `NWC_CONNECTION_URI`: private NWC connection used only by the `nwc` adapter. Treat it as a spend credential and never log or return it.
- `NWC_TIMEOUT_SECONDS`: NWC relay/provider timeout, clamped to 5–60 seconds. Default: `30`.
- `NWC_CANARY_MIN_SATS`: minimum wallet balance threshold checked by readiness without exposing the balance. Default: `21`.
- `BLINK_API_KEY`: private Blink API key; required only when `PAYMENT_RAIL=blink`.
- `BLINK_WALLET_ID`: dedicated merchant BTC wallet id for Blink.
- `BLINK_GRAPHQL_URL`: Blink GraphQL endpoint; defaults to `https://api.staging.blink.sv/graphql`.

## NWC readiness and reconciliation

Before enabling real payouts, operators can run the authenticated, read-only readiness probe:

```bash
curl "$BASE_URL/admin/payments/nwc/readiness" \
  -H "Authorization: Bearer <PAYOUT_ADMIN_KEY>"
```

The response reports capability discovery, an authenticated wallet check, `pay_invoice` / `lookup_invoice` support, and whether the wallet can fund the configured canary threshold. It never returns the connection URI or exact balance and never initiates a payment. `LIGHTNING_PAYOUTS_ENABLED=false` may remain set while probing.

NWC attempts persist the prepared payment hash before payment. Ambiguous outcomes remain `UNKNOWN` and are never retried automatically; `POST /admin/payment-attempts/{attempt_id}/refresh` uses NIP-47 `lookup_invoice` to reconcile the existing hash without creating a payment.

## Merchant tracking snippet

Real merchants can add:

```html
<script src="https://meerat.com/mrt.js"></script>
```

The snippet reads `mrt_click_id` and `mrt_ref` from URL params, stores them in first-party cookie + localStorage, injects hidden checkout form inputs, and exposes:

```js
window.MeeratAttribution.get()
window.MeeratAttribution.debug()
```

The demo merchant page is available at `/demo-merchant`. Visit it with params like:

```text
/demo-merchant?mrt_click_id=clk_y8DrWEwJ8R&mrt_ref=ref_I6al7223jL
```

Then submit the checkout form to simulate a paid order and trigger the conversion proof.

`/bb.js`, the hidden `/bumbei/*` routes, legacy cookies, and legacy Shopify attributes remain bounded read-compatibility surfaces for already-installed storefronts and in-flight carts. New redirects, snippets, API schemas, responses, and installation instructions use only `mrt_*`. Contradictory canonical and legacy attribution values are rejected.

## Merchant webhook

```bash
curl -X POST "$BASE_URL/merchant/conversions" \
  -H "Authorization: Bearer <merchant-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "order_id": "order_123",
    "mrt_click_id": "clk_from_redirect",
    "order_total": 250000,
    "currency": "SATS",
    "customer_hash": "sha256:optional_customer_hash",
    "metadata": {"platform": "oshigoods"}
  }'
```

Supported currencies:

- `SATS`: `order_total` is already sats, ideal for Nostr-native merchants.
- `BTC`: `order_total` is BTC and the app converts to sats.
- `USD`: the merchant sends fiat amount; Meerat converts to sats with server-side `SATS_PER_USD`.

Merchants do **not** send `sats_per_usd`; exchange-rate policy stays server-side.

The response includes `order_total_sats`, `receipt_url`, `json_receipt_url`, `nostr_event_id`, payout status, and relay results. Duplicate `order_id` submissions are idempotent and return the original conversion.

## Shopify paid-order webhook

Register the Shopify topic `ORDERS_PAID` with this HTTPS callback:

```text
https://meerat.com/shopify/webhooks/orders-paid
```

The endpoint verifies `X-Shopify-Hmac-Sha256` against the unmodified request body, requires the configured shop domain, and reads canonical `mrt_click_id`/`mrt_campaign` from `note_attributes`. Paid orders without affiliate attribution are acknowledged and ignored so Shopify does not retry them. Attributed paid orders use the same authoritative conversion, Nostr proof, commission, and pending-payout flow as `/merchant/conversions`.

## Privacy note

Clicks and order IDs are not published raw. Conversion proof events include only hashes such as `click_hash` and `order_hash`.
