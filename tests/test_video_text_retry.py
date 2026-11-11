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
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
HOME = tempfile.TemporaryDirectory(prefix="muse-video-retry-test-")
os.environ.update(MUSE2API_HOME=HOME.name, MUSE2API_PROFILE_ROOT=HOME.name,
                  MUSE2API_KEY="test-only", MUSE2API_VIDEO_TEXT_RETRY_LIMIT="0")
spec = importlib.util.spec_from_file_location("video_text_retry_target", ROOT / "app.py")
app = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = app
spec.loader.exec_module(app)


class VideoTextRetryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.first = app.store.add_account({"hatch_sess": "fixture-one", "hatch_vml": "fixture-one",
                                          "hatch_native_auth_device": "fixture-one"}, "one")
        cls.second = app.store.add_account({"hatch_sess": "fixture-two", "hatch_vml": "fixture-two",
                                           "hatch_native_auth_device": "fixture-two"}, "two")
        cls.result = {"filename": "local-video.mp4", "size": 42, "kind": "video"}

    @contextmanager
    def engine_case(self, effects, limit=1):
        with patch.object(app.CFG, "video_text_retry_limit", limit), \
             patch.object(app, "_renew_and_persist", side_effect=lambda aid, **_: app.store.get_account(aid)), \
             patch.object(app, "_sync_cookies"), patch.object(app.engine, "start"), \
             patch.object(app.engine, "close_current_page"), patch.object(app.engine, "reset_thread"), \
             patch.object(app.engine, "generate", side_effect=effects) as generate:
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
        self.assertEqual(events[-1]["attempts"], 1)
        self.assertEqual(events[-1]["retry_count"], 0)

    def test_successful_first_attempt_does_not_use_retry(self):
        for limit in [0, 1]:
            with self.subTest(limit=limit), self.engine_case([self.result], limit=limit) as generate:
                result, aid = self.run_video()
                self.assertEqual(result, self.result)
                self.assertEqual(aid, self.first["id"])
                self.assertEqual(generate.call_count, 1)

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
            self.assertEqual(second.args[1], original + "\n\n" + app.VIDEO_OUTPUT_CONTRACT)
            self.assertEqual(first.args[0], second.args[0])
            self.assertEqual(first.kwargs["account_id"], second.kwargs["account_id"])
            self.assertEqual(aid, self.first["id"])
            for call in [first, second]:
                self.assertEqual(call.kwargs["reference_images"], references)
            self.assertLessEqual(second.kwargs["timeout"], first.kwargs["timeout"])
        self.assertEqual([event["attempts"] for event in events], [1, 2])
        self.assertEqual(events[-1]["retry_count"], 1)
        self.assertEqual(events[-1]["retry_reason"], "video_text_only")
        self.assertEqual(references, ["local-reference-a.png", "local-reference-b.png"])

    def test_second_failure_stops_even_if_config_limit_is_too_large(self):
        for error in [app.MuseTextOnlyError("still text"), app.MuseGenerationError("unknown"), TimeoutError("timeout")]:
            with self.subTest(error=type(error).__name__), self.engine_case([app.MuseTextOnlyError("text"), error], limit=99) as generate:
                with self.assertRaises(type(error)):
                    self.run_video()
                self.assertEqual(generate.call_count, 2)

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
                for key in ["attempts", "retry_count", "retry_reason", "stage", "error_code"]:
                    self.assertEqual(saved[key], task[key])
                for _ in range(3):
                    self.assertEqual(app.get_video(tid)["id"], tid)
                self.assertEqual(generate.call_count, attempts)
                if not expected_code:
                    self.assertEqual(task["result"]["url"], task["video"]["url"])

    def test_admin_exposes_only_busy_boolean_and_retry_limit_metadata(self):
        with patch.object(app.CFG, "video_text_retry_limit", 1):
            self.assertFalse(app.admin_status()["generation_busy"])
            with app.GEN_LOCK:
                status = app.admin_status()
                self.assertIs(status["generation_busy"], True)
                self.assertEqual(status["config"]["video_text_retry_limit"], 1)
                self.assertNotIn("busy_accounts", status)

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
