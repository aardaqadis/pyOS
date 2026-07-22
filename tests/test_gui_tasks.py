import unittest
import threading

from pyOSgui import TkTaskManager, UiTextBatcher


class _FakeRoot:
    def __init__(self):
        self.after_calls = []
        self.callback_errors = []

    def after(self, delay, callback):
        self.after_calls.append((delay, callback))
        return len(self.after_calls)

    def after_cancel(self, _after_id):
        return None

    def report_callback_exception(self, *details):
        self.callback_errors.append(details)


class GuiTaskTests(unittest.TestCase):
    def test_completion_lane_is_lossless_and_events_are_bounded_and_coalesced(self):
        root = _FakeRoot()
        manager = TkTaskManager(root, max_workers=1, max_pending=1, max_callbacks=2)
        seen = []
        try:
            for index in range(10):
                self.assertTrue(manager.post(seen.append, f"control-{index}"))
            self.assertTrue(
                manager.post_event(seen.append, "old", coalesce_key="progress")
            )
            self.assertTrue(
                manager.post_event(seen.append, "new", coalesce_key="progress")
            )
            self.assertTrue(manager.post_event(seen.append, "ordinary"))
            self.assertFalse(manager.post_event(seen.append, "overflow"))

            manager._drain()

            self.assertEqual(seen[:10], [f"control-{index}" for index in range(10)])
            self.assertEqual(seen[10:], ["new", "ordinary"])
            self.assertFalse(root.callback_errors)
        finally:
            manager.shutdown()

    def test_shutdown_reports_an_active_worker_before_temporary_cleanup_is_safe(self):
        root = _FakeRoot()
        manager = TkTaskManager(root, max_workers=1, max_pending=1)
        started = threading.Event()
        release = threading.Event()

        def blocking_work():
            started.set()
            release.wait(2)

        self.assertTrue(manager.submit(blocking_work))
        self.assertTrue(started.wait(1))
        self.assertFalse(manager.shutdown(wait_timeout=.01))
        release.set()
        self.assertTrue(manager.shutdown(wait_timeout=1))

    def test_text_batcher_keeps_one_callback_and_bounds_pending_text(self):
        callbacks = []
        consumed = []
        batcher = UiTextBatcher(
            lambda callback: callbacks.append(callback) or True,
            consumed.append,
            max_pending_chars=5,
        )

        for value in ("ab", "cd", "ef"):
            self.assertTrue(batcher.append(value))

        self.assertEqual(len(callbacks), 1)
        callbacks.pop()()
        self.assertEqual(
            consumed, ["[... pending output truncated ...]\nbcdef"]
        )


if __name__ == "__main__":
    unittest.main()
