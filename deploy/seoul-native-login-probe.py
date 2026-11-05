"""Check official login initialization only; no account, OTP or generation."""
import json
import uuid
import requests

with requests.Session() as session:
    session.headers.update({'Origin': 'https://muse.ai', 'Referer': 'https://muse.ai/',
                            'Content-Type': 'application/json',
                            'Sec-Fetch-Site': 'same-origin', 'Sec-Fetch-Mode': 'cors', 'Sec-Fetch-Dest': 'empty',
                            'X-Hatch-Waterfall-Id': str(uuid.uuid4()),
                            'X-Hatch-Caa-Reg-Entry-Point': 'login_home'})
    landing = session.get('https://muse.ai/', timeout=20, allow_redirects=False)
    print(json.dumps({'step':'landing', 'http_status':landing.status_code, 'cookie_names':sorted(c.name for c in session.cookies)}), flush=True)
    for step in ('restart', 'start'):
        if step == 'start' and result.get('flow_state') != 'new':
            break
        response = session.post('https://muse.ai/api/auth/native/' + step, json={}, timeout=20, allow_redirects=False)
        try:
            result = response.json()
        except ValueError:
            result = {}
        csrf = result.get('csrf_token')
        print(json.dumps({'step':step,'http_status':response.status_code,
                          'flow_state':result.get('flow_state'), 'registration_state':result.get('registration_state'),
                          'csrf_available':bool(csrf), 'cookie_names': sorted(c.name for c in session.cookies)}), flush=True)
        if csrf:
            session.headers['X-Hatch-CSRF-Token'] = csrf
        if not 200 <= response.status_code < 300:
            print(json.dumps({'error_field':result.get('error') if isinstance(result.get('error'), str) else 'request_failed'}), flush=True)
            break
