import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
res = client.post('/demo')
res.raise_for_status()
data = res.json()
print(json.dumps({
    'campaign_id': data['campaign']['campaign_id'],
    'ref_url': data['enrollment']['ref_url'],
    'click_id': data['click_id'],
    'conversion_id': data['conversion']['conversion_id'],
    'commission_sats': data['conversion']['commission_sats'],
    'pending_sats': data['affiliate']['pending_sats'],
    'proof_event_id': data['conversion']['nostr_event_id'],
}, indent=2))
