"""Quota refresh state, bounded capacity waits and safe errors; no network/account."""
import asyncio
import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_home = tempfile.TemporaryDirectory(prefix='muse-quota-refresh-')
os.environ.update(MUSE2API_HOME=_home.name, MUSE2API_PROFILE_ROOT=_home.name,
                  MUSE2API_KEY='test-only', MUSE2API_CONCURRENCY='1')
import app


async def quota_http(aid, token):
    path = '/admin/accounts/' + aid + '/quota'
    scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
             'method': 'POST', 'scheme': 'https', 'path': path,
             'raw_path': path.encode(), 'query_string': b'', 'root_path': '',
             'headers': [(b'authorization', ('Bearer ' + token).encode())],
             'server': ('test', 443), 'client': ('test', 1)}
    messages = []
    received = False
    async def receive():
        nonlocal received
        if not received:
            received = True
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        await asyncio.Event().wait()
    async def send(message):
        messages.append(message)
    await app.app(scope, receive, send)
    return next(m['status'] for m in messages if m['type'] == 'http.response.start')


class QuotaRefreshTests(unittest.TestCase):
    def setUp(self):
        app.store.accounts.clear()
        app.store.tasks.clear()
        self.account = app.store.add_account({'hatch_sess': 'fixture-cookie'})
        self.aid = self.account['id']
        self.cached = {'found': True, 'weekly_used_pct': 17, 'checked_at': 1000}
        self.account.update(quota=copy.deepcopy(self.cached), quota_error='previous failure',
                            quota_attempted_at=1100, enabled=True)
        self.network = patch('requests.sessions.Session.request', side_effect=AssertionError('unexpected network'))
        self.network.start()
        self.renew = patch.object(app, '_renew_and_persist', return_value=self.account)
        self.renew.start()
        self.start = patch.object(app.engine, 'start')
        self.start.start()

    def tearDown(self):
        self.start.stop()
        self.renew.stop()
        self.network.stop()
        self.assertFalse(app.GEN_LOCK.locked())
        self.assertEqual(app.ACCOUNT_POOL.busy_accounts, set())

    def refresh(self):
        return asyncio.run(app.query_quota(self.aid))

    def test_success_writes_new_quota_and_clears_old_failure(self):
        with patch.object(app.engine, 'quota', return_value={'found': True, 'weekly_used_pct': 21}), \
             patch.object(app.time, 'time', return_value=1300):
            result = self.refresh()
        self.assertEqual(result['checked_at'], 1300)
        self.assertEqual(self.account['quota']['weekly_used_pct'], 21)
        self.assertEqual(self.account['quota_attempted_at'], 1300)
        self.assertEqual(self.account['quota_error'], '')
        public = app.store.public_account(self.account)
        self.assertEqual(public['quota_attempted_at'], 1300)
        self.assertNotIn('cookies', public)

    def test_failure_preserves_success_time_and_records_error_before_releasing_lease(self):
        original = app.store.update_account
        writes = []
        def update(aid, **values):
            if 'quota_error' in values:
                writes.append(values)
                self.assertIn(aid, app.ACCOUNT_POOL.busy_accounts)
            return original(aid, **values)
        with patch.object(app.engine, 'quota', side_effect=app.MuseGenerationError('usage loading timed out')), \
             patch.object(app.store, 'update_account', side_effect=update), \
             patch.object(app.time, 'time', return_value=1400):
            with self.assertRaises(app.HTTPException) as caught:
                self.refresh()
        self.assertEqual(caught.exception.status_code, 502)
        self.assertEqual(self.account['quota'], self.cached)
        self.assertEqual(self.account['quota_attempted_at'], 1400)
        self.assertEqual(self.account['quota_error'], 'usage loading timed out')
        self.assertEqual(len(writes), 1)

    def test_not_found_never_replaces_known_quota(self):
        with patch.object(app.engine, 'quota', return_value={'found': False}):
            with self.assertRaises(app.HTTPException) as caught:
                self.refresh()
        self.assertEqual(caught.exception.status_code, 502)
        self.assertEqual(self.account['quota'], self.cached)

    def test_busy_capacity_is_bounded_and_does_not_touch_cached_state(self):
        before = copy.deepcopy(self.account)
        with patch.object(app.GEN_LOCK, 'acquire', return_value=False) as acquire, \
             patch.object(app.GEN_LOCK, 'release') as release, \
             patch.object(app.engine, 'quota') as quota:
            with self.assertRaises(app.HTTPException) as caught:
                self.refresh()
        self.assertEqual(caught.exception.status_code, 409)
        acquire.assert_called_once_with(timeout=15)
        release.assert_not_called()
        quota.assert_not_called()
        self.assertEqual(self.account, before)

    def test_account_busy_does_not_change_a_successful_result(self):
        before = copy.deepcopy(self.account)
        app.ACCOUNT_POOL.busy_accounts.add(self.aid)
        try:
            with self.assertRaises(app.HTTPException) as caught:
                self.refresh()
            self.assertEqual(caught.exception.status_code, 409)
            self.assertEqual(self.account, before)
        finally:
            app.ACCOUNT_POOL.busy_accounts.discard(self.aid)

    def test_muse_login_failure_is_not_admin_authentication_401(self):
        with patch.object(app, '_renew_and_persist', side_effect=app.MuseAuthError('fixture session expired')):
            with self.assertRaises(app.HTTPException) as caught:
                self.refresh()
        self.assertEqual(caught.exception.status_code, 502)
        self.assertIn('Muse', caught.exception.detail)
        self.assertIn('管理 API Key 不受影响', caught.exception.detail)
        self.assertFalse(self.account['session_ok'])
        self.assertEqual(self.account['quota'], self.cached)

    def test_unexpected_errors_do_not_leak_details_or_cookies_to_logs(self):
        with patch.object(app.engine, 'quota', side_effect=RuntimeError('private fixture-cookie raw HTML')), \
             patch.object(app.log, 'warning') as warning:
            with self.assertRaises(app.HTTPException) as caught:
                self.refresh()
        self.assertNotIn('fixture-cookie', caught.exception.detail)
        self.assertNotIn('raw HTML', self.account['quota_error'])
        self.assertNotIn('fixture-cookie', repr(warning.call_args_list))
        self.assertIn('RuntimeError', repr(warning.call_args_list))

    def test_admin_authentication_still_rejects_wrong_key(self):
        with patch.object(app.engine, 'quota') as quota:
            status = asyncio.run(quota_http(self.aid, 'wrong'))
        self.assertEqual(status, 401)
        quota.assert_not_called()

    def test_authenticated_http_reports_muse_auth_failure_as_502(self):
        with patch.object(app, '_renew_and_persist', side_effect=app.MuseAuthError('fixture session expired')):
            status = asyncio.run(quota_http(self.aid, 'test-only'))
        self.assertEqual(status, 502)


if __name__ == '__main__':
    try:
        unittest.main(verbosity=2)
    finally:
        _home.cleanup()
