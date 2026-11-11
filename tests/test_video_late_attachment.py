"""Deterministic regression for successful media text preceding a late video.

Run directly. --probe PATH prints the same fixture outcomes for any engine source.
No account, network, browser, sleeps, or third-party test framework is required.
"""
import ast
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def load_waiter(path):
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    engine = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "MuseEngine")
    names = {"attachments", "_agent_count", "_generate_attached", "_wait_attachment", "_clean_model_text", "_media_refusal_text",
             "_media_status_pending", "_video_text_only_failure"}
    methods = [n for n in engine.body if isinstance(n, ast.FunctionDef) and n.name in names]
    module = ast.Module(body=[
        ast.ClassDef(name="MuseGenerationError", bases=[ast.Name(id="RuntimeError", ctx=ast.Load())],
                     keywords=[], body=[ast.Pass()], decorator_list=[]),
        ast.ClassDef(name="MuseModelRefusalError", bases=[ast.Name(id="MuseGenerationError", ctx=ast.Load())],
                     keywords=[], body=[ast.Pass()], decorator_list=[]),
        ast.ClassDef(name="MuseTextOnlyError", bases=[ast.Name(id="MuseModelRefusalError", ctx=ast.Load())],
                     keywords=[], body=[ast.Pass()], decorator_list=[]),
        ast.ClassDef(name="Waiter", bases=[], keywords=[], body=methods, decorator_list=[]),
    ], type_ignores=[])
    scope = {"re": re, "json": json}
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), scope)
    return scope


def scenario(source, text, ready_at=25, timeout=90, later_text=None, stop_until=0, cover=False,
             cover_until=None, cards_until=0, state_failure=None, attachment_failure=None,
             cancel_at=None, base_agent_cnt=0, new_message_at=None):
    scope = load_waiter(source)
    clock = SimpleNamespace(now=0.0)
    def sleep(seconds):
        clock.now += seconds
    scope["time"] = SimpleNamespace(time=lambda: clock.now, sleep=sleep)
    waiter = scope["Waiter"]()
    def state(_script):
        if state_failure and state_failure[0] <= clock.now < state_failure[1]:
            if len(state_failure) > 2:
                return "null"
            raise RuntimeError("temporary state CDP failure")
        current = later_text[1] if later_text and clock.now >= later_text[0] else text
        count = 2 if new_message_at is not None and clock.now >= new_message_at else 1
        return json.dumps({"cnt": count, "txt": current, "tail": current,
                           "stop": clock.now < stop_until, "path": "/thread/fixture",
                           "cards": int(clock.now < cards_until)})
    waiter.page = SimpleNamespace(js=state)
    waiter._scroll_bottom = lambda: None
    def attachments(*, strict=False):
        assert strict, "terminal detection must not hide attachment read failures"
        if attachment_failure and attachment_failure[0] <= clock.now < attachment_failure[1]:
            raise RuntimeError("temporary attachment CDP failure")
        if ready_at is not None and clock.now >= ready_at:
            return [{"tid": "hatch-chat-attachment-presentation-video", "src": "blob:result",
                     "vSrc": "blob:result", "hasVideo": True, "w": 720, "h": 1280}]
        if cover and (cover_until is None or clock.now < cover_until):
            return [{"tid": "hatch-chat-attachment-presentation-video", "src": "blob:cover",
                     "vSrc": "", "hasVideo": False, "w": 720, "h": 1280}]
        return []
    waiter.attachments = attachments
    try:
        stop_event = SimpleNamespace(is_set=lambda: cancel_at is not None and clock.now >= cancel_at)
        result = waiter._wait_attachment("", timeout, "video", stop_event=stop_event,
                                         base_agent_cnt=base_agent_cnt)
        outcome = "video" if result and result.get("vSrc") else "timeout"
        return {"outcome": outcome, "elapsed": round(clock.now, 1)}
    except scope["MuseGenerationError"] as exc:
        return {"outcome": type(exc).__name__, "elapsed": round(clock.now, 1), "error": str(exc)}


