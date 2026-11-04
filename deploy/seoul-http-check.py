"""Read-only deployment smoke checks; suppress all credentials/account content."""
import json
from pathlib import Path
import requests

settings = dict(line.split('=', 1) for line in Path('/var/lib/muse2api/.env').read_text().splitlines() if '=' in line and not line.startswith('#'))
base = 'https://alexych.asia/muse'
auth = {'Authorization': 'Bearer ' + settings['MUSE2API_KEY']}
session = requests.Session()
session.trust_env = False
checks = []

def expect(ok, label):
    if not ok:
        raise AssertionError(label)
    checks.append(label)

r = session.get(base + '/', timeout=30)
expect(r.status_code == 200 and 'APP_PATH' in r.text, 'admin page 200 with subpath adaptation')
r = session.get(base + '/healthz', timeout=30)
expect(r.status_code == 200 and r.json()['status'] == 'ok', 'public health 200')
r = session.get(base + '/v1/models', timeout=30)
expect(r.status_code == 401 and r.json()['error']['code'] == 401, 'unauthenticated API rejects with OpenAI error envelope')
r = session.get(base + '/v1/models', headers=auth, timeout=30)
expect(r.status_code == 200 and len(r.json()['data']) >= 3, 'authenticated model list 200')
r = session.get(base + '/admin/status', timeout=30)
expect(r.status_code == 401, 'unauthenticated admin rejects')
r = session.get(base + '/admin/status', headers=auth, timeout=30)
expect(r.status_code == 200 and r.json()['base_url'] == base + '/v1', 'authenticated admin retains public prefix')
account_count = len(r.json()['accounts'])
r = session.post(base + '/v1/images/generations', headers=auth, json={'prompt': ['invalid-type']}, timeout=30)
expect(r.status_code == 422 and 'error' in r.json(), 'validation error envelope retains prefix; no generation')
r = session.get(base + '/v1/models/', allow_redirects=False, timeout=30)
expect(r.status_code in (307, 308) and r.headers['Location'] == base + '/v1/models', 'slash redirect preserves HTTPS and /muse')
for route in ('/admin/update/check', '/admin/update/upgrade', '/admin/repo/pull', '/admin/repo/push', '/admin/repo/status'):
    r = session.get(base + route, headers=auth, timeout=30)
    expect(r.status_code == 403, 'managed deployment blocks ' + route)
for route in ('/admin/extension', '/admin/cookie-helper'):
    r = session.get(base + route, timeout=30)
    expect(r.status_code == 200 and len(r.content) > 100, 'download route ' + route)
for route in ('/.env', '/data/accounts.json'):
    r = session.get(base + route, timeout=30)
    expect(r.status_code == 404, 'private files not served: ' + route)
for route in ('/', '/child/'):
    r = session.get('https://alexych.asia' + route, timeout=30)
    expect(r.status_code == 200, 'existing application responds: ' + route)
expect(settings['MUSE2API_CONCURRENCY'] == '1', 'configured generation concurrency 1')
print(json.dumps({'checks_passed': len(checks), 'checks': checks,
                  'accounts_imported': account_count, 'generation_tested': False}, ensure_ascii=False, indent=2))
