import base64
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from nostr_sdk import EventBuilder, Keys, Kind, Tag
from sqlalchemy import text


from app import main
from app.workspaces import affiliate_workspace_data, merchant_workspace_data


def configured_client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/accounts.db")
    monkeypatch.setenv("NOSTR_PUBLISH", "false")
    monkeypatch.setenv("APP_SECRET", "test-app-secret-with-enough-entropy")
    monkeypatch.setenv("PAYOUT_ADMIN_KEY", "test-admin-key")
    monkeypatch.setattr(main, "BASE_URL", "https://testserver")
    main._ENGINE = None
    main._ENGINE_URL = None
    main._INVOICE_PREPARE_LAST.clear()
    main._INVOICE_PREPARE_ACTIVE.clear()
    return TestClient(main.app, base_url="https://testserver")


def signed_login_event(keys: Keys, challenge: dict, *, role: str | None = None, relay: str | None = None) -> dict:
    event = (
        EventBuilder(Kind(challenge.get("kind", main.AUTH_EVENT_KIND)), "")
        .tags(
            [
                Tag.parse(["challenge", challenge["challenge"]]),
                Tag.parse(["relay", relay or challenge["relay"]]),
                Tag.parse(["role", role or challenge["role"]]),
            ]
        )
        .sign_with_keys(keys)
    )
    return json.loads(event.as_json())