def cases(source):
    cn = "视频已生成，规格正好：720x1280 竖屏、时长 6 秒。 Video not found"
    en = "The video has been generated successfully. Video not found"
    result = {}
    for label, text in (("cn_claim", cn), ("cn_ready", "视频生成好了，时长6秒。"),
                        ("cn_short", "已生成，等待附件加载。"),
                        ("en_claim", en), ("en_ready", "Your video is ready."),
                        ("verified_report", "verified generated artifact"),
                        ("progress", "正在生成视频，请稍等。"),
                        ("planning", "我会先安排镜头，再制作这个视频。")):
        for delay in (15, 30, 60):
            result[f"{label}_{delay}"] = scenario(source, text, ready_at=delay)
    result["claim_no_attachment"] = scenario(source, cn, ready_at=None, timeout=45)
    result["claim_only_cover"] = scenario(source, cn, ready_at=None, timeout=45, cover=True)
    result["stop_lifecycle"] = scenario(source, cn, ready_at=30, stop_until=20)
    refusal = "视频没生成成功：你这个账号所在的国家/地区目前不支持视频生成。"
    result["explicit_refusal"] = scenario(source, refusal)
    result["claim_then_refusal"] = scenario(source, cn, later_text=(12, refusal))
    failed = "这镜没能生成出来，没有出片。可以改成四格分镜静帧。"
    for label, text in (("explicit_cn", failed),
                        ("explicit_en", "The video has not been generated successfully. Please try again."),
                        ("explicit_could_not", "The video could not be generated."),
                        ("explicit_none", "No video has been generated.")):
        result[label] = scenario(source, text, ready_at=None, stop_until=2)
        result[label + "_late_video"] = scenario(source, text, ready_at=25, stop_until=2)
    for label, text in (("content_block", "视频没有生成成功，内容违反安全政策，无法生成。"),
                        ("permission_block", "视频生成失败，账号没有权限，无法使用视频生成功能。"),
                        ("region_block", refusal),
                        ("moderation_block", "Video generation failed due to content moderation."),
                        ("review_block", "视频生成失败，内容安全检查未通过。"),
                        ("violation_block", "视频生成失败：请求违规。"),
                        ("illegal_block", "视频生成失败：涉及违法内容。"),
                        ("disallowed_block", "Video generation failed because this request is disallowed."),
                        ("english_block", "Video generation failed. This content is not allowed by our safety policy.")):
        result[label] = scenario(source, text, ready_at=None, stop_until=2)
    result["quota_block"] = scenario(source, "视频生成失败，账号额度不足。", ready_at=None, stop_until=2)
    for label, extra in (
        ("no_stop_observed", {}), ("old_message", {"stop_until": 2, "base_agent_cnt": 1}),
        ("cover", {"stop_until": 2, "cover": True}),
        ("vanished_cover", {"stop_until": 2, "cover": True, "cover_until": 10}),
        ("empty_card", {"stop_until": 2, "cards_until": 10}),
        ("still_running", {"stop_until": 100}),
    ):
        result[label] = scenario(source, failed, ready_at=None, timeout=45, **extra)
    for label, text in (
        ("mixed_progress", "视频生成失败，不过我正在重新生成视频，请稍等。"),
        ("ambiguous", "视频可能没有生成成功。"),
        ("claim_then_upload_failed", "The video was generated, but the upload failed."),
        ("failed_after_claim", "视频已生成，但随后处理失败，无法提供视频。"),
        ("history_cn", "上次视频生成失败。本次已完成，成品稍后可见。"),
        ("history_en", "The previous video generation failed. The current render completed successfully; the attachment appears shortly."),
        ("quoted_failure", "引用说明：“视频生成失败”并不是当前状态。"),
        ("quoted_en", "The message says 'video generation failed'."),
        ("completed_after_failure", "视频生成失败。本次已完成，成品稍后可见。"),
    ):
        result[label] = scenario(source, text, ready_at=None, timeout=45, stop_until=2)
    result["state_gap"] = scenario(source, failed, ready_at=45, stop_until=2, state_failure=(20, 22))
    result["invalid_state_gap"] = scenario(source, failed, ready_at=45, stop_until=2, state_failure=(20, 22, "null"))
    result["attachment_gap"] = scenario(source, failed, ready_at=45, stop_until=2, attachment_failure=(20, 22))
    result["new_message_grace"] = scenario(source, failed, ready_at=45, stop_until=2, new_message_at=20)
    result["short_deadline"] = scenario(source, failed, ready_at=None, timeout=20, stop_until=2)
    result["cancelled"] = scenario(source, failed, ready_at=None, stop_until=2, cancel_at=15)
    return result


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--probe":
        print(json.dumps(cases(sys.argv[2]), ensure_ascii=False, sort_keys=True))
        return
    source = ROOT / "engine.py"
    result = cases(source)
    for name, item in result.items():
        if name.startswith(("cn_claim_", "cn_ready_", "cn_short_", "en_claim_", "en_ready_", "verified_report_", "progress_", "planning_")):
            assert item["outcome"] == "video", (name, item)
            assert item["elapsed"] >= int(name.rsplit("_", 1)[1]), (name, item)
        elif name in ("claim_no_attachment", "claim_only_cover", "no_stop_observed", "old_message",
                      "cover", "vanished_cover", "empty_card", "still_running", "mixed_progress",
                      "ambiguous", "claim_then_upload_failed", "failed_after_claim", "history_cn",
                      "history_en", "quoted_failure", "quoted_en", "completed_after_failure"):
            assert item["outcome"] == "timeout" and 45 <= item["elapsed"] < 46, (name, item)
        elif name == "stop_lifecycle":
            assert item["outcome"] == "video" and item["elapsed"] >= 30, (name, item)
        elif name.endswith("_late_video") or name in ("state_gap", "invalid_state_gap", "attachment_gap", "new_message_grace"):
            assert item["outcome"] == "video", (name, item)
        elif name.startswith("explicit_") and name != "explicit_refusal":
            assert item["outcome"] == "MuseTextOnlyError", (name, item)
            assert 32 <= item["elapsed"] < 34, (name, item)
        elif name == "quota_block" or name == "cancelled":
            assert item["outcome"] == "MuseGenerationError", (name, item)
        elif name == "short_deadline":
            assert item["outcome"] == "timeout" and 20 <= item["elapsed"] < 21, (name, item)
        else:
            assert item["outcome"] == "MuseModelRefusalError", (name, item)
            assert item["elapsed"] < 25, (name, item)
    # The public no-argument attachment API remains tolerant; terminal detection opts in.
    scope = load_waiter(source)
    waiter = scope["Waiter"]()
    waiter._ATT_JS = "unused"
    waiter.page = SimpleNamespace(js=lambda _: "not-json")
    assert waiter.attachments() == []
    try:
        waiter.attachments(strict=True)
    except ValueError:
        pass
    else:
        raise AssertionError("strict attachment probe hid malformed data")
    # An unknown pre-send baseline must never be coerced to zero or send a prompt.
    baseline_cases = [(None, "0"), ("not-json", "0"), ("[]", None), ("[]", "bad-count"),
                      ("[]", -1), ("[]", False),
                      (RuntimeError("attachment CDP disconnected"), "0"),
                      ("[]", RuntimeError("count CDP disconnected"))]
    for attachment_value, count_value in baseline_cases:
        candidate = scope["Waiter"]()
        candidate._ATT_JS = "attachments"
        candidate._AGENT_COUNT_JS = "count"
        def baseline_read(script):
            value = attachment_value if script == "attachments" else count_value
            if isinstance(value, Exception):
                raise value
            return value
        candidate.page = SimpleNamespace(js=baseline_read)
        sent = []
        candidate._send = lambda _: sent.append(True)
        try:
            candidate._generate_attached("fixture", "video", float("inf"), None, None, set())
        except scope["MuseGenerationError"] as exc:
            assert not isinstance(exc, scope["MuseTextOnlyError"])
            assert "已停止发送" in str(exc)
        else:
            raise AssertionError("unknown baseline was accepted")
        assert not sent, "unknown baseline sent a generation request"
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    print(f"PASS: {len(result)} wait cases, {len(baseline_cases)} pre-send guards, strict attachment compatibility")


if __name__ == "__main__":
    main()
