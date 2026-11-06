"""Exercise Tk event delivery with a slow service without starting a server."""
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import launcher


@unittest.skipUnless(os.name == "nt", "Windows desktop event loop")
class LauncherWindowTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from desktop_ui import enable_high_dpi
        enable_high_dpi()
        self.root = tk.Tk()
        self.root.withdraw()
        self.profile = Path(tempfile.mkdtemp(prefix="pla-window-"))
        self.window = launcher.LauncherWindow(self.root, launcher.ServiceController(self.profile), auto_start=False)

    def tearDown(self):
        if not self.window.closed:
            self.window.destroy()

    def until(self, condition, timeout=2):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.assertTrue(condition())

    def test_slow_probe_does_not_block_ui_or_override_a_new_action(self):
        started, release = threading.Event(), threading.Event()
        def slow_probe(_controller):
            started.set()
            release.wait(2)
            return {"version": launcher.VERSION}
        with patch.object(launcher.ServiceController, "find_running", slow_probe):
            self.window.request_status()
            self.assertTrue(started.wait(1))
            tick = []
            self.root.after(0, lambda: tick.append(True))
            self.root.update()
            self.assertTrue(tick)
            self.window.generation += 1
            self.window.working = True
            self.window.status.set("正在备份资料库")
            release.set()
            self.until(lambda: not self.window.probing)
            self.assertEqual(self.window.status.get(), "正在备份资料库")

    def test_current_probe_updates_status_and_disables_busy_stop(self):
        with patch.object(launcher.ServiceController, "find_running", return_value={"version": launcher.VERSION, "busy": True}):
            self.window.request_status()
            self.until(lambda: not self.window.probing)
        self.assertEqual(self.window.status.get(), "正在处理任务")
        self.assertIn("disabled", self.window.stop_button.state())

    def test_action_completes_on_ui_thread_and_can_close_cleanly(self):
        main_thread = threading.get_ident()
        executed = []
        completed = []
        def work():
            executed.append(threading.get_ident())
            return "saved"
        def complete(value):
            completed.append((threading.get_ident(), value))
            self.window.destroy()
        with patch.object(self.window, "request_status"):
            self.window.action("正在备份资料库", work, complete)
            self.until(lambda: self.window.closed)
        self.assertNotEqual(executed[0], main_thread)
        self.assertEqual(completed, [(main_thread, "saved")])


if __name__ == "__main__":
    unittest.main()
