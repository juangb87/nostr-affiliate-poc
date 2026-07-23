from fastapi.testclient import TestClient
from app.main import app


def test_demo_flow_creates_conversion_and_proof(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{tmp_path}/test.db')
    client = TestClient(app)
    health = client.get('/health')
    assert health.status_code == 200
    res = client.post('/demo')
    assert res.status_code == 200, res.text
    data = res.json()
    assert data['campaign']['campaign_id'].startswith('camp_')
    assert data['enrollment']['ref_url'].endswith(data['enrollment']['ref_code'])
    assert data['conversion']['commission_sats'] == 20000
    assert data['conversion']['nostr_event']['kind'] == 39005
    assert data['conversion']['nostr_event']['pubkey']
    assert data['conversion']['nostr_event']['sig']
    assert data['conversion']['relay_results']
    assert data['conversion']['relay_results'][0]['status'] == 'skipped'
    assert ['status', 'approved'] in data['conversion']['nostr_event']['tags']
    proof = client.get('/proofs').json()
    assert len(proof['events']) >= 3
    detail = client.get(f"/nostr/events/{data['conversion']['nostr_event_id']}")
    assert detail.status_code == 200
    assert detail.json()['event_id'] == data['conversion']['nostr_event_id']
