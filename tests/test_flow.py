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
    dashboard = client.get('/dashboard')
    assert dashboard.status_code == 200
    assert 'Nostr Affiliate POC Dashboard' in dashboard.text
    dashboard_data = client.get('/dashboard/data')
    assert dashboard_data.status_code == 200
    assert dashboard_data.json()['counts']['conversions'] >= 1
    receipt = client.get(f"/flows/{data['conversion']['conversion_id']}")
    assert receipt.status_code == 200
    receipt_json = receipt.json()
    assert receipt_json['campaign']['id'] == data['campaign']['campaign_id']
    assert receipt_json['enrollment']['ref_code'] == data['enrollment']['ref_code']
    assert receipt_json['conversion']['id'] == data['conversion']['conversion_id']
    assert len(receipt_json['events']) >= 3
    receipt_page = client.get(f"/flows/{data['conversion']['conversion_id']}/receipt")
    assert receipt_page.status_code == 200
    assert 'Flow receipt' in receipt_page.text
    assert data['conversion']['conversion_id'] in receipt_page.text
    click = client.post('/clicks/simulate', json={'ref_code': data['enrollment']['ref_code']})
    assert click.status_code == 200
    assert click.json()['click_id'].startswith('clk_')
    no_auth = client.post('/merchant/conversions', json={'order_id': 'merchant_order_1', 'bb_click_id': click.json()['click_id'], 'order_total': 125, 'currency': 'USD'})
    assert no_auth.status_code == 401
    webhook = client.post(
        '/merchant/conversions',
        headers={'Authorization': 'Bearer bumbei-demo-key'},
        json={'order_id': 'merchant_order_1', 'bb_click_id': click.json()['click_id'], 'order_total': 125, 'currency': 'USD', 'metadata': {'platform': 'shopify'}},
    )
    assert webhook.status_code == 200, webhook.text
    webhook_json = webhook.json()
    assert webhook_json['ok'] is True
    assert webhook_json['duplicate'] is False
    assert webhook_json['receipt_url'].endswith(f"/flows/{webhook_json['conversion_id']}/receipt")
    duplicate = client.post(
        '/merchant/conversions',
        headers={'Authorization': 'Bearer bumbei-demo-key'},
        json={'order_id': 'merchant_order_1', 'bb_click_id': click.json()['click_id'], 'order_total': 125, 'currency': 'USD'},
    )
    assert duplicate.status_code == 200
    assert duplicate.json()['duplicate'] is True
    assert duplicate.json()['conversion_id'] == webhook_json['conversion_id']
