import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from nostr_sdk import EventBuilder, Keys, Kind, Tag
from sqlalchemy import text

from app import main
from app.account_erasure import identity_erasure_hmac, is_nostr_identity_erased


def configured_client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/erasure.db")
    monkeypatch.setenv("NOSTR_PUBLISH", "false")
    monkeypatch.setenv("APP_SECRET", "test-app-secret-with-enough-entropy")
    monkeypatch.setenv("ACCOUNT_ERASURE_PEPPER", "test-erasure-pepper-with-enough-entropy")
    monkeypatch.setattr(main, "BASE_URL", "https://testserver")
    main._ENGINE = None
    main._ENGINE_URL = None
    return TestClient(main.app, base_url="https://testserver")


def signed_event(keys: Keys, challenge: dict) -> dict:
    event = (
        EventBuilder(Kind(challenge.get("kind", main.AUTH_EVENT_KIND)), "")
        .tags(
            [
                Tag.parse(["challenge", challenge["challenge"]]),
                Tag.parse(["relay", challenge["relay"]]),
                Tag.parse(["role", challenge["role"]]),
            ]
        )
        .sign_with_keys(keys)
    )
    return json.loads(event.as_json())


def insert_tombstone(connection, affiliate: Keys) -> None:
    connection.execute(
        text(
            """
            INSERT INTO erased_nostr_identities
              (identity_hmac,anonymous_pubkey,anonymous_pubkey_hex,erased_at,reason)
            VALUES (:digest,:npub,:hex,:now,'user_request')
            """
        ),
        {
            "digest": identity_erasure_hmac(affiliate.public_key().to_hex()),
            "npub": Keys.generate().public_key().to_bech32(),
            "hex": Keys.generate().public_key().to_hex(),
            "now": main.now(),
        },
    )


def test_erased_identity_cannot_log_in_or_recreate_account(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    affiliate = Keys.generate()
    original_hex = affiliate.public_key().to_hex()

    main.init_db()
    with main.engine().begin() as connection:
        insert_tombstone(connection, affiliate)
        assert is_nostr_identity_erased(connection, original_hex)

    challenge = client.post("/auth/nostr/challenge", json={"role": "affiliate"}).json()
    response = client.post("/auth/nostr/verify", json={"event": signed_event(affiliate, challenge)})

    assert response.status_code == 403
    assert response.json()["detail"] == "Esta identidad Nostr fue eliminada y no puede volver a registrarse."
    with main.engine().connect() as connection:
        assert connection.execute(
            text("SELECT COUNT(*) FROM accounts WHERE nostr_pubkey_hex=:hex"), {"hex": original_hex}
        ).scalar_one() == 0


def test_erased_identity_cannot_join_open_campaign_or_create_affiliate_session(tmp_path, monkeypatch):
    client = configured_client(tmp_path, monkeypatch)
    merchant = Keys.generate()
    affiliate = Keys.generate()
    campaign = client.post(
        "/campaigns",
        json={
            "merchant_pubkey": merchant.public_key().to_bech32(),
            "name": "Preserved merchant",
            "commission_bps": 800,
            "attribution_window_days": 30,
            "destination_url": "https://merchant.example/shop",
            "enrollment_mode": "open",
        },
    ).json()
    with main.engine().begin() as connection:
        insert_tombstone(connection, affiliate)
        with pytest.raises(HTTPException, match="identidad Nostr fue eliminada"):
            main._create_affiliate_session(
                connection,
                {"hex": affiliate.public_key().to_hex(), "npub": affiliate.public_key().to_bech32()},
            )

    challenge = client.post(
        f"/campaigns/{campaign['campaign_id']}/join/challenge",
        headers={"Origin": "https://testserver"},
        json={},
    ).json()
    response = client.post(
        f"/campaigns/{campaign['campaign_id']}/join",
        headers={"Origin": "https://testserver"},
        json={"event": signed_event(affiliate, challenge)},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Esta identidad Nostr fue eliminada y no puede volver a registrarse."
    with main.engine().connect() as connection:
        assert connection.execute(
            text("SELECT COUNT(*) FROM enrollments WHERE affiliate_pubkey_hex=:hex"),
            {"hex": affiliate.public_key().to_hex()},
        ).scalar_one() == 0


def test_erasure_requires_dedicated_pepper(monkeypatch):
    monkeypatch.delenv("ACCOUNT_ERASURE_PEPPER", raising=False)
    with pytest.raises(RuntimeError, match="ACCOUNT_ERASURE_PEPPER"):
        identity_erasure_hmac("ab" * 32)
