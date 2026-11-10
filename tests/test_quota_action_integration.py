"""Account-local action reads, public-metadata caching and safe UI fallback."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine as module


class ActionTests(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.engine = module.MuseEngine(SimpleNamespace(profile_dir=home.name))
        self.action = 'a' * 40
        self.next_action = 'b' * 40
        self.calls = []
        self.deadline = module.time.monotonic() + 90

    def response(self, percent):
        body = '0:{"a":"$@1"}\n1:' + json.dumps({
            'success': True, 'subscription': {
                'usage': {'state': 'METERED', 'percentUsed': percent},
                'usageRowLabel': 'Free plan',
                'statusSubtitle': 'Weekly limit resets on Nov 17'}})
        return json.dumps({'status': 200, 'content_type': 'text/x-component', 'body': body})

    def page(self, responses, action=None):
        def js(expr, **kwargs):
            self.calls.append(expr)
            if expr == module.DISCOVER_SUBSCRIPTION_ACTION_JS:
                return json.dumps({'action': action or self.action})
            return responses.pop(0)
        self.engine.page = SimpleNamespace(js=js)

    def test_cache_reuses_only_action_metadata_not_previous_accounts_quota(self):
        self.page([self.response(19)])
        first = self.engine._quota_subscription(self.deadline)
        self.page([self.response(55)])
        second = self.engine._quota_subscription(self.deadline)
        self.assertEqual((first['weekly_used_pct'], second['weekly_used_pct']), (19, 55))
        self.assertEqual(self.calls.count(module.DISCOVER_SUBSCRIPTION_ACTION_JS), 1)
        self.assertEqual(len(self.calls), 3)

    def test_stale_action_rediscovered_once(self):
        self.engine._quota_action_cache = (self.action, self.deadline)
        self.page([json.dumps({'status': 404}), self.response(19)], action=self.next_action)
        result = self.engine._quota_subscription(self.deadline)
        self.assertEqual(result['weekly_used_pct'], 19)
        self.assertEqual(self.engine._quota_action_cache[0], self.next_action)
        self.assertEqual(self.calls.count(module.DISCOVER_SUBSCRIPTION_ACTION_JS), 1)

    def test_bad_response_falls_back_without_logging_response(self):
        self.page([json.dumps({'status': 200, 'content_type': 'text/x-component',
                               'body': 'private-account-fixture'})])
        with self.assertLogs(module.log, level='INFO') as log:
            self.assertIsNone(self.engine._quota_subscription(self.deadline))
        self.assertNotIn('private-account-fixture', '\n'.join(log.output))

    def test_official_action_not_found_header_invalidates_cache(self):
        self.engine._quota_action_cache = (self.action, self.deadline)
        self.page([json.dumps({'status': 500, 'action_not_found': True}),
                   self.response(19)], action=self.next_action)
        self.assertEqual(self.engine._quota_subscription(self.deadline)['weekly_used_pct'], 19)
        self.assertEqual(self.engine._quota_action_cache[0], self.next_action)

    def test_forbidden_does_not_retry_action_or_invent_zero(self):
        self.page([json.dumps({'status': 403})])
        self.assertIsNone(self.engine._quota_subscription(self.deadline))
        self.assertEqual(len(self.calls), 2)

    def test_insufficient_time_uses_fallback_without_new_request(self):
        self.page([])
        self.assertIsNone(self.engine._quota_subscription(module.time.monotonic() + 2))
        self.assertEqual(self.calls, [])

    def test_discovery_never_interpolates_unvalidated_action(self):
        self.page([], action='bad-script-string')
        self.assertIsNone(self.engine._quota_subscription(self.deadline))
        self.assertEqual(len(self.calls), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
