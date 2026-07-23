# Nostr Affiliate POC

Minimal proof-of-concept for a Nostr-inspired affiliate network:

`campaign → enrollment → redirect click → conversion → Nostr-style proof → pending Lightning payout`

## What this MVP proves

- Portable merchant/affiliate identity via pubkeys/npubs
- Campaign terms as signed, timestamped Nostr-style events
- Last-click attribution using `click_id`
- Conversion proof events with hashed click/order IDs
- Pending Lightning payout rows for future settlement

For now, events are HMAC-signed mock Nostr events (`relay_status=mock_not_published`). The next iteration can publish to real Nostr relays and integrate LNURL/Alby/LNbits payouts.

## Local run

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://localhost:8000 and click **Run demo flow**, or use:

```bash
python scripts/e2e.py
```

## API

- `POST /campaigns`
- `POST /enrollments`
- `GET /r/{ref_code}`
- `POST /conversions`
- `GET /affiliates/{affiliate_pubkey}`
- `GET /proofs`
- `POST /demo`

## Railway

Railway can run this via the included `Procfile`:

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Recommended environment variables:

- `APP_SECRET`: random signing secret for mock event signatures
- `BASE_URL`: public Railway URL
- `DEFAULT_DESTINATION_URL`: merchant checkout URL used for redirect links
- `DATABASE_URL`: defaults to `sqlite:///./data/poc.db`; use a mounted volume or Postgres in a later version
