from fastapi.testclient import TestClient
from nostr_sdk import Keys
from sqlalchemy import text

import app.main as main


def _create_campaign(client: TestClient, name: str, destination: str) -> dict:
    response = client.post(
        "/campaigns",
        json={
            "merchant_pubkey": main.DEFAULT_MERCHANT_NPUB,
            "name": name,
            "commission_bps": 800,
            "attribution_window_days": 30,
            "destination_url": destination,
            "terms_url": "https://merchant.example/private-terms",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_campaign_directory_lists_only_active_unarchived_campaigns_without_private_data(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/directory.db")
    monkeypatch.setenv("NOSTR_PUBLISH", "false")
    client = TestClient(main.app)

    active = _create_campaign(client, "Active <script>alert(1)</script>", "https://active.example/secret-destination")
    paused = _create_campaign(client, "Paused campaign", "https://paused.example")
    ended = _create_campaign(client, "Ended campaign", "https://ended.example")
    archived = _create_campaign(client, "Archived campaign", "https://archived.example")

    timestamp = main.now()
    with main.engine().begin() as connection:
        connection.execute(text("UPDATE campaigns SET status='paused' WHERE id=:id"), {"id": paused["campaign_id"]})
        connection.execute(text("UPDATE campaigns SET status='ended' WHERE id=:id"), {"id": ended["campaign_id"]})
        connection.execute(
            text("UPDATE campaigns SET archived_at=:archived_at WHERE id=:id"),
            {"id": archived["campaign_id"], "archived_at": timestamp},
        )
        connection.execute(
            text(
                """
                INSERT INTO merchant_profiles (
                    merchant_pubkey_hex, merchant_pubkey, display_name, tagline, logo_url, created_at, updated_at
                ) VALUES (:hex, :npub, :display_name, :tagline, :logo_url, :created_at, :updated_at)
                """
            ),
            {
                "hex": active["merchant_pubkey_hex"],
                "npub": active["merchant_pubkey"],
                "display_name": "Merchant & Friends",
                "tagline": "Value for value",
                "logo_url": "https://cdn.example/merchant.webp",
                "created_at": timestamp,
                "updated_at": timestamp,
            },
        )

    legacy_affiliate = Keys.generate().public_key().to_bech32()
    legacy_enrollment = main._create_enrollment_record(
        main.EnrollmentIn(campaign_id=active["campaign_id"], affiliate_pubkey=legacy_affiliate)
    )
    with main.engine().begin() as connection:
        connection.execute(
            text("UPDATE enrollments SET affiliate_pubkey_hex=NULL WHERE id=:id"),
            {"id": legacy_enrollment["enrollment_id"]},
        )

    response = client.get("/campaigns")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Directorio público" in response.text
    assert "Public directory" in response.text
    assert "Active &lt;script&gt;alert(1)&lt;/script&gt;" in response.text
    assert "Active <script>" not in response.text
    assert "Paused campaign" not in response.text
    assert "Ended campaign" not in response.text
    assert "Archived campaign" not in response.text
    assert f'href="/campaigns/{active["campaign_id"]}/page"' in response.text
    assert 'href="/app?role=affiliate"' in response.text
    assert "Merchant &amp; Friends" in response.text
    assert "Value for value" in response.text
    assert 'src="https://cdn.example/merchant.webp"' in response.text
    assert 'loading="lazy"' in response.text
    assert 'referrerpolicy="no-referrer"' in response.text
    assert "<dt>Affiliates</dt><dd>1</dd>" in response.text

    # The directory is a deliberately narrow public projection, not a dump of campaign records.
    assert active["merchant_pubkey"] not in response.text
    assert active["merchant_pubkey_hex"] not in response.text
    assert "https://active.example/secret-destination" not in response.text
    assert "https://merchant.example/private-terms" not in response.text
    assert "nostr_event_json" not in response.text
    assert "ref_code" not in response.text
    assert "lightning_address" not in response.text


def test_campaign_directory_has_bilingual_empty_state_and_server_resolved_language(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/empty-directory.db")
    monkeypatch.setenv("NOSTR_PUBLISH", "false")
    client = TestClient(main.app)

    response = client.get("/campaigns?lang=en")

    assert response.status_code == 200
    assert response.headers["content-language"] == "en"
    assert '<html lang="en" data-theme="night" data-lang="en">' in response.text
    assert "There are no active campaigns" in response.text
    assert "No hay campañas activas" in response.text
    assert "<title>Active campaigns · Meerat</title>" in response.text
    assert 'content="Directory of active public Meerat campaigns, with terms and aggregate results."' in response.text
    assert 'href="/campaigns"' in response.text
    assert 'aria-current="page"' in response.text
