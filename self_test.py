from __future__ import annotations
import json
from pathlib import Path
from fastapi.testclient import TestClient
import server
from bot import compose

ROOT=Path(__file__).parent
DATA=ROOT/'expanded'

def load(dirname,key):
    out={}
    for p in (DATA/dirname).glob('*.json'):
        d=json.loads(p.read_text(encoding='utf-8')); out[d[key]]=d
    return out

cats=load('categories','slug'); merchants=load('merchants','merchant_id'); customers=load('customers','customer_id'); triggers=load('triggers','id')
pairs=json.loads((DATA/'test_pairs.json').read_text())['pairs']

# Reset in-process API state.
for bucket in server.contexts.values(): bucket.clear()
server.conversations.clear(); server.auto_reply_counts.clear(); server.suppressed_conversations.clear(); server.sent_suppression_keys.clear()

client=TestClient(server.app)
for slug, payload in cats.items():
    r=client.post('/v1/context',json={'scope':'category','context_id':slug,'version':1,'payload':payload}); assert r.status_code==200 and r.json()['accepted']
for mid, payload in merchants.items():
    r=client.post('/v1/context',json={'scope':'merchant','context_id':mid,'version':1,'payload':payload}); assert r.status_code==200 and r.json()['accepted']
for cid, payload in customers.items():
    r=client.post('/v1/context',json={'scope':'customer','context_id':cid,'version':1,'payload':payload}); assert r.status_code==200 and r.json()['accepted']
for tid, payload in triggers.items():
    r=client.post('/v1/context',json={'scope':'trigger','context_id':tid,'version':1,'payload':payload}); assert r.status_code==200 and r.json()['accepted']

# Validate all canonical compositions.
issues=[]
for p in pairs:
    m=merchants[p['merchant_id']]; t=triggers[p['trigger_id']]; c=customers.get(p.get('customer_id'))
    result=compose(cats[m['category_slug']],m,t,c)
    for k in ['body','cta','send_as','suppression_key','rationale']:
        if k not in result: issues.append((p['test_id'],f'missing {k}'))
    if result['body']:
        if result['send_as'] not in {'vera','merchant_on_behalf'}: issues.append((p['test_id'],'bad send_as'))
        if not result['suppression_key']: issues.append((p['test_id'],'missing suppression'))
        if len(result['body']) > 1000: issues.append((p['test_id'],'message too long'))

# Tick each canonical trigger in a fresh suppression state; guarded/no-op is valid.
for bucket in server.sent_suppression_keys, server.conversations:
    bucket.clear()
for p in pairs:
    tid=p['trigger_id']
    r=client.post('/v1/tick',json={'now':'2026-04-26T10:00:00Z','available_triggers':[tid]})
    if r.status_code != 200:
        issues.append((p['test_id'],f'tick http {r.status_code}'))

# Context conflict / replacement behavior.
probe_id='__probe__'
r=client.post('/v1/context',json={'scope':'merchant','context_id':probe_id,'version':2,'payload':{'merchant_id':probe_id}})
assert r.status_code==200 and r.json()['accepted']
r=client.post('/v1/context',json={'scope':'merchant','context_id':probe_id,'version':1,'payload':{'merchant_id':probe_id}})
assert r.status_code==200 and r.json()['accepted'] is False and r.json()['reason']=='stale_version'

# Replay behavior.
for i in range(1,4):
    r=client.post('/v1/reply',json={'conversation_id':f'auto_{i}','merchant_id':'m_001_drmeera_dentist_delhi','from_role':'merchant','message':'Thank you for contacting us! Our team will respond shortly.','turn_number':i+1})
    expected=['send','wait','end'][i-1]
    if r.json().get('action') != expected: issues.append(('replay',f'auto turn {i} -> {r.json()}'))
r=client.post('/v1/reply',json={'conversation_id':'intent','merchant_id':'m_001_drmeera_dentist_delhi','from_role':'merchant','message':'Ok lets do it. Whats next?','turn_number':2})
if r.json().get('action')!='send' or 'draft' not in r.json().get('body','').lower(): issues.append(('replay','intent transition failed'))
r=client.post('/v1/reply',json={'conversation_id':'hostile','merchant_id':'m_001_drmeera_dentist_delhi','from_role':'merchant','message':'Stop messaging me. This is useless spam.','turn_number':2})
if r.json().get('action')!='end': issues.append(('replay','hostile failed'))

print('canonical_pairs', len(pairs))
print('contexts', {k:len(v) for k,v in server.contexts.items() if k!='merchant' or True})
print('issues', issues)
raise SystemExit(1 if issues else 0)
