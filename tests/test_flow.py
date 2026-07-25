from fastapi.testclient import TestClient
from app.main import app
from app.nostr_kinds import CAMPAIGN_KIND, CONVERSION_KIND, ENROLLMENT_KIND, PAYOUT_KIND, REVERSAL_KIND


def test_demo_flow_creates_conversion_and_proof(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{tmp_path}/test.db')
    monkeypatch.setenv('PAYOUT_ADMIN_KEY', 'test-payout-admin-key')
    monkeypatch.setenv('SANDBOX_PAYOUT_MARK_PAID_ENABLED', 'true')
    client = TestClient(app)
    health = client.get('/health')
    assert health.status_code == 200
    res = client.post('/demo')
    assert res.status_code == 200, res.text
    data = res.json()
    assert data['campaign']['campaign_id'].startswith('camp_')
    assert data['campaign']['merchant_pubkey'].startswith('npub')
    assert len(data['campaign']['merchant_pubkey_hex']) == 64
    assert any(t[0] == 'p' and t[3] == 'merchant' for t in data['campaign']['nostr_event']['tags'])
    assert data['campaign']['nostr_event']['kind'] == CAMPAIGN_KIND
    assert ['v', '2'] in data['campaign']['nostr_event']['tags']
    assert ['status', 'active'] in data['campaign']['nostr_event']['tags']
    bad_campaign = client.post('/campaigns', json={'merchant_pubkey': 'merchant_pubkey_demo', 'destination_url': 'https://example.com'})
    assert bad_campaign.status_code == 400
    assert data['enrollment']['ref_url'].endswith(data['enrollment']['ref_code'])
    assert data['enrollment']['affiliate_pubkey'].startswith('npub')
    assert len(data['enrollment']['affiliate_pubkey_hex']) == 64
    assert any(t[0] == 'p' and t[3] == 'affiliate' for t in data['enrollment']['nostr_event']['tags'])
    assert data['enrollment']['nostr_event']['kind'] == ENROLLMENT_KIND
    assert ['v', '2'] in data['enrollment']['nostr_event']['tags']
    assert ['status', 'approved'] in data['enrollment']['nostr_event']['tags']
    assert data['conversion']['commission_sats'] == 20000
    assert data['conversion']['nostr_event']['kind'] == CONVERSION_KIND
    assert data['conversion']['nostr_event']['pubkey']
    assert data['conversion']['nostr_event']['sig']
    assert data['conversion']['relay_results']
    assert data['conversion']['relay_results'][0]['status'] == 'skipped'
    assert ['status', 'approved'] in data['conversion']['nostr_event']['tags']
    assert ['v', '2'] in data['conversion']['nostr_event']['tags']
    assert any(t[0] == 'a' and t[1].startswith(f"{CAMPAIGN_KIND}:") and t[1].endswith(data['campaign']['campaign_id']) for t in data['conversion']['nostr_event']['tags'])
    assert any(t[0] == 'a' and t[1].startswith(f"{ENROLLMENT_KIND}:") and t[1].endswith(data['enrollment']['enrollment_id']) for t in data['conversion']['nostr_event']['tags'])
    assert ['order_fiat_amount', '100.0'] in data['conversion']['nostr_event']['tags']
    assert ['order_fiat_currency', 'USD'] in data['conversion']['nostr_event']['tags']
    assert not any(t[0] in {'merchant', 'merchant_npub', 'affiliate', 'affiliate_npub', 'order_currency'} for t in data['conversion']['nostr_event']['tags'])
    proof = client.get('/proofs').json()
    assert len(proof['events']) >= 3
    detail = client.get(f"/nostr/events/{data['conversion']['nostr_event_id']}")
    assert detail.status_code == 200
    assert detail.json()['event_id'] == data['conversion']['nostr_event_id']
    dashboard = client.get('/dashboard')
    assert dashboard.status_code == 200
    assert 'Nostr Affiliate POC Dashboard' in dashboard.text
    campaign_summary = client.get(f"/campaigns/{data['campaign']['campaign_id']}/summary")
    assert campaign_summary.status_code == 200
    campaign_summary_json = campaign_summary.json()
    assert campaign_summary_json['campaign']['id'] == data['campaign']['campaign_id']
    assert campaign_summary_json['totals']['enrollments'] >= 1
    assert campaign_summary_json['totals']['conversions'] >= 1
    campaign_page = client.get(f"/campaigns/{data['campaign']['campaign_id']}/page")
    assert campaign_page.status_code == 200
    assert 'Public campaign' in campaign_page.text
    assert data['campaign']['campaign_id'] in campaign_page.text
    affiliate_summary = client.get(f"/affiliates/{data['enrollment']['affiliate_pubkey']}/summary")
    assert affiliate_summary.status_code == 200
    affiliate_summary_json = affiliate_summary.json()
    assert affiliate_summary_json['identity']['npub'] == data['enrollment']['affiliate_pubkey']
    assert affiliate_summary_json['totals']['enrollments'] >= 1
    assert affiliate_summary_json['totals']['conversions'] >= 1
    affiliate_profile = client.get(f"/affiliates/{data['enrollment']['affiliate_pubkey']}/profile")
    assert affiliate_profile.status_code == 200
    assert 'Affiliate public profile' in affiliate_profile.text
    assert data['enrollment']['affiliate_pubkey'] in affiliate_profile.text
    affiliate_profile_hex = client.get(f"/affiliates/{data['enrollment']['affiliate_pubkey_hex']}/profile")
    assert affiliate_profile_hex.status_code == 200
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
    payout_id = receipt_json['payout']['id']
    payout_detail = client.get(f"/payouts/{payout_id}")
    assert payout_detail.status_code == 200
    assert payout_detail.json()['payout']['status'] == 'pending'
    paid = client.post(
        f"/payouts/{payout_id}/mark-paid",
        headers={'Authorization': 'Bearer test-payout-admin-key'},
        json={'payment_hash': 'sandbox_payment_hash_123'},
    )
    assert paid.status_code == 200, paid.text
    paid_json = paid.json()
    assert paid_json['ok'] is True
    assert paid_json['payout_status'] == 'paid'
    assert paid_json['nostr_event']['kind'] == PAYOUT_KIND
    assert ['status', 'paid'] in paid_json['nostr_event']['tags']
    assert ['v', '2'] in paid_json['nostr_event']['tags']
    assert ['e', data['conversion']['nostr_event_id']] in paid_json['nostr_event']['tags']
    assert any(t[0] == 'p' and t[3] == 'affiliate' for t in paid_json['nostr_event']['tags'])
    duplicate_paid = client.post(
        f"/payouts/{payout_id}/mark-paid",
        headers={'Authorization': 'Bearer test-payout-admin-key'},
        json={'payment_hash': 'sandbox_payment_hash_123'},
    )
    assert duplicate_paid.status_code == 200
    assert duplicate_paid.json()['duplicate'] is True
    paid_receipt = client.get(f"/payouts/{payout_id}/receipt")
    assert paid_receipt.status_code == 200
    assert 'Payout receipt' in paid_receipt.text
    flow_after_payout = client.get(f"/flows/{data['conversion']['conversion_id']}").json()
    assert flow_after_payout['payout']['status'] == 'paid'
    assert len(flow_after_payout['events']) >= 4
    reversal = client.post(
        f"/conversions/{data['conversion']['conversion_id']}/reverse",
        headers={'Authorization': 'Bearer bumbei-demo-key'},
        json={'reason': 'refund', 'refund_sats': 250000, 'note': 'test refund'},
    )
    assert reversal.status_code == 200, reversal.text
    reversal_json = reversal.json()
    assert reversal_json['nostr_event']['kind'] == REVERSAL_KIND
    assert ['v', '2'] in reversal_json['nostr_event']['tags']
    assert ['e', data['conversion']['nostr_event_id']] in reversal_json['nostr_event']['tags']
    assert ['reason', 'refund'] in reversal_json['nostr_event']['tags']
    assert ['refund_sats', '250000'] in reversal_json['nostr_event']['tags']
    duplicate_reversal = client.post(
        f"/conversions/{data['conversion']['conversion_id']}/reverse",
        headers={'Authorization': 'Bearer bumbei-demo-key'},
        json={'reason': 'refund', 'refund_sats': 250000},
    )
    assert duplicate_reversal.status_code == 200
    assert duplicate_reversal.json()['duplicate'] is True
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
    assert webhook_json['order_total_sats'] == 312500
    assert webhook_json['sats_per_usd_source'] == 'server'
    assert webhook_json['receipt_url'].endswith(f"/flows/{webhook_json['conversion_id']}/receipt")
    duplicate = client.post(
        '/merchant/conversions',
        headers={'Authorization': 'Bearer bumbei-demo-key'},
        json={'order_id': 'merchant_order_1', 'bb_click_id': click.json()['click_id'], 'order_total': 125, 'currency': 'USD'},
    )
    assert duplicate.status_code == 200
    assert duplicate.json()['duplicate'] is True
    assert duplicate.json()['conversion_id'] == webhook_json['conversion_id']
    sat_click = client.post('/clicks/simulate', json={'ref_code': data['enrollment']['ref_code']}).json()['click_id']
    sats_webhook = client.post(
        '/merchant/conversions',
        headers={'Authorization': 'Bearer bumbei-demo-key'},
        json={'order_id': 'merchant_order_sats', 'bb_click_id': sat_click, 'order_total': 250000, 'currency': 'SATS', 'metadata': {'platform': 'oshigoods'}},
    )
    assert sats_webhook.status_code == 200, sats_webhook.text
    assert sats_webhook.json()['order_total_sats'] == 250000
    assert sats_webhook.json()['commission_sats'] == 20000
    assert sats_webhook.json()['sats_per_usd_source'] == 'not_required'
    btc_click = client.post('/clicks/simulate', json={'ref_code': data['enrollment']['ref_code']}).json()['click_id']
    btc_webhook = client.post(
        '/merchant/conversions',
        headers={'Authorization': 'Bearer bumbei-demo-key'},
        json={'order_id': 'merchant_order_btc', 'bb_click_id': btc_click, 'order_total': 0.0025, 'currency': 'BTC'},
    )
    assert btc_webhook.status_code == 200, btc_webhook.text
    assert btc_webhook.json()['order_total_sats'] == 250000
    assert btc_webhook.json()['commission_sats'] == 20000
    snippet = client.get('/bb.js')
    assert snippet.status_code == 200
    assert 'window.BumbeiAttribution' in snippet.text
    assert 'bb_click_id' in snippet.text
    landing = client.get(f"/demo-merchant?bb_click_id={click.json()['click_id']}&bb_ref={data['enrollment']['ref_code']}")
    assert landing.status_code == 200
    assert '/bb.js' in landing.text
    demo_checkout = client.post(
        '/demo-merchant/checkout',
        json={'bb_click_id': click.json()['click_id'], 'bb_ref': data['enrollment']['ref_code'], 'order_total': 250000, 'currency': 'SATS'},
    )
    assert demo_checkout.status_code == 200, demo_checkout.text
    assert demo_checkout.json()['ok'] is True
    assert demo_checkout.json()['order_total_sats'] == 250000
    assert demo_checkout.json()['receipt_url'].endswith(f"/flows/{demo_checkout.json()['conversion_id']}/receipt")

    merchant_headers = {'Authorization': 'Bearer bumbei-demo-key'}
    assert client.post(f"/enrollments/{data['enrollment']['enrollment_id']}/status", json={'status': 'terminated'}).status_code == 401
    terminated = client.post(f"/enrollments/{data['enrollment']['enrollment_id']}/status", headers=merchant_headers, json={'status': 'terminated'})
    assert terminated.status_code == 200, terminated.text
    assert terminated.json()['nostr_event']['kind'] == ENROLLMENT_KIND
    assert ['status', 'terminated'] in terminated.json()['nostr_event']['tags']
    assert ['d', data['enrollment']['enrollment_id']] in terminated.json()['nostr_event']['tags']
    reapproved = client.post(f"/enrollments/{data['enrollment']['enrollment_id']}/status", headers=merchant_headers, json={'status': 'approved'})
    assert reapproved.status_code == 200
    paused = client.post(f"/campaigns/{data['campaign']['campaign_id']}/status", headers=merchant_headers, json={'status': 'paused'})
    assert paused.status_code == 200, paused.text
    assert paused.json()['nostr_event']['kind'] == CAMPAIGN_KIND
    assert ['status', 'paused'] in paused.json()['nostr_event']['tags']
    assert ['d', data['campaign']['campaign_id']] in paused.json()['nostr_event']['tags']
    reactivate = client.post(f"/campaigns/{data['campaign']['campaign_id']}/status", headers=merchant_headers, json={'status': 'active'})
    assert reactivate.status_code == 200
