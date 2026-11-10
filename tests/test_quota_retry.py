"""Quota loading/retry regressions using a fake clock; no browser or accounts."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine as module


class Clock:
    def __init__(self):
        self.now = 0.0
    def time(self):
        return self.now
    monotonic = time
    def sleep(self, seconds):
        self.now += seconds


class QuotaRetryTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.engine = module.MuseEngine(SimpleNamespace(profile_dir=self.home.name))
        self.clock = Clock()
        self.patcher = patch.object(module, 'time', self.clock)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.opened = []
        self.closed = []

    def fixture(self, delay=0, fail_first=False, failure=None):
        def ensure(cookies, expires, account_id=None):
            self.assertIsNone(self.engine.page)
            self.opened.append(account_id)
            began = self.clock.now
            attempt = len(self.opened)
            if failure:
                raise failure
            def js(script):
                if script == self.engine._WORKSPACE_UNAVAILABLE_JS:
                    return False
                if script == self.engine._QUOTA_SETTINGS_JS:
                    return '{"x":10,"y":20}'
                if script == self.engine._QUOTA_MENU_JS:
                    return None
                if script == self.engine._QUOTA_DIALOG_JS:
                    if fail_first and attempt == 1 or self.clock.now - began < delay:
                        return 'Settings\nGeneral\nUsage\nLanguage'
                    return 'Settings\nUsage\nFree plan\n17% used\nLanguage'
                raise AssertionError('Unexpected script')
            self.engine.page = SimpleNamespace(js=js, attempt=attempt)
        def close():
            if self.engine.page is not None:
                self.closed.append(self.engine.page.attempt)
            self.engine.page = None
        self.engine.ensure_page = ensure
        self.engine._close_page = close
        self.engine._click_point = lambda *args: None
        self.engine._quota_subscription = lambda deadline: None

    def test_slow_usage_after_original_twelve_seconds_succeeds_without_retry(self):
        self.fixture(delay=18)
        result = self.engine.quota({'hatch_sess': 'secret-fixture'}, account_id='a')
        self.assertEqual(result['weekly_used_pct'], 17)
        self.assertEqual(result['read_attempts'], 1)
        self.assertGreaterEqual(self.clock.now, 18)
        self.assertEqual(self.opened, ['a'])
        self.assertEqual(self.closed, [1])

    def test_empty_usage_retries_with_a_fresh_context_and_same_account(self):
        self.fixture(fail_first=True)
        with self.assertLogs(module.log, level='INFO') as logs:
            result = self.engine.quota({'hatch_sess': 'secret-fixture'}, account_id='a')
        self.assertEqual(result['read_attempts'], 2)
        self.assertEqual(self.opened, ['a', 'a'])
        self.assertEqual(self.closed, [1, 2])
        self.assertNotIn('secret-fixture', '\n'.join(logs.output))
        self.assertNotIn('Free plan', '\n'.join(logs.output))

    def test_two_empty_attempts_fail_without_fabricating_zero(self):
        self.fixture(delay=999)
        with self.assertRaisesRegex(module.MuseGenerationError, '已尝试 2 次'):
            self.engine.quota({}, account_id='a')
        self.assertEqual(self.opened, ['a', 'a'])
        self.assertEqual(self.closed, [1, 2])
        self.assertLess(self.clock.now, 90)

    def test_auth_workspace_and_proxy_errors_never_retry(self):
        for error in (module.MuseAuthError('fixture'),
                      module.MuseWorkspaceUnavailableError(), module.MuseProxyError('fixture')):
            with self.subTest(error=type(error).__name__):
                self.opened.clear()
                self.fixture(failure=error)
                with self.assertRaises(type(error)):
                    self.engine.quota({}, account_id='a')
                self.assertEqual(self.opened, ['a'])
                self.assertIsNone(self.engine.page)

    def test_transient_cdp_error_retries_once(self):
        self.fixture()
        ensure = self.engine.ensure_page
        calls = [0]
        def failing_once(*args, **kwargs):
            calls[0] += 1
            if calls[0] == 1:
                raise module.CDPError('fixture renderer unavailable')
            return ensure(*args, **kwargs)
        self.engine.ensure_page = failing_once
        result = self.engine.quota({}, account_id='a')
        self.assertEqual(result['read_attempts'], 2)
        self.assertEqual(calls[0], 2)
        self.assertIsNone(self.engine.page)

    def test_explicit_upstream_usage_error_retries_without_full_loading_wait(self):
        self.fixture()
        ensure = self.engine.ensure_page
        def first_error(*args, **kwargs):
            ensure(*args, **kwargs)
            if len(self.opened) == 1:
                js = self.engine.page.js
                self.engine.page.js = lambda script: (
                    "Settings\nUsage\nWe couldn't load your usage. Please try again later."
                    if script == self.engine._QUOTA_DIALOG_JS else js(script))
        self.engine.ensure_page = first_error
        result = self.engine.quota({}, account_id='a')
        self.assertEqual(result['read_attempts'], 2)
        self.assertLess(self.clock.now, 2)

    def test_page_load_consumes_budget_and_stops_additional_attempts(self):
        self.fixture()
        ensure = self.engine.ensure_page
        def slow(*args, **kwargs):
            ensure(*args, **kwargs)
            self.clock.sleep(95)
        self.engine.ensure_page = slow
        with self.assertRaisesRegex(module.MuseGenerationError, '已尝试 1 次'):
            self.engine.quota({}, account_id='a')
        self.assertEqual(self.opened, ['a'])
        self.assertEqual(self.closed, [1])

    def test_structured_usage_succeeds_when_settings_never_renders_usage(self):
        self.fixture(delay=999)
        self.engine._quota_subscription = lambda deadline: {
            'found': True, 'weekly_used_pct': 19, 'source': 'subscription_action'}
        result = self.engine.quota({}, account_id='a')
        self.assertEqual(result['weekly_used_pct'], 19)
        self.assertEqual(result['read_attempts'], 1)
        self.assertEqual(self.closed, [1])
        self.assertLess(self.clock.now, 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
