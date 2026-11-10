"""Structured Muse quota parsing and actual browser-helper JS, without network."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quota_reader import (DISCOVER_SUBSCRIPTION_ACTION_JS,
                          SubscriptionResponseError, build_subscription_fetch_js,
                          parse_subscription_response)

ACTION = 'b' * 40


def subscription():
    return {'usage': {'state': 'METERED', 'percentUsed': 19, 'resetsAt': 1794892171,
                      'quotaStatus': 'SUFFICIENT'},
            'usageRowLabel': 'Free plan', 'statusSubtitle': 'Weekly limit resets on Nov 17',
            'topupBalance': 1200000000, 'topupTotal': 1200000000,
            'topupRowValueLabel': '0% used (30B tokens left)',
            'accountId': 'private-account-id', 'agreement': {'private': 'secret-agreement'}}


def flight(sub=None, success=True, reference='1'):
    return '0:' + json.dumps({'a': '$@' + reference, 'q': '', 'i': False}) + '\n' + reference + ':' + json.dumps(
        {'success': success, 'subscription': subscription() if sub is None else sub})


class SubscriptionParserTests(unittest.TestCase):
    def test_actual_structured_usage_and_display_labels(self):
        result = parse_subscription_response(flight())
        self.assertTrue(result['found'])
        self.assertEqual(result['weekly_used_pct'], 19)
        self.assertEqual(result['plan'], 'Free plan')
        self.assertEqual(result['weekly_reset'], 'Nov 17')
        self.assertEqual(result['extra_used_pct'], 0)
        self.assertEqual(result['extra_left'], '30B tokens left')
        self.assertNotIn('extra_expires', result)
        serialized = json.dumps(result)
        for private in ('private-account-id', 'secret-agreement', '1200000000', 'agreement', 'accountId'):
            self.assertNotIn(private, serialized)

    def test_genuine_zero_and_decimal_usage_are_preserved(self):
        for value in (0, 0.0, 12.5, 100):
            sub = subscription()
            sub['usage']['percentUsed'] = value
            result = parse_subscription_response(flight(sub))
            self.assertEqual(result['weekly_used_pct'], value)

    def test_only_the_action_referenced_by_root_is_used(self):
        misleading = '2:' + json.dumps({'success': True, 'subscription': subscription()})
        sub = subscription()
        sub['usage']['percentUsed'] = 27
        result = parse_subscription_response(misleading + '\n' + flight(sub, reference='a'))
        self.assertEqual(result['weekly_used_pct'], 27)
        with self.assertRaises(SubscriptionResponseError):
            parse_subscription_response('0:{"a":"$@1"}\n' + misleading)

    def test_unreferenced_success_false_or_error_result_are_rejected(self):
        for response in (
            json.dumps({'success': True, 'subscription': subscription()}),
            '1:' + json.dumps({'success': True, 'subscription': subscription()}),
            flight(success=False), flight(success=1),
            '0:{"a":"$@1"}\n1:E{"message":"failed"}',
            '0:{"a":"$@0"}\n1:{"success":true}',
            '0:{"a":{"success":true}}\n1:{"success":true}',
        ):
            with self.subTest(response=response[:50]), self.assertRaises(SubscriptionResponseError):
                parse_subscription_response(response)

    def test_missing_unmetered_and_nonfinite_usage_never_become_zero(self):
        for usage in (None, {}, {'state': 'METERED'}, {'state': 'UNLIMITED', 'percentUsed': 0}):
            sub = subscription()
            sub['usage'] = usage
            with self.subTest(usage=usage), self.assertRaises(SubscriptionResponseError):
                parse_subscription_response(flight(sub))
        for value in (None, False, '19', -1, 101, float('nan'), float('inf'), float('-inf')):
            sub = subscription()
            sub['usage']['percentUsed'] = value
            with self.subTest(value=value), self.assertRaises(SubscriptionResponseError):
                parse_subscription_response(flight(sub))

    def test_topup_units_are_never_treated_as_tokens_and_expiry_is_not_assumed(self):
        sub = subscription()
        del sub['topupRowValueLabel']
        result = parse_subscription_response(flight(sub))
        self.assertNotIn('extra_left', result)
        self.assertNotIn('extra_used_pct', result)
        self.assertNotIn('extra_expires', result)
        self.assertNotIn('1200000000', result['raw'])

    def test_optional_labels_and_reset_timestamp_fallback(self):
        sub = subscription()
        sub['usageRowLabel'] = None
        sub['statusSubtitle'] = None
        sub['topupRowValueLabel'] = '已使用 7.5%（剩餘 30 億個憑證）'
        result = parse_subscription_response(flight(sub))
        self.assertNotIn('plan', result)
        self.assertTrue(result['weekly_reset'].endswith(' UTC'))
        self.assertEqual(result['extra_used_pct'], 7.5)
        self.assertEqual(result['extra_left'], '剩餘 30 億個憑證')

    def test_ambiguous_truncated_and_oversized_flight_are_rejected(self):
        for body in (flight() + '\n1:{}', flight() + '\n01:{}', flight()[:-5],
                     '0:{"a":"$@1"}\n1:{"success":true,"subscription":null}', 'x' * 65537):
            with self.subTest(length=len(body)), self.assertRaises(SubscriptionResponseError):
                parse_subscription_response(body)

    def test_action_id_is_validated_before_embedding_in_javascript(self):
        for action in ('', 'a' * 39, 'a' * 65, 'A' * 40, 'g' * 40, '";alert(1);//', None):
            with self.subTest(action=action), self.assertRaises(ValueError):
                build_subscription_fetch_js(action)


@unittest.skipUnless(shutil.which('node'), 'Node is required for offline browser-JS tests')
class BrowserHelperTests(unittest.TestCase):
    def run_js(self, helper, setup, checks):
        code = '''const vm=require('node:vm'),assert=require('node:assert/strict');
        (async()=>{
          const ctx={URL,AbortController,TextDecoder,TextEncoder,Response,setTimeout,clearTimeout};
        ''' + setup + '\nconst result=JSON.parse(await vm.runInNewContext(' + json.dumps(helper) + ',ctx));\n' + checks + '''
        })().catch(e=>{console.error(e);process.exitCode=1;});'''
        result = subprocess.run(['node', '-e', code], capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_discovery_uses_current_public_bundle_and_exact_action(self):
        correct = '(0,x.createServerReference)("' + ACTION + '",x,void 0,x,"fetchSubscriptionAction")'
        wrong = '(0,x.createServerReference)("' + 'a' * 40 + '",x,void 0,x,"anotherAction"),'
        setup = '''
          const urls=['https://evil.test/_next/static/chunks/a.js','http://muse.ai/_next/static/chunks/b.js',
            'https://user:pass@muse.ai/_next/static/chunks/b.js','https://muse.ai/api/session',
            'https://muse.ai/_next/static/chunks/current.js'];
          ctx.document={scripts:urls.map(src=>({src}))};
          ctx.performance={getEntriesByType:()=>[{name:urls.at(-1)}]};
          const calls=[];
          ctx.fetch=async(url,opts)=>{calls.push({url,opts});return new Response(SOURCE)};
        '''.replace('SOURCE', json.dumps(wrong + correct))
        self.run_js(DISCOVER_SUBSCRIPTION_ACTION_JS, setup, '''
          assert.equal(result.action,"''' + ACTION + '''");
          assert.equal(result.script,'https://muse.ai/_next/static/chunks/current.js');
          assert.equal(calls.length,1);assert.equal(calls[0].opts.credentials,'omit');
          assert.equal(calls[0].opts.redirect,'error');
        ''')

    def test_discovery_caps_sources_and_parallel_requests(self):
        self.run_js(DISCOVER_SUBSCRIPTION_ACTION_JS, '''
          ctx.document={scripts:Array.from({length:300},(_,i)=>({src:'https://muse.ai/_next/static/chunks/'+i+'.js'}))};
          ctx.performance={getEntriesByType:()=>[]};
          let calls=0,active=0,maxActive=0;
          ctx.fetch=async()=>{calls++;active++;maxActive=Math.max(active,maxActive);
            await new Promise(r=>setImmediate(r));active--;return new Response('unrelated script')};
        ''', '''assert.equal(calls,256);assert.ok(maxActive<=6);assert.equal(result.error,'action_not_found');''')

    def test_discovery_never_bridges_closed_calls_or_complex_arguments(self):
        for source in (
            '(0,x.createServerReference)("' + ACTION + '",x,void 0,x,"anotherAction"), unrelated("fetchSubscriptionAction")',
            '(0,x.createServerReference)("' + ACTION + '",callSomething(),void 0,x,"fetchSubscriptionAction")',
            '(0,x.createServerReference)("' + ACTION + '",x,void 0,x,"fetchSubscriptionActionExtra")',
        ):
            with self.subTest(source=source):
                setup = '''
                  ctx.document={scripts:[{src:'https://muse.ai/_next/static/chunks/a.js'}]};
                  ctx.performance={getEntriesByType:()=>[]};
                  ctx.fetch=async()=>new Response(SOURCE);
                '''.replace('SOURCE', json.dumps(source))
                self.run_js(DISCOVER_SUBSCRIPTION_ACTION_JS, setup,
                            "assert.equal(result.error,'action_not_found');assert.equal(result.action,undefined);")

    def test_discovery_abort_has_a_twelve_second_budget(self):
        self.run_js(DISCOVER_SUBSCRIPTION_ACTION_JS, '''
          ctx.document={scripts:[{src:'https://muse.ai/_next/static/chunks/a.js'}]};ctx.performance={getEntriesByType:()=>[]};
          let timeout;
          ctx.setTimeout=(fn,ms)=>{timeout=ms;setImmediate(fn);return 1};ctx.clearTimeout=()=>{};
          ctx.fetch=(url,opts)=>new Promise((resolve,reject)=>opts.signal.addEventListener('abort',()=>reject(Error('cancelled'))));
        ''', '''assert.equal(timeout,12000);assert.equal(result.error,'action_not_found');''')

    def test_same_origin_post_contract_and_success_body(self):
        self.run_js(build_subscription_fetch_js(ACTION), '''
          ctx.location={origin:'https://muse.ai'};const calls=[];
          ctx.fetch=async(url,opts)=>{calls.push({url,opts});return new Response('0:{"a":"$@1"}\\n1:{}',{headers:{'content-type':'text/x-component'}})};
        ''', '''
          assert.equal(calls.length,1);assert.equal(calls[0].url,'/thread/new');
          const opts=calls[0].opts;assert.equal(opts.method,'POST');assert.equal(opts.credentials,'same-origin');
          assert.equal(opts.headers['Next-Action'],"''' + ACTION + '''");
          assert.equal(opts.headers.Accept,'text/x-component');
          assert.equal(opts.headers['Content-Type'],'text/plain;charset=UTF-8');
          assert.deepEqual(JSON.parse(opts.body),[{includeAgreement:false}]);
          assert.equal(result.status,200);assert.equal(result.content_type,'text/x-component');
          assert.equal(result.action_not_found,false);
          assert.ok(result.body.startsWith('0:'));
        ''')

    def test_action_not_found_header_returns_before_reading_unrelated_body(self):
        self.run_js(build_subscription_fetch_js(ACTION), '''
          ctx.location={origin:'https://muse.ai'};let signal;
          ctx.fetch=async(url,opts)=>{signal=opts.signal;return {
            status:404,headers:new Headers({'content-type':'text/x-component','x-nextjs-action-not-found':'1'}),
            get body(){throw Error('unrelated RSC body must not be read')}
          }};
        ''', '''
          assert.equal(result.status,404);assert.equal(result.action_not_found,true);
          assert.equal(result.body,'');assert.equal(result.error,undefined);assert.ok(signal.aborted);
        ''')

    def test_wrong_origin_never_posts(self):
        self.run_js(build_subscription_fetch_js(ACTION), '''
          ctx.location={origin:'https://evil.test'};ctx.fetch=()=>{throw Error('should not fetch')};
        ''', '''assert.equal(result.status,0);assert.equal(result.error,'invalid_origin');''')

    def test_response_size_is_bounded_by_bytes_and_not_truncated_into_success(self):
        self.run_js(build_subscription_fetch_js(ACTION), '''
          ctx.location={origin:'https://muse.ai'};
          ctx.fetch=async()=>new Response('用'.repeat(22000),{headers:{'content-type':'text/x-component'}});
        ''', '''assert.equal(result.status,200);assert.equal(result.error,'response_too_large');assert.equal(result.body,'');''')

    def test_fetch_timeout_uses_twenty_seconds_and_never_returns_partial_data(self):
        self.run_js(build_subscription_fetch_js(ACTION), '''
          ctx.location={origin:'https://muse.ai'};let timeout;
          ctx.setTimeout=(fn,ms)=>{timeout=ms;setImmediate(fn);return 1};ctx.clearTimeout=()=>{};
          ctx.fetch=(url,opts)=>new Promise((resolve,reject)=>opts.signal.addEventListener('abort',()=>reject(Error('secret failure'))));
        ''', '''assert.equal(timeout,20000);assert.equal(result.error,'request_timeout');assert.equal(result.body,'');assert.ok(!JSON.stringify(result).includes('secret'));''')


if __name__ == '__main__':
    unittest.main(verbosity=2)
