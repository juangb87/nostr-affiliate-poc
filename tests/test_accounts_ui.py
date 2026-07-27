import json

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
    return TestClient(main.app, base_url="https://testserver")


def signed_login_event(keys: Keys, challenge: dict, *, role: str | None = None, relay: str | None = None) -> dict:
    event = (
        EventBuilder(Kind(22242), "")
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


def create_campaign(client: TestClient, merchant: Keys, *, name: str = "Merchant campaign") -> dict:
    response = client.post(
        "/campaigns",
        json={
            "merchant_pubkey": merchant.public_key().to_bech32(),
            "name": name,
            "commission_bps": 800,
            "attribution_window_days": 30,
            "destination_url": "https://merchant.example/shop",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def create_enrollment(client: TestClient, campaign_id: str, affiliate: Keys) -> dict:
    return main._create_enrollment_record(
        main.EnrollmentIn(
            campaign_id=campaign_id,
            affiliate_pubkey=affiliate.public_key().to_bech32(),
            lightning_address="affiliate@example.com",
        )
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
    assert response.json()["detail"] == "merchant account binding configuration is invalid"
    assert client.get("/auth/me").status_code == 401


def test_challenge_is_one_use_and_role_origin_are_bound(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    create_campaign(client, merchant)

    challenge = client.post("/auth/nostr/challenge", json={"role": "merchant"}).json()
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
    merchant_page = merchant_client.get("/app/merchant")
    assert merchant_page.status_code == 200
    assert "Merchant account" in merchant_page.text
    assert "Private merchant campaign" in merchant_page.text
    assert "affiliate@example.com" not in merchant_page.text
    assert merchant_client.get("/app/affiliate", follow_redirects=False).status_code in {302, 303, 307}

    affiliate_client = TestClient(main.app, base_url="https://testserver")
    login(affiliate_client, affiliate, "affiliate")
    affiliate_page = affiliate_client.get("/app/affiliate")
    assert affiliate_page.status_code == 200
    assert "Affiliate account" in affiliate_page.text
    assert "Private merchant campaign" in affiliate_page.text
    assert "/r/" in affiliate_page.text
    assert affiliate_client.get("/app/merchant", follow_redirects=False).status_code in {302, 303, 307}


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
    assert "Nostr Affiliate POC Dashboard" in ops.text
    ops_data = client.get("/ops/data")
    assert ops_data.status_code == 200
    assert "counts" in ops_data.json()


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

    invitation = create_invitation(client, campaign["campaign_id"])

    assert invitation["invite_url"].startswith("https://testserver/invite#token=")
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
    assert token not in page.text
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
    assert payload["redirect"] == "/app/affiliate"
    assert payload["ref_url"].startswith("https://testserver/r/")
    assert main.SESSION_COOKIE.lower() in accepted.headers["set-cookie"].lower()
    workspace = client.get("/app/affiliate")
    assert workspace.status_code == 200
    assert "Signed invitation campaign" in workspace.text

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
    page = client.get("/app/merchant")
    assert page.status_code == 200
    assert "Bound merchant campaign" in page.text
    assert 'data-merchant-invitation' in page.text
    assert 'name="affiliate_pubkey"' not in page.text
    assert 'value="' + campaign["campaign_id"] + '"' in page.text


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
    alpha_page = client.get("/app/merchant")
    assert "Only Merchant Alpha" in alpha_page.text
    assert "Only Merchant Beta" not in alpha_page.text

    client.post("/auth/logout")
    login(client, merchant_b, "merchant")
    beta_page = client.get("/app/merchant")
    assert "Only Merchant Beta" in beta_page.text
    assert "Only Merchant Alpha" not in beta_page.text


def test_ops_dashboard_escapes_untrusted_table_values(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    attacker = Keys.generate()
    create_campaign(client, attacker, name='<img src=x onerror="alert(1)">')
    operator = Keys.generate()
    monkeypatch.setenv("OPS_NOSTR_PUBKEYS", operator.public_key().to_bech32())
    login(client, operator, "ops")

    source = client.get("/ops").text
    assert "function esc(value)" in source
    assert "c[2]?c[2](r[c[1]],r):esc(r[c[1]])" in source
    assert "safePath(v)" in source


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


def test_affiliate_updates_lightning_address_and_only_safe_pending_payouts(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    affiliate = Keys.generate()
    campaign = create_campaign(client, merchant, name="Lightning destination campaign")
    enrollment = create_enrollment(client, campaign["campaign_id"], affiliate)
    payout_id = seed_payable_payout(campaign, enrollment, affiliate)
    login(client, affiliate, "affiliate")

    wrong_origin = client.put(
        "/app/affiliate/lightning-address",
        headers={"origin": "https://evil.example"},
        json={"lightning_address": "juan@getalby.com"},
    )
    assert wrong_origin.status_code == 403
    saved = client.put(
        "/app/affiliate/lightning-address",
        headers={"origin": "https://testserver"},
        json={"lightning_address": "juan@getalby.com"},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["updated_payouts"] == 1

    with main.engine().connect() as connection:
        address = connection.execute(text("SELECT lightning_address FROM enrollments WHERE id=:id"), {"id": enrollment["enrollment_id"]}).scalar_one()
        payout_address = connection.execute(text("SELECT lightning_address FROM payouts WHERE id=:id"), {"id": payout_id}).scalar_one()
    assert address == "juan@getalby.com"
    assert payout_address == "juan@getalby.com"
    assert "juan@getalby.com" in client.get("/app/affiliate").text


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
    merchant_page = client.get("/app/merchant")
    assert "old@example.com" in merchant_page.text
    assert f'data-manual-payout="{payout_id}"' in merchant_page.text
    assert 'class="record payout-record"' in merchant_page.text
    assert 'class="payout-actions"' in merchant_page.text
    assert 'class="form-panel payout-form"' in merchant_page.text
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

    replay = client.post(
        f"/app/merchant/payouts/{payout_id}/manual-settlement",
        headers={"origin": "https://testserver"},
        json={"payment_hash": payment_hash.upper()},
    )
    assert replay.status_code == 200
    assert replay.json()["duplicate"] is True
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
