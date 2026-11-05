"""Live route/auth checks without requesting an OTP or invoking generation."""
import json
from pathlib import Path
import requests

settings = dict(line.split('=', 1) for line in Path('/var/lib/muse2api/.env').read_text().splitlines() if '=' in line and not line.startswith('#'))
base = 'https://alexych.asia/muse'
session = requests.Session()
session.trust_env = False
auth = {'Authorization': 'Bearer ' + settings['MUSE2API_KEY']}
checks = []

def expect(ok, name):
    if not ok:
        raise AssertionError(name)
    checks.append(name)

page = session.get(base + '/', timeout=20)
expect(page.status_code == 200 and 'nativeLoginIdentifier' in page.text and 'nativeLoginRecover' in page.text,
       'login form and recovery control deployed')
routes = [('POST', '/admin/login/start', {'identifier':'invalid'}),
          ('POST', '/admin/login/fixture/verify', {'code':'123456'}),
          ('POST', '/admin/login/fixture/resend', {}),
          ('GET', '/admin/login/fixture', None), ('DELETE', '/admin/login/fixture', None),
          ('GET', '/admin/login/current', None)]
for method, route, body in routes:
    r = session.request(method, base + route, json=body, timeout=20)
    expect(r.status_code == 401, 'requires API key: ' + route)
r = session.get(base + '/admin/login/current', headers=auth, timeout=20)
expect(r.status_code == 200 and 'state' in r.json() and 'cookies' not in r.json(), 'current login exposes public state only')
r = session.post(base + '/admin/login/start', headers=auth, json={'identifier':'invalid'}, timeout=20)
expect(r.status_code == 400, 'invalid identifier rejected without sending OTP')
r = session.get(base + '/admin/status', headers=auth, timeout=20)
expect(r.status_code == 200, 'existing accounts remain readable')
accounts = r.json()['accounts']
print(json.dumps({'checks_passed':len(checks),'checks':checks,'accounts_preserved':len(accounts),
                  'otp_sent':False,'generation_invoked':False},ensure_ascii=False,indent=2))
