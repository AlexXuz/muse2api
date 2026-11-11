"""Offline CDP deadline regressions; no socket connections or browser processes."""
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cdp


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def time(self):
        return 1000.0 + self.now

    def advance(self, seconds):
        self.now += seconds


class FakeSocket:
    def __init__(self, clock):
        self.clock = clock
        self.timeout = 90.0
        self.timeout_changes = []
        self.gettimeout_calls = 0
        self.sent = []
        self.send_timeouts = []
        self.recv_timeouts = []
        self.events = []
        self.on_send = None

    def gettimeout(self):
        self.gettimeout_calls += 1
        return self.timeout

    def settimeout(self, value):
        self.timeout = value
        self.timeout_changes.append(value)

    def send(self, payload):
        self.sent.append(json.loads(payload))
        self.send_timeouts.append(self.timeout)
        if self.on_send:
            self.on_send(self)

    def recv(self):
        self.recv_timeouts.append(self.timeout)
        value = self.events.pop(0) if self.events else self.reply()
        if callable(value):
            value = value(self)
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, str) else json.dumps(value)

    def reply(self):
        return {"id": self.sent[-1]["id"], "result": {"result": {"value": "done"}}}


class WaitingLock:
    def __init__(self, clock, delay):
        self.clock, self.delay = clock, delay
        self.waits = []
        self.released = 0

    def acquire(self, timeout):
        self.waits.append(timeout)
        self.clock.advance(min(timeout, self.delay))
        return self.delay <= timeout

    def release(self):
        self.released += 1


class BoundedIoTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.socket = FakeSocket(self.clock)
        self.time_patch = patch.object(cdp, "time", SimpleNamespace(
            time=self.clock.time, monotonic=self.clock.monotonic))
        self.time_patch.start()
        self.addCleanup(self.time_patch.stop)
        with patch.object(cdp.websocket, "create_connection", return_value=self.socket):
            self.client = cdp.CDP("ws://offline", timeout=90)

    def assert_restored(self, timeout=90):
        self.assertEqual(self.socket.timeout, timeout)
        self.assertFalse(self.client._lock.locked())

    def test_success_and_notifications_share_one_remaining_budget(self):
        self.socket.on_send = lambda ws: self.clock.advance(2)
        def notification(ws):
            self.clock.advance(1)
            return {"method": "Runtime.consoleAPICalled"}
        self.socket.events = [notification]
        with self.client.bounded_io(10):
            self.assertEqual(self.client.js("fixture"), "done")
        self.assertEqual(self.socket.send_timeouts, [10])
        self.assertEqual(self.socket.recv_timeouts, [8, 7])
        self.assert_restored()

    def test_recv_timeout_restores_numeric_and_unlimited_socket_timeouts(self):
        for original in (90.0, None):
            with self.subTest(original=original):
                self.socket.timeout = original
                deadline = self.clock.now + 5
                def blocking_recv(ws):
                    self.clock.advance(ws.timeout)
                    return cdp.websocket.WebSocketTimeoutException("offline receive timeout")
                self.socket.events = [blocking_recv]
                with self.assertRaises(TimeoutError), self.client.bounded_io(deadline):
                    self.client.send("Runtime.evaluate")
                self.assertEqual(self.clock.now, deadline)
                self.assert_restored(original)

    def test_send_timeout_restores_socket_and_unlocks(self):
        def blocking_send(ws):
            self.clock.advance(ws.timeout)
            raise cdp.websocket.WebSocketTimeoutException("offline send timeout")
        self.socket.on_send = blocking_send
        with self.assertRaises(TimeoutError), self.client.bounded_io(3):
            self.client.send("Runtime.evaluate")
        self.assertEqual(self.clock.now, 3)
        self.assertEqual(self.socket.recv_timeouts, [])
        self.assert_restored()

    def test_reply_arriving_after_deadline_is_not_success(self):
        def late_reply(ws):
            self.clock.advance(ws.timeout + 0.1)
            return ws.reply()
        self.socket.events = [late_reply]
        with self.assertRaises(TimeoutError), self.client.bounded_io(5):
            self.client.send("Runtime.evaluate")
        self.assert_restored()

    def test_lock_wait_timeout_does_not_touch_socket_or_release_unowned_lock(self):
        lock = WaitingLock(self.clock, delay=6)
        self.client._lock = lock
        with self.assertRaises(TimeoutError), self.client.bounded_io(5):
            self.client.send("Runtime.evaluate")
        self.assertEqual(lock.waits, [5])
        self.assertEqual(lock.released, 0)
        self.assertEqual(self.socket.sent, [])
        self.assertEqual(self.socket.gettimeout_calls, 0)

    def test_successful_lock_wait_reduces_the_io_budget(self):
        lock = WaitingLock(self.clock, delay=2)
        self.client._lock = lock
        with self.client.bounded_io(5):
            self.client.send("Runtime.evaluate")
        self.assertEqual(self.socket.send_timeouts, [3])
        self.assertEqual(self.socket.recv_timeouts, [3])
        self.assertEqual(lock.released, 1)
        self.assertEqual(self.socket.timeout, 90)

    def test_disconnection_malformed_response_and_protocol_error_fail_closed(self):
        for response, error in (
            (cdp.websocket.WebSocketConnectionClosedException("offline disconnect"),
             cdp.websocket.WebSocketConnectionClosedException),
            ("not-json", cdp.CDPError),
            ([], cdp.CDPError),
            (lambda ws: {"id": ws.sent[-1]["id"], "error": {"message": "fixture"}}, cdp.CDPError),
        ):
            with self.subTest(response=type(response).__name__):
                self.socket.events = [response]
                with self.assertRaises(error), self.client.bounded_io(5):
                    self.client.send("Runtime.evaluate")
                self.assert_restored()

    def test_call_timeout_can_shorten_scope_but_cannot_extend_it(self):
        with self.client.bounded_io(5):
            self.client.send("Runtime.evaluate", timeout=2)
            self.client.send("Runtime.evaluate", timeout=100)
        self.assertEqual(self.socket.send_timeouts, [2, 5])
        self.assert_restored()

    def test_nested_scope_uses_earliest_deadline_and_restores_outer_scope(self):
        with self.client.bounded_io(20):
            with self.client.bounded_io(30):
                self.client.send("Runtime.evaluate")
            with self.client.bounded_io(10):
                self.client.send("Runtime.evaluate")
            self.client.send("Runtime.evaluate")
        self.assertEqual(self.socket.send_timeouts, [20, 10, 20])
        changes = len(self.socket.timeout_changes)
        self.client.send("Runtime.evaluate")
        self.assertEqual(len(self.socket.timeout_changes), changes)
        self.assert_restored()

    def test_scope_is_removed_after_exception(self):
        with self.assertRaisesRegex(RuntimeError, "fixture"):
            with self.client.bounded_io(5):
                raise RuntimeError("fixture")
        self.client.send("Runtime.evaluate")
        self.assertEqual(self.socket.timeout_changes, [])

    def test_expired_scope_does_not_send(self):
        with self.assertRaises(TimeoutError), self.client.bounded_io(0):
            self.client.send("Runtime.evaluate")
        self.assertEqual(self.socket.sent, [])
        self.assertEqual(self.socket.timeout_changes, [])

    def test_scope_is_thread_local_and_default_path_is_unchanged(self):
        results, errors = [], []
        def other_thread():
            try:
                results.append(self.client.js("fixture"))
            except Exception as exc:
                errors.append(exc)
        with self.client.bounded_io(0):
            worker = threading.Thread(target=other_thread)
            worker.start()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, ["done"])
        self.assertEqual(self.socket.send_timeouts, [90])
        self.assertEqual(self.socket.timeout_changes, [])
        self.assertEqual(self.socket.gettimeout_calls, 0)

    def test_invalid_scope_deadline_is_rejected(self):
        for deadline in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(deadline=deadline), self.assertRaises(ValueError):
                with self.client.bounded_io(deadline):
                    pass
        self.assertEqual(self.socket.sent, [])


if __name__ == "__main__":
    unittest.main()