def create_campaign(client: TestClient, merchant: Keys, *, name: str = "Merchant campaign", enrollment_mode: str = "private") -> dict:
    response = client.post(
        "/campaigns",
        json={
            "merchant_pubkey": merchant.public_key().to_bech32(),
            "name": name,
            "commission_bps": 800,
            "attribution_window_days": 30,
            "destination_url": "https://merchant.example/shop",
            "enrollment_mode": enrollment_mode,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def create_enrollment(client: TestClient, campaign_id: str, affiliate: Keys) -> dict:
    enrollment = main._create_enrollment_record(
        main.EnrollmentIn(
            campaign_id=campaign_id,
            affiliate_pubkey=affiliate.public_key().to_bech32(),
            lightning_address="affiliate@example.com",
        )
    )
    # Most suite fixtures model enrollments created before Affiliate onboarding.
    with main.engine().begin() as connection:
        connection.execute(
            text("UPDATE enrollments SET destination_verified_at='legacy' WHERE id=:id"),
            {"id": enrollment["enrollment_id"]},
        )
    return enrollment


def seed_verified_affiliate_profile(affiliate: Keys, address: str = "affiliate@wallet.example") -> None:
    timestamp = main.now()
    with main.engine().begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO affiliate_profiles
                  (affiliate_pubkey_hex, affiliate_pubkey, lightning_address, verified_at, created_at, updated_at)
                VALUES (:hex, :npub, :address, :timestamp, :timestamp, :timestamp)
                """
            ),
            {
                "hex": affiliate.public_key().to_hex(),
                "npub": affiliate.public_key().to_bech32(),
                "address": address,
                "timestamp": timestamp,
            },
        )


def login(client: TestClient, keys: Keys, role: str):
    challenge = client.post("/auth/nostr/challenge", json={"role": role})
    assert challenge.status_code == 200, challenge.text
    event = signed_login_event(keys, challenge.json())
    verify = client.post("/auth/nostr/verify", json={"event": event})
    assert verify.status_code == 200, verify.text
    return verify


def seed_payable_payout(campaign: dict, enrollment: dict, affiliate: Keys, *, amount_sats: int = 210) -> str:
    payout_id = f"pay_manual_{main.hid('test')}"
    conversion_id = f"conv_manual_{main.hid('test')}"
    click_id = f"clk_manual_{main.hid('test')}"
    timestamp = main.now()
    with main.engine().begin() as connection:
        connection.execute(
            text("INSERT INTO clicks (id, ref_code, campaign_id, affiliate_pubkey, created_at) VALUES (:id, :ref, :campaign, :affiliate, :created_at)"),
            {"id": click_id, "ref": enrollment["ref_code"], "campaign": campaign["campaign_id"], "affiliate": affiliate.public_key().to_bech32(), "created_at": timestamp},
        )
        connection.execute(
            text(
                """
                INSERT INTO conversions
                  (id, order_id_hash, click_id, campaign_id, affiliate_pubkey, order_total,
                   currency, commission_sats, status, nostr_event_id, nostr_event_json, created_at)
                VALUES (:id, :order_hash, :click_id, :campaign_id, :affiliate, 1, 'USD',
                        :amount, 'approved', :event_id, '{}', :created_at)
                """
            ),
            {"id": conversion_id, "order_hash": f"order_{conversion_id}", "click_id": click_id, "campaign_id": campaign["campaign_id"], "affiliate": affiliate.public_key().to_bech32(), "amount": amount_sats, "event_id": f"event_{conversion_id}", "created_at": timestamp},
        )
        connection.execute(
            text(
                """
                INSERT INTO payouts
                  (id, conversion_id, affiliate_pubkey, amount_sats, lightning_address, status,
                   state, fee_sats, fee_state, reserved_sats, return_window_ends_at, created_at)
                VALUES (:id, :conversion_id, :affiliate, :amount, :address, 'pending',
                        'PAYABLE', 0, 'FEE_PENDING', :amount, :return_window, :created_at)
                """
            ),
            {"id": payout_id, "conversion_id": conversion_id, "affiliate": affiliate.public_key().to_bech32(), "amount": amount_sats, "address": "old@example.com", "return_window": "2020-01-01T00:00:00+00:00", "created_at": timestamp},
        )
        assert main.reserve_campaign_budget(connection, campaign["campaign_id"], payout_id, amount_sats)
    return payout_id


def test_login_frontend_preserves_role_card_markup_and_formats_structured_signer_errors(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)

    script = client.get("/static/app.js")
    login_page = client.get("/app?role=merchant")

    assert script.status_code == 200
    assert '/static/app.js?v=20260730-authux-1' in login_page.text
    assert "function readableError(error" in script.text
    assert 'value === "[object Object]"' in script.text
    assert "new Error(readableError(data.detail" in script.text
    assert "status.textContent = readableError(error)" in script.text
    assert "button.textContent = previous" not in script.text


def test_salvia_homepage_is_the_public_root(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)

    homepage = client.get("/", follow_redirects=False)
    stylesheet = client.get("/static/home.css")
    script = client.get("/static/home.js")
    arena_wordmark = client.get("/static/brand/wordmark-arena.png")
    space_grotesk = client.get("/static/fonts/space-grotesk-latin.woff2")
    dm_mono_regular = client.get("/static/fonts/dm-mono-regular-latin.woff2")
    dm_mono_medium = client.get("/static/fonts/dm-mono-medium-latin.woff2")

    assert homepage.status_code == 200
    assert "Cada venta deja una" in homepage.text
    assert "Every sale leaves a" in homepage.text
    assert 'href="/app?role=merchant"' in homepage.text
    assert 'href="/app?role=affiliate"' in homepage.text
    assert 'href="/static/home.css?v=20260728-salvia-home2"' in homepage.text
    assert 'src="/static/home.js?v=20260728-salvia-home2"' in homepage.text
    assert 'href="/static/fonts/space-grotesk-latin.woff2"' in homepage.text
    assert "fonts.googleapis.com" not in homepage.text
    assert "Concept preview" not in homepage.text
    assert "not production" not in homepage.text
    assert "atribución pública" not in homepage.text
    assert 'role="group"' in homepage.text
    assert "example-event-2802" in homepage.text
    assert stylesheet.status_code == 200
    assert "--night: #141914" in stylesheet.text
    assert "--sage: #9cc97e" in stylesheet.text
    assert "--sage-ink: #3f6b2d" in stylesheet.text
    assert '@font-face' in stylesheet.text
    assert 'font-family:"Space Grotesk"' in stylesheet.text
    assert script.status_code == 200
    assert "data-language" in script.text
    assert "data-label-es" in script.text
    assert arena_wordmark.status_code == 200
    assert space_grotesk.status_code == 200
    assert dm_mono_regular.status_code == 200
    assert dm_mono_medium.status_code == 200
    assert space_grotesk.content.startswith(b"wOF2")


def test_www_meerat_redirects_to_canonical_apex_and_preserves_path_and_query(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)

    response = client.get(
        "/app?role=affiliate",
        headers={"host": "www.meerat.com"},
        follow_redirects=False,
    )

    assert response.status_code == 308
    assert response.headers["location"] == "https://meerat.com/app?role=affiliate"

    apex = client.get("/health", headers={"host": "meerat.com"}, follow_redirects=False)
    assert apex.status_code == 200


def test_mrt_short_domain_redirects_root_slug_and_reserved_paths(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)

    root = client.get("/?utm_source=launch", headers={"host": "mrt.st"}, follow_redirects=False)
    short = client.get("/ref_ABC-123?utm_source=nostr", headers={"host": "mrt.st"}, follow_redirects=False)
    reserved = client.get("/app?role=affiliate", headers={"host": "mrt.st"}, follow_redirects=False)
    unrelated = client.get("/ref_ABC-123", headers={"host": "testserver"}, follow_redirects=False)

    assert root.status_code == 308
    assert root.headers["location"] == "https://testserver/?utm_source=launch"
    assert short.status_code == 302
    assert short.headers["location"] == "https://testserver/r/ref_ABC-123?utm_source=nostr"
    assert reserved.status_code == 308
    assert reserved.headers["location"] == "https://testserver/app?role=affiliate"
    assert unrelated.status_code == 404


def test_salvia_concept_brand_contract_is_served(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)

    login_page = client.get("/app?role=merchant")
    stylesheet = client.get("/static/app.css")
    wordmark = client.get("/static/brand/wordmark-night.png")
    favicon = client.get("/static/brand/favicon.svg")

    assert login_page.status_code == 200
    assert '/static/app.css?v=20260730-authux-1' in login_page.text
    assert '/static/brand/wordmark-night.png' in login_page.text
    assert stylesheet.status_code == 200
    assert "--sage: #9cc97e" in stylesheet.text.lower()
    assert '"Space Grotesk"' in stylesheet.text
    assert wordmark.status_code == 200
    assert wordmark.headers["content-type"] == "image/png"
    assert favicon.status_code == 200


def test_login_frontend_rejects_empty_or_invalid_signer_responses_before_verify(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)

    script = client.get("/static/app.js")
    login_page = client.get("/app?role=merchant")

    assert script.status_code == 200
    assert "function requireSignedNostrEvent(result)" in script.text
    assert 'typeof result === "string"' in script.text
    assert 'result = result.event' in script.text
    assert 'const required = ["id", "pubkey", "sig", "kind", "created_at", "tags", "content"]' in script.text
    assert "NostrKey no devolvió un evento firmado" in script.text
    assert "signWithNostr(unsignedEvent" in script.text
    assert 'JSON.stringify({event})' in script.text
    assert '/static/app.js?v=20260730-authux-1' in login_page.text


def test_nip46_mobile_signer_assets_are_self_hosted_and_secret_safe(tmp_path, monkeypatch):
    monkeypatch.setenv("NIP46_RELAYS", "wss://relay.primal.net, wss://nos.lol")
    client = configured_client(tmp_path, monkeypatch)

    login_page = client.get("/app?role=affiliate")
    invite_page = client.get("/invite")
    connect_script = client.get("/static/nostr-connect.js")
    nip46_bundle = client.get("/static/vendor/nostr-tools-nip46-2.24.1.mjs")
    pure_bundle = client.get("/static/vendor/nostr-tools-pure-2.24.1.mjs")
    qr_script = client.get("/static/vendor/qrcode-generator-1.4.4.js")

    assert login_page.status_code == 200
    assert 'data-login-method="auto"' not in login_page.text
    assert login_page.text.count('data-login-method="nip46"') == 1
    assert "Continuar con una app Nostr" in login_page.text
    assert "Usar otra app Nostr o QR" not in login_page.text
    assert 'id="nostr-connect-config"' in login_page.text
    assert '"wss://relay.primal.net"' in login_page.text
    assert '"wss://nos.lol"' in login_page.text
    assert '/static/nostr-connect.js?v=20260730-authux-1' in login_page.text
    assert '/static/vendor/qrcode-generator-1.4.4.js' in login_page.text
    assert invite_page.status_code == 200
    assert 'id="nostr-connect-config"' in invite_page.text
    assert '/static/nostr-connect.js?v=20260803-invite-unified1' in invite_page.text
    assert connect_script.status_code == nip46_bundle.status_code == pure_bundle.status_code == qr_script.status_code == 200
    assert "createNostrConnectURI" in connect_script.text
    assert "BunkerSigner.fromURI" in connect_script.text
    assert "generateSecretKey" in connect_script.text
    assert 'sign_event:${unsignedEvent.kind}' in connect_script.text
    assert "sign_event:22242" not in connect_script.text
    assert "kind: affiliateInvitationEventKind" in client.get("/static/app.js").text
    assert "window.history.state?.inviteToken" in client.get("/static/app.js").text
    assert "const eventToSign = {...unsignedEvent, created_at: Math.floor(Date.now() / 1000)}" in connect_script.text
    assert "identityMismatch" in connect_script.text
    assert "signingFailed" in connect_script.text
    assert "shareIdentityFailed" in connect_script.text
    assert "raceWithAbort" in connect_script.text
    assert "await raceWithAbort(signer.getPublicKey()" in connect_script.text
    assert "await raceWithAbort(signer.signEvent(eventToSign)" in connect_script.text
    assert "if (activeAttempt === attempt) {\n      activeAttempt = null;\n      clearDialog();" in connect_script.text
    assert "localStorage" not in connect_script.text
    assert "sessionStorage" not in connect_script.text
    assert "console.log" not in connect_script.text
    assert len(nip46_bundle.content) > 50_000
    assert len(pure_bundle.content) > 20_000
    assert len(qr_script.content) > 40_000


def test_nip46_relay_configuration_rejects_unsafe_urls(monkeypatch):
    monkeypatch.setenv(
        "NIP46_RELAYS",
        "wss://[::1,wss://relay.primal.net,https://not-a-websocket.example,wss://relay.primal.net,"
        "ws://insecure.example,wss://relay.example:99999,wss://relay.example/#fragment",
    )

    assert main.nip46_relays() == ["wss://relay.primal.net"]


def test_nostr_login_issues_http_only_session_and_logout(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    create_campaign(client, merchant)

    result = login(client, merchant, "merchant")
    payload = result.json()
    assert payload["account"]["role"] == "merchant"
    assert payload["account"]["npub"] == merchant.public_key().to_bech32()
    set_cookie = result.headers["set-cookie"].lower()
    assert main.SESSION_COOKIE.lower() in set_cookie
    assert "httponly" in set_cookie
    assert "secure" in set_cookie
    assert "samesite=lax" in set_cookie
    assert "token" not in json.dumps(payload).lower()

    me = client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["authenticated"] is True
    assert me.json()["account"]["role"] == "merchant"

    logout = client.post("/auth/logout")
    assert logout.status_code == 200
    assert client.get("/auth/me").status_code == 401


def test_direct_merchant_owner_ignores_malformed_optional_bindings(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    create_campaign(client, merchant)
    monkeypatch.setenv("MERCHANT_ACCOUNT_BINDINGS", "malformed-binding")

    result = login(client, merchant, "merchant")

    assert result.json()["account"]["npub"] == merchant.public_key().to_bech32()
    assert client.get("/app/merchant").status_code == 200


def test_malformed_bindings_remove_stale_delegation_for_direct_owner(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    direct_owner = Keys.generate()
    delegated_tenant = Keys.generate()
    create_campaign(client, direct_owner, name="Direct owner campaign")
    create_campaign(client, delegated_tenant, name="Delegated tenant campaign")
    monkeypatch.setenv(
        "MERCHANT_ACCOUNT_BINDINGS",
        f"{direct_owner.public_key().to_bech32()}:{delegated_tenant.public_key().to_bech32()}",
    )
    login(client, direct_owner, "merchant")
    assert "Delegated tenant campaign" in client.get("/app/merchant?view=campaigns").text

    monkeypatch.setenv("MERCHANT_ACCOUNT_BINDINGS", "malformed-binding")
    page = client.get("/app/merchant?view=campaigns")

    assert page.status_code == 200
    assert "Direct owner campaign" in page.text
    assert "Delegated tenant campaign" not in page.text
    with main.engine().connect() as connection:
        assert connection.execute(
            text("SELECT COUNT(*) FROM merchant_account_links WHERE source='environment_binding'")
        ).scalar_one() == 0


def test_malformed_bindings_do_not_authorize_unrelated_identity(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    stranger = Keys.generate()
    monkeypatch.setenv("MERCHANT_ACCOUNT_BINDINGS", "malformed-binding")
    challenge = client.post("/auth/nostr/challenge", json={"role": "merchant"}).json()

    response = client.post(
        "/auth/nostr/verify",
        json={"event": signed_login_event(stranger, challenge)},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "La configuración de vinculación de la cuenta del comerciante no es válida."
    assert client.get("/auth/me").status_code == 401


def test_challenge_is_one_use_and_role_origin_are_bound(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    create_campaign(client, merchant)

    challenge = client.post("/auth/nostr/challenge", json={"role": "merchant"}).json()
    assert challenge["kind"] == 27236
    event = signed_login_event(merchant, challenge)
    first = client.post("/auth/nostr/verify", json={"event": event})
    assert first.status_code == 200
    replay = client.post("/auth/nostr/verify", json={"event": event})
    assert replay.status_code == 409

    wrong_role_challenge = client.post("/auth/nostr/challenge", json={"role": "merchant"}).json()
    wrong_role = client.post(
        "/auth/nostr/verify",
        json={"event": signed_login_event(merchant, wrong_role_challenge, role="affiliate")},
    )
    assert wrong_role.status_code == 400

    wrong_origin_challenge = client.post("/auth/nostr/challenge", json={"role": "merchant"}).json()
    wrong_origin = client.post(
        "/auth/nostr/verify",
        json={"event": signed_login_event(merchant, wrong_origin_challenge, relay="https://evil.example")},
    )
    assert wrong_origin.status_code == 400


def test_auth_challenge_rate_limit_is_partitioned_by_client_ip(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    for _ in range(20):
        response = client.post(
            "/auth/nostr/challenge",
            headers={"x-forwarded-for": "198.51.100.10"},
            json={"role": "affiliate"},
        )
        assert response.status_code == 200
    limited = client.post(
        "/auth/nostr/challenge",
        headers={"x-forwarded-for": "198.51.100.10"},
        json={"role": "affiliate"},
    )
    assert limited.status_code == 429
    other_client = client.post(
        "/auth/nostr/challenge",
        headers={"x-forwarded-for": "198.51.100.11"},
        json={"role": "affiliate"},
    )
    assert other_client.status_code == 200


def test_role_requires_server_side_evidence(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    stranger = Keys.generate()
    challenge = client.post("/auth/nostr/challenge", json={"role": "merchant"}).json()
    response = client.post(
        "/auth/nostr/verify",
        json={"event": signed_login_event(stranger, challenge)},
    )
    assert response.status_code == 403
    assert client.get("/auth/me").status_code == 401


def test_merchant_and_affiliate_workspaces_are_role_scoped(tmp_path, monkeypatch):
    merchant_client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    affiliate = Keys.generate()
    campaign = create_campaign(merchant_client, merchant, name="Private merchant campaign")
    create_enrollment(merchant_client, campaign["campaign_id"], affiliate)

    login(merchant_client, merchant, "merchant")
    merchant_page = merchant_client.get("/app/merchant?view=campaigns")
    assert merchant_page.status_code == 200
    assert "Cuenta de comerciante" in merchant_page.text
    assert "Merchant account" not in merchant_page.text
    assert "Private merchant campaign" in merchant_page.text
    assert "affiliate@example.com" not in merchant_page.text
    assert merchant_client.get("/app/affiliate", follow_redirects=False).status_code in {302, 303, 307}

    affiliate_client = TestClient(main.app, base_url="https://testserver")
    seed_verified_affiliate_profile(affiliate)
    login(affiliate_client, affiliate, "affiliate")
    affiliate_page = affiliate_client.get("/app/affiliate?view=links")
    assert affiliate_page.status_code == 200
    assert "Cuenta de afiliado" in affiliate_page.text
    assert "Affiliate account" not in affiliate_page.text
    assert "Private merchant campaign" in affiliate_page.text
    assert "https://mrt.st/" in affiliate_page.text
    assert affiliate_client.get("/app/merchant", follow_redirects=False).status_code in {302, 303, 307}


def test_campaign_archive_hides_workspace_and_preserves_public_history(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    affiliate = Keys.generate()
    keep = create_campaign(client, merchant, name="Lightning Koffee Affiliate Program")
    canary = create_campaign(client, merchant, name="Meerat NWC Canary 21 sats")
    enrollment = create_enrollment(client, canary["campaign_id"], affiliate)
    login(client, merchant, "merchant")

    assert main.archive_campaign_preserving_history(
        canary["campaign_id"],
        expected_merchant_hex=merchant.public_key().to_hex(),
        expected_name="Meerat NWC Canary 21 sats",
    ) is True
    assert main.archive_campaign_preserving_history(
        canary["campaign_id"],
        expected_merchant_hex=merchant.public_key().to_hex(),
        expected_name="Meerat NWC Canary 21 sats",
    ) is False

    merchant_page = client.get("/app/merchant?view=campaigns")
    assert merchant_page.status_code == 200
    assert "Lightning Koffee Affiliate Program" in merchant_page.text
    assert "Meerat NWC Canary 21 sats" not in merchant_page.text

    public_summary = client.get(f"/campaigns/{canary['campaign_id']}/summary")
    assert public_summary.status_code == 200
    payload = public_summary.json()
    assert payload["campaign"]["status"] == "ended"
    assert payload["campaign"]["archived_at"]
    assert payload["totals"]["enrollments"] == 1
    assert payload["enrollments"][0]["id"] == enrollment["enrollment_id"]
    assert next(tag[1] for tag in payload["campaign"]["nostr_event"]["tags"] if tag[0] == "status") == "ended"

    with main.engine().connect() as connection:
        campaign_events = connection.execute(
            text("SELECT COUNT(*) FROM nostr_events WHERE entity_type='campaign' AND entity_id=:id"),
            {"id": canary["campaign_id"]},
        ).scalar_one()
        preserved_enrollment = connection.execute(
            text("SELECT COUNT(*) FROM enrollments WHERE id=:id"),
            {"id": enrollment["enrollment_id"]},
        ).scalar_one()
    assert campaign_events == 2
    assert preserved_enrollment == 1
    assert keep["campaign_id"] != canary["campaign_id"]


def test_ops_is_allowlisted_and_dashboard_redirects(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    operator = Keys.generate()
    monkeypatch.setenv("OPS_NOSTR_PUBKEYS", operator.public_key().to_bech32())

    anonymous_ops = client.get("/ops", follow_redirects=False)
    assert anonymous_ops.status_code in {302, 303, 307}
    dashboard = client.get("/dashboard", follow_redirects=False)
    assert dashboard.status_code in {302, 303, 307}
    assert dashboard.headers["location"] == "/ops"

    login(client, operator, "ops")
    ops = client.get("/ops")
    assert ops.status_code == 200
    assert "Estado de la red" in ops.text
    assert "Observabilidad" in ops.text
    assert "Conversiones recientes" in ops.text
    assert "Nostr &amp; relays" in ops.text
    assert "data-ops-health" in ops.text
    assert "data-ops-metric" in ops.text
    assert "Nostr Affiliate POC Dashboard" not in ops.text
    assert "Run full demo" not in ops.text
    assert "onclick=" not in ops.text
    assert ops.headers["cache-control"] == "no-store"
    ops_data = client.get("/ops/data")
    assert ops_data.status_code == 200
    assert "counts" in ops_data.json()
    assert "attention" in ops_data.json()
    assert "snapshot_at" in ops_data.json()
    assert ops_data.headers["cache-control"] == "no-store"

    client.post("/auth/logout")
    assert client.get("/ops/data").status_code == 401


def create_invitation(client: TestClient, campaign_id: str) -> dict:
    response = client.post(
        "/app/merchant/invitations",
        headers={"origin": "https://testserver"},
        json={"campaign_id": campaign_id},
    )
    assert response.status_code == 200, response.text
    return response.json()


def invitation_acceptance_event(keys: Keys, token: str) -> dict:
    return signed_login_event(
        keys,
        {"challenge": token, "relay": "https://testserver", "role": "affiliate_invite"},
    )


def test_merchant_creates_hashed_single_use_invitation_for_owned_campaign(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    campaign = create_campaign(client, merchant, name="Invite-only campaign")
    login(client, merchant, "merchant")

    merchant_page = client.get("/app/merchant?view=affiliates")
    assert merchant_page.status_code == 200
    assert '/static/app.js?v=20260803-commission1' in merchant_page.text
    assert 'data-invite-origin="https://mrt.st"' in merchant_page.text

    invitation = create_invitation(client, campaign["campaign_id"])

    assert invitation["invite_url"].startswith("https://mrt.st/invite#token=")
    assert invitation["status"] == "pending"
    token = invitation["invite_url"].split("#token=", 1)[1]
    assert token not in invitation["invitation_id"]
    with main.engine().connect() as connection:
        row = connection.execute(
            text("SELECT * FROM affiliate_invitations WHERE id=:id"),
            {"id": invitation["invitation_id"]},
        ).one()._mapping
    assert row["token_hash"] == main.auth_digest(token)
    assert token not in row["token_hash"]
    assert row["campaign_id"] == campaign["campaign_id"]


def test_mrt_short_domain_canonicalizes_invite_path_without_exposing_token(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)

    response = client.get(
        "/invite",
        headers={"host": "mrt.st"},
        follow_redirects=False,
    )

    assert response.status_code == 308
    assert response.headers["location"] == "https://testserver/invite"


def test_invitation_resolve_derives_clean_merchant_name_from_campaign_fallback(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    campaign = create_campaign(client, merchant, name="Lightning Koffee Affiliate Program")
    login(client, merchant, "merchant")
    invitation = create_invitation(client, campaign["campaign_id"])
    token = invitation["invite_url"].split("#token=", 1)[1]

    resolved = client.post(
        "/invite/resolve",
        headers={"origin": "https://testserver"},
        json={"token": token},
    )
    assert resolved.status_code == 200, resolved.text
    payload = resolved.json()
    assert payload["auth_event_kind"] == 27236
    assert payload["merchant"]["display_name"] == "Lightning Koffee"
    assert payload["merchant"]["initials"] == "LK"
    assert payload["campaign"]["invite_headline"] == "Recomendá Lightning Koffee. Ganá sats."


def test_invitation_resolve_uses_structured_merchant_brand_and_campaign_copy(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    campaign = create_campaign(client, merchant, name="Lightning Koffee Affiliate Program")
    login(client, merchant, "merchant")

    profile = client.put(
        "/app/merchant/profile",
        headers={"origin": "https://testserver"},
        json={
            "merchant_pubkey": merchant.public_key().to_bech32(),
            "display_name": "Lightning Koffee",
            "tagline": "Café, Bitcoin y comunidad",
            "logo_url": "https://lightningkoffee.io/logo.png",
        },
    )
    assert profile.status_code == 200, profile.text
    branding = client.put(
        "/app/merchant/campaign-invite",
        headers={"origin": "https://testserver"},
        json={
            "campaign_id": campaign["campaign_id"],
            "invite_eyebrow": "Programa de afiliados · Value for value",
            "invite_headline": "Recomendá café. Ganá sats.",
            "invite_description": "Compartí Lightning Koffee con tu comunidad y recibí sats cuando tu recomendación termina en una compra.",
        },
    )
    assert branding.status_code == 200, branding.text
    invitation = create_invitation(client, campaign["campaign_id"])
    token = invitation["invite_url"].split("#token=", 1)[1]

    resolved = client.post(
        "/invite/resolve",
        headers={"origin": "https://testserver"},
        json={"token": token},
    )
    assert resolved.status_code == 200, resolved.text
    payload = resolved.json()
    assert payload["merchant"] == {
        "display_name": "Lightning Koffee",
        "tagline": "Café, Bitcoin y comunidad",
        "tagline_es": "Café, Bitcoin y comunidad",
        "tagline_en": "Café, Bitcoin y comunidad",
        "logo_url": "https://lightningkoffee.io/logo.png",
        "initials": "LK",
    }
    assert payload["campaign"]["name"] == "Lightning Koffee Affiliate Program"
    assert payload["campaign"]["commission_percent"] == "8"
    assert payload["campaign"]["window_days"] == 30
    assert "terms_url" in payload["campaign"]
    assert payload["campaign"]["invite_eyebrow"] == "Programa de afiliados · Value for value"
    assert payload["campaign"]["invite_headline"] == "Recomendá café. Ganá sats."
    assert payload["campaign"]["invite_headline_es"] == "Recomendá café. Ganá sats."
    assert payload["campaign"]["invite_headline_en"] == "Recomendá café. Ganá sats."
    assert payload["campaign"]["invite_description"].startswith("Compartí Lightning Koffee")


def test_affiliate_accepts_invitation_with_nip07_and_gets_session(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    affiliate = Keys.generate()
    campaign = create_campaign(client, merchant, name="Signed invitation campaign")
    login(client, merchant, "merchant")
    invitation = create_invitation(client, campaign["campaign_id"])
    token = invitation["invite_url"].split("#token=", 1)[1]
    client.post("/auth/logout")

    page = client.get("/invite")
    assert page.status_code == 200
    assert "data-affiliate-lander" in page.text
    assert 'data-invite-language="es"' in page.text
    assert 'data-invite-language="en"' in page.text
    assert page.text.count("data-invite-accept") == 2
    assert 'data-sign-method="nip46"' in page.text
    assert "Ventana de atribución" in page.text
    assert "Attribution window" in page.text
    assert "Crear mi link de afiliado" in page.text
    assert "Create my affiliate link" in page.text
    assert token not in page.text
    assert "Meerat" not in page.text
    assert page.text.count("data-invite-window") == 2
    assert "data-invite-merchant-name" in page.text
    assert "Crear mi link de afiliado" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["referrer-policy"] == "no-referrer"
    resolved = client.post(
        "/invite/resolve",
        headers={"origin": "https://testserver"},
        json={"token": token},
    )
    assert resolved.status_code == 200
    assert resolved.json()["campaign_name"] == "Signed invitation campaign"
    accepted = client.post(
        "/invite/accept",
        headers={"origin": "https://testserver"},
        json={"token": token, "event": invitation_acceptance_event(affiliate, token)},
    )

    assert accepted.status_code == 200, accepted.text
    payload = accepted.json()
    assert payload["affiliate_pubkey"] == affiliate.public_key().to_bech32()
    assert payload["redirect"] == "/app/affiliate?view=links"
    assert payload["ref_url"].startswith("https://mrt.st/")
    assert main.SESSION_COOKIE.lower() in accepted.headers["set-cookie"].lower()
    onboarding = client.get("/app/affiliate")
    assert onboarding.status_code == 200
    assert "Antes de compartir, asegurá cómo cobrar" in onboarding.text
    ref_code = payload["ref_url"].rsplit("/", 1)[-1]
    blocked_referral = client.get(f"/r/{ref_code}", follow_redirects=False)
    assert blocked_referral.status_code == 409
    assert blocked_referral.json()["detail"] == "El destino de cobro del afiliado no está configurado o verificado."
    monkeypatch.setattr(main, "validate_lightning_address", lambda _address: {"tag": "payRequest"})
    destination = client.put(
        "/app/affiliate/lightning-address",
        headers={"origin": "https://testserver"},
        json={"lightning_address": "affiliate@wallet.example"},
    )
    assert destination.status_code == 200
    payable_referral = client.get(f"/r/{ref_code}", follow_redirects=False)
    assert payable_referral.status_code in {302, 303, 307}
    workspace = client.get("/app/affiliate?view=links")
    assert workspace.status_code == 200
    assert "Tu enlace para compartir" in workspace.text
    assert "Signed invitation campaign" in workspace.text
    assert main.workspace_short(merchant.public_key().to_bech32()) in workspace.text
    assert payload["ref_url"] in workspace.text
    assert "8% · ventana 30 días" in workspace.text
    assert "Listo para compartir" in workspace.text
    assert "Copiar" in workspace.text
    assert "Abrir visita de prueba" in workspace.text

    replay = client.post(
        "/invite/accept",
        headers={"origin": "https://testserver"},
        json={"token": token, "event": invitation_acceptance_event(affiliate, token)},
    )
    assert replay.status_code == 200
    assert replay.json()["recovered"] is True
    assert replay.json()["enrollment_id"] == payload["enrollment_id"]

    other_affiliate = Keys.generate()
    stolen_replay = client.post(
        "/invite/accept",
        headers={"origin": "https://testserver"},
        json={"token": token, "event": invitation_acceptance_event(other_affiliate, token)},
    )
    assert stolen_replay.status_code == 409


def test_paused_campaign_link_is_visible_but_not_actionable(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    affiliate = Keys.generate()
    campaign = create_campaign(client, merchant, name="Paused pilot program")
    login(client, merchant, "merchant")
    invitation = create_invitation(client, campaign["campaign_id"])
    token = invitation["invite_url"].split("#token=", 1)[1]
    client.post("/auth/logout")
    accepted = client.post(
        "/invite/accept",
        headers={"origin": "https://testserver"},
        json={"token": token, "event": invitation_acceptance_event(affiliate, token)},
    )
    assert accepted.status_code == 200
    with main.engine().begin() as connection:
        connection.execute(
            text("UPDATE campaigns SET status='paused' WHERE id=:campaign_id"),
            {"campaign_id": campaign["campaign_id"]},
        )
    session = {
        "npub": affiliate.public_key().to_bech32(),
        "nostr_pubkey_hex": affiliate.public_key().to_hex(),
    }
    with main.engine().connect() as connection:
        data = affiliate_workspace_data(connection, session, base_url="https://testserver")
    assert data["totals"]["active_links"] == 0
    assert data["links"][0]["campaign_status"] == "paused"
    assert data["links"][0]["available"] is False

    seed_verified_affiliate_profile(affiliate)
    workspace = client.get("/app/affiliate?view=links")
    assert workspace.status_code == 200
    assert accepted.json()["ref_url"] in workspace.text
    assert "Programa pausado" in workspace.text
    assert "Esperá a que el comerciante active el programa" in workspace.text
    assert "Abrir visita de prueba" not in workspace.text


def test_inactive_account_cannot_consume_invitation(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    affiliate = Keys.generate()
    campaign = create_campaign(client, merchant)
    login(client, merchant, "merchant")
    invitation = create_invitation(client, campaign["campaign_id"])
    token = invitation["invite_url"].split("#token=", 1)[1]
    client.post("/auth/logout")
    timestamp = main.now()
    with main.engine().begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO accounts (id, nostr_pubkey_hex, npub, status, created_at, updated_at, last_login_at)
                VALUES (:id, :hex, :npub, 'suspended', :now, :now, :now)
                """
            ),
            {
                "id": "acct_suspended_test",
                "hex": affiliate.public_key().to_hex(),
                "npub": affiliate.public_key().to_bech32(),
                "now": timestamp,
            },
        )

    denied = client.post(
        "/invite/accept",
        headers={"origin": "https://testserver"},
        json={"token": token, "event": invitation_acceptance_event(affiliate, token)},
    )
    assert denied.status_code == 403
    with main.engine().connect() as connection:
        invite_status = connection.execute(
            text("SELECT status FROM affiliate_invitations WHERE id=:id"),
            {"id": invitation["invitation_id"]},
        ).scalar_one()
        enrollments = connection.execute(
            text("SELECT COUNT(*) FROM enrollments WHERE campaign_id=:campaign_id"),
            {"campaign_id": campaign["campaign_id"]},
        ).scalar_one()
    assert invite_status == "pending"
    assert enrollments == 0


