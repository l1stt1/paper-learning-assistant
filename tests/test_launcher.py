"""Launcher lifecycle tests use isolated profiles and never touch the personal library."""
import json
import io
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

import launcher


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="pla-launcher-")).resolve()

    def test_source_and_frozen_command_keep_complete_profile(self):
        profile = self.root / "中文资料库 with spaces"
        source = launcher.server_command(profile, 18900, frozen=False, executable="pythonw.exe", app_root=self.root)
        self.assertEqual(source[:2], ["pythonw.exe", str(self.root / "server.py")])
        self.assertEqual(source[2:], ["--data-dir", str(profile.resolve()), "--port", "18900"])
        frozen = launcher.server_command(profile, 18901, frozen=True, executable="PaperLearningAssistant.exe")
        self.assertEqual(frozen[:2], ["PaperLearningAssistant.exe", "--serve"])
        self.assertEqual(frozen[2:4], ["--data-dir", str(profile.resolve())])

    def test_serve_startup_errors_exit_cleanly_and_log_to_available_stderr(self):
        for error in (OSError("port already in use"), RuntimeError("profile already open"), ValueError("invalid profile")):
            with self.subTest(error=type(error).__name__):
                def fail(_args):
                    raise error
                output = io.StringIO()
                with patch.dict("sys.modules", {"server": types.SimpleNamespace(main=fail)}), patch.object(launcher.sys, "stderr", output):
                    with self.assertRaises(SystemExit) as stopped:
                        launcher.main(["--serve", "--data-dir", str(self.root)])
                self.assertEqual(stopped.exception.code, 1)
                self.assertIn(str(error), output.getvalue())
                self.assertNotIn("Traceback", output.getvalue())

    def test_serve_startup_without_stderr_still_exits_without_another_error(self):
        def fail(_args):
            raise OSError("port already in use")
        with patch.dict("sys.modules", {"server": types.SimpleNamespace(main=fail)}), patch.object(launcher.sys, "stderr", None):
            with self.assertRaises(SystemExit) as stopped:
                launcher.main(["--serve"])
        self.assertEqual(stopped.exception.code, 1)

    def test_serve_preserves_explicit_exit_and_keyboard_interrupt(self):
        for error in (SystemExit(2), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                def fail(_args):
                    raise error
                with patch.dict("sys.modules", {"server": types.SimpleNamespace(main=fail)}):
                    with self.assertRaises(type(error)) as stopped:
                        launcher.main(["--serve"])
                self.assertIs(stopped.exception, error)

    def test_occupied_port_uses_another_local_port(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            preferred = occupied.getsockname()[1]
            self.assertNotEqual(launcher.available_port(preferred), preferred)

    @unittest.skipUnless(os.name == "nt", "Windows source entry point")
    def test_powershell_entry_keeps_profile_argument_without_extra_spaces(self):
        script = str(launcher.APP_ROOT / "start.ps1").replace("'", "''")
        profile = str(self.root / "中文资料库 with spaces")
        escaped = profile.replace("'", "''")
        command = "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); "
        command += "function Start-Process { param($FilePath,[string[]]$ArgumentList,$WorkingDirectory,$WindowStyle) ConvertTo-Json -Compress -InputObject $ArgumentList }; "
        command += f"& '{script}' -DataDir '{escaped}' -Port 18982"
        result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command], capture_output=True, text=True, encoding="utf-8", check=True)
        args = json.loads(result.stdout)
        self.assertEqual(args, ['"' + str(launcher.APP_ROOT / "launcher.py") + '"', "--port", "18982", "--data-dir", '"' + profile + '"'])

    def test_health_must_match_product_and_complete_profile(self):
        health = {"product": launcher.PRODUCT, "root": str(self.root), "version": launcher.VERSION}
        with patch.object(launcher, "request_json", return_value=health):
            self.assertEqual(launcher.health_for_profile("http://127.0.0.1:1234", self.root), health)
            self.assertIsNone(launcher.health_for_profile("http://127.0.0.1:1234", self.root / "other"))
        with patch.object(launcher, "request_json", return_value={**health, "product": "other"}):
            self.assertIsNone(launcher.health_for_profile("http://127.0.0.1:1234", self.root))

    def test_metadata_url_is_not_trusted_and_matching_service_reused(self):
        service = {"port": 18890, "url": "https://untrusted.invalid", "status": "running"}
        (self.root / ".service.json").write_text(json.dumps(service), encoding="utf-8")
        controller = launcher.ServiceController(self.root)
        health = {"root": str(self.root), "product": launcher.PRODUCT, "version": launcher.VERSION}
        with patch.object(launcher, "health_for_profile", return_value=health) as probe, patch.object(launcher.subprocess, "Popen") as spawn:
            self.assertEqual(controller.start(), health)
            self.assertEqual(controller.url, "http://127.0.0.1:18890")
            probe.assert_called_once_with("http://127.0.0.1:18890", self.root)
            spawn.assert_not_called()

    def test_busy_service_cannot_be_stopped_or_backed_up(self):
        controller = launcher.ServiceController(self.root)
        with patch.object(controller, "find_running", return_value={"busy": True, "version": launcher.VERSION}), patch.object(launcher, "request_json") as request:
            with self.assertRaisesRegex(launcher.LauncherError, "任务"):
                controller.stop()
            request.assert_not_called()

    def test_legacy_running_service_is_not_treated_as_current(self):
        controller = launcher.ServiceController(self.root)
        with patch.object(controller, "find_running", return_value={"product": launcher.PRODUCT}), \
             patch.object(launcher.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(launcher.LauncherError, "其他版本"):
                controller.start()
            with self.assertRaisesRegex(launcher.LauncherError, "其他版本"):
                controller.stop()
            spawn.assert_not_called()

    def test_backup_stops_then_restarts_even_after_backup_failure(self):
        controller = launcher.ServiceController(self.root)
        calls = []
        with patch.object(controller, "find_running", return_value={"busy": False}), \
             patch.object(controller, "stop", side_effect=lambda: calls.append("stop")), \
             patch.object(controller, "start", side_effect=lambda: calls.append("start")), \
             patch("backup.create_backup", side_effect=ValueError("synthetic failure")):
            with self.assertRaisesRegex(ValueError, "synthetic failure"):
                controller.backup(self.root / "backup.zip")
        self.assertEqual(calls, ["stop", "start"])

    def test_offline_backup_does_not_start_service(self):
        controller = launcher.ServiceController(self.root)
        with patch.object(controller, "find_running", return_value=None), \
             patch.object(controller, "start") as start, \
             patch("backup.create_backup", return_value={"article_count": 0}) as backup:
            result = controller.backup(self.root / "backup.zip")
            self.assertEqual(result["article_count"], 0)
            backup.assert_called_once_with(self.root, self.root / "backup.zip")
            start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
