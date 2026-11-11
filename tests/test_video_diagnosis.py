"""Offline diagnosis-page/clock regressions. No account or upstream requests."""
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine as module


class Clock:
    def __init__(self):
        self.elapsed = 0.0

    def monotonic(self):
        return 100.0 + self.elapsed

    def time(self):
        return 1000.0 + self.elapsed

    def sleep(self, seconds):
        self.elapsed += seconds


class Page:
    def __init__(self, clock):
        self.clock = clock
        self.sent = []
        self.deadlines = []
        self.timeouts = []
        self.in_scope = False
        self.before = dict(cnt=1, txt="这次没有出片。", stop=False, cards=0,
                           attachments=[], path="/thread/failed", origin="https://muse.ai")
        self.after = lambda: dict(self.before, cnt=2, txt="原因：动作太多。建议：保留一个连续动作。",
                                 stop=self.clock.elapsed < 1)

    @contextmanager
    def bounded_io(self, deadline):
        self.deadlines.append(deadline)
        self.in_scope = True
        try:
            yield
        finally:
            self.in_scope = False

    def js(self, _script, timeout=None):
        assert self.in_scope, "diagnosis CDP read escaped its deadline scope"
        self.timeouts.append(timeout)
        value = self.after() if self.sent else self.before
        if isinstance(value, Exception):
            raise value
        return json.dumps(value) if not isinstance(value, str) else value


class VideoDiagnosisTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.clock_patch = patch.object(module, "time", self.clock)
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.engine = object.__new__(module.MuseEngine)
        self.page = Page(self.clock)
        self.engine.page = self.page
        for name in ("ensure_page", "reset_thread", "chat_stream", "_poll_chat"):
            setattr(self.engine, name, Mock(side_effect=AssertionError(f"must not call {name}")))
        def send(prompt, *, deadline=None, stop_event=None, before_submit=None):
            self.assertIs(self.engine.page, self.page)
            self.assertTrue(self.page.in_scope)
            self.assertEqual(deadline, self.page.deadlines[-1])
            before_submit()
            self.page.sent.append(prompt)
            return "clicked"
        self.engine._send = Mock(side_effect=send)

    def diagnose(self, **kwargs):
        return self.engine.diagnose_video_failure("只诊断原因，不要生成媒体。", **kwargs)

    def assert_unknown(self, **kwargs):
        with self.assertRaises(module.MuseGenerationError) as caught:
            self.diagnose(**kwargs)
        self.assertNotIsInstance(caught.exception, module.MuseTextOnlyError)
        self.assertFalse(self.page.in_scope)
        return str(caught.exception)

    def test_same_page_one_send_final_snapshot_not_repeated_deltas(self):
        final = "原因：动作太多。建议：保留一个连续动作。"
        self.page.after = lambda: dict(self.page.before, cnt=2,
            txt="原因：动作" if self.clock.elapsed < .4 else final,
            stop=self.clock.elapsed < 1)
        self.assertEqual(self.diagnose(), final)
        self.assertEqual(self.engine._send.call_count, 1)
        self.assertEqual(len(self.page.deadlines), 1)
        self.assertGreaterEqual(self.clock.elapsed + 1e-9, 2.2)
        self.assertIs(self.engine.page, self.page)
        self.assertTrue(all(0 < value <= 45 for value in self.page.timeouts))
        for name in ("ensure_page", "reset_thread", "chat_stream", "_poll_chat"):
            getattr(self.engine, name).assert_not_called()

    def test_unsafe_baseline_never_sends(self):
        cases = [dict(stop=True), dict(cnt=0), dict(txt=" "), dict(cards=1),
                 dict(attachments=[{"src": "blob:cover"}]), dict(cnt=False), dict(cards="0"),
                 dict(attachments=None), dict(path=""), dict(stop=None)]
        baseline = self.page.before
        for fields in cases:
            with self.subTest(fields=fields):
                self.page.before = dict(baseline, **fields)
                self.assert_unknown()
                self.engine._send.assert_not_called()
        for invalid in (RuntimeError("CDP disconnected"), "null", "bad-json"):
            with self.subTest(invalid=repr(invalid)):
                self.page.before = invalid
                self.assert_unknown()
                self.engine._send.assert_not_called()

    def test_read_error_and_malformed_snapshot_do_not_complete(self):
        for invalid in (RuntimeError("CDP disconnected"), "null", "bad-json", {"cnt": 2}):
            with self.subTest(invalid=repr(invalid)):
                self.page.sent.clear()
                self.page.after = lambda: invalid
                self.assert_unknown()
                self.assertEqual(len(self.page.sent), 1)

    def test_new_media_or_source_less_cover_aborts_diagnosis(self):
        for fields in (dict(cards=1), dict(attachments=[{"src": "blob:video", "hasVideo": True}]),
                       dict(attachments=[{"src": "blob:cover", "hasVideo": False}])):
            with self.subTest(fields=fields):
                self.page.sent.clear()
                self.page.after = lambda: dict(self.page.before, cnt=2, **fields)
                self.assertIn("媒体或封面", self.assert_unknown())
                self.assertEqual(len(self.page.sent), 1)

    def test_path_change_aborts(self):
        self.page.after = lambda: dict(self.page.before, path="/thread/new")
        self.assertIn("路径", self.assert_unknown())
        self.assertEqual(len(self.page.sent), 1)

    def test_origin_change_with_same_path_aborts(self):
        self.page.after = lambda: dict(self.page.before, origin="https://other.invalid")
        self.assertIn("来源", self.assert_unknown())

    def test_pre_submit_guard_catches_changes_during_send_preparation(self):
        baseline = self.page.before
        for fields in (dict(stop=True), dict(cards=1), dict(path="/thread/new"),
                       dict(origin="https://other.invalid"), dict(cnt=2, txt="新的生成结果说明")):
            with self.subTest(fields=fields):
                self.page.before = baseline
                def send(_prompt, *, before_submit=None, **_kwargs):
                    self.page.before = dict(baseline, **fields)
                    before_submit()
                    self.fail("changed page reached diagnostic submission")
                self.engine._send = Mock(side_effect=send)
                self.assert_unknown()
                self.assertEqual(self.page.sent, [])

    def test_page_object_change_aborts_even_if_path_matches(self):
        def switched():
            self.engine.page = Page(self.clock)
            return self.page.before
        self.page.after = switched
        self.assertIn("页面已变化", self.assert_unknown())

    def test_stop_still_present_does_not_finish_and_budget_caps_at_45(self):
        self.page.after = lambda: dict(self.page.before, cnt=2, txt="稳定但未完成", stop=True)
        self.assertIn("超时", self.assert_unknown(timeout=120))
        self.assertAlmostEqual(self.clock.elapsed, 45)
        self.assertEqual(len(self.page.sent), 1)

    def test_new_text_without_observed_stop_lifecycle_is_unknown(self):
        self.page.after = lambda: dict(self.page.before, cnt=2, txt="未观察到结束过程")
        self.assertIn("超时", self.assert_unknown(timeout=3))
        self.assertAlmostEqual(self.clock.elapsed, 3)

    def test_old_reply_is_not_reused(self):
        self.page.after = lambda: dict(self.page.before, stop=self.clock.elapsed < .5)
        self.assertIn("超时", self.assert_unknown(timeout=3))

    def test_message_count_regression_is_unknown(self):
        self.page.after = lambda: dict(self.page.before, cnt=0)
        self.assertIn("消息已变化", self.assert_unknown())

    def test_changed_final_text_restarts_stability(self):
        self.page.after = lambda: dict(self.page.before, cnt=2, stop=self.clock.elapsed < 1,
            txt="初稿" if self.clock.elapsed < 1.8 else "最终完整建议")
        self.assertEqual(self.diagnose(), "最终完整建议")
        self.assertGreaterEqual(self.clock.elapsed, 3)

    def test_reappearing_stop_restarts_stability(self):
        self.page.after = lambda: dict(self.page.before, cnt=2,
            stop=self.clock.elapsed < 1 or 1.6 <= self.clock.elapsed < 2)
        self.diagnose()
        self.assertGreaterEqual(self.clock.elapsed, 3.2)

    def test_cancel_before_send_and_during_wait(self):
        self.assertIn("取消", self.assert_unknown(stop_event=SimpleNamespace(is_set=lambda: True)))
        self.engine._send.assert_not_called()
        self.assertIn("取消", self.assert_unknown(stop_event=SimpleNamespace(is_set=lambda: self.clock.elapsed >= .6)))
        self.assertEqual(len(self.page.sent), 1)

    def test_unconfirmed_send_does_not_repeat_question(self):
        self.engine._send = Mock(return_value="no-button")
        self.assertIn("发送未确认", self.assert_unknown())
        self.engine._send.assert_called_once()

    def test_late_snapshot_cannot_succeed_after_deadline(self):
        def late():
            self.clock.sleep(4)
            return dict(self.page.before, cnt=2)
        self.page.after = late
        self.assertIn("超时", self.assert_unknown(timeout=3))

    def test_invalid_inputs_never_send(self):
        for timeout in (0, -1, float("inf"), float("nan")):
            self.assert_unknown(timeout=timeout)
        self.engine._send.assert_not_called()

    def test_actual_readiness_wait_respects_monotonic_deadline(self):
        self.engine._raise_if_workspace_unavailable = lambda *_: None
        page = SimpleNamespace(js=Mock(return_value="connecting"))
        with self.assertRaisesRegex(module.MuseGenerationError, "超时"):
            self.engine._wait_ws_ready(page, timeout=8, io_deadline=self.clock.monotonic() + .3)
        self.assertAlmostEqual(self.clock.elapsed, .3)

    def test_diagnostic_readiness_does_not_swallow_cdp_error(self):
        self.engine._raise_if_workspace_unavailable = lambda *_: None
        page = SimpleNamespace(js=Mock(side_effect=RuntimeError("read failed")))
        with self.assertRaisesRegex(RuntimeError, "read failed"):
            self.engine._wait_ws_ready(page, io_deadline=self.clock.monotonic() + 8)
        self.assertEqual(self.clock.elapsed, 0)
        page.js.assert_called_once()

    def test_actual_send_does_not_send_after_short_deadline(self):
        self.engine._raise_if_workspace_unavailable = lambda *_: None
        self.engine._wait_ws_ready = lambda *_args, **_kwargs: True
        self.engine.page = SimpleNamespace(js=Mock(return_value=None), send=Mock())
        with self.assertRaisesRegex(module.MuseGenerationError, "超时"):
            module.MuseEngine._send(self.engine, "diagnostic", deadline=self.clock.monotonic() + .05)
        self.engine.page.send.assert_not_called()
        self.assertAlmostEqual(self.clock.elapsed, .05)

    def prepare_actual_sender(self, click="no-button", on_focus=None):
        self.engine._raise_if_workspace_unavailable = lambda *_: None
        self.engine._wait_ws_ready = lambda *_args, **_kwargs: True
        def evaluate(script):
            if "scrollIntoView" in script:
                if on_focus:
                    on_focus()
                return None
            if "getBoundingClientRect" in script:
                return json.dumps({"x": 1, "y": 1})
            if "Object.getOwnPropertyDescriptor" in script:
                return 10
            if "b.click();return 'clicked'" in script:
                return click
            raise AssertionError("unexpected script")
        page = SimpleNamespace(js=Mock(side_effect=evaluate), send=Mock())
        self.engine.page = page
        return page

    def test_actual_diagnostic_send_has_no_enter_fallback(self):
        page = self.prepare_actual_sender()
        with self.assertRaisesRegex(module.MuseGenerationError, "未执行键盘补发"):
            module.MuseEngine._send(self.engine, "diagnostic", deadline=self.clock.monotonic() + 8)
        self.assertTrue(all(call.args[0] != "Input.dispatchKeyEvent" for call in page.send.call_args_list))

    def test_actual_send_focus_read_error_is_not_ignored(self):
        def fail():
            raise RuntimeError("focus read disconnected")
        page = self.prepare_actual_sender(on_focus=fail)
        with self.assertRaisesRegex(RuntimeError, "focus read disconnected"):
            module.MuseEngine._send(self.engine, "diagnostic", deadline=self.clock.monotonic() + 8)
        page.send.assert_not_called()

    def test_actual_send_rejects_page_change_before_using_new_connection(self):
        replacement = SimpleNamespace(js=Mock(), send=Mock())
        page = self.prepare_actual_sender(on_focus=lambda: setattr(self.engine, "page", replacement))
        with self.assertRaisesRegex(module.MuseGenerationError, "页面已变化"):
            module.MuseEngine._send(self.engine, "diagnostic", deadline=self.clock.monotonic() + 8)
        page.send.assert_not_called()
        replacement.js.assert_not_called()
        replacement.send.assert_not_called()

    def test_actual_send_executes_guard_before_click(self):
        page = self.prepare_actual_sender(click="clicked")
        guard = Mock(side_effect=module.MuseGenerationError("guard blocked"))
        with self.assertRaisesRegex(module.MuseGenerationError, "guard blocked"):
            module.MuseEngine._send(self.engine, "diagnostic", deadline=self.clock.monotonic() + 8, before_submit=guard)
        guard.assert_called_once()
        self.assertFalse(any("b.click();return 'clicked'" in call.args[0] for call in page.js.call_args_list))

    def test_snapshot_javascript_counts_source_less_media_card(self):
        script = """
        const bubble = {className:'hatch-chat-groupable-bubble hatch-agent-bubble-bg', innerText:'失败说明'};
        const card = {closest:()=>null, querySelector:()=>null, tagName:'DIV', getAttribute:()=> 'hatch-chat-attachment-presentation-video'};
        const document = {querySelector:()=>null, querySelectorAll:s => s.includes('groupable-bubble') ? [bubble] : s.startsWith('[data-testid^=') ? [card] : []};
        const location = {pathname:'/thread/failed', origin:'https://muse.ai'};
        console.log(%s);
        """ % module.MuseEngine._VIDEO_DIAGNOSIS_JS
        state = json.loads(subprocess.check_output(["node", "-e", script], text=True, encoding="utf-8"))
        self.assertEqual(state["cnt"], 1)
        self.assertEqual(state["txt"], "失败说明")
        self.assertEqual(state["cards"], 1)
        self.assertEqual(state["attachments"], [])


if __name__ == "__main__":
    unittest.main()