def test_invitation_rejects_wrong_origin_expiry_and_cross_tenant_campaign(tmp_path, monkeypatch):
    merchant_client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    other_merchant = Keys.generate()
    affiliate = Keys.generate()
    campaign = create_campaign(merchant_client, merchant)
    other_campaign = create_campaign(merchant_client, other_merchant)
    login(merchant_client, merchant, "merchant")

    cross_tenant = merchant_client.post(
        "/app/merchant/invitations",
        headers={"origin": "https://testserver"},
        json={"campaign_id": other_campaign["campaign_id"]},
    )
    assert cross_tenant.status_code == 404
    invitation = create_invitation(merchant_client, campaign["campaign_id"])
    token = invitation["invite_url"].split("#token=", 1)[1]
    merchant_client.post("/auth/logout")

    wrong_origin = merchant_client.post(
        "/invite/accept",
        headers={"origin": "https://evil.example"},
        json={"token": token, "event": invitation_acceptance_event(affiliate, token)},
    )
    assert wrong_origin.status_code == 403
    with main.engine().begin() as connection:
        connection.execute(
            text("UPDATE affiliate_invitations SET expires_at=:expired WHERE token_hash=:token_hash"),
            {"expired": "2020-01-01T00:00:00+00:00", "token_hash": main.auth_digest(token)},
        )
    expired = merchant_client.post(
        "/invite/accept",
        headers={"origin": "https://testserver"},
        json={"token": token, "event": invitation_acceptance_event(affiliate, token)},
    )
    assert expired.status_code == 410


