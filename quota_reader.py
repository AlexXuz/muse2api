"""Read the current Muse subscription action without depending on Settings UI.

Browser helpers only use observed, same-origin public bundles and action routes.
The response parser selects display-safe quota fields; account/agreement payloads
must never be logged or returned as the ``raw`` quota text.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import re


MAX_SUBSCRIPTION_RESPONSE_BYTES = 64 * 1024
_ACTION = re.compile(r"[a-f0-9]{40,64}\Z")
_RECORD = re.compile(r"([0-9a-fA-F]+):(.*)\Z")
_REFERENCE = re.compile(r"\$@([0-9a-fA-F]+)\Z")


class SubscriptionResponseError(ValueError):
    """The action did not provide a complete, trustworthy metered usage result."""


def _percentage(value) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SubscriptionResponseError("Muse 用量百分比无效")
    try:
        valid = math.isfinite(value) and 0 <= value <= 100
    except (OverflowError, TypeError):
        valid = False
    if not valid:
        raise SubscriptionResponseError("Muse 用量百分比无效")
    return int(value) if value == int(value) else value


def _label(value, limit=300) -> str:
    if not isinstance(value, str) or len(value) > limit:
        return ""
    return value.strip()


def parse_subscription_response(body: str) -> dict:
    """Parse only the successful action result referenced by Flight root ``0.a``.

    A random record containing ``success`` or a plan label is not quota evidence.
    Missing/unmetered usage raises an error rather than being interpreted as 0%.
    """
    if not isinstance(body, str) or len(body.encode("utf-8")) > MAX_SUBSCRIPTION_RESPONSE_BYTES:
        raise SubscriptionResponseError("Muse 用量响应格式或大小无效")
    records = {}
    for line in body.splitlines():
        match = _RECORD.fullmatch(line)
        if not match:
            continue
        record_id = int(match[1], 16)
        if record_id in records:
            raise SubscriptionResponseError("Muse 用量响应记录重复")
        records[record_id] = match[2]

    def record(record_id):
        try:
            return json.loads(records[record_id])
        except (KeyError, ValueError, TypeError, RecursionError):
            raise SubscriptionResponseError("Muse 用量响应缺少完整动作结果") from None

    root = record(0)
    reference = _REFERENCE.fullmatch(root.get("a", "")) if isinstance(root, dict) and isinstance(root.get("a"), str) else None
    if not reference or int(reference[1], 16) == 0:
        raise SubscriptionResponseError("Muse 用量响应缺少动作引用")
    result = record(int(reference[1], 16))
    if not isinstance(result, dict) or result.get("success") is not True:
        raise SubscriptionResponseError("Muse 用量动作未成功")
    subscription = result.get("subscription")
    usage = subscription.get("usage") if isinstance(subscription, dict) else None
    if not isinstance(usage, dict) or usage.get("state") != "METERED":
        raise SubscriptionResponseError("Muse 未返回有效计量用量")
    percent = _percentage(usage.get("percentUsed"))
    output = {"found": True, "weekly_used_pct": percent}
    plan = _label(subscription.get("usageRowLabel"), 120)
    subtitle = _label(subscription.get("statusSubtitle"))
    topup = _label(subscription.get("topupRowValueLabel"))
    if plan:
        output["plan"] = plan
    reset = re.search(r"Weekly limit resets? on\s+([^\n]+)", subtitle, re.I)
    if not reset:
        reset = re.search(r"(?:每(?:週|周|星期)|週|周)(?:上限|限額|限额|額度|额度)"
                          r"(?:會|会|將|将)?(?:於|于|在)\s*([^\n]+?)\s*(?:重設|重置)", subtitle)
    if reset:
        output["weekly_reset"] = reset[1].strip()
    else:
        resets_at = usage.get("resetsAt")
        if isinstance(resets_at, (int, float)) and not isinstance(resets_at, bool):
            try:
                if math.isfinite(resets_at) and resets_at > 0:
                    output["weekly_reset"] = datetime.fromtimestamp(resets_at, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            except (ValueError, OverflowError, OSError):
                pass

    # topupBalance/topupTotal are internal accounting units, not token counts.
    # Only the official human-readable label is used for remaining tokens.
    extra = re.search(r"(\d+(?:\.\d+)?)\s*%\s*(?:used\b|已(?:使)?用)", topup, re.I)
    if not extra:
        extra = re.search(r"已(?:使)?用\s*(\d+(?:\.\d+)?)\s*%", topup)
    if extra:
        try:
            output["extra_used_pct"] = _percentage(float(extra[1]))
        except SubscriptionResponseError:
            pass
    remaining = re.search(r"[（(]([^()（）]+)[）)]", topup)
    if remaining:
        output["extra_left"] = remaining[1].strip()
    # No expiry assumption: an absent expiry is not evidence of "never".
    safe_lines = ["Usage", plan, subtitle, f"{percent}% used"]
    if topup:
        safe_lines.extend(("Additional tokens", topup))
    output["raw"] = "\n".join(line for line in safe_lines if line)
    return output


DISCOVER_SUBSCRIPTION_ACTION_JS = r"""(async () => {
    const urls = [], seen = new Set();
    const candidates = [...document.scripts].map(s => s.src).concat(
        performance.getEntriesByType('resource').map(r => r.name));
    for (const value of candidates) {
        try {
            const u = new URL(value);
            if (u.origin !== 'https://muse.ai' || u.username || u.password
                || !/^\/_next\/static\/chunks\/[^?#]+\.js$/.test(u.pathname)) continue;
            u.hash = '';
            if (!seen.has(u.href)) { seen.add(u.href); urls.push(u.href); }
            if (urls.length >= 256) break;
        } catch (_) {}
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 12000);
    let next = 0, result = null;
    async function readScript(response) {
        if (!response.ok || !response.body) return '';
        const reader = response.body.getReader(), decoder = new TextDecoder();
        let bytes = 0, text = '';
        try {
            while (true) {
                const part = await reader.read();
                if (part.done) break;
                bytes += part.value.byteLength;
                if (bytes > 4 * 1024 * 1024) { await reader.cancel(); return ''; }
                text += decoder.decode(part.value, {stream:true});
            }
            return text + decoder.decode();
        } finally { reader.releaseLock(); }
    }
    async function worker() {
        while (!result && !controller.signal.aborted && next < urls.length) {
            const script = urls[next++];
            try {
                const response = await fetch(script, {credentials:'omit', redirect:'error',
                    cache:'force-cache', signal:controller.signal});
                const text = await readScript(response);
                // Do not bridge into the next exported action in a minified
                // comma-separated declaration and accidentally select its ID.
                const match = /createServerReference\)?\(\s*["']([a-f0-9]{40,64})["']\s*,[^()\r\n;]{0,250},\s*["']fetchSubscriptionAction["']\s*\)/.exec(text);
                if (match && !result) {
                    result = {action:match[1], script};
                    controller.abort();
                    return;
                }
            } catch (_) {}
        }
    }
    try {
        await Promise.all(Array.from({length:Math.min(6, urls.length)}, () => worker()));
        return JSON.stringify(result || {error:'action_not_found'});
    } finally { clearTimeout(timer); }
})()"""


def build_subscription_fetch_js(action: str) -> str:
    """Build a bounded same-origin fetch for a discovered server action ID."""
    if not isinstance(action, str) or not _ACTION.fullmatch(action):
        raise ValueError("Invalid subscription action identifier")
    return r"""(async () => {
        if (location.origin !== 'https://muse.ai')
            return JSON.stringify({status:0, content_type:'', body:'', action_not_found:false, error:'invalid_origin'});
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 20000);
        let status = 0, contentType = '', actionNotFound = false;
        try {
            const response = await fetch('/thread/new', {
                method:'POST', credentials:'same-origin', redirect:'error', cache:'no-store',
                signal:controller.signal,
                headers:{'Next-Action':ACTION_ID, 'Accept':'text/x-component',
                    'Content-Type':'text/plain;charset=UTF-8'},
                body:'[{"includeAgreement":false}]'
            });
            status = response.status;
            contentType = response.headers.get('content-type') || '';
            actionNotFound = response.headers.get('x-nextjs-action-not-found') === '1';
            if (actionNotFound) {
                controller.abort();
                return JSON.stringify({status, content_type:contentType, body:'', action_not_found:true});
            }
            if (!response.body)
                return JSON.stringify({status, content_type:contentType, body:'', action_not_found:false, error:'empty_response'});
            const reader = response.body.getReader(), decoder = new TextDecoder();
            let bytes = 0, body = '';
            try {
                while (true) {
                    const part = await reader.read();
                    if (part.done) break;
                    bytes += part.value.byteLength;
                    if (bytes > 65536) {
                        await reader.cancel();
                        return JSON.stringify({status, content_type:contentType, body:'', action_not_found:false, error:'response_too_large'});
                    }
                    body += decoder.decode(part.value, {stream:true});
                }
                body += decoder.decode();
            } finally { reader.releaseLock(); }
            return JSON.stringify({status, content_type:contentType, body, action_not_found:false});
        } catch (_) {
            return JSON.stringify({status, content_type:contentType, body:'', action_not_found:actionNotFound,
                error:controller.signal.aborted ? 'request_timeout' : 'request_failed'});
        } finally { clearTimeout(timer); }
    })()""".replace("ACTION_ID", json.dumps(action))
