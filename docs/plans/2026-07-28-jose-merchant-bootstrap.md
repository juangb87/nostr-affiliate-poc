# José Merchant Bootstrap + Default Affiliate Program Implementation Plan

> **For Hermes:** Implement task-by-task with strict RED→GREEN→REFACTOR and independent review before commit.

**Goal:** Let an administratively bound NIP-07 owner sign in before any campaign exists and idempotently create one tenant-scoped paused default Affiliate Program, while keeping legacy mutations disabled.

**Architecture:** Reuse `MERCHANT_ACCOUNT_BINDINGS` and `merchant_account_links` as the pilot's explicit/revocable administrative enrollment source. A cookie-authenticated, same-origin endpoint selects only a server-bound merchant tenant, creates a deterministic default campaign plus budget and durable Nostr outbox atomically, and publishes only after commit. No `nsec`, new table, migration, public signup, arbitrary campaign mutation, or Shopify readiness claim.

**Tech Stack:** FastAPI, Pydantic, SQLAlchemy text queries, SQLite/PostgreSQL, Jinja, Nostr SDK, pytest.

---

## Product and security invariants

- Human login is NIP-07 only; Meerat never requests or receives an `nsec`.
- Pilot bootstrap is admin-assisted through `MERCHANT_ACCOUNT_BINDINGS`.
- The owner signer and tenant merchant identity must be distinct for bootstrap bindings so removing the binding remains revocable even after the tenant owns a campaign.
- Existing direct campaign owners retain legacy-compatible access.
- The browser may name a tenant pubkey only to select an existing server-side link; it cannot create or infer a binding.
- Unauthorized tenant selection returns `404`.
- Default campaign starts `paused`.
- One deterministic default campaign per tenant; retries do not create another event or budget.
- Campaign, budget, and pending Nostr event persist in one transaction. Relay I/O occurs only after commit.
- `POST /campaigns` remains disabled by default.

## Task 1: Authorize a bound owner before a campaign exists

**Files:**
- Modify: `app/main.py::_grant_role_if_authorized`
- Test: `tests/test_accounts_ui.py`

1. Add a test with distinct generated owner and tenant pubkeys in `MERCHANT_ACCOUNT_BINDINGS`, with no campaigns.
2. Assert owner NIP-07 merchant login succeeds and materializes one `environment_binding` link.
3. Assert a self-binding does not bootstrap a campaignless merchant.
4. Run focused tests and observe RED because the current code requires an existing tenant campaign.
5. Remove only the campaign-existence prerequisite for valid distinct bindings.
6. Keep malformed optional-binding behavior and direct-owner compatibility unchanged.
7. Run focused tests GREEN.

## Task 2: Add idempotent session-scoped bootstrap endpoint

**Files:**
- Modify: `app/main.py` models/helpers/routes
- Test: `tests/test_accounts_ui.py`

**Route:** `POST /app/merchant/bootstrap`

**Request:**
```json
{"merchant_pubkey":"npub1..."}
```

**Response:**
```json
{
  "ok": true,
  "duplicate": false,
  "campaign_id": "camp_default_<tenant hex>",
  "status": "paused",
  "merchant_pubkey": "npub1...",
  "nostr_event_id": "...",
  "relay_results": []
}
```

1. Add RED tests for anonymous `401`, missing/foreign Origin `403`, foreign tenant `404`, and self-bound/ineligible tenant `404`.
2. Add RED idempotency test: two calls return same campaign ID; second says `duplicate=true`; database has one campaign, one campaign budget, and one campaign Nostr event.
3. Add test that campaign fields come from server-controlled defaults and status is `paused`.
4. Add request model with `extra='forbid'` and bounded pubkey.
5. Derive deterministic ID `camp_default_<tenant_hex>`.
6. Require merchant session and same-origin.
7. Normalize requested tenant and recheck current `environment_binding` link inside transaction.
8. On PostgreSQL acquire transaction advisory lock keyed by tenant; use the existing process lock as SQLite fallback.
9. If campaign exists, validate ownership and return duplicate without signing/publishing another event.
10. Otherwise build paused campaign/event, insert campaign, ensure budget, and persist event with empty relay results inside one transaction.
11. After commit call `finalize_committed_nostr_event`; return real relay statuses.
12. Run focused tests GREEN.

## Task 3: Add honest empty-state activation UI

**Files:**
- Modify: `app/main.py::merchant_account_page`
- Modify: `app/templates/merchant.html`
- Modify: `app/static/app.js`
- Test: `tests/test_accounts_ui.py`

1. Add RED HTML test: bound merchant with no campaign sees `Activate default Affiliate Program`, the bound tenant identity, and no active-program claim.
2. Add RED browser-contract test for form endpoint and same-origin JSON body.
3. Add bound tenant identities to template context from `merchant_account_links`.
4. Show bootstrap panel only when no campaigns exist and at least one eligible bound tenant exists.
5. Add form POST handler with accessible pending/success/error status; reload on success.
6. Keep operational sections and invitation control disabled until a program is active.
7. Run focused tests GREEN.

## Task 4: Revocation, transaction, and regression gates

**Files:**
- Test: `tests/test_accounts_ui.py`
- Modify production code only if a RED test proves a gap.

1. Test that removing `MERCHANT_ACCOUNT_BINDINGS` revokes the active owner session and deletes the environment link even after bootstrap.
2. Test tenant A cannot bootstrap tenant B.
3. Test disabled/failing relay publication leaves a durable campaign/event and retry is duplicate.
4. Preserve the fail-closed legacy mutation test.
5. Run `pytest -q tests/test_accounts_ui.py`.
6. Run full `pytest -q`, `python -m compileall -q app tests`, and `git diff --check`.
7. Request independent security/tenant/code review; fix all P0/P1 findings.

## Task 5: Delivery boundary

1. Update README with pilot binding format and separate owner/tenant invariant without any real keys or secrets.
2. Commit Issue #8 only after all gates pass.
3. Push and verify remote SHA.
4. Verify Railway `/health`, `/openapi.json` bootstrap route marker, anonymous `401`, and legacy `/campaigns` fail-closed using GET/read-only or rejection-only probes.
5. Do not configure José's binding until his public `npub` and intended tenant public identity are confirmed.
6. Move #8 to Verify/Done only after deployment; then start #9.