def test_human_owner_can_be_bound_to_merchant_identity(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant_identity = Keys.generate()
    human_owner = Keys.generate()
    campaign = create_campaign(client, merchant_identity, name="Bound merchant campaign")
    monkeypatch.setenv(
        "MERCHANT_ACCOUNT_BINDINGS",
        f"{human_owner.public_key().to_bech32()}:{merchant_identity.public_key().to_bech32()}",
    )

    login(client, human_owner, "merchant")
    page = client.get("/app/merchant?view=affiliates")
    assert page.status_code == 200
    assert "Bound merchant campaign" in page.text
    assert 'data-merchant-invitation' in page.text
    assert 'name="affiliate_pubkey"' not in page.text
    assert 'value="' + campaign["campaign_id"] + '"' in page.text


def test_merchant_workspace_separates_operational_views_and_settings(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    create_campaign(client, merchant, name="Navigation campaign")
    login(client, merchant, "merchant")

    overview = client.get("/app/merchant")
    assert overview.status_code == 200
    assert "Tu programa, bajo control." in overview.text
    assert 'data-merchant-profile' not in overview.text
    assert 'data-merchant-invitation' not in overview.text

    campaigns = client.get("/app/merchant?view=campaigns")
    assert campaigns.status_code == 200
    assert "Campañas" in campaigns.text
    assert ">Activa<" in campaigns.text
    assert ">active<" not in campaigns.text

    affiliates = client.get("/app/merchant?view=affiliates")
    assert affiliates.status_code == 200
    assert 'data-merchant-invitation' in affiliates.text
    assert "Afiliados inscritos" in affiliates.text
    assert 'href="/app/merchant?view=affiliates" aria-current="page"' in affiliates.text

    settings = client.get("/app/merchant?view=settings")
    assert settings.status_code == 200
    assert "Marca e invitación" in settings.text
    assert 'data-merchant-profile' in settings.text
    assert 'class="merchant-settings-card' in settings.text
    assert 'href="/app/merchant?view=settings" aria-current="page"' in settings.text
    onboarding = client.get("/app/merchant/onboarding", follow_redirects=False)
    assert onboarding.status_code == 303
    assert onboarding.headers["location"] == "/app/merchant?view=settings"

    invalid = client.get("/app/merchant?view=unknown")
    assert invalid.status_code == 404


def test_bound_owner_can_sign_in_before_tenant_has_a_campaign(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    human_owner = Keys.generate()
    merchant_identity = Keys.generate()
    monkeypatch.setenv(
        "MERCHANT_ACCOUNT_BINDINGS",
        f"{human_owner.public_key().to_bech32()}:{merchant_identity.public_key().to_bech32()}",
    )

    result = login(client, human_owner, "merchant")

    assert result.json()["account"]["npub"] == human_owner.public_key().to_bech32()
    with main.engine().connect() as connection:
        link = connection.execute(
            text("SELECT merchant_pubkey_hex, source FROM merchant_account_links"),
        ).one()._mapping
        campaign_count = connection.execute(text("SELECT COUNT(*) FROM campaigns")).scalar_one()
    assert link["merchant_pubkey_hex"] == merchant_identity.public_key().to_hex()
    assert link["source"] == "environment_binding"
    assert campaign_count == 0
    profile = client.put(
        "/app/merchant/profile",
        headers={"origin": "https://testserver"},
        json={
            "merchant_pubkey": merchant_identity.public_key().to_bech32(),
            "display_name": "Onboarding Merchant",
            "tagline": "Bitcoin commerce",
            "logo_url": None,
        },
    )
    assert profile.status_code == 200
    unrelated = client.put(
        "/app/merchant/profile",
        headers={"origin": "https://testserver"},
        json={"merchant_pubkey": Keys.generate().public_key().to_bech32(), "display_name": "Nope"},
    )
    assert unrelated.status_code == 404
    redirect = client.get("/app/merchant", follow_redirects=False)
    assert redirect.status_code == 303
    assert redirect.headers["location"] == "/app/merchant/onboarding"

    page = client.get("/app/merchant/onboarding")
    assert page.status_code == 200
    assert "Configurá tu programa" in page.text
    assert 'data-merchant-onboarding-wizard' in page.text
    assert 'data-onboarding-step="1"' in page.text
    assert 'data-onboarding-step="2"' in page.text
    assert 'data-onboarding-step="3"' in page.text
    assert 'data-merchant-bootstrap' in page.text
    assert f'value="{merchant_identity.public_key().to_bech32()}"' in page.text
    assert "Crear programa y terminar" in page.text
    assert "Shopify conectado" not in page.text


def test_merchant_onboarding_endpoint_is_idempotent_and_persists_all_steps(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    owner = Keys.generate()
    merchant = Keys.generate()
    monkeypatch.setenv(
        "MERCHANT_ACCOUNT_BINDINGS",
        f"{owner.public_key().to_bech32()}:{merchant.public_key().to_bech32()}",
    )
    login(client, owner, "merchant")
    payload = {
        "merchant_pubkey": merchant.public_key().to_bech32(),
        "display_name": "Lightning Koffee",
        "tagline": "Café, Bitcoin y comunidad",
        "logo_url": None,
        "program_name": "Lightning Koffee Affiliate Program",
        "commission_percent": "8",
        "attribution_window_days": 30,
        "destination_url": "https://lightningkoffee.io",
        "terms_url": "https://lightningkoffee.io/terms",
        "invite_eyebrow": "Programa de afiliados · Value for value",
        "invite_headline": "Recomendá café. Ganá sats.",
        "invite_description": "Compartí la marca con tu comunidad.",
    }
    created = client.post(
        "/app/merchant/onboarding",
        headers={"origin": "https://testserver"},
        json=payload,
    )
    assert created.status_code == 200
    assert created.json()["duplicate"] is False
    retried = client.post(
        "/app/merchant/onboarding",
        headers={"origin": "https://testserver"},
        json=payload,
    )
    assert retried.status_code == 200
    assert retried.json()["duplicate"] is True
    with main.engine().connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT c.name, c.commission_bps, c.invite_headline,
                       p.display_name, p.tagline
                FROM campaigns c JOIN merchant_profiles p
                  ON p.merchant_pubkey_hex=c.merchant_pubkey_hex
                WHERE c.id=:id
                """
            ),
            {"id": created.json()["campaign_id"]},
        ).mappings().one()
    assert row == {
        "name": "Lightning Koffee Affiliate Program",
        "commission_bps": 800,
        "invite_headline": "Recomendá café. Ganá sats.",
        "display_name": "Lightning Koffee",
        "tagline": "Café, Bitcoin y comunidad",
    }


def test_merchant_onboarding_page_keeps_campaignless_bound_tenant_eligible(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    owner = Keys.generate()
    configured = Keys.generate()
    campaignless = Keys.generate()
    create_campaign(client, configured, name="Configured tenant")
    monkeypatch.setenv(
        "MERCHANT_ACCOUNT_BINDINGS",
        ",".join(
            [
                f"{owner.public_key().to_bech32()}:{configured.public_key().to_bech32()}",
                f"{owner.public_key().to_bech32()}:{campaignless.public_key().to_bech32()}",
            ]
        ),
    )
    login(client, owner, "merchant")

    page = client.get("/app/merchant/onboarding", follow_redirects=False)
    english = client.get("/app/merchant/onboarding?lang=en", follow_redirects=False)

    assert page.status_code == 200
    assert f'value="{campaignless.public_key().to_bech32()}"' in page.text
    assert f'value="{configured.public_key().to_bech32()}"' not in page.text
    assert english.status_code == 200
    assert "Set up your program." in english.text
    assert "Who can join?" in english.text
    assert 'value="Affiliate program · Value for value"' in english.text
    assert "Configurá tu programa" not in english.text
    assert "¿Quién puede sumarse?" not in english.text


def test_merchant_onboarding_conflict_does_not_modify_profile(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    owner = Keys.generate()
    merchant = Keys.generate()
    monkeypatch.setenv(
        "MERCHANT_ACCOUNT_BINDINGS",
        f"{owner.public_key().to_bech32()}:{merchant.public_key().to_bech32()}",
    )
    login(client, owner, "merchant")
    payload = {
        "merchant_pubkey": merchant.public_key().to_bech32(),
        "display_name": "Original profile",
        "program_name": "Original program",
        "commission_percent": "8",
        "attribution_window_days": 30,
        "destination_url": "https://merchant.example/shop",
        "terms_url": "https://merchant.example/terms",
    }
    assert client.post(
        "/app/merchant/onboarding", headers={"origin": "https://testserver"}, json=payload
    ).status_code == 200

    conflict = client.post(
        "/app/merchant/onboarding",
        headers={"origin": "https://testserver"},
        json={**payload, "display_name": "Must roll back", "program_name": "Conflicting program"},
    )

    assert conflict.status_code == 409
    with main.engine().connect() as connection:
        display_name = connection.execute(
            text("SELECT display_name FROM merchant_profiles WHERE merchant_pubkey_hex=:hex"),
            {"hex": merchant.public_key().to_hex()},
        ).scalar_one()
    assert display_name == "Original profile"


def test_merchant_onboarding_final_failure_rolls_back_campaign_and_profile(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    owner = Keys.generate()
    merchant = Keys.generate()
    monkeypatch.setenv(
        "MERCHANT_ACCOUNT_BINDINGS",
        f"{owner.public_key().to_bech32()}:{merchant.public_key().to_bech32()}",
    )
    login(client, owner, "merchant")

    def fail_invitation(*args, **kwargs):
        raise RuntimeError("final onboarding persistence failed")

    monkeypatch.setattr(main, "_persist_merchant_campaign_invite", fail_invitation)
    with pytest.raises(RuntimeError, match="final onboarding persistence failed"):
        client.post(
            "/app/merchant/onboarding",
            headers={"origin": "https://testserver"},
            json={
                "merchant_pubkey": merchant.public_key().to_bech32(),
                "display_name": "Must not persist",
                "program_name": "Must not persist",
                "destination_url": "https://merchant.example/shop",
                "terms_url": "https://merchant.example/terms",
            },
        )

    with main.engine().connect() as connection:
        campaign_count = connection.execute(
            text("SELECT COUNT(*) FROM campaigns WHERE merchant_pubkey_hex=:hex"),
            {"hex": merchant.public_key().to_hex()},
        ).scalar_one()
        profile_count = connection.execute(
            text("SELECT COUNT(*) FROM merchant_profiles WHERE merchant_pubkey_hex=:hex"),
            {"hex": merchant.public_key().to_hex()},
        ).scalar_one()
    assert campaign_count == 0
    assert profile_count == 0


def test_merchant_bootstrap_browser_contract_is_same_origin_json(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    human_owner = Keys.generate()
    merchant_identity = Keys.generate()
    monkeypatch.setenv(
        "MERCHANT_ACCOUNT_BINDINGS",
        f"{human_owner.public_key().to_bech32()}:{merchant_identity.public_key().to_bech32()}",
    )
    login(client, human_owner, "merchant")

    page = client.get("/app/merchant/onboarding")
    script = client.get("/static/app.js")

    assert page.status_code == 200
    assert 'name="merchant_pubkey"' in page.text
    assert 'name="program_name"' in page.text
    assert 'name="commission_percent"' in page.text
    assert 'name="attribution_window_days"' in page.text
    assert 'name="destination_url"' in page.text
    assert 'name="terms_url"' in page.text
    assert 'name="logo_url"' in page.text
    assert 'name="display_name"' in page.text
    assert 'name="tagline"' in page.text
    assert 'name="invite_eyebrow"' in page.text
    assert 'name="invite_headline"' in page.text
    assert 'name="invite_description"' in page.text
    assert 'data-bootstrap-status' in page.text
    assert script.status_code == 200
    assert 'event.target.closest("[data-merchant-bootstrap]")' in script.text
    assert 'await jsonFetch("/app/merchant/bootstrap"' in script.text
    assert 'await jsonFetch("/app/merchant/onboarding"' in script.text
    assert "currentStep < 3" in script.text
    assert 'method: "POST", body: JSON.stringify(payload)' in script.text
    assert 'program_name: String(fields.get("program_name")' in script.text
    assert 'commission_percent: String(fields.get("commission_percent")' in script.text
    assert 'logo_url: String(fields.get("logo_url")' in script.text


def test_self_binding_does_not_bootstrap_campaignless_merchant(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    merchant_npub = merchant.public_key().to_bech32()
    monkeypatch.setenv("MERCHANT_ACCOUNT_BINDINGS", f"{merchant_npub}:{merchant_npub}")
    challenge = client.post("/auth/nostr/challenge", json={"role": "merchant"}).json()

    response = client.post(
        "/auth/nostr/verify",
        json={"event": signed_login_event(merchant, challenge)},
    )

    assert response.status_code == 403
    with main.engine().connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM merchant_account_links")).scalar_one() == 0
        assert connection.execute(text("SELECT COUNT(*) FROM campaigns")).scalar_one() == 0


def test_merchant_bootstrap_requires_session_origin_and_bound_tenant(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    human_owner = Keys.generate()
    merchant_identity = Keys.generate()
    foreign_merchant = Keys.generate()
    monkeypatch.setenv(
        "MERCHANT_ACCOUNT_BINDINGS",
        f"{human_owner.public_key().to_bech32()}:{merchant_identity.public_key().to_bech32()}",
    )
    payload = {"merchant_pubkey": merchant_identity.public_key().to_bech32()}

    anonymous = client.post(
        "/app/merchant/bootstrap",
        headers={"origin": "https://testserver"},
        json=payload,
    )
    assert anonymous.status_code == 401

    login(client, human_owner, "merchant")
    wrong_origin = client.post(
        "/app/merchant/bootstrap",
        headers={"origin": "https://evil.example"},
        json=payload,
    )
    assert wrong_origin.status_code == 403
    missing_origin = client.post("/app/merchant/bootstrap", json=payload)
    assert missing_origin.status_code == 403
    foreign_tenant = client.post(
        "/app/merchant/bootstrap",
        headers={"origin": "https://testserver"},
        json={"merchant_pubkey": foreign_merchant.public_key().to_bech32()},
    )
    assert foreign_tenant.status_code == 404
    extra_field = client.post(
        "/app/merchant/bootstrap",
        headers={"origin": "https://testserver"},
        json={**payload, "commission_bps": 1},
    )
    assert extra_field.status_code == 422


def test_merchant_bootstrap_creates_configurable_active_program_and_reusable_logo_idempotently(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    human_owner = Keys.generate()
    merchant_identity = Keys.generate()
    merchant_npub = merchant_identity.public_key().to_bech32()
    monkeypatch.setenv(
        "MERCHANT_ACCOUNT_BINDINGS",
        f"{human_owner.public_key().to_bech32()}:{merchant_npub}",
    )

    publish_calls = []

    def fake_publish(event):
        publish_calls.append(event["id"])
        return [{"relay": "wss://relay.example", "status": "skipped", "error": "test"}]

    monkeypatch.setattr(main, "publish_event", fake_publish)
    login(client, human_owner, "merchant")
    payload = {
        "merchant_pubkey": merchant_npub,
        "program_name": "Shapersfit Affiliate Program",
        "commission_percent": "7.25",
        "attribution_window_days": 45,
        "destination_url": "https://shapersfit.myshopify.com/collections/new",
        "terms_url": "https://shapersfit.com/affiliate-terms",
        "logo_url": "https://cdn.example.com/merchant/shapersfit.webp",
    }
    headers = {"origin": "https://testserver"}

    first = client.post("/app/merchant/bootstrap", headers=headers, json=payload)
    second = client.post("/app/merchant/bootstrap", headers=headers, json=payload)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True
    assert first.json()["campaign_id"] == second.json()["campaign_id"]
    assert first.json()["status"] == second.json()["status"] == "active"
    assert first.json()["merchant_pubkey"] == merchant_npub
    assert len(publish_calls) == 1
    conflict = client.post(
        "/app/merchant/bootstrap",
        headers=headers,
        json={**payload, "commission_percent": "9"},
    )
    assert conflict.status_code == 409
    workspace = client.get("/app/merchant?view=settings")
    assert 'data-merchant-profile' in workspace.text
    assert f'value="{payload["logo_url"]}"' in workspace.text
    updated_logo = "https://images.example.com/brands/shapersfit.png"
    updated_profile = client.put(
        "/app/merchant/profile",
        headers=headers,
        json={"merchant_pubkey": merchant_npub, "logo_url": updated_logo},
    )
    assert updated_profile.status_code == 200, updated_profile.text
    assert updated_profile.json()["logo_url"] == updated_logo
    with main.engine().connect() as connection:
        campaigns = [dict(row._mapping) for row in connection.execute(text("SELECT * FROM campaigns")).fetchall()]
        merchant_profile = dict(connection.execute(text("SELECT * FROM merchant_profiles")).one()._mapping)
        budgets = connection.execute(text("SELECT COUNT(*) FROM campaign_budgets")).scalar_one()
        events = connection.execute(
            text("SELECT COUNT(*) FROM nostr_events WHERE entity_type='campaign' AND entity_id=:id"),
            {"id": first.json()["campaign_id"]},
        ).scalar_one()
    assert len(campaigns) == 1
    assert campaigns[0]["merchant_pubkey"] == merchant_npub
    assert campaigns[0]["merchant_pubkey_hex"] == merchant_identity.public_key().to_hex()
    assert campaigns[0]["name"] == "Shapersfit Affiliate Program"
    assert campaigns[0]["commission_bps"] == 725
    assert campaigns[0]["window_days"] == 45
    assert campaigns[0]["destination_url"] == "https://shapersfit.myshopify.com/collections/new"
    assert campaigns[0]["terms_url"] == "https://shapersfit.com/affiliate-terms"
    assert campaigns[0]["terms_hash"] == main.sha("https://shapersfit.com/affiliate-terms")
    assert campaigns[0]["status"] == "active"
    assert ["status", "active"] in json.loads(campaigns[0]["nostr_event_json"])["tags"]
    assert budgets == 1
    assert events == 1
    assert merchant_profile["merchant_pubkey_hex"] == merchant_identity.public_key().to_hex()
    assert merchant_profile["logo_url"] == updated_logo

    summary = client.get(f"/campaigns/{first.json()['campaign_id']}/summary")
    public_page = client.get(f"/campaigns/{first.json()['campaign_id']}/page")
    assert summary.json()["merchant_profile"]["logo_url"] == updated_logo
    assert f'src="{updated_logo}"' in public_page.text
    assert 'referrerpolicy="no-referrer"' in public_page.text


def test_merchant_bootstrap_rejects_unsafe_or_invalid_configurable_fields(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    human_owner = Keys.generate()
    merchant_identity = Keys.generate()
    merchant_npub = merchant_identity.public_key().to_bech32()
    monkeypatch.setenv(
        "MERCHANT_ACCOUNT_BINDINGS",
        f"{human_owner.public_key().to_bech32()}:{merchant_npub}",
    )
    login(client, human_owner, "merchant")
    headers = {"origin": "https://testserver"}
    valid = {
        "merchant_pubkey": merchant_npub,
        "program_name": "Safe program",
        "commission_percent": "8",
        "attribution_window_days": 30,
        "destination_url": "https://merchant.example/shop",
        "terms_url": "https://merchant.example/terms",
        "logo_url": "https://cdn.example.com/logo.png",
    }

    invalid_overrides = [
        {"commission_percent": "0"},
        {"commission_percent": "100.001"},
        {"attribution_window_days": 0},
        {"destination_url": "javascript:alert(1)"},
        {"terms_url": "file:///tmp/terms"},
        {"logo_url": "http://cdn.example.com/logo.png"},
        {"logo_url": "https://127.0.0.1/logo.png"},
        {"logo_url": "https://images.example:444/logo.png"},
        {"logo_url": "https://[::1/logo.png"},
        {"logo_url": "https://images.example/logo.svg"},
    ]
    for override in invalid_overrides:
        response = client.post("/app/merchant/bootstrap", headers=headers, json={**valid, **override})
        assert response.status_code == 422, (override, response.text)

    with main.engine().connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM campaigns")).scalar_one() == 0
        assert connection.execute(text("SELECT COUNT(*) FROM merchant_profiles")).scalar_one() == 0


def test_removing_merchant_binding_revokes_session_after_bootstrap(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    human_owner = Keys.generate()
    merchant_identity = Keys.generate()
    merchant_npub = merchant_identity.public_key().to_bech32()
    monkeypatch.setenv(
        "MERCHANT_ACCOUNT_BINDINGS",
        f"{human_owner.public_key().to_bech32()}:{merchant_npub}",
    )
    login(client, human_owner, "merchant")
    created = client.post(
        "/app/merchant/bootstrap",
        headers={"origin": "https://testserver"},
        json={"merchant_pubkey": merchant_npub},
    )
    assert created.status_code == 200

    monkeypatch.delenv("MERCHANT_ACCOUNT_BINDINGS", raising=False)
    denied = client.get("/app/merchant", follow_redirects=False)

    assert denied.status_code == 303
    assert denied.headers["location"] == "/app?role=merchant"
    with main.engine().connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM merchant_account_links")).scalar_one() == 0
        assert connection.execute(
            text("SELECT COUNT(*) FROM account_sessions WHERE revoked_at IS NOT NULL")
        ).scalar_one() == 1
        assert connection.execute(text("SELECT COUNT(*) FROM campaigns")).scalar_one() == 1


def test_failed_bootstrap_relay_is_durable_and_retry_is_duplicate(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    human_owner = Keys.generate()
    merchant_identity = Keys.generate()
    merchant_npub = merchant_identity.public_key().to_bech32()
    monkeypatch.setenv(
        "MERCHANT_ACCOUNT_BINDINGS",
        f"{human_owner.public_key().to_bech32()}:{merchant_npub}",
    )
    publish_calls = []

    def failed_then_published(event):
        publish_calls.append(event["id"])
        if len(publish_calls) == 1:
            raise RuntimeError("relay publisher offline")
        return [{"relay": "wss://relay.example", "status": "published"}]

    monkeypatch.setattr(main, "publish_event", failed_then_published)
    login(client, human_owner, "merchant")
    request = {
        "headers": {"origin": "https://testserver"},
        "json": {"merchant_pubkey": merchant_npub},
    }

    first = client.post("/app/merchant/bootstrap", **request)
    second = client.post("/app/merchant/bootstrap", **request)

    assert first.status_code == 200
    assert first.json()["relay_results"][0]["status"] == "failed"
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["relay_results"][0]["status"] == "published"
    assert len(publish_calls) == 2
    with main.engine().connect() as connection:
        event = connection.execute(
            text("SELECT relay_status FROM nostr_events WHERE entity_type='campaign'")
        ).one()._mapping
        assert connection.execute(text("SELECT COUNT(*) FROM campaigns")).scalar_one() == 1
        assert connection.execute(text("SELECT COUNT(*) FROM nostr_events WHERE entity_type='campaign'")).scalar_one() == 1
    assert event["relay_status"] == "published"


def test_concurrent_bootstrap_publication_cannot_downgrade_published_event(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    human_owner = Keys.generate()
    merchant_identity = Keys.generate()
    merchant_npub = merchant_identity.public_key().to_bech32()
    monkeypatch.setenv(
        "MERCHANT_ACCOUNT_BINDINGS",
        f"{human_owner.public_key().to_bech32()}:{merchant_npub}",
    )
    login(client, human_owner, "merchant")
    first_started = threading.Event()
    release_first = threading.Event()
    calls = []
    calls_lock = threading.Lock()

    def racing_publish(event):
        with calls_lock:
            calls.append(event["id"])
            attempt = len(calls)
        if attempt == 1:
            first_started.set()
            assert release_first.wait(5)
            return [{"relay": "wss://relay.example", "status": "failed", "error": "late failure"}]
        return [{"relay": "wss://relay.example", "status": "published"}]

    monkeypatch.setattr(main, "publish_event", racing_publish)

    def bootstrap():
        return client.post(
            "/app/merchant/bootstrap",
            headers={"origin": "https://testserver"},
            json={"merchant_pubkey": merchant_npub},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(bootstrap)
        assert first_started.wait(5)
        second = pool.submit(bootstrap)
        try:
            time.sleep(0.15)
            assert len(calls) == 1
        finally:
            release_first.set()
        responses = [first.result(timeout=5), second.result(timeout=5)]

    assert [response.status_code for response in responses] == [200, 200]
    assert sorted(response.json()["duplicate"] for response in responses) == [False, True]
    assert len(calls) == 2
    with main.engine().connect() as connection:
        stored = connection.execute(
            text("SELECT relay_status, published_at, event_json FROM nostr_events WHERE entity_type='campaign'")
        ).one()._mapping
        campaign_event = connection.execute(
            text("SELECT nostr_event_json FROM campaigns WHERE id=:id"),
            {"id": responses[0].json()["campaign_id"]},
        ).scalar_one()
    assert stored["relay_status"] == "published"
    assert stored["published_at"] is not None
    assert json.loads(stored["event_json"])["relay_status"] == "published"
    assert json.loads(campaign_event)["relay_status"] == "published"


def test_legacy_enrollment_hook_is_removed(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    denied = client.post(
        "/enrollments",
        json={"campaign_id": "camp_unused", "affiliate_pubkey": Keys.generate().public_key().to_bech32()},
    )
    assert denied.status_code == 404


def test_direct_merchant_enrollment_endpoint_is_retired(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant_identity = Keys.generate()
    human_owner = Keys.generate()
    affiliate = Keys.generate()
    campaign = create_campaign(client, merchant_identity, name="Session-managed campaign")
    monkeypatch.setenv(
        "MERCHANT_ACCOUNT_BINDINGS",
        f"{human_owner.public_key().to_bech32()}:{merchant_identity.public_key().to_bech32()}",
    )
    login(client, human_owner, "merchant")

    payload = {
        "campaign_id": campaign["campaign_id"],
        "affiliate_pubkey": affiliate.public_key().to_bech32(),
    }
    first = client.post(
        "/app/merchant/enrollments",
        headers={"origin": "https://testserver"},
        json=payload,
    )
    assert first.status_code == 404, first.text


def test_inactive_enrollment_does_not_grant_affiliate_login(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    affiliate = Keys.generate()
    campaign = create_campaign(client, merchant)
    enrollment = create_enrollment(client, campaign["campaign_id"], affiliate)
    with main.engine().begin() as connection:
        connection.execute(
            text("UPDATE enrollments SET status='suspended' WHERE id=:id"),
            {"id": enrollment["enrollment_id"]},
        )

    challenge = client.post("/auth/nostr/challenge", json={"role": "affiliate"}).json()
    denied = client.post(
        "/auth/nostr/verify",
        json={"event": signed_login_event(affiliate, challenge)},
    )
    assert denied.status_code == 403


def test_retired_direct_enrollment_cannot_be_used_without_origin(tmp_path, monkeypatch):
    owner_client = configured_client(tmp_path, monkeypatch)
    owner_identity = Keys.generate()
    other_identity = Keys.generate()
    affiliate = Keys.generate()
    owned = create_campaign(owner_client, owner_identity, name="Owned campaign")
    foreign = create_campaign(owner_client, other_identity, name="Foreign campaign")
    login(owner_client, owner_identity, "merchant")
    payload = {
        "campaign_id": owned["campaign_id"],
        "affiliate_pubkey": affiliate.public_key().to_bech32(),
    }

    no_origin = owner_client.post("/app/merchant/enrollments", json=payload)
    assert no_origin.status_code == 404


def test_legacy_demo_mutations_are_fail_closed_by_default(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    monkeypatch.delenv("ENABLE_LEGACY_DEMO_MUTATIONS", raising=False)
    merchant = Keys.generate()
    blocked = client.post(
        "/campaigns",
        json={
            "merchant_pubkey": merchant.public_key().to_bech32(),
            "name": "Should not exist",
            "commission_bps": 800,
            "attribution_window_days": 30,
            "destination_url": "https://merchant.example",
        },
    )
    assert blocked.status_code == 404
    assert blocked.json()["detail"] == "legacy demo mutations are disabled"


def test_workspace_kpis_are_not_truncated_to_recent_30(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    affiliate = Keys.generate()
    campaign = create_campaign(client, merchant, name="Scale KPI campaign")
    create_enrollment(client, campaign["campaign_id"], affiliate)
    timestamp = main.now()
    with main.engine().begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO conversions
                  (id, order_id_hash, click_id, campaign_id, affiliate_pubkey, order_total,
                   currency, commission_sats, status, nostr_event_id, nostr_event_json, created_at)
                VALUES
                  (:id, :order_hash, :click_id, :campaign_id, :affiliate, 1, 'USD', 100,
                   'approved', :event_id, '{}', :created_at)
                """
            ),
            [
                {
                    "id": f"conv_scale_{index}",
                    "order_hash": f"order_{index}",
                    "click_id": f"click_scale_{index}",
                    "campaign_id": campaign["campaign_id"],
                    "affiliate": affiliate.public_key().to_bech32(),
                    "event_id": f"event_scale_{index}",
                    "created_at": timestamp,
                }
                for index in range(35)
            ],
        )

    login(client, merchant, "merchant")
    with main.engine().connect() as connection:
        account = dict(
            connection.execute(
                text("SELECT * FROM accounts WHERE nostr_pubkey_hex=:hex"),
                {"hex": merchant.public_key().to_hex()},
            ).one()._mapping
        )
        session = {"account_id": account["id"], "nostr_pubkey_hex": account["nostr_pubkey_hex"], "npub": account["npub"]}
        merchant_data = merchant_workspace_data(
            connection, session, base_url=main.BASE_URL, shopify_ready=False, shopify_detail="not configured"
        )
    assert len(merchant_data["conversions"]) == 30
    assert merchant_data["totals"]["conversions"] == 35
    assert merchant_data["totals"]["commission_sats"] == 3500
    assert merchant_data["totals"]["affiliates"] == 1

    client.post("/auth/logout")
    login(client, affiliate, "affiliate")
    with main.engine().connect() as connection:
        account = dict(
            connection.execute(
                text("SELECT * FROM accounts WHERE nostr_pubkey_hex=:hex"),
                {"hex": affiliate.public_key().to_hex()},
            ).one()._mapping
        )
        session = {"account_id": account["id"], "nostr_pubkey_hex": account["nostr_pubkey_hex"], "npub": account["npub"]}
        affiliate_data = affiliate_workspace_data(connection, session, base_url=main.BASE_URL)
    assert len(affiliate_data["conversions"]) == 30
    assert affiliate_data["totals"]["conversions"] == 35
    assert affiliate_data["totals"]["gross_sats"] == 3500


def test_merchants_cannot_see_each_others_campaigns(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant_a = Keys.generate()
    merchant_b = Keys.generate()
    create_campaign(client, merchant_a, name="Only Merchant Alpha")
    create_campaign(client, merchant_b, name="Only Merchant Beta")

    login(client, merchant_a, "merchant")
    alpha_page = client.get("/app/merchant?view=campaigns")
    assert "Only Merchant Alpha" in alpha_page.text
    assert "Only Merchant Beta" not in alpha_page.text

    client.post("/auth/logout")
    login(client, merchant_b, "merchant")
    beta_page = client.get("/app/merchant?view=campaigns")
    assert "Only Merchant Beta" in beta_page.text
    assert "Only Merchant Alpha" not in beta_page.text


def test_ops_dashboard_escapes_untrusted_table_values(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    attacker = Keys.generate()
    campaign = create_campaign(client, attacker, name="Unsafe campaign")
    malicious = '<img src=x onerror="alert(1)">'
    with main.engine().begin() as connection:
        event_id = connection.execute(
            text("SELECT event_id FROM nostr_events WHERE entity_id=:campaign_id LIMIT 1"),
            {"campaign_id": campaign["campaign_id"]},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO nostr_event_relays (event_id, relay_url, status, error, created_at)
                VALUES (:event_id, 'wss://unsafe.example', 'failed', :error, :created_at)
                """
            ),
            {"event_id": event_id, "error": malicious, "created_at": main.now()},
        )
    operator = Keys.generate()
    monkeypatch.setenv("OPS_NOSTR_PUBKEYS", operator.public_key().to_bech32())
    login(client, operator, "ops")

    source = client.get("/ops").text
    assert malicious not in source
    assert "&lt;img src=x onerror=&#34;alert(1)&#34;&gt;" in source
    assert "unsafe.example" in source
    assert "Deslizá para ver todas las columnas" in source


def test_removing_ops_allowlist_revokes_active_session(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    operator = Keys.generate()
    monkeypatch.setenv("OPS_NOSTR_PUBKEYS", operator.public_key().to_bech32())
    login(client, operator, "ops")
    assert client.get("/ops").status_code == 200

    monkeypatch.delenv("OPS_NOSTR_PUBKEYS", raising=False)
    denied = client.get("/ops", follow_redirects=False)
    assert denied.status_code == 303
    assert denied.headers["location"] == "/app?role=ops"
    assert client.get("/auth/me").status_code == 401


def test_merchant_api_key_is_fail_closed_and_scoped_to_configured_merchant(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant_a = Keys.generate()
    merchant_b = Keys.generate()
    campaign_a = create_campaign(client, merchant_a, name="Authorized merchant")
    campaign_b = create_campaign(client, merchant_b, name="Other merchant")
    enrollment_a = create_enrollment(client, campaign_a["campaign_id"], Keys.generate())
    enrollment_b = create_enrollment(client, campaign_b["campaign_id"], Keys.generate())
    click_a = client.post("/clicks/simulate", json={"ref_code": enrollment_a["ref_code"]}).json()["click_id"]
    click_b = client.post("/clicks/simulate", json={"ref_code": enrollment_b["ref_code"]}).json()["click_id"]
    headers = {"Authorization": "Bearer test-merchant-key"}

    monkeypatch.delenv("MERCHANT_API_KEYS", raising=False)
    missing = client.post(
        f"/campaigns/{campaign_a['campaign_id']}/status", headers=headers, json={"status": "paused"}
    )
    assert missing.status_code == 503

    monkeypatch.setenv("MERCHANT_API_KEYS", "test-merchant-key")
    monkeypatch.setenv("SHOPIFY_MERCHANT_PUBKEY", merchant_a.public_key().to_bech32())
    allowed = client.post(
        f"/campaigns/{campaign_a['campaign_id']}/status", headers=headers, json={"status": "active"}
    )
    assert allowed.status_code == 200
    denied = client.post(
        f"/campaigns/{campaign_b['campaign_id']}/status", headers=headers, json={"status": "paused"}
    )
    assert denied.status_code == 403

    enrollment_allowed = client.post(
        f"/enrollments/{enrollment_a['enrollment_id']}/status", headers=headers, json={"status": "approved"}
    )
    assert enrollment_allowed.status_code == 200
    enrollment_denied = client.post(
        f"/enrollments/{enrollment_b['enrollment_id']}/status", headers=headers, json={"status": "approved"}
    )
    assert enrollment_denied.status_code == 403

    webhook_allowed = client.post(
        "/merchant/conversions", headers=headers,
        json={"order_id": "owner-order", "bb_click_id": click_a, "order_total": 1000, "currency": "SATS"},
    )
    assert webhook_allowed.status_code == 200
    webhook_denied = client.post(
        "/merchant/conversions", headers=headers,
        json={"order_id": "other-order-denied", "bb_click_id": click_b, "order_total": 1000, "currency": "SATS"},
    )
    assert webhook_denied.status_code == 403

    other_conversion = main.process_merchant_conversion(
        main.MerchantConversionIn(
            order_id="other-order-created",
            bb_click_id=click_b,
            order_total=1000,
            currency="SATS",
        ),
        merchant_b.public_key().to_hex(),
    )
    reversal_denied = client.post(
        f"/conversions/{other_conversion['conversion_id']}/reverse",
        headers=headers,
        json={"reason": "refund", "refund_sats": 1000},
    )
    assert reversal_denied.status_code == 403


def test_workspace_kpis_exclude_reversed_conversions(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    affiliate = Keys.generate()
    campaign = create_campaign(client, merchant, name="Net KPI campaign")
    create_enrollment(client, campaign["campaign_id"], affiliate)
    timestamp = main.now()
    with main.engine().begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO conversions
                  (id, order_id_hash, click_id, campaign_id, affiliate_pubkey, order_total,
                   currency, commission_sats, status, nostr_event_id, nostr_event_json, created_at)
                VALUES
                  (:id, :order_hash, :click_id, :campaign_id, :affiliate, 1, 'USD', :commission,
                   :status, :event_id, '{}', :created_at)
                """
            ),
            [
                {"id": "conv_net_ok", "order_hash": "order_net_ok", "click_id": "click_net_ok",
                 "campaign_id": campaign["campaign_id"], "affiliate": affiliate.public_key().to_bech32(),
                 "commission": 100, "status": "approved", "event_id": "event_net_ok", "created_at": timestamp},
                {"id": "conv_net_reversed", "order_hash": "order_net_reversed", "click_id": "click_net_reversed",
                 "campaign_id": campaign["campaign_id"], "affiliate": affiliate.public_key().to_bech32(),
                 "commission": 900, "status": "reversed", "event_id": "event_net_reversed", "created_at": timestamp},
            ],
        )

    login(client, merchant, "merchant")
    with main.engine().connect() as connection:
        account = dict(connection.execute(text("SELECT * FROM accounts WHERE nostr_pubkey_hex=:hex"),
                                          {"hex": merchant.public_key().to_hex()}).one()._mapping)
        merchant_data = merchant_workspace_data(
            connection,
            {"account_id": account["id"], "nostr_pubkey_hex": account["nostr_pubkey_hex"], "npub": account["npub"]},
            base_url=main.BASE_URL, shopify_ready=False, shopify_detail="not configured",
        )
    assert merchant_data["totals"]["conversions"] == 1
    assert merchant_data["totals"]["commission_sats"] == 100

    client.post("/auth/logout")
    login(client, affiliate, "affiliate")
    with main.engine().connect() as connection:
        account = dict(connection.execute(text("SELECT * FROM accounts WHERE nostr_pubkey_hex=:hex"),
                                          {"hex": affiliate.public_key().to_hex()}).one()._mapping)
        affiliate_data = affiliate_workspace_data(
            connection,
            {"account_id": account["id"], "nostr_pubkey_hex": account["nostr_pubkey_hex"], "npub": account["npub"]},
            base_url=main.BASE_URL,
        )
    assert affiliate_data["totals"]["conversions"] == 1
    assert affiliate_data["totals"]["gross_sats"] == 100


def test_merchant_dashboard_exposes_clicks_affiliate_npubs_shopify_sales_and_copyable_installation_code(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPIFY_STORE_DOMAIN", "pilot-shop.myshopify.com")
    client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    monkeypatch.setenv("SHOPIFY_MERCHANT_PUBKEY", merchant.public_key().to_bech32())
    affiliate_a = Keys.generate()
    affiliate_b = Keys.generate()
    other_merchant = Keys.generate()
    other_affiliate = Keys.generate()

    campaign = create_campaign(client, merchant, name="Pilot program")
    enrollment_a = create_enrollment(client, campaign["campaign_id"], affiliate_a)
    create_enrollment(client, campaign["campaign_id"], affiliate_b)
    payout_id = seed_payable_payout(campaign, enrollment_a, affiliate_a, amount_sats=320)

    other_campaign = create_campaign(client, other_merchant, name="Other private program")
    create_enrollment(client, other_campaign["campaign_id"], other_affiliate)

    timestamp = main.now()
    with main.engine().begin() as connection:
        conversion_id = connection.execute(
            text("SELECT conversion_id FROM payouts WHERE id=:id"), {"id": payout_id}
        ).scalar_one()
        connection.execute(
            text("UPDATE conversions SET order_total=149.50, order_total_decimal='149.50', currency='USD' WHERE id=:id"),
            {"id": conversion_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO shopify_webhook_deliveries
                  (webhook_id, order_key, shop_domain, topic, click_id, order_total,
                   order_total_decimal, currency, status, conversion_id, error, created_at, processed_at)
                VALUES
                  ('wh_dashboard', 'order_dashboard', 'pilot-shop.myshopify.com', 'orders/paid',
                   :click_id, 149.50, '149.50', 'USD', 'processed', :conversion_id, NULL,
                   :created_at, :processed_at)
                """
            ),
            {
                "click_id": connection.execute(
                    text("SELECT click_id FROM conversions WHERE id=:id"), {"id": conversion_id}
                ).scalar_one(),
                "conversion_id": conversion_id,
                "created_at": timestamp,
                "processed_at": timestamp,
            },
        )

    login(client, merchant, "merchant")
    overview = client.get("/app/merchant")
    affiliates_page = client.get("/app/merchant?view=affiliates")
    activity_page = client.get("/app/merchant?view=activity")
    integration_page = client.get("/app/merchant?view=integration")

    assert overview.status_code == affiliates_page.status_code == activity_page.status_code == integration_page.status_code == 200
    assert "Clics" in activity_page.text
    assert "Clicks" not in activity_page.text
    assert "Compras Shopify" in overview.text
    assert "$149.50" in overview.text
    assert affiliate_a.public_key().to_bech32() in affiliates_page.text
    assert affiliate_b.public_key().to_bech32() in affiliates_page.text
    assert other_affiliate.public_key().to_bech32() not in affiliates_page.text
    assert "Script del tema de Shopify" in integration_page.text
    assert "Píxel personalizado de Shopify" in integration_page.text
    assert "Shopify Theme Script" not in integration_page.text
    assert "Shopify Custom Pixel" not in integration_page.text
    assert "https://testserver/v1/events" in integration_page.text
    assert "https://testserver/v1/conversions" in integration_page.text
    assert "pilot-shop.myshopify.com" in integration_page.text
    assert "/cart/update.js" in integration_page.text
    assert 'data-copy-target="#shopify-theme-script"' in integration_page.text
    assert 'data-copy-target="#shopify-custom-pixel"' in integration_page.text


def test_affiliate_onboarding_requires_verified_destination_and_segments_workspace(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    affiliate = Keys.generate()
    campaign = create_campaign(client, merchant, name="Payable from day one")
    enrollment = create_enrollment(client, campaign["campaign_id"], affiliate)
    login(client, affiliate, "affiliate")

    gated = client.get("/app/affiliate", follow_redirects=False)
    assert gated.status_code == 303
    assert gated.headers["location"] == "/app/affiliate/onboarding"
    onboarding = client.get("/app/affiliate/onboarding")
    assert onboarding.status_code == 200
    assert "Antes de compartir, asegurá cómo cobrar" in onboarding.text
    assert 'data-affiliate-onboarding' in onboarding.text
    assert 'name="lightning_address"' in onboarding.text

    monkeypatch.setattr(main, "validate_lightning_address", lambda _address: {
        "tag": "payRequest",
        "callback": "https://wallet.example/lnurl/callback",
        "minSendable": 1_000,
        "maxSendable": 1_000_000,
    })
    saved = client.put(
        "/app/affiliate/lightning-address",
        headers={"origin": "https://testserver"},
        json={"lightning_address": "affiliate@wallet.example"},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["redirect"] == "/app/affiliate"
    assert saved.json()["verified"] is True

    with main.engine().connect() as connection:
        profile = dict(connection.execute(text("SELECT * FROM affiliate_profiles WHERE affiliate_pubkey_hex=:hex"), {"hex": affiliate.public_key().to_hex()}).one()._mapping)
        stored_enrollment = connection.execute(text("SELECT lightning_address FROM enrollments WHERE id=:id"), {"id": enrollment["enrollment_id"]}).scalar_one()
    assert profile["lightning_address"] == "affiliate@wallet.example"
    assert profile["verified_at"]
    assert stored_enrollment == "affiliate@wallet.example"

    overview = client.get("/app/affiliate")
    assert overview.status_code == 200
    assert "Tus resultados, sin mezclar tareas." in overview.text
    assert 'href="/app/affiliate?view=links"' in overview.text
    assert 'href="/app/affiliate?view=earnings"' in overview.text
    assert 'href="/app/affiliate?view=activity"' in overview.text
    assert 'href="/app/affiliate?view=settings"' in overview.text
    assert "Payable from day one" not in overview.text

    links = client.get("/app/affiliate?view=links")
    assert links.status_code == 200
    assert "Payable from day one" in links.text
    assert "Tu enlace para compartir" in links.text
    assert 'id="earnings"' not in links.text

    settings = client.get("/app/affiliate?view=settings")
    assert settings.status_code == 200
    assert "affiliate@wallet.example" in settings.text
    assert "Destino verificado" in settings.text
    assert client.get("/app/affiliate?view=unknown").status_code == 404

    with main.engine().begin() as connection:
        connection.execute(
            text("UPDATE enrollments SET lightning_address='stale@wallet.example' WHERE id=:id"),
            {"id": enrollment["enrollment_id"]},
        )
    stale_referral = client.get(f"/r/{enrollment['ref_code']}", follow_redirects=False)
    assert stale_referral.status_code == 409
    stale_links = client.get("/app/affiliate?view=links")
    assert "Falta destino verificado" in stale_links.text
    assert f'data-copy="{enrollment["ref_url"]}"' not in stale_links.text


def test_new_unverified_enrollment_address_is_not_grandfathered(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    affiliate = Keys.generate()
    campaign = create_campaign(client, merchant, name="No implicit trust")
    enrollment = main._create_enrollment_record(
        main.EnrollmentIn(
            campaign_id=campaign["campaign_id"],
            affiliate_pubkey=affiliate.public_key().to_bech32(),
            lightning_address="unverified@wallet.example",
        )
    )
    with main.engine().connect() as connection:
        marker = connection.execute(
            text("SELECT destination_verified_at FROM enrollments WHERE id=:id"),
            {"id": enrollment["enrollment_id"]},
        ).scalar_one_or_none()
    assert marker is None
    blocked = client.get(f"/r/{enrollment['ref_code']}", follow_redirects=False)
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "El destino de cobro del afiliado no está configurado o verificado."


def test_verified_affiliate_destination_is_inherited_by_new_enrollments(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    affiliate = Keys.generate()
    first_campaign = create_campaign(client, merchant, name="First program")
    create_enrollment(client, first_campaign["campaign_id"], affiliate)
    login(client, affiliate, "affiliate")
    monkeypatch.setattr(main, "validate_lightning_address", lambda _address: {"tag": "payRequest"})
    saved = client.put(
        "/app/affiliate/lightning-address",
        headers={"origin": "https://testserver"},
        json={"lightning_address": "affiliate@wallet.example"},
    )
    assert saved.status_code == 200

    second_campaign = create_campaign(client, merchant, name="Second program")
    inherited = main._create_enrollment_record(main.EnrollmentIn(
        campaign_id=second_campaign["campaign_id"],
        affiliate_pubkey=affiliate.public_key().to_bech32(),
        lightning_address=None,
    ))
    with main.engine().connect() as connection:
        destination = connection.execute(text("SELECT lightning_address FROM enrollments WHERE id=:id"), {"id": inherited["enrollment_id"]}).scalar_one()
    assert destination == "affiliate@wallet.example"

    client.post("/auth/logout")
    login(client, merchant, "merchant")
    invited_campaign = create_campaign(client, merchant, name="Invitation program")
    invitation = create_invitation(client, invited_campaign["campaign_id"])
    token = invitation["invite_url"].split("#token=", 1)[1]
    client.post("/auth/logout")
    acceptance_event = invitation_acceptance_event(affiliate, token)
    accepted = client.post(
        "/invite/accept",
        headers={"origin": "https://testserver"},
        json={"token": token, "event": acceptance_event},
    )
    assert accepted.status_code == 200, accepted.text
    invitation_enrollment_id = accepted.json()["enrollment_id"]
    ref_code = accepted.json()["ref_url"].rsplit("/", 1)[-1]
    with main.engine().connect() as connection:
        invitation_destination = connection.execute(
            text("SELECT lightning_address FROM enrollments WHERE id=:id"),
            {"id": invitation_enrollment_id},
        ).scalar_one()
    assert invitation_destination == "affiliate@wallet.example"
    assert client.get(f"/r/{ref_code}", follow_redirects=False).status_code in {302, 303, 307}

    with main.engine().begin() as connection:
        connection.execute(
            text("UPDATE enrollments SET lightning_address=NULL WHERE id=:id"),
            {"id": invitation_enrollment_id},
        )
    replayed = client.post(
        "/invite/accept",
        headers={"origin": "https://testserver"},
        json={"token": token, "event": acceptance_event},
    )
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["duplicate"] is True
    with main.engine().connect() as connection:
        repaired = connection.execute(
            text("SELECT lightning_address FROM enrollments WHERE id=:id"),
            {"id": invitation_enrollment_id},
        ).scalar_one()
    assert repaired == "affiliate@wallet.example"


def test_pending_enrollment_inherits_destination_before_approval(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    affiliate = Keys.generate()
    campaign = create_campaign(client, merchant, name="Pending destination program")
    enrollment = create_enrollment(client, campaign["campaign_id"], affiliate)
    approved_campaign = create_campaign(client, merchant, name="Existing approved program")
    create_enrollment(client, approved_campaign["campaign_id"], affiliate)
    with main.engine().begin() as connection:
        connection.execute(
            text("UPDATE enrollments SET status='pending', lightning_address=NULL WHERE id=:id"),
            {"id": enrollment["enrollment_id"]},
        )
    login(client, affiliate, "affiliate")
    monkeypatch.setattr(main, "validate_lightning_address", lambda _address: {"tag": "payRequest"})
    saved = client.put(
        "/app/affiliate/lightning-address",
        headers={"origin": "https://testserver"},
        json={"lightning_address": "pending@wallet.example"},
    )
    assert saved.status_code == 200, saved.text
    with main.engine().connect() as connection:
        pending_destination = connection.execute(
            text("SELECT lightning_address FROM enrollments WHERE id=:id"),
            {"id": enrollment["enrollment_id"]},
        ).scalar_one()
    assert pending_destination == "pending@wallet.example"
    with main.engine().begin() as connection:
        connection.execute(
            text("UPDATE enrollments SET lightning_address='stale@wallet.example' WHERE id=:id"),
            {"id": enrollment["enrollment_id"]},
        )

    monkeypatch.setenv("MERCHANT_API_KEYS", "test-merchant-key")
    monkeypatch.setenv("SHOPIFY_MERCHANT_PUBKEY", merchant.public_key().to_bech32())
    approved = client.post(
        f"/enrollments/{enrollment['enrollment_id']}/status",
        headers={"Authorization": "Bearer test-merchant-key"},
        json={"status": "approved"},
    )
    assert approved.status_code == 200, approved.text
    assert client.get(f"/r/{enrollment['ref_code']}", follow_redirects=False).status_code in {302, 303, 307}


def test_affiliate_updates_lightning_address_and_only_safe_pending_payouts(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    affiliate = Keys.generate()
    campaign = create_campaign(client, merchant, name="Lightning destination campaign")
    enrollment = create_enrollment(client, campaign["campaign_id"], affiliate)
    payout_id = seed_payable_payout(campaign, enrollment, affiliate)
    on_hold_payout_id = seed_payable_payout(campaign, enrollment, affiliate, amount_sats=75)
    with main.engine().begin() as connection:
        connection.execute(
            text("UPDATE payouts SET state='ON_HOLD', status='on_hold' WHERE id=:id"),
            {"id": on_hold_payout_id},
        )
    login(client, affiliate, "affiliate")

    def fake_validate(address: str):
        if address == "juang87@cash.app":
            raise main.LightningPaymentError("Lightning Address rejected the request")
        return {
            "tag": "payRequest",
            "callback": "https://example.com/lnurl/callback",
            "minSendable": 1_000,
            "maxSendable": 1_000_000,
        }

    monkeypatch.setattr(main, "validate_lightning_address", fake_validate)

    wrong_origin = client.put(
        "/app/affiliate/lightning-address",
        headers={"origin": "https://evil.example"},
        json={"lightning_address": "juan@getalby.com"},
    )
    assert wrong_origin.status_code == 403

    invalid = client.put(
        "/app/affiliate/lightning-address",
        headers={"origin": "https://testserver"},
        json={"lightning_address": "juang87@cash.app"},
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"] == "La dirección Lightning no existe o no ofrece LNURL-pay."
    with main.engine().connect() as connection:
        assert connection.execute(
            text("SELECT lightning_address FROM payouts WHERE id=:id"), {"id": payout_id}
        ).scalar_one() != "juang87@cash.app"

    saved = client.put(
        "/app/affiliate/lightning-address",
        headers={"origin": "https://testserver"},
        json={"lightning_address": "juan@getalby.com"},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["updated_payouts"] == 2

    with main.engine().connect() as connection:
        address = connection.execute(text("SELECT lightning_address FROM enrollments WHERE id=:id"), {"id": enrollment["enrollment_id"]}).scalar_one()
        payout_address = connection.execute(text("SELECT lightning_address FROM payouts WHERE id=:id"), {"id": payout_id}).scalar_one()
        on_hold_address = connection.execute(
            text("SELECT lightning_address FROM payouts WHERE id=:id"), {"id": on_hold_payout_id}
        ).scalar_one()
    assert address == "juan@getalby.com"
    assert payout_address == "juan@getalby.com"
    assert on_hold_address == "juan@getalby.com"
    assert "juan@getalby.com" in client.get("/app/affiliate").text


def test_merchant_prepares_owned_lnurl_invoice_and_qr_without_mutating_payout(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    other_merchant = Keys.generate()
    affiliate = Keys.generate()
    campaign = create_campaign(client, merchant, name="QR payout")
    enrollment = create_enrollment(client, campaign["campaign_id"], affiliate)
    payout_id = seed_payable_payout(campaign, enrollment, affiliate, amount_sats=200)
    create_campaign(client, other_merchant, name="Other QR merchant")
    observed = []

    async def fake_prepare(address: str, amount_sats: int):
        observed.append((address, amount_sats))
        return "lnbc2u1testinvoice", "ab" * 32

    monkeypatch.setattr(main, "prepare_lnurl_payment", fake_prepare)
    monkeypatch.setattr(main, "bolt11_expires_at", lambda _invoice: "2026-07-28T00:00:00+00:00")
    login(client, other_merchant, "merchant")
    hidden = client.post(
        f"/app/merchant/payouts/{payout_id}/prepare-invoice",
        headers={"origin": "https://testserver"},
    )
    assert hidden.status_code == 404
    assert observed == []

    client.post("/auth/logout")
    login(client, merchant, "merchant")
    wrong_origin = client.post(
        f"/app/merchant/payouts/{payout_id}/prepare-invoice",
        headers={"origin": "https://evil.example"},
    )
    assert wrong_origin.status_code == 403

    prepared = client.post(
        f"/app/merchant/payouts/{payout_id}/prepare-invoice",
        headers={"origin": "https://testserver"},
    )
    assert prepared.status_code == 200, prepared.text
    payload = prepared.json()
    assert payload["invoice"] == "lnbc2u1testinvoice"
    assert payload["payment_hash"] == "ab" * 32
    assert payload["amount_sats"] == 200
    assert payload["lightning_address"] == "old@example.com"
    assert payload["expires_at"] == "2026-07-28T00:00:00+00:00"
    assert payload["qr_data_uri"].startswith("data:image/svg+xml;base64,")
    svg = base64.b64decode(payload["qr_data_uri"].split(",", 1)[1]).decode()
    assert "<svg" in svg and "lnbc2u1testinvoice" not in svg
    assert prepared.headers["cache-control"] == "no-store"
    assert observed == [("old@example.com", 200)]
    throttled = client.post(
        f"/app/merchant/payouts/{payout_id}/prepare-invoice",
        headers={"origin": "https://testserver"},
    )
    assert throttled.status_code == 429
    assert observed == [("old@example.com", 200)]

    with main.engine().connect() as connection:
        payout = dict(connection.execute(text(
            "SELECT state, status, payment_provider, bolt11_invoice, payment_hash FROM payouts WHERE id=:id"
        ), {"id": payout_id}).one()._mapping)
        attempts = connection.execute(text(
            "SELECT COUNT(*) FROM payment_attempts WHERE payout_id=:id"
        ), {"id": payout_id}).scalar_one()
    assert payout == {
        "state": "PAYABLE", "status": "pending", "payment_provider": None,
        "bolt11_invoice": None, "payment_hash": None,
    }
    assert attempts == 0


def test_merchant_discards_invoice_when_payout_changes_during_lnurl(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    affiliate = Keys.generate()
    campaign = create_campaign(client, merchant, name="QR race")
    enrollment = create_enrollment(client, campaign["campaign_id"], affiliate)
    payout_id = seed_payable_payout(campaign, enrollment, affiliate, amount_sats=200)

    async def change_destination_during_prepare(_address: str, _amount_sats: int):
        with main.engine().begin() as connection:
            connection.execute(text(
                "UPDATE payouts SET lightning_address='new@example.com' WHERE id=:id"
            ), {"id": payout_id})
        return "lnbc2u1staleinvoice", "cd" * 32

    monkeypatch.setattr(main, "prepare_lnurl_payment", change_destination_during_prepare)
    login(client, merchant, "merchant")
    response = client.post(
        f"/app/merchant/payouts/{payout_id}/prepare-invoice",
        headers={"origin": "https://testserver"},
    )
    assert response.status_code == 409
    assert "cambió" in response.json()["detail"]


def test_merchant_manual_settlement_is_owned_idempotent_and_attested(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    other_merchant = Keys.generate()
    affiliate = Keys.generate()
    campaign = create_campaign(client, merchant, name="LightningKoffee")
    enrollment = create_enrollment(client, campaign["campaign_id"], affiliate)
    payout_id = seed_payable_payout(campaign, enrollment, affiliate)
    payment_hash = "ab" * 32

    other_campaign = create_campaign(client, other_merchant, name="Other merchant bootstrap")
    assert other_campaign["campaign_id"]
    login(client, other_merchant, "merchant")
    hidden = client.post(
        f"/app/merchant/payouts/{payout_id}/manual-settlement",
        headers={"origin": "https://testserver"},
        json={"payment_hash": payment_hash},
    )
    assert hidden.status_code == 404

    client.post("/auth/logout")
    login(client, merchant, "merchant")
    merchant_page = client.get("/app/merchant?view=payouts")
    assert "old@example.com" in merchant_page.text
    assert f'data-manual-payout="{payout_id}"' in merchant_page.text
    assert 'class="record payout-record"' in merchant_page.text
    assert 'class="payout-actions"' in merchant_page.text
    assert 'class="form-panel payout-form"' in merchant_page.text
    assert "Generar factura Lightning y QR" in merchant_page.text
    assert "data-prepare-invoice" in merchant_page.text
    assert "data-invoice-panel hidden" in merchant_page.text
    assert "Ya pagué · cargar el hash de la factura" in merchant_page.text
    assert "Generar la factura no realiza ni verifica el pago" in merchant_page.text
    assert "Hash de pago Lightning (64 caracteres hexadecimales)" in merchant_page.text
    assert "No pegues el ID UUID de Strike" in merchant_page.text
    assert 'data-manual-payout="' in merchant_page.text and "novalidate" in merchant_page.text
    assert 'role="status" aria-live="polite" data-manual-status' in merchant_page.text

    invalid = client.post(
        f"/app/merchant/payouts/{payout_id}/manual-settlement",
        headers={"origin": "https://testserver"},
        json={"payment_hash": "not-a-hash"},
    )
    assert invalid.status_code == 422
    paid = client.post(
        f"/app/merchant/payouts/{payout_id}/manual-settlement",
        headers={"origin": "https://testserver"},
        json={"payment_hash": payment_hash},
    )
    assert paid.status_code == 200, paid.text
    assert paid.json()["payout_state"] == "PUBLISHED"
    tags = paid.json()["nostr_event"]["tags"]
    assert ["settlement_mode", "manual"] in tags
    assert ["evidence", "merchant_attestation"] in tags
    assert not any(tag[0] == "preimage" for tag in tags)
    receipt = client.get(f"/payouts/{payout_id}/receipt")
    assert receipt.status_code == 200
    assert 'Non-sandbox payout receipt' in receipt.text
    assert 'data-proof-sandbox="non-sandbox"' in receipt.text
    assert 'merchant_attestation' in receipt.text
    assert 'Merchant attestation is not trustless settlement evidence.' in receipt.text
    assert 'No payment preimage is disclosed by this receipt.' in receipt.text
    assert 'merchant_account:' not in receipt.text
    assert 'data-event-verified="true"' in receipt.text

    replay = client.post(
        f"/app/merchant/payouts/{payout_id}/manual-settlement",
        headers={"origin": "https://testserver"},
        json={"payment_hash": payment_hash.upper()},
    )
    assert replay.status_code == 200
    assert replay.json()["duplicate"] is True
    prepare_after_paid = client.post(
        f"/app/merchant/payouts/{payout_id}/prepare-invoice",
        headers={"origin": "https://testserver"},
    )
    assert prepare_after_paid.status_code == 409
    with main.engine().connect() as connection:
        attempts = connection.execute(text("SELECT COUNT(*) FROM payment_attempts WHERE payout_id=:id AND rail='manual'"), {"id": payout_id}).scalar_one()
        stored_preimages = connection.execute(text("SELECT COUNT(*) FROM payment_attempts WHERE payout_id=:id AND preimage IS NOT NULL"), {"id": payout_id}).scalar_one()
    assert attempts == 1
    assert stored_preimages == 0


def test_manual_settlement_persists_outbox_before_publish_and_retries_same_event(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    affiliate = Keys.generate()
    campaign = create_campaign(client, merchant, name="Outbox payout")
    enrollment = create_enrollment(client, campaign["campaign_id"], affiliate)
    payout_id = seed_payable_payout(campaign, enrollment, affiliate)
    payment_hash = "cd" * 32
    login(client, merchant, "merchant")

    def relay_crash(_event):
        raise RuntimeError("simulated relay outage")

    monkeypatch.setattr(main, "publish_event", relay_crash)
    with pytest.raises(RuntimeError, match="simulated relay outage"):
        client.post(
            f"/app/merchant/payouts/{payout_id}/manual-settlement",
            headers={"origin": "https://testserver"},
            json={"payment_hash": payment_hash},
        )

    with main.engine().connect() as connection:
        durable = dict(connection.execute(text(
            "SELECT state, status, payment_hash, nostr_event_id, nostr_event_json, reserved_sats FROM payouts WHERE id=:id"
        ), {"id": payout_id}).one()._mapping)
        outbox = connection.execute(text(
            "SELECT event_json, relay_status FROM nostr_events WHERE event_id=:id"
        ), {"id": durable["nostr_event_id"]}).one()
    first_event_id = durable["nostr_event_id"]
    assert durable["state"] == "SETTLED"
    assert durable["status"] == "paid"
    assert durable["reserved_sats"] == 0
    assert outbox.relay_status == "unknown"

    monkeypatch.setattr(main, "publish_event", lambda _event: [
        {"relay": "wss://relay.test", "status": "published", "error": None}
    ])
    retried = client.post(
        f"/app/merchant/payouts/{payout_id}/manual-settlement",
        headers={"origin": "https://testserver"},
        json={"payment_hash": payment_hash},
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["duplicate"] is True
    assert retried.json()["payout_state"] == "PUBLISHED"
    assert retried.json()["nostr_event_id"] == first_event_id
    with main.engine().connect() as connection:
        final = connection.execute(text(
            "SELECT state, reserved_sats FROM payouts WHERE id=:id"
        ), {"id": payout_id}).one()
    assert final.state == "PUBLISHED"
    assert final.reserved_sats == 0


def test_app_language_selector_persists_english_and_keeps_spanish_default(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)

    spanish = client.get("/app?role=merchant&lang=es")
    assert spanish.status_code == 200
    assert '<html lang="es">' in spanish.text
    assert "Cuenta de comerciante" in spanish.text
    assert 'lang="en"' in spanish.text and ">EN<" in spanish.text
    assert spanish.headers["content-language"] == "es"

    english = client.get("/app?role=merchant&lang=en")
    assert english.status_code == 200
    assert '<html lang="en">' in english.text
    assert "Merchant account" in english.text
    assert "Continue with a Nostr app" in english.text
    assert "Use another Nostr app or QR" not in english.text
    assert "On mobile, we open your Nostr app" in english.text
    assert "The operation could not be completed. Try again." in english.text
    assert ">Cuenta de comerciante<" not in english.text
    assert english.headers["content-language"] == "en"
    assert "meerat_lang=en" in english.headers["set-cookie"]

    persisted = client.get("/app?role=affiliate")
    assert '<html lang="en">' in persisted.text
    assert "Affiliate account" in persisted.text
    assert persisted.headers["content-language"] == "en"

    restored = client.get("/app?role=affiliate&lang=es")
    assert '<html lang="es">' in restored.text
    assert "Cuenta de afiliado" in restored.text


def test_authenticated_merchant_and_affiliate_views_render_in_english(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    affiliate = Keys.generate()
    campaign = create_campaign(client, merchant, name="Lightning Koffee")
    create_enrollment(client, campaign["campaign_id"], affiliate)

    login(client, merchant, "merchant")
    merchant_page = client.get("/app/merchant?view=integration&lang=en")
    assert merchant_page.status_code == 200
    assert '<html lang="en">' in merchant_page.text
    assert "Merchant account" in merchant_page.text
    assert "Shopify theme script" in merchant_page.text
    assert ">Script del tema de Shopify<" not in merchant_page.text
    assert "Lightning Koffee" in merchant_page.text

    client.post("/auth/logout")
    seed_verified_affiliate_profile(affiliate)
    login(client, affiliate, "affiliate")
    affiliate_page = client.get("/app/affiliate?view=links&lang=en")
    assert affiliate_page.status_code == 200
    assert '<html lang="en">' in affiliate_page.text
    assert "Affiliate account" in affiliate_page.text
    assert "My links" in affiliate_page.text
    assert ">Mis enlaces<" not in affiliate_page.text
    assert "Lightning Koffee" in affiliate_page.text


def test_all_merchant_views_render_application_copy_in_english(tmp_path, monkeypatch):
    from app.i18n import translate_html, translate_text
    from html.parser import HTMLParser

    class VisibleTextParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.hidden_depth = 0
            self.parts = []

        def handle_starttag(self, tag, attrs):
            if tag in {"script", "style"}:
                self.hidden_depth += 1

        def handle_endtag(self, tag):
            if tag in {"script", "style"} and self.hidden_depth:
                self.hidden_depth -= 1

        def handle_data(self, data):
            if not self.hidden_depth and data.strip():
                self.parts.append(data.strip())

    client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    approved_affiliate = Keys.generate()
    pending_affiliate = Keys.generate()
    private_campaign = create_campaign(client, merchant, name="Privada", enrollment_mode="private")
    approval_campaign = create_campaign(client, merchant, name="Approval Rewards", enrollment_mode="approval")
    create_campaign(client, merchant, name="Open Rewards", enrollment_mode="open")
    approved = create_enrollment(client, private_campaign["campaign_id"], approved_affiliate)
    pending = create_enrollment(client, approval_campaign["campaign_id"], pending_affiliate)
    with main.engine().begin() as connection:
        connection.execute(
            text("UPDATE enrollments SET status='pending' WHERE id=:id"),
            {"id": pending["enrollment_id"]},
        )
    seed_payable_payout(private_campaign, approved, approved_affiliate, amount_sats=321)

    monkeypatch.setenv("SHOPIFY_MERCHANT_PUBKEY", merchant.public_key().to_bech32())
    login(client, merchant, "merchant")
    pages = {
        view: client.get(f"/app/merchant?view={view}&lang=en")
        for view in ("overview", "campaigns", "affiliates", "activity", "payouts", "integration", "settings")
    }
    assert all(page.status_code == 200 for page in pages.values())

    expected = {
        "overview": ("Next actions", "Clicks, conversions, and commissions."),
        "campaigns": ("Invitation only", "Save mode", "Commission (%)", "Save commission", "30-day attribution window"),
        "affiliates": ("Pending applications", "Approved npubs linked to your programs.", "Approve", "Reject"),
        "activity": ("Confirmed conversions and commissions.",),
        "payouts": ("Generate Lightning invoice and QR", "Lightning evidence (64 hexadecimal characters)", "This records a merchant-signed declaration."),
        "integration": ("Shopify orders/paid webhook", "Shopify theme script", "Create a pixel under"),
        "settings": ("Public PNG, JPG, or WebP. If omitted, we use the initials.", "Join the affiliate program…"),
    }
    for view, phrases in expected.items():
        for phrase in phrases:
            assert phrase in pages[view].text, f"{view}: missing {phrase}"

    forbidden = (
        "Npubs aprobados y vinculados", "Solicitudes pendientes", "Revisá cada identidad",
        "Guardar modo", "Guardar comisión", "Aplica a conversiones registradas", "Solo invitación", "Requiere aprobación", "Inscripción abierta",
        "ventana 30 días", "Conversiones y comisiones confirmadas", "Evidencia Lightning",
        "Esto registra una declaración", "Webhook orders/paid de Shopify", "Recibiendo eventos",
        "Creá un píxel", "Configurá este endpoint", "PNG, JPG o WebP público. Si falta",
        "campañas", "afiliados", "comerciante", "Configuración", "Copiar", "Generar",
        "Guardá", "dirección Lightning", "factura Lightning", "píxel", "invitación",
        "solicitud", "pagos", "condiciones", "aprobados",
    )
    visible = {}
    for view, page in pages.items():
        parser = VisibleTextParser()
        parser.feed(page.text)
        visible[view] = " ".join(parser.parts)

    for view, page_text in visible.items():
        for phrase in forbidden:
            assert phrase not in page_text, f"{view}: leaked Spanish copy {phrase}"

    assert 'data-i18n-ignore>Privada</strong>' in pages["campaigns"].text
    assert translate_html('<h3 data-i18n-ignore>Privada</h3><span>Privada</span>', "en") == '<h3 data-i18n-ignore>Privada</h3><span>Private</span>'
    assert translate_text("1 webhook de orders/paid aprobado", "en") == "1 approved orders/paid webhook"
    assert translate_text("2 webhooks de orders/paid aprobados", "en") == "2 approved orders/paid webhooks"
    assert translate_text("1 pago requiere atención.", "en") == "1 payment needs attention."
    assert translate_text("2 pagos requieren atención.", "en") == "2 payments need attention."
    assert translate_text("1 eventos asociados a tus campañas.", "en") == "1 event associated with your campaigns."
    assert translate_text("de 1 programas", "en") == "out of 1 program"


def test_open_campaign_join_requires_fresh_signed_challenge_and_creates_session(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    affiliate = Keys.generate()
    campaign = create_campaign(client, merchant, enrollment_mode="open")
    campaign_id = campaign["campaign_id"]

    page = client.get(f"/campaigns/{campaign_id}/join?lang=en")
    assert page.status_code == 200
    assert "data-affiliate-lander" in page.text
    assert "Create my affiliate link" in page.text
    assert 'data-invite-language="es"' in page.text
    assert 'data-invite-language="en"' in page.text
    assert 'data-join-method="auto"' in page.text
    assert 'data-join-method="nip46"' in page.text
    assert "Attribution window" in page.text
    assert "By continuing, you accept this program's terms." in page.text
    assert "/static/campaign-join.js?v=20260803-invite-unified1" in page.text

    challenge_response = client.post(
        f"/campaigns/{campaign_id}/join/challenge", headers={"Origin": "https://testserver"}, json={}
    )
    assert challenge_response.status_code == 200
    challenge = challenge_response.json()
    assert challenge["role"] == f"campaign_join:{campaign_id}"
    event = signed_login_event(affiliate, challenge)

    joined = client.post(
        f"/campaigns/{campaign_id}/join",
        headers={"Origin": "https://testserver"},
        json={"event": event},
    )
    assert joined.status_code == 200, joined.text
    assert joined.json()["status"] == "approved"
    assert joined.json()["ref_url"].endswith(joined.json()["ref_code"])
    assert "meerat_session=" in joined.headers["set-cookie"]
    assert client.get("/auth/me").json()["account"]["role"] == "affiliate"

    replay = client.post(
        f"/campaigns/{campaign_id}/join",
        headers={"Origin": "https://testserver"},
        json={"event": event},
    )
    assert replay.status_code == 409


def test_approval_campaign_stays_pending_until_merchant_decides(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    affiliate = Keys.generate()
    campaign = create_campaign(client, merchant, enrollment_mode="approval")
    campaign_id = campaign["campaign_id"]
    challenge = client.post(
        f"/campaigns/{campaign_id}/join/challenge", headers={"Origin": "https://testserver"}, json={}
    ).json()

    requested = client.post(
        f"/campaigns/{campaign_id}/join",
        headers={"Origin": "https://testserver"},
        json={"event": signed_login_event(affiliate, challenge)},
    )
    assert requested.status_code == 202, requested.text
    enrollment_id = requested.json()["enrollment_id"]
    assert requested.json()["status"] == "pending"
    assert "meerat_session=" not in requested.headers.get("set-cookie", "")

    login(client, merchant, "merchant")
    queue = client.get("/app/merchant?view=affiliates")
    assert queue.status_code == 200
    assert "Solicitudes pendientes" in queue.text
    assert affiliate.public_key().to_bech32() in queue.text

    approved = client.post(
        f"/app/merchant/enrollments/{enrollment_id}/decision",
        headers={"Origin": "https://testserver"},
        json={"status": "approved"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    with main.engine().begin() as connection:
        row = connection.execute(
            text("SELECT status, nostr_event_json FROM enrollments WHERE id=:id"), {"id": enrollment_id}
        ).mappings().one()
    assert row["status"] == "approved"
    assert ["status", "approved"] in json.loads(row["nostr_event_json"])["tags"]


def test_merchant_can_update_commission_and_only_future_conversions_use_it(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    affiliate = Keys.generate()
    campaign = create_campaign(client, merchant, name="Lightning Koffee", enrollment_mode="private")
    enrollment = create_enrollment(client, campaign["campaign_id"], affiliate)
    first_click = client.post("/clicks/simulate", json={"ref_code": enrollment["ref_code"]}).json()["click_id"]
    first = client.post(
        "/conversions",
        json={"order_id": "before-rate-change", "click_id": first_click, "order_total": 100000, "currency": "SATS"},
    )
    assert first.status_code == 200, first.text
    first_data = first.json()
    assert first_data["commission_sats"] == 8000
    assert ["commission_bps", "800"] in first_data["nostr_event"]["tags"]
    with main.engine().connect() as connection:
        first_conversion_before = dict(connection.execute(
            text("SELECT commission_sats, nostr_event_json FROM conversions WHERE id=:id"),
            {"id": first_data["conversion_id"]},
        ).mappings().one())
        first_payout_before = dict(connection.execute(
            text("SELECT amount_sats, fee_sats, reserved_sats FROM payouts WHERE conversion_id=:id"),
            {"id": first_data["conversion_id"]},
        ).mappings().one())

    login(client, merchant, "merchant")
    changed = client.put(
        f"/app/merchant/campaigns/{campaign['campaign_id']}/commission",
        headers={"Origin": "https://testserver"},
        json={"commission_percent": "12.50"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["commission_percent"] == "12.5"
    assert changed.json()["commission_bps"] == 1250
    assert changed.json()["duplicate"] is False
    duplicate = client.put(
        f"/app/merchant/campaigns/{campaign['campaign_id']}/commission",
        headers={"Origin": "https://testserver"},
        json={"commission_percent": "12.5"},
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True

    second_click = client.post("/clicks/simulate", json={"ref_code": enrollment["ref_code"]}).json()["click_id"]
    second = client.post(
        "/conversions",
        json={"order_id": "after-rate-change", "click_id": second_click, "order_total": 100000, "currency": "SATS"},
    )
    assert second.status_code == 200, second.text
    second_data = second.json()
    assert second_data["commission_sats"] == 12500
    assert ["commission_bps", "1250"] in second_data["nostr_event"]["tags"]
    with main.engine().connect() as connection:
        first_conversion_after = dict(connection.execute(
            text("SELECT commission_sats, nostr_event_json FROM conversions WHERE id=:id"),
            {"id": first_data["conversion_id"]},
        ).mappings().one())
        first_payout_after = dict(connection.execute(
            text("SELECT amount_sats, fee_sats, reserved_sats FROM payouts WHERE conversion_id=:id"),
            {"id": first_data["conversion_id"]},
        ).mappings().one())
        stored = connection.execute(
            text("SELECT commission_sats FROM conversions ORDER BY created_at, id")
        ).scalars().all()
        campaign_row = connection.execute(
            text("SELECT commission_bps, nostr_event_json FROM campaigns WHERE id=:id"),
            {"id": campaign["campaign_id"]},
        ).fetchone()
    assert first_conversion_after == first_conversion_before
    assert first_payout_after == first_payout_before
    assert sorted(stored) == [8000, 12500]
    assert campaign_row.commission_bps == 1250
    event = json.loads(campaign_row.nostr_event_json)
    assert ["commission_bps", "1250"] in event["tags"]

    page = client.get("/app/merchant?view=campaigns&lang=en")
    assert page.status_code == 200
    assert 'class="record campaign-mode-record campaign-card"' in page.text
    assert 'class="campaign-card__header"' in page.text
    assert 'class="campaign-card__settings"' in page.text
    assert 'class="campaign-card__actions"' in page.text
    assert 'name="commission_percent"' in page.text
    assert 'value="12.5"' in page.text
    assert "Commission (%)" in page.text
    assert "Save commission" in page.text
    assert "Applies to conversions recorded from now on" not in page.text
    assert "Existing conversions and payouts will not change." not in page.text


def test_commission_change_serializes_with_conversion_ingestion(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    affiliate = Keys.generate()
    campaign = create_campaign(client, merchant, name="Serialized rate")
    enrollment = create_enrollment(client, campaign["campaign_id"], affiliate)
    click_id = client.post("/clicks/simulate", json={"ref_code": enrollment["ref_code"]}).json()["click_id"]
    login(client, merchant, "merchant")

    entered = threading.Event()
    release = threading.Event()
    original_build = main.build_campaign_event

    def blocking_build(campaign_row, terms_url=None):
        if int(campaign_row["commission_bps"]) == 1250:
            entered.set()
            assert release.wait(timeout=5)
        return original_build(campaign_row, terms_url)

    monkeypatch.setattr(main, "build_campaign_event", blocking_build)
    results = {}

    def update_rate():
        results["update"] = client.put(
            f"/app/merchant/campaigns/{campaign['campaign_id']}/commission",
            headers={"Origin": "https://testserver"},
            json={"commission_percent": "12.5"},
        )

    def record_conversion():
        conversion_client = TestClient(main.app, base_url="https://testserver")
        results["conversion"] = conversion_client.post(
            "/conversions",
            json={"order_id": "concurrent-rate-boundary", "click_id": click_id, "order_total": 100000, "currency": "SATS"},
        )

    update_thread = threading.Thread(target=update_rate)
    conversion_thread = threading.Thread(target=record_conversion)
    update_thread.start()
    assert entered.wait(timeout=5)
    conversion_thread.start()
    time.sleep(0.1)
    assert conversion_thread.is_alive()
    release.set()
    update_thread.join(timeout=5)
    conversion_thread.join(timeout=5)
    assert not update_thread.is_alive()
    assert not conversion_thread.is_alive()

    assert results["update"].status_code == 200
    assert results["conversion"].status_code == 200
    assert results["conversion"].json()["commission_sats"] == 12500


def test_campaign_commission_events_are_monotonic_and_stale_finalization_is_safe(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    campaign = create_campaign(client, merchant, name="Monotonic campaign")
    login(client, merchant, "merchant")

    first = client.put(
        f"/app/merchant/campaigns/{campaign['campaign_id']}/commission",
        headers={"Origin": "https://testserver"},
        json={"commission_percent": "12"},
    )
    second = client.put(
        f"/app/merchant/campaigns/{campaign['campaign_id']}/commission",
        headers={"Origin": "https://testserver"},
        json={"commission_percent": "13"},
    )
    assert first.status_code == second.status_code == 200
    first_event = first.json()["nostr_event"]
    second_event = second.json()["nostr_event"]
    assert second_event["created_at"] > first_event["created_at"]

    main.finalize_committed_nostr_event(first_event, "campaign", campaign["campaign_id"])
    with main.engine().connect() as connection:
        row = connection.execute(
            text("SELECT commission_bps, nostr_event_id, nostr_event_json FROM campaigns WHERE id=:id"),
            {"id": campaign["campaign_id"]},
        ).mappings().one()
    assert row["commission_bps"] == 1300
    assert row["nostr_event_id"] == second_event["id"]
    assert json.loads(row["nostr_event_json"])["id"] == second_event["id"]


def test_merchant_commission_update_validates_input_and_tenant_ownership(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    owner = Keys.generate()
    stranger = Keys.generate()
    campaign = create_campaign(client, owner, name="Owner campaign")
    create_campaign(client, stranger, name="Stranger campaign")
    login(client, stranger, "merchant")

    denied = client.put(
        f"/app/merchant/campaigns/{campaign['campaign_id']}/commission",
        headers={"Origin": "https://testserver"},
        json={"commission_percent": "10"},
    )
    assert denied.status_code == 404
    login(client, owner, "merchant")
    for invalid in ("0", "100.01", "12.345", "not-a-number"):
        response = client.put(
            f"/app/merchant/campaigns/{campaign['campaign_id']}/commission",
            headers={"Origin": "https://testserver"},
            json={"commission_percent": invalid},
        )
        assert response.status_code == 422
    with main.engine().begin() as connection:
        connection.execute(
            text("UPDATE campaigns SET status='paused' WHERE id=:id"),
            {"id": campaign["campaign_id"]},
        )
    inactive = client.put(
        f"/app/merchant/campaigns/{campaign['campaign_id']}/commission",
        headers={"Origin": "https://testserver"},
        json={"commission_percent": "10"},
    )
    assert inactive.status_code == 409
    with main.engine().connect() as connection:
        bps = connection.execute(
            text("SELECT commission_bps FROM campaigns WHERE id=:id"),
            {"id": campaign["campaign_id"]},
        ).scalar_one()
    assert bps == 800


def test_private_campaign_rejects_public_join_and_non_owner_cannot_change_mode(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    owner = Keys.generate()
    stranger = Keys.generate()
    campaign = create_campaign(client, owner, enrollment_mode="private")
    campaign_id = campaign["campaign_id"]

    assert client.get(f"/campaigns/{campaign_id}/join").status_code == 404
    assert client.post(
        f"/campaigns/{campaign_id}/join/challenge", headers={"Origin": "https://testserver"}, json={}
    ).status_code == 404

    create_campaign(client, stranger, name="Stranger campaign")
    login(client, stranger, "merchant")
    denied = client.put(
        f"/app/merchant/campaigns/{campaign_id}/enrollment-mode",
        headers={"Origin": "https://testserver"},
        json={"enrollment_mode": "open"},
    )
    assert denied.status_code == 404


def test_switching_private_campaign_revokes_old_invitations(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    campaign = create_campaign(client, merchant, enrollment_mode="private")
    campaign_id = campaign["campaign_id"]
    login(client, merchant, "merchant")
    invitation = create_invitation(client, campaign_id)
    token = invitation["invite_url"].split("#token=", 1)[1]

    changed = client.put(
        f"/app/merchant/campaigns/{campaign_id}/enrollment-mode",
        headers={"Origin": "https://testserver"},
        json={"enrollment_mode": "approval"},
    )
    assert changed.status_code == 200, changed.text
    resolved = client.post(
        "/invite/resolve",
        headers={"Origin": "https://testserver"},
        json={"token": token},
    )
    assert resolved.status_code == 409
    with main.engine().connect() as connection:
        status = connection.execute(
            text("SELECT status FROM affiliate_invitations WHERE id=:id"),
            {"id": invitation["invitation_id"]},
        ).scalar_one()
    assert status == "revoked"


def test_open_enrollment_outbox_survives_post_commit_publication_gap(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    affiliate = Keys.generate()
    campaign = create_campaign(client, merchant, enrollment_mode="open")
    campaign_id = campaign["campaign_id"]
    challenge = client.post(
        f"/campaigns/{campaign_id}/join/challenge",
        headers={"Origin": "https://testserver"},
        json={},
    ).json()
    real_finalize = main.finalize_committed_nostr_event
    monkeypatch.setattr(main, "finalize_committed_nostr_event", lambda *_args, **_kwargs: [])

    joined = client.post(
        f"/campaigns/{campaign_id}/join",
        headers={"Origin": "https://testserver"},
        json={"event": signed_login_event(affiliate, challenge)},
    )
    assert joined.status_code == 200, joined.text
    enrollment_id = joined.json()["enrollment_id"]
    with main.engine().connect() as connection:
        relay_status = connection.execute(
            text("SELECT relay_status FROM nostr_events WHERE entity_type='enrollment' AND entity_id=:id"),
            {"id": enrollment_id},
        ).scalar_one()
    assert relay_status == "pending_publication"

    monkeypatch.setattr(main, "finalize_committed_nostr_event", real_finalize)
    main.retry_pending_nostr_outbox()
    with main.engine().connect() as connection:
        recovered = connection.execute(
            text("SELECT relay_status FROM nostr_events WHERE entity_type='enrollment' AND entity_id=:id"),
            {"id": enrollment_id},
        ).scalar_one()
    assert recovered == "skipped"
