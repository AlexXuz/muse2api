"""Offline app retry policy: run with python tests/test_video_text_retry.py."""
import asyncio
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
HOME = tempfile.TemporaryDirectory(prefix="muse-video-retry-test-")
os.environ.update(MUSE2API_HOME=HOME.name, MUSE2API_PROFILE_ROOT=HOME.name,
                  MUSE2API_KEY="test-only", MUSE2API_VIDEO_TEXT_RETRY_LIMIT="0")
spec = importlib.util.spec_from_file_location("video_text_retry_target", ROOT / "app.py")
app = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = app
spec.loader.exec_module(app)
DIAGNOSIS = json.dumps({"decision": "retry_same_content", "reason": "Separate visual action and voiceover.",
                       "adjustments": ["separate_audio_visual"],
                       "changed_requirements": []})


class VideoTextRetryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.first = app.store.add_account({"hatch_sess": "fixture-one", "hatch_vml": "fixture-one",
                                          "hatch_native_auth_device": "fixture-one"}, "one")
        cls.second = app.store.add_account({"hatch_sess": "fixture-two", "hatch_vml": "fixture-two",
                                           "hatch_native_auth_device": "fixture-two"}, "two")
        cls.result = {"filename": "local-video.mp4", "size": 42, "kind": "video"}

    @contextmanager
    def engine_case(self, effects, limit=1, diagnosis=DIAGNOSIS):
        diagnostic_kwargs = {"side_effect": diagnosis} if isinstance(diagnosis, Exception) or callable(diagnosis) else {"return_value": diagnosis}
        with patch.object(app.CFG, "video_text_retry_limit", limit), \
             patch.object(app, "_renew_and_persist", side_effect=lambda aid, **_: app.store.get_account(aid)), \
             patch.object(app, "_sync_cookies"), patch.object(app.engine, "start"), \
             patch.object(app.engine, "close_current_page"), patch.object(app.engine, "reset_thread"), \
             patch.object(app.engine, "generate", side_effect=effects) as generate, \
             patch.object(app.engine, "diagnose_video_failure", create=True, **diagnostic_kwargs) as diagnose:
            generate.diagnose = diagnose
            yield generate
        self.assertFalse(app.ACCOUNT_POOL.busy_accounts)
        self.assertFalse(app.GEN_LOCK.locked())

    def run_video(self, **kwargs):
        return app._run_generation("original scene", "video", 600,
                                   account_id=self.first["id"], **kwargs)

    def test_disabled_policy_never_retries_text_only(self):
        events = []
        with self.engine_case([app.MuseTextOnlyError("only text")], limit=0) as generate:
            with self.assertRaises(app.MuseTextOnlyError):
                self.run_video(on_attempt=events.append)
            self.assertEqual(generate.call_count, 1)
            generate.diagnose.assert_not_called()
        self.assertEqual(events[-1]["attempts"], 1)
        self.assertEqual(events[-1]["retry_count"], 0)

    def test_successful_first_attempt_does_not_use_retry(self):
        for limit in [0, 1]:
            with self.subTest(limit=limit), self.engine_case([self.result], limit=limit) as generate:
                result, aid = self.run_video()
                self.assertEqual(result, self.result)
                self.assertEqual(aid, self.first["id"])
                self.assertEqual(generate.call_count, 1)
                generate.diagnose.assert_not_called()

    def test_second_attempt_preserves_original_prompt_references_and_same_account(self):
        references = ["local-reference-a.png", "local-reference-b.png"]
        original = "Original wording, 9:16, duration 12 seconds; keep the referenced characters."
        events = []
        with self.engine_case([app.MuseTextOnlyError("only text"), self.result]) as generate:
            result, aid = app._run_generation(original, "video", 600, account_id=self.first["id"],
                                              reference_images=references, on_attempt=events.append)
            self.assertEqual(result, self.result)
            self.assertEqual(generate.call_count, 2)
            first, second = generate.call_args_list
            self.assertEqual(first.args[1], original)
            self.assertTrue(second.args[1].endswith(original))
            self.assertLess(second.args[1].index("按原要求分别组织"), second.args[1].index(original))
            self.assertNotIn("Separate visual action and voiceover.", second.args[1])
            self.assertEqual(generate.diagnose.call_count, 1)
            self.assertIn(original, generate.diagnose.call_args.args[0])
            self.assertLessEqual(generate.diagnose.call_args.kwargs["timeout"], 45)
            self.assertEqual(first.args[0], second.args[0])
            self.assertEqual(first.kwargs["account_id"], second.kwargs["account_id"])
            self.assertEqual(aid, self.first["id"])
            for call in [first, second]:
                self.assertEqual(call.kwargs["reference_images"], references)
            self.assertLessEqual(second.kwargs["timeout"], first.kwargs["timeout"])
        self.assertEqual([event["attempts"] for event in events if "attempts" in event], [1, 1, 2])
        self.assertEqual(events[-1]["retry_count"], 1)
        self.assertEqual(events[-1]["retry_reason"], "video_text_only")
        self.assertEqual(references, ["local-reference-a.png", "local-reference-b.png"])

    def test_second_failure_stops_even_if_config_limit_is_too_large(self):
        for error in [app.MuseTextOnlyError("still text"), app.MuseGenerationError("unknown"), TimeoutError("timeout")]:
            with self.subTest(error=type(error).__name__), self.engine_case([app.MuseTextOnlyError("text"), error], limit=99) as generate:
                with self.assertRaises(type(error)):
                    self.run_video()
                self.assertEqual(generate.call_count, 2)
                self.assertEqual(generate.diagnose.call_count, 1)

    def test_refusal_quota_auth_and_unknown_failures_never_rotate_or_retry(self):
        errors = [app.MuseModelRefusalError("region unavailable"),
                  app.MuseModelRefusalError("content rejected"),
                  app.MuseModelRefusalError("quota exhausted"),
                  app.MuseGenerationError("insufficient credits"),
                  app.MuseGenerationError("generation status unknown"), TimeoutError("timeout"),
                  app.MuseAuthError("expired"), app.MuseProxyError("network error"),
                  app.MuseWorkspaceUnavailableError("workspace unavailable")]
        for error in errors:
            with self.subTest(error=str(error)), self.engine_case([error]) as generate:
                with self.assertRaises(type(error)):
                    self.run_video()
                self.assertEqual(generate.call_count, 1)
                generate.diagnose.assert_not_called()
                self.assertEqual(generate.call_args.kwargs["account_id"], self.first["id"])
            # Health marking is exercised, but keep the fixture usable for subsequent cases.
            app.store.update_account(self.first["id"], enabled=True, ok=True, session_ok=True, workspace_available=True)

    def test_insufficient_remaining_deadline_does_not_count_an_unexecuted_retry(self):
        clock = [1000.0]
        events = []
        def text_only(*_args, **_kwargs):
            clock[0] += 481
            raise app.MuseTextOnlyError("only text")
        with self.engine_case(text_only) as generate, patch.object(app.time, "monotonic", side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(app.MuseTextOnlyError, "120"):
                self.run_video(on_attempt=events.append)
            self.assertEqual(generate.call_count, 1)
            generate.diagnose.assert_not_called()
        self.assertEqual(events[-1]["attempts"], 1)
        self.assertEqual(events[-1]["retry_count"], 0)
        self.assertEqual(events[-1]["retry_reason"], "insufficient_deadline")

    def test_single_reference_and_initial_output_contract_are_preserved(self):
        req = app.VideoRequest(prompt="untouched original scene", duration=12, aspect_ratio="9:16", images=["image-a"])
        prompt = app.build_video_prompt(req)
        self.assertIn(req.prompt, prompt)
        self.assertIn("12", prompt)
        self.assertIn("9:16", prompt)
        self.assertTrue(prompt.endswith(app.VIDEO_OUTPUT_CONTRACT))
        with self.engine_case([app.MuseTextOnlyError("only text"), self.result]) as generate:
            self.run_video(reference_images=["local-reference.png"])
            self.assertEqual([call.kwargs["reference_image"] for call in generate.call_args_list],
                             ["local-reference.png", "local-reference.png"])

    def test_same_task_persists_attempt_metadata_and_polling_does_not_retry(self):
        for effects, expected_code, attempts in [
            ([self.result], None, 1),
            ([app.MuseTextOnlyError("only text"), self.result], None, 2),
            ([app.MuseTextOnlyError("only text"), app.MuseTextOnlyError("still text")], "video_text_only", 2),
            ([app.MuseModelRefusalError("content rejected")], "model_refusal", 1),
            ([app.MuseGenerationError("账号额度不足，请稍后再试")], "quota_exceeded", 1),
            ([TimeoutError("unknown completion")], "generation_unknown", 1),
        ]:
            with self.subTest(code=expected_code, attempts=attempts), self.engine_case(effects) as generate:
                before = set(app.store.tasks)
                written = threading.Event()
                update_task = app.store.update_task
                def record_update(tid, **metadata):
                    result = update_task(tid, **metadata)
                    if metadata.get("status") in ("completed", "failed"):
                        written.set()
                    return result
                with patch.object(app.store, "update_task", side_effect=record_update):
                    response = asyncio.run(app.create_video(app.VideoRequest(prompt="API original", duration=8, size="16:9")))
                    self.assertTrue(written.wait(3), "worker did not persist its terminal state")
                tid = response["id"]
                task = app.get_video(tid)
                self.assertEqual(set(app.store.tasks) - before, {tid})
                self.assertEqual(task["attempts"], attempts)
                self.assertEqual(task["retry_count"], attempts - 1)
                self.assertEqual(task["error_code"], expected_code)
                self.assertEqual(task["stage"], "failed" if expected_code else "completed")
                self.assertEqual(task["prompt"], "API original")
                saved = json.loads(Path(app.CFG.tasks_file).read_text(encoding="utf-8"))[tid]
                for key in ["attempts", "retry_count", "retry_reason", "stage", "error_code",
                            "diagnostic_status", "diagnostic_summary", "diagnostic_decision", "diagnostic_reply", "retry_prompt"]:
                    self.assertEqual(saved[key], task[key])
                if attempts == 2:
                    self.assertEqual(task["diagnostic_status"], "completed")
                    self.assertEqual(task["diagnostic_decision"], "retry_same_content")
                    self.assertEqual(task["diagnostic_reply"], DIAGNOSIS)
                    self.assertEqual(task["retry_prompt"], generate.call_args_list[1].args[1])
                    self.assertTrue(task["retry_prompt"].endswith(task["api_prompt"]))
                else:
                    self.assertEqual(task["diagnostic_status"], "not_requested")
                    self.assertIsNone(task["diagnostic_reply"])
                for _ in range(3):
                    self.assertEqual(app.get_video(tid)["id"], tid)
                self.assertEqual(generate.call_count, attempts)
                if not expected_code:
                    self.assertEqual(task["result"]["url"], task["video"]["url"])

    def test_diagnostic_keeps_same_lock_account_page_and_does_not_count_as_generation(self):
        events = []
        def diagnose(_prompt, **_kwargs):
            self.assertTrue(app.GEN_LOCK.locked())
            self.assertIn(self.first["id"], app.ACCOUNT_POOL.busy_accounts)
            self.assertEqual(events[-1]["stage"], "retrying")
            self.assertEqual(events[-1]["attempts"], 1)
            self.assertEqual(events[-1]["retry_count"], 0)
            app.engine.close_current_page.assert_not_called()
            return DIAGNOSIS
        with self.engine_case([app.MuseTextOnlyError("text"), self.result], diagnosis=diagnose) as generate:
            self.run_video(on_attempt=events.append)
            self.assertEqual(generate.call_count, 2)
            self.assertEqual(generate.diagnose.call_count, 1)
            app.engine.start.assert_called_once()
            app.engine.close_current_page.assert_called_once()

    def test_diagnostic_refusal_and_requirement_changes_never_start_second_generation(self):
        cases = [
            ({"decision": "blocked", "reason": "Unavailable in this region.", "adjustments": [], "changed_requirements": []}, "model_refusal"),
            ({"decision": "needs_user_change", "reason": "Must replace the mother reference.", "adjustments": [], "changed_requirements": ["replace mother reference"]}, "video_revision_required"),
            ({"decision": "retry_same_content", "reason": "A change is needed.", "adjustments": ["reinforce_video_output"], "changed_requirements": ["delete dialogue"]}, "video_revision_required"),
            ("因为地区限制，无法生成视频。", "model_refusal"),
            ("必须删除台词才能生成。", "video_revision_required"),
            ("Your region is not supported for video generation.", "model_refusal"),
            ("Out of credits.", "model_refusal"),
            ("额度已达到上限。", "model_refusal"),
            ("这种内容不能生成。", "model_refusal"),
            ("请把画外音去掉再生成。", "video_revision_required"),
            ("Remove the narration and retry.", "video_revision_required"),
            ("需要先缩短视频时长。", "video_revision_required"),
        ]
        for diagnosis, code in cases:
            with self.subTest(code=code), self.engine_case([app.MuseTextOnlyError("text")], diagnosis=json.dumps(diagnosis) if isinstance(diagnosis, dict) else diagnosis) as generate:
                with self.assertRaises(app.MuseModelRefusalError) as caught:
                    self.run_video()
                self.assertEqual(app._video_error_code(caught.exception), code)
                self.assertEqual(generate.call_count, 1)
                self.assertEqual(generate.diagnose.call_count, 1)

    def test_completed_unknown_or_invalid_diagnosis_stops_as_text_only(self):
        replies = ["FREE RESPONSE DO NOT COPY", '{"bad":"UNTRUSTED PLAN"}',
                   json.dumps({"decision": "unknown", "reason": "Not certain.", "adjustments": [], "changed_requirements": []})]
        for reply in replies:
            events = []
            with self.subTest(reply=reply), self.engine_case([app.MuseTextOnlyError("text")], diagnosis=reply) as generate:
                with self.assertRaises(app.MuseTextOnlyError) as caught:
                    self.run_video(on_attempt=events.append)
                self.assertEqual(app._video_error_code(caught.exception), "video_text_only")
                self.assertEqual(generate.call_count, 1)
                self.assertEqual(generate.diagnose.call_count, 1)
                self.assertIsNone(events[-1]["retry_prompt"])
                self.assertEqual(events[-1]["diagnostic_status"], "unverified")
                self.assertIn("停止自动重试", events[-1]["diagnostic_summary"])

    def test_old_free_execution_schema_cannot_start_a_second_generation(self):
        examples = ["Remove all dialogue and replace the poster with a plain background.",
                    "删除画外音，把参考海报改成纯色背景。", "把视频改为5秒，去掉母亲，只保留男孩。",
                    "Generate an otherwise ordinary video."]
        for execution in examples:
            diagnosis = json.dumps({"decision": "retry_same_content", "reason": "Reorganize only.",
                                    "execution_prompt": execution, "changed_requirements": []})
            with self.subTest(execution=execution), self.engine_case([app.MuseTextOnlyError("text")], diagnosis=diagnosis) as generate:
                with self.assertRaises(app.MuseModelRefusalError):
                    self.run_video()
                self.assertEqual(generate.call_count, 1)
                self.assertEqual(generate.diagnose.call_count, 1)

    def test_diagnostic_timeout_unknown_media_and_empty_output_stop_without_retry(self):
        for failure in [TimeoutError("diagnostic timeout"), app.MuseGenerationError("media changed"), RuntimeError("CDP unknown"), "", None]:
            events = []
            with self.subTest(failure=type(failure).__name__), self.engine_case([app.MuseTextOnlyError("text")], diagnosis=failure) as generate:
                with self.assertRaises(app.MuseGenerationError) as caught:
                    self.run_video(on_attempt=events.append)
                self.assertEqual(app._video_error_code(caught.exception), "generation_unknown")
                self.assertEqual(generate.call_count, 1)
                self.assertEqual(events[-1]["diagnostic_status"], "failed")

    def test_diagnostic_budget_reserves_120_seconds_and_rechecks_before_retry(self):
        for overrun in [False, True]:
            clock = [1000.0]
            events = []
            def generate_first(*_args, **_kwargs):
                clock[0] += 450
                raise app.MuseTextOnlyError("text")
            def diagnose(_prompt, timeout):
                self.assertEqual(timeout, 30)
                clock[0] += 31 if overrun else 30
                return DIAGNOSIS
            effects = [generate_first, self.result]
            def generate(*args, **kwargs):
                effect = effects.pop(0)
                return effect(*args, **kwargs) if callable(effect) else effect
            with self.subTest(overrun=overrun), self.engine_case(generate, diagnosis=diagnose) as mock, patch.object(app.time, "monotonic", side_effect=lambda: clock[0]):
                if overrun:
                    with self.assertRaises(app.MuseTextOnlyError):
                        self.run_video(on_attempt=events.append)
                    self.assertEqual(mock.call_count, 1)
                    self.assertEqual(events[-1]["retry_count"], 0)
                else:
                    self.run_video(on_attempt=events.append)
                    self.assertEqual(mock.call_count, 2)
                    self.assertEqual(mock.call_args.kwargs["timeout"], 120)

    def test_video_normalization_changes_only_the_leading_known_still_style(self):
        original = 'high-quality 3D CG animation still, poster: hold still. Dialogue: "animation still".'
        req = app.VideoRequest(prompt=original, duration=10, aspect_ratio="16:9", images=["mother", "child", "poster"])
        video = app.build_video_prompt(req)
        self.assertEqual(req.prompt, original)
        self.assertIn('high-quality 3D CG animation, poster: hold still. Dialogue: "animation still".', video)
        self.assertIn("3 张参考图片", video)
        self.assertIn("10 秒", video)
        self.assertIn("16:9", video)
        image = app.build_image_prompt(app.ImageRequest(prompt=original))
        self.assertIn(original, image)

    def test_same_task_persists_diagnostic_failures_without_incrementing_retry(self):
        for diagnosis, code, diagnostic_status in [
            (TimeoutError("unknown"), "generation_unknown", "failed"),
            (json.dumps({"decision": "needs_user_change", "reason": "Must remove dialogue.", "adjustments": [], "changed_requirements": ["dialogue"]}), "video_revision_required", "completed"),
            ("content violates policy", "model_refusal", "completed"),
            ("Uncertain, no structured plan", "video_text_only", "unverified"),
            ("unstructured " * 1200, "video_text_only", "unverified"),
            (json.dumps({"decision": "unknown", "reason": "Not sure.", "adjustments": [], "changed_requirements": []}), "video_text_only", "unverified"),
        ]:
            with self.subTest(code=code), self.engine_case([app.MuseTextOnlyError("text")], diagnosis=diagnosis) as generate:
                written = threading.Event()
                update_task = app.store.update_task
                def record_update(tid, **metadata):
                    result = update_task(tid, **metadata)
                    if metadata.get("status") in ("completed", "failed"):
                        written.set()
                    return result
                with patch.object(app.store, "update_task", side_effect=record_update):
                    response = asyncio.run(app.create_video(app.VideoRequest(prompt="keep original dialogue")))
                    self.assertTrue(written.wait(3))
                task = app.get_video(response["id"])
                self.assertEqual(task["attempts"], 1)
                self.assertEqual(task["retry_count"], 0)
                self.assertEqual(task["diagnostic_status"], diagnostic_status)
                self.assertEqual(task["error_code"], code)
                self.assertIsNone(task["retry_prompt"])
                saved = json.loads(Path(app.CFG.tasks_file).read_text(encoding="utf-8"))[task["id"]]
                self.assertEqual(saved["diagnostic_summary"], task["diagnostic_summary"])
                self.assertEqual(saved["diagnostic_decision"], task["diagnostic_decision"])
                expected_reply = diagnosis[:app.MAX_DIAGNOSTIC_CHARS] if isinstance(diagnosis, str) else None
                self.assertEqual(saved["diagnostic_reply"], expected_reply)
                self.assertEqual(task["diagnostic_reply"], expected_reply)
                self.assertEqual(generate.call_count, 1)

    def test_admin_exposes_only_busy_boolean_and_retry_limit_metadata(self):
        with patch.object(app.CFG, "video_text_retry_limit", 1):
            self.assertFalse(app.admin_status()["generation_busy"])
            with app.GEN_LOCK:
                status = app.admin_status()
                self.assertIs(status["generation_busy"], True)
                self.assertEqual(status["config"]["video_text_retry_limit"], 1)
                self.assertNotIn("busy_accounts", status)

    def test_restart_marks_interrupted_video_unknown_and_preserves_finished_diagnosis(self):
        running = app.store.create_task("video", "synthetic interrupted diagnostic")
        app.store.update_task(running["id"], status="processing", stage="retrying", attempts=1,
                              retry_count=0, diagnostic_status="running")
        diagnosed = app.store.create_task("video", "synthetic completed diagnostic")
        app.store.update_task(diagnosed["id"], status="processing", stage="retrying", attempts=2,
                              retry_count=1, diagnostic_status="completed", diagnostic_decision="retry_same_content",
                              diagnostic_summary="Preserved completed diagnosis.", retry_prompt="Fixed execution and original.")
        queued = app.store.create_task("video", "synthetic queued")
        app.store.update_task(queued["id"], stage="queued", attempts=0, diagnostic_status="not_requested")
        completed = app.store.create_task("video", "synthetic completed video")
        app.store.update_task(completed["id"], status="completed", stage="completed", error_code=None)
        with self.engine_case([]) as generate, patch.object(app, "_keepalive_loop", new_callable=AsyncMock):
            asyncio.run(app._startup())
            generate.assert_not_called()
            generate.diagnose.assert_not_called()
        for task in [running, diagnosed, queued]:
            saved = app.get_video(task["id"])
            self.assertEqual(saved["status"], "failed")
            self.assertEqual(saved["stage"], "failed")
            self.assertEqual(saved["error_code"], "generation_unknown")
        interrupted = app.get_video(running["id"])
        self.assertEqual(interrupted["diagnostic_status"], "failed")
        self.assertEqual(interrupted["diagnostic_decision"], "unknown")
        self.assertEqual(interrupted["retry_count"], 0)
        self.assertIn("中断", interrupted["diagnostic_summary"])
        preserved = app.get_video(diagnosed["id"])
        self.assertEqual(preserved["diagnostic_status"], "completed")
        self.assertEqual(preserved["diagnostic_summary"], "Preserved completed diagnosis.")
        self.assertEqual(preserved["diagnostic_decision"], "retry_same_content")
        self.assertEqual(preserved["retry_prompt"], "Fixed execution and original.")
        self.assertEqual(preserved["attempts"], 2)
        self.assertEqual(app.get_video(completed["id"])["status"], "completed")
        disk = json.loads(Path(app.CFG.tasks_file).read_text(encoding="utf-8"))
        self.assertEqual(disk[running["id"]]["diagnostic_status"], "failed")
        self.assertEqual(disk[diagnosed["id"]]["diagnostic_summary"], "Preserved completed diagnosis.")

    def test_browser_queue_timeout_starts_no_attempt_and_is_distinguished_from_engine_timeout(self):
        with self.engine_case([self.result]) as generate, patch.object(app.GEN_LOCK, "acquire", return_value=False):
            with self.assertRaises(app.MuseGenerationError) as caught:
                self.run_video()
            self.assertEqual(app._video_error_code(caught.exception), "browser_queue_timeout")
            self.assertEqual(generate.call_count, 0)
        self.assertEqual(app._video_error_code(TimeoutError("generation timeout")), "generation_unknown")


def tearDownModule():
    HOME.cleanup()


if __name__ == "__main__":
    unittest.main()
