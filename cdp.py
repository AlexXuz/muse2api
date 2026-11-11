"""极简 Chrome DevTools Protocol 客户端（仅依赖 websocket-client / requests）。"""
from __future__ import annotations

import json
import math
import time
from contextlib import contextmanager

import threading

import requests
import websocket


class CDPError(RuntimeError):
    pass


class CDP:
    def __init__(self, ws_url: str, timeout: float = 90.0, max_size: int = 256 << 20):
        self.ws = websocket.create_connection(ws_url, timeout=timeout, max_size=max_size)
        self.timeout = timeout
        self._id = 0
        self._lock = threading.Lock()
        self._io_context = threading.local()

    @contextmanager
    def bounded_io(self, deadline: float):
        """Bound calls made by this thread without changing other callers' defaults."""
        deadline = float(deadline)
        if not math.isfinite(deadline):
            raise ValueError("CDP deadline must be finite")
        previous = getattr(self._io_context, "deadline", None)
        self._io_context.deadline = min(previous, deadline) if previous is not None else deadline
        try:
            yield self
        finally:
            if previous is None:
                del self._io_context.deadline
            else:
                self._io_context.deadline = previous

    def send(self, method: str, params: dict | None = None, timeout: float | None = None):
        deadline = getattr(self._io_context, "deadline", None)
        if deadline is not None:
            return self._send_bounded(method, params, timeout, deadline)
        with self._lock:
            self._id += 1
            mid = self._id
            self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            deadline = time.time() + (timeout or self.timeout)
            while time.time() < deadline:
                try:
                    msg = json.loads(self.ws.recv())
                except websocket.WebSocketTimeoutException:
                    continue
                except Exception:  # noqa: BLE001
                    continue
                if msg.get("id") == mid:
                    if "error" in msg:
                        raise CDPError(f"{method}: {msg['error']}")
                    return msg
            raise TimeoutError(f"CDP {method} 超时")

    def _send_bounded(self, method: str, params: dict | None,
                      timeout: float | None, deadline: float):
        deadline = min(deadline, time.monotonic() + (timeout or self.timeout))

        def remaining():
            value = deadline - time.monotonic()
            if value <= 0:
                raise TimeoutError(f"CDP {method} 超时")
            return value

        if not self._lock.acquire(timeout=remaining()):
            raise TimeoutError(f"CDP {method} 等待连接锁超时")
        try:
            # The connection timeout is shared, so adjust it only while owning the
            # same lock as ordinary send/close, and restore it before releasing.
            previous_timeout = self.ws.gettimeout()
            try:
                remaining()
                self._id += 1
                mid = self._id
                self.ws.settimeout(remaining())
                self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
                while True:
                    self.ws.settimeout(remaining())
                    raw = self.ws.recv()
                    remaining()
                    try:
                        msg = json.loads(raw)
                    except (TypeError, ValueError) as exc:
                        raise CDPError(f"CDP {method} 响应无效") from exc
                    if not isinstance(msg, dict):
                        raise CDPError(f"CDP {method} 响应无效")
                    remaining()
                    if msg.get("id") == mid:
                        if "error" in msg:
                            raise CDPError(f"{method}: {msg['error']}")
                        return msg
            except websocket.WebSocketTimeoutException as exc:
                raise TimeoutError(f"CDP {method} 超时") from exc
            finally:
                self.ws.settimeout(previous_timeout)
        finally:
            self._lock.release()

    def js(self, expr: str, await_promise: bool = False, timeout: float | None = None):
        r = self.send("Runtime.evaluate",
                      {"expression": expr, "returnByValue": True,
                       "awaitPromise": await_promise}, timeout)
        res = r.get("result", {}).get("result", {})
        if "value" in res:
            return res["value"]
        return r.get("result")

    def pump(self, seconds: float, on_event=None, sock_timeout: float = 2.0):
        """在 seconds 秒内持续读取事件，交给回调处理。"""
        end = time.time() + seconds
        self.ws.settimeout(sock_timeout)
        while time.time() < end:
            try:
                ev = json.loads(self.ws.recv())
            except Exception:  # noqa: BLE001
                continue
            if on_event and "method" in ev:
                on_event(ev)

    def close(self):
        with self._lock:
            try:
                self.ws.close()
            except Exception:  # noqa: BLE001
                pass


def http_json(url: str, timeout: float = 5.0):
    return requests.get(url, timeout=timeout).json()
