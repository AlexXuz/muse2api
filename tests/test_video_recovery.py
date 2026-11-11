"""Pure diagnostic parsing and immutable prompt tests; no network or account."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from video_recovery import (MAX_DIAGNOSTIC_CHARS, build_diagnostic_prompt,
                            build_retry_prompt, normalize_video_style, parse_diagnostic)


def reply(**overrides):
    value = {"decision": "retry_same_content", "reason": "Separate scene and voiceover.",
             "adjustments": ["separate_audio_visual"],
             "changed_requirements": []}
    value.update(overrides)
    return json.dumps(value, ensure_ascii=False)


class VideoRecoveryTests(unittest.TestCase):
    def test_valid_execution_precedes_byte_for_byte_original_requirements(self):
        original = 'Three refs: mother, son, poster. Dialogue: "keep me". 10s, 9:16.\nDo not omit the mother.'
        plan = parse_diagnostic(reply())
        self.assertEqual(plan.decision, "retry_same_content")
        prompt = build_retry_prompt(original, plan)
        self.assertTrue(prompt.endswith(original))
        self.assertLess(prompt.index("按原要求分别组织"), prompt.index(original))
        self.assertNotIn(plan.summary, prompt)
        self.assertIn("优先于上方执行稿", prompt)
        self.assertIn(original, build_diagnostic_prompt(original))
        self.assertIn("不要启动", build_diagnostic_prompt(original))

    def test_exact_schema_types_lengths_and_duplicate_keys(self):
        malformed = [
            reply(extra="unexpected"), reply(decision=True), reply(reason=42),
            reply(reason="r" * 301), reply(reason=""), reply(reason=" " * 301 + "why"),
            reply(adjustments="separate_audio_visual"), reply(adjustments=[True]), reply(adjustments=[{}]),
            reply(adjustments=["remove_dialogue"]), reply(adjustments=["separate_audio_visual"] * 2),
            reply(adjustments=["separate_audio_visual"] * 4),
            reply(changed_requirements="none"), reply(changed_requirements=[False]),
            reply(changed_requirements=["c"] * 9), reply(changed_requirements=[""]),
            reply(decision="unknown"), reply(adjustments=[]),
            reply().replace('"decision": "retry_same_content"', '"decision":"unknown","decision":"retry_same_content"'),
            "n" * (MAX_DIAGNOSTIC_CHARS + 1), "[]", "null", "{incomplete", "narrative " + reply(),
        ]
        for value in malformed:
            with self.subTest(value=value[:60]):
                plan = parse_diagnostic(value)
                self.assertEqual(plan.decision, "unknown")
                self.assertEqual(plan.status, "unverified")
                self.assertEqual(plan.adjustments, ())
                with self.assertRaises(ValueError):
                    build_retry_prompt("original", plan)

    def test_markdown_or_prose_around_json_cannot_authorize_retry(self):
        for raw in ["```json\n" + reply() + "\n```", "untrusted prefix " + reply()]:
            plan = parse_diagnostic(raw)
            self.assertEqual(plan.decision, "unknown")
            with self.assertRaises(ValueError):
                build_retry_prompt("original", plan)

    def test_explicit_restrictions_block_even_invalid_schema_and_natural_language(self):
        evidence = ["没有视频，因为额度不足", "没有出片，当前地区受限", "没有出片，内容违规",
                    "内容审核不通过", "审核未通过", "insufficient credits", "quota exhausted",
                    "content disallowed", "request violates policy", "region unsupported",
                    "不存在地区限制，但是额度不足", "no regional restrictions; insufficient credits",
                    "Your region is not supported for video generation.", "Out of credits.",
                    "额度已达到上限。", "这种内容不能生成。"]
        for text in evidence:
            for raw in [text, reply(reason=text), '{"bad":' + json.dumps(text) + '}']:
                with self.subTest(text=text):
                    plan = parse_diagnostic(raw)
                    self.assertEqual(plan.decision, "blocked")
                    with self.assertRaises(ValueError):
                        build_retry_prompt("original", plan)

    def test_explicit_blocked_always_stops(self):
        self.assertEqual(parse_diagnostic(reply(decision="blocked", reason="Not available.")).decision, "blocked")
        self.assertEqual(parse_diagnostic(reply(extra=True, decision="blocked")).decision, "blocked")
        duplicate = reply().replace('"decision": "retry_same_content"', '"decision":"blocked","decision":"retry_same_content"')
        self.assertEqual(parse_diagnostic(duplicate).decision, "blocked")

    def test_requirement_changes_are_never_silently_applied(self):
        for raw in [reply(decision="needs_user_change"), reply(changed_requirements=["remove dialogue"]),
                    "必须删除台词才能生成", "需要更换母亲参考图", "must replace reference image",
                    "请把画外音去掉再生成。", "Remove the narration and retry.", "需要先缩短视频时长。"]:
            with self.subTest(raw=raw):
                plan = parse_diagnostic(raw)
                self.assertEqual(plan.decision, "needs_user_change")
                with self.assertRaises(ValueError):
                    build_retry_prompt("original", plan)

    def test_negation_does_not_invent_restrictions_or_user_changes(self):
        for text in ["没有地区限制", "不涉及内容违规", "不存在额度不足", "不需要删除台词", "no need to remove dialogue"]:
            self.assertEqual(parse_diagnostic(reply(reason=text)).decision, "retry_same_content")

    def test_unknown_or_invalid_reply_cannot_authorize_or_build_retry(self):
        for raw in [reply(decision="unknown", reason="UNTRUSTED CHANGE"), "UNTRUSTED CHANGE"]:
            plan = parse_diagnostic(raw)
            self.assertEqual(plan.decision, "unknown")
            self.assertIn("停止自动重试", plan.summary)
            with self.assertRaises(ValueError):
                build_retry_prompt("original exactly", plan)

    def test_free_execution_schema_and_extra_execution_field_never_authorize_retry(self):
        examples = ["Remove all dialogue and replace the poster with a plain background.",
                    "删除画外音，把参考海报改成纯色背景。", "把视频改为5秒，去掉母亲，只保留男孩。",
                    "Generate an otherwise ordinary video."]
        for execution in examples:
            old_schema = {"decision": "retry_same_content", "reason": "Reorganize only.",
                          "execution_prompt": execution, "changed_requirements": []}
            for raw in [json.dumps(old_schema), reply(execution_prompt=execution)]:
                with self.subTest(execution=execution):
                    plan = parse_diagnostic(raw)
                    self.assertNotEqual(plan.decision, "retry_same_content")
                    with self.assertRaises(ValueError):
                        build_retry_prompt("original", plan)

    def test_every_allowlisted_strategy_is_fixed_and_reason_is_never_executed(self):
        strategies = ["separate_audio_visual", "reinforce_video_output", "preserve_reference_roles"]
        plan = parse_diagnostic(reply(adjustments=strategies, reason="FREE MODEL TEXT ONLY FOR DISPLAY"))
        self.assertEqual(plan.adjustments, tuple(strategies))
        prompt = build_retry_prompt("original unchanged", plan)
        self.assertIn("逐字保留原台词", prompt)
        self.assertIn("可播放视频附件", prompt)
        self.assertIn("不增删或替换素材", prompt)
        self.assertNotIn("FREE MODEL TEXT", prompt)
        self.assertTrue(prompt.endswith("original unchanged"))

    def test_normalization_is_leading_video_style_only(self):
        phrase = "high-quality 3D CG animation still"
        self.assertEqual(normalize_video_style(phrase + ", cinematic"), "high-quality 3D CG animation, cinematic")
        for original in ['He says "' + phrase + '"', "Stand still. " + phrase,
                         phrase + "ness", "animation still", "台词：still；主体保持不动"]:
            self.assertEqual(normalize_video_style(original), original)


if __name__ == "__main__":
    unittest.main()
