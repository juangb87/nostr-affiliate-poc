# Meerat Nostr Affiliate Event Schema v2

Status: experimental implementation specification.

## Kinds

| Kind | Type | Nature |
|---:|---|---|
| 39001 | `affiliate_campaign` | Addressable state (`d=campaign_id`) |
| 39002 | `affiliate_enrollment` | Addressable state (`d=enrollment_id`) |
| 2801 | `affiliate_conversion` | Immutable fact |
| 2802 | `affiliate_payout` | Immutable fact |
| 2803 | `affiliate_reversal` | Immutable fact |

Kinds 2801–2803 were checked against the official `nostr-protocol/nips` repository before implementation and were unassigned at that time. They remain experimental Meerat kinds until formally coordinated or registered.

## Shared conventions

Every v2 event includes:

```json
["v", "2"]
```

Identity tags use hex pubkeys and an explicit role:

```json
["p", "<merchant_pubkey_hex>", "", "merchant"]
["p", "<affiliate_pubkey_hex>", "", "affiliate"]
```

Raw click IDs, order IDs, Lightning addresses, customer data, IPs, user agents, invoices and private wallet credentials must not be published.

## Campaign — kind 39001

Required tags:

```json
["v", "2"]
["d", "<campaign_id>"]
["type", "affiliate_campaign"]
["p", "<merchant_hex>", "", "merchant"]
["campaign", "<campaign_id>"]
["status", "active|paused|ended"]
["commission_bps", "<integer>"]
["window_days", "<integer>"]
["terms", "sha256:<hash>"]
["destination", "<url>"]
```

Status changes republish the same `(pubkey, kind, d)` coordinate. A `state_revision` tag prevents same-second state transitions from producing an identical event ID.

## Enrollment — kind 39002

Required tags:

```json
["v", "2"]
["d", "<enrollment_id>"]
["type", "affiliate_enrollment"]
["p", "<merchant_hex>", "", "merchant"]
["p", "<affiliate_hex>", "", "affiliate"]
["campaign", "<campaign_id>"]
["status", "pending|approved|rejected|terminated"]
["terms", "sha256:<hash>"]
```

Status changes republish the same `(pubkey, kind, d)` coordinate. A `state_revision` tag prevents same-second state transitions from producing an identical event ID.

## Conversion — kind 2801

Required tags:

```json
["v", "2"]
["type", "affiliate_conversion"]
["p", "<merchant_hex>", "", "merchant"]
["p", "<affiliate_hex>", "", "affiliate"]
["campaign", "<campaign_id>"]
["a", "39001:<platform_pubkey>:<campaign_id>"]
["a", "39002:<platform_pubkey>:<enrollment_id>"]
["click_hash", "sha256:<hash>"]
["order_hash", "sha256:<hash>"]
["order_total_sats", "<integer>"]
["commission_sats", "<integer>"]
["commission_bps", "<integer>"]
["status", "approved"]
```

Optional fiat tags:

```json
["order_fiat_amount", "25.00"]
["order_fiat_currency", "USD"]
```

## Payout — kind 2802

Required tags:

```json
["v", "2"]
["type", "affiliate_payout"]
["e", "<conversion_event_id>"]
["p", "<merchant_hex>", "", "merchant"]
["p", "<affiliate_hex>", "", "affiliate"]
["campaign", "<campaign_id>"]
["status", "paid"]
["amount_sats", "<integer>"]
["payment_hash", "<hash>"]
["settled_at", "<unix_seconds>"]
```

Sandbox payouts additionally include `["sandbox", "true"]`. Lightning preimages and destination addresses remain private by default.

## Reversal — kind 2803

Required tags:

```json
["v", "2"]
["type", "affiliate_reversal"]
["e", "<conversion_event_id>"]
["p", "<merchant_hex>", "", "merchant"]
["p", "<affiliate_hex>", "", "affiliate"]
["campaign", "<campaign_id>"]
["reason", "refund|fraud|chargeback|cancelled|other"]
["reversed_at", "<unix_seconds>"]
```

A partial or full refund may include:

```json
["refund_sats", "<integer>"]
```

One reversal is emitted per conversion in the current MVP; duplicate submissions return the original event.

## Cutover policy

- Existing kind 39005 conversion events and kind 39006 payout events remain historical v1 records.
- Meerat does not attempt to delete old relay events.
- New events created after the production v2 deployment use this schema.
- The verified production cutover timestamp and first v2 event IDs are recorded below after deployment.

```text
cutover_utc: PENDING_DEPLOYMENT
first_campaign_v2: PENDING_DEPLOYMENT
first_enrollment_v2: PENDING_DEPLOYMENT
first_conversion_v2: PENDING_DEPLOYMENT
first_payout_v2: PENDING_DEPLOYMENT
first_reversal_v2: PENDING_DEPLOYMENT
```
