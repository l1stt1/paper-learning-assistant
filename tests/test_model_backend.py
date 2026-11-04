"""Exercise the model boundary with fake transport; no live credentials or requests."""
import io
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import model_backend as models


SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"], "additionalProperties": False}
FAKE_CREDENTIAL = "TEST_ONLY_NON_CREDENTIAL"


def response(text='{"answer":"source-bound result"}', **changes):
    value = {"status": "completed", "model": "test-model-reported",
             "output": [{"type": "message", "status": "completed", "content": [{"type": "output_text", "text": text}]}],
             "usage": {"input_tokens": 37, "output_tokens": 19, "total_tokens": 56}}
    value.update(changes)
    return value


class ModelBackendTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / "work" / "validation" / ("model-backend-" + uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        self.env = {}
        self.env_patch = patch.object(models.os, "environ", self.env)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.cli_path = "test-only-cli"
        self.backend = models.ModelBackend(self.root / "data", lambda: self.cli_path)
        self.run = self.root / "call"

    def api(self, **settings):
        self.env["TEST_MODEL_KEY"] = FAKE_CREDENTIAL
        return self.backend.configure({"backend": "openai", "model": "test-model", "key_env": "TEST_MODEL_KEY", **settings})

    def fake_transport(self, value):
        opener = Mock()
        raw = value if isinstance(value, bytes) else json.dumps(value).encode()
        opener.open.side_effect = lambda *args, **kwargs: io.BytesIO(raw)
        return opener

    def generate(self, **kwargs):
        return self.backend.generate(SCHEMA, "Private reading context stays out of usage logs.", [], self.run, **kwargs)

    def restored_backend(self, settings, marker):
        self.backend.data_root.mkdir(parents=True, exist_ok=True)
        self.backend.settings_path.write_text(json.dumps(settings), encoding="utf-8")
        self.backend.review_path.write_text(marker, encoding="utf-8")
        return models.ModelBackend(self.backend.data_root, lambda: "synthetic-cli")

    def test_restored_api_settings_require_confirmation_before_credential_or_transport_use(self):
        settings = {"backend": "openai", "base_url": "https://example.invalid", "model": "synthetic-model", "key_env": "SYNTHETIC_ENV_NAME"}
        backend = self.restored_backend(settings, '{"requires_review":true}')
        with patch.object(models.os, "environ") as environment, patch.object(models, "build_opener") as network, patch.object(models.subprocess, "run") as cli:
            self.assertFalse(backend.available())
            self.assertIn("来自备份", backend.status()["message"])
            with self.assertRaises(models.ModelBackendError) as caught:
                backend.generate(SCHEMA, "synthetic question", [], self.run)
            self.assertEqual(caught.exception.code, "not_configured")
            environment.get.assert_not_called()
            network.assert_not_called()
            cli.assert_not_called()
        self.assertEqual(backend.settings()["base_url"], settings["base_url"])
        self.assertFalse(self.run.exists())
        self.env["SYNTHETIC_ENV_NAME"] = FAKE_CREDENTIAL
        self.assertTrue(backend.configure(settings)["available"])
        self.assertEqual(json.loads(backend.review_path.read_text()), {"requires_review": False})
        opener = self.fake_transport(response())
        with patch.object(models, "build_opener", return_value=opener), patch.object(models.subprocess, "run") as cli:
            backend.generate(SCHEMA, "confirmed synthetic question", [], self.run)
        self.assertEqual(opener.open.call_args.args[0].full_url, "https://example.invalid/responses")
        self.assertEqual(opener.open.call_args.args[0].get_header("Authorization"), "Bearer " + FAKE_CREDENTIAL)
        cli.assert_not_called()
        self.assertTrue(models.ModelBackend(backend.data_root, lambda: "synthetic-cli").available())

    def test_restored_cli_and_corrupt_review_markers_remain_blocked(self):
        for marker in ('{"requires_review":true}', 'not-json', '{}', '{"requires_review":0}', '{"requires_review":"false"}'):
            with self.subTest(marker=marker):
                backend = self.restored_backend({"backend": "codex"}, marker)
                with patch.object(backend, "codex_provider") as provider, patch.object(models, "build_opener") as network, patch.object(models.subprocess, "run") as cli:
                    self.assertFalse(backend.available())
                    with self.assertRaises(models.ModelBackendError):
                        backend.generate(SCHEMA, "synthetic question", [], self.run)
                    provider.assert_not_called()
                    network.assert_not_called()
                    cli.assert_not_called()
        self.assertTrue(backend.configure({"backend": "codex"})["available"])
        with patch.object(backend, "_call_cli", return_value={"answer": "confirmed"}) as cli:
            self.assertEqual(backend.generate(SCHEMA, "synthetic", [], self.run), {"answer": "confirmed"})
        cli.assert_called_once()

    def test_invalid_confirmation_or_failed_marker_save_does_not_unlock(self):
        backend = self.restored_backend({"backend": "codex"}, '{"requires_review":true}')
        with self.assertRaises(ValueError):
            backend.configure({"api_key": FAKE_CREDENTIAL})
        self.assertFalse(backend.available())
        original_write = models._write_json

        def fail_review(path, value):
            if Path(path) == backend.review_path.with_suffix(".json.pending"):
                raise OSError("synthetic storage failure")
            return original_write(path, value)

        with patch.object(models, "_write_json", side_effect=fail_review), self.assertRaises(OSError):
            backend.configure({"backend": "codex"})
        self.assertFalse(backend.available())
        self.assertEqual(json.loads(backend.review_path.read_text()), {"requires_review": True})

    def test_cli_default_uses_existing_arguments_and_dynamic_path(self):
        self.assertEqual(self.backend.settings()["backend"], "codex")
        self.assertFalse(self.backend.settings_path.exists())
        self.cli_path = "changed-test-only-cli"
        image = self.root / "page.png"
        image.write_bytes(b"test image bytes")

        def cli(command, **kwargs):
            self.assertEqual(command[0], self.cli_path)
            self.assertIn("--ephemeral", command)
            self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
            self.assertNotIn("--model", command)
            self.assertEqual(command[-3:], ["--image", str(image), "-"])
            self.assertEqual(kwargs["timeout"], 900)
            self.assertTrue(kwargs["capture_output"])
            self.assertNotIn("env", kwargs)
            Path(command[command.index("--output-last-message") + 1]).write_text('{"answer":"CLI result"}', encoding="utf-8")
            return SimpleNamespace(returncode=0)

        with patch.object(models.subprocess, "run", side_effect=cli) as run, patch.object(models, "build_opener") as network:
            result = self.backend.generate(SCHEMA, "CLI prompt", [image], self.run, output_file="response.json", schema_file="schema.json", timeout=900, kind="study")
        self.assertEqual(result, {"answer": "CLI result"})
        run.assert_called_once()
        network.assert_not_called()
        self.assertEqual(json.loads((self.run / "schema.json").read_text()), SCHEMA)
        usage = self.backend.status()["usage"]
        self.assertEqual((usage["calls"], usage["api_calls"], usage["token_usage_calls"]), (1, 0, 0))
        self.assertNotIn("input_tokens", json.loads((self.run / "model-call.json").read_text()))

    def test_api_payload_contains_text_image_and_strict_schema(self):
        self.api(reasoning_effort="high", max_output_tokens=2048)
        image = self.root / "page.png"
        image.write_bytes(b"page image bytes")
        opener = self.fake_transport(response())
        with patch.object(models, "build_opener", return_value=opener) as build, patch.object(models.subprocess, "run") as cli:
            result = self.backend.generate(SCHEMA, "Read this figure", [image], self.run)
        self.assertEqual(result["answer"], "source-bound result")
        cli.assert_not_called()
        self.assertIsInstance(build.call_args.args[0], models.NoRedirects)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.openai.com/v1/responses")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + FAKE_CREDENTIAL)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 600)
        body = json.loads(request.data)
        self.assertFalse(body["store"])
        self.assertEqual(body["reasoning"], {"effort": "high"})
        self.assertEqual(body["max_output_tokens"], 2048)
        self.assertEqual(body["text"]["format"], {"type": "json_schema", "name": "learning_result", "strict": True, "schema": SCHEMA})
        content = body["input"][0]["content"]
        self.assertEqual(content[0], {"type": "input_text", "text": "Read this figure"})
        self.assertEqual(content[1]["type"], "input_image")
        self.assertTrue(content[1]["image_url"].startswith("data:image/png;base64,"))
        self.assertEqual(json.loads((self.run / "answer.json").read_text()), result)
        usage = self.backend.status()["usage"]
        self.assertEqual((usage["input_tokens"], usage["output_tokens"], usage["total_tokens"]), (37, 19, 56))
        self.assertEqual(usage["last_call"]["returned_model"], "test-model-reported")
        for path in (self.backend.settings_path, self.backend.usage_path, self.run / "model-call.json"):
            contents = path.read_text(encoding="utf-8")
            self.assertNotIn(FAKE_CREDENTIAL, contents)
            self.assertNotIn("Read this figure", contents)

    def test_api_readiness_is_local_and_no_missing_configuration_request(self):
        with patch.object(models, "build_opener") as network:
            self.api()
            self.env.clear()
            self.assertFalse(self.backend.available())
            self.assertFalse(self.backend.status()["available"])
            with self.assertRaises(models.ModelBackendError) as caught:
                self.generate()
        self.assertEqual(caught.exception.code, "not_configured")
        network.assert_not_called()
        self.assertFalse(self.backend.usage_path.exists())
        with self.assertRaises(ValueError):
            self.backend.configure({"model": ""})
        self.cli_path = None
        self.backend.configure({"backend": "codex"})
        self.assertFalse(self.backend.available())

    def test_configuration_rejects_secrets_unknown_fields_and_bad_urls_without_writing(self):
        self.api()
        original = self.backend.settings_path.read_bytes()
        invalid = [
            {"api_key": FAKE_CREDENTIAL}, {"token": FAKE_CREDENTIAL},
            {"base_url": "http://api.example.test/v1"}, {"base_url": "https://user:password@api.example.test/v1"},
            {"base_url": "https://api.example.test/v1?api_key=value"}, {"base_url": "https://api.example.test/v1#fragment"},
            {"base_url": "https://api.example.test\\unsafe"}, {"base_url": "\x00https://api.example.test"},
            {"model": "sk-not-a-real-key"}, {"key_env": "not-an-env-name"},
            {"backend": []}, {"reasoning_effort": []}, {"max_output_tokens": True}, {"max_output_tokens": 1},
        ]
        for settings in invalid:
            with self.subTest(settings=list(settings)):
                with self.assertRaises(ValueError):
                    self.backend.configure(settings)
                self.assertEqual(original, self.backend.settings_path.read_bytes())

    def test_corrupt_saved_configuration_never_silently_uses_cli(self):
        self.backend.data_root.mkdir()
        self.backend.settings_path.write_text('{"backend":', encoding="utf-8")
        reopened = models.ModelBackend(self.backend.data_root, lambda: "available-cli")
        self.assertFalse(reopened.available())
        self.assertIn("未自动切换", reopened.status()["message"])
        with patch.object(models.subprocess, "run") as cli:
            with self.assertRaises(models.ModelBackendError):
                reopened.generate(SCHEMA, "prompt", [], self.run)
        cli.assert_not_called()
        reopened.configure({"backend": "codex"})
        self.assertTrue(reopened.available())

    def test_http_errors_are_safe_and_never_retry_or_fall_back(self):
        self.api()
        for code in (401, 429, 500, 307):
            with self.subTest(code=code):
                opener = Mock()
                opener.open.side_effect = HTTPError("https://api.example.test/", code, FAKE_CREDENTIAL, {}, io.BytesIO(FAKE_CREDENTIAL.encode()))
                with patch.object(models, "build_opener", return_value=opener), patch.object(models.subprocess, "run") as cli:
                    with self.assertRaises(models.ModelBackendError) as caught:
                        self.generate()
                self.assertEqual(caught.exception.code, "http_" + str(code))
                self.assertNotIn(FAKE_CREDENTIAL, str(caught.exception))
                opener.open.assert_called_once()
                cli.assert_not_called()
        for path in (self.backend.usage_path, self.run / "model-call.json"):
            self.assertNotIn(FAKE_CREDENTIAL, path.read_text())
        self.assertFalse((self.run / "answer.json").exists())

    def test_refusal_truncation_tools_and_non_json_are_not_saved_as_answers(self):
        self.api()
        cases = [
            (response(status="incomplete", incomplete_details={"reason": "max_output_tokens"}), "incomplete"),
            (response(output=[{"type": "message", "content": [{"type": "refusal", "refusal": "provider-specific refusal detail"}]}]), "refusal"),
            (response(output=[{"type": "function_call", "arguments": "provider-specific tool arguments"}]), "empty_output"),
            (response("```json\n{}\n```"), "invalid_json"),
            (response("[]"), "invalid_json"), (b"not JSON", "invalid_response"),
        ]
        for value, code in cases:
            with self.subTest(code=code):
                opener = self.fake_transport(value)
                with patch.object(models, "build_opener", return_value=opener):
                    with self.assertRaises(models.ModelBackendError) as caught:
                        self.generate()
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn(FAKE_CREDENTIAL, str(caught.exception))
                self.assertFalse((self.run / "answer.json").exists())
                opener.open.assert_called_once()
        self.assertEqual(self.backend.status()["usage"]["failed_calls"], len(cases))

    def test_network_and_timeout_errors_do_not_expose_provider_details(self):
        self.api()
        for error in (URLError(FAKE_CREDENTIAL), TimeoutError(FAKE_CREDENTIAL), ValueError(FAKE_CREDENTIAL)):
            opener = Mock()
            opener.open.side_effect = error
            with patch.object(models, "build_opener", return_value=opener):
                with self.assertRaises(models.ModelBackendError) as caught:
                    self.generate()
            self.assertNotIn(FAKE_CREDENTIAL, str(caught.exception))
            opener.open.assert_called_once()
        self.assertNotIn(FAKE_CREDENTIAL, self.backend.usage_path.read_text())

    def test_cli_timeout_propagates_without_recording_output(self):
        with patch.object(models.subprocess, "run", side_effect=subprocess.TimeoutExpired("test-cli", 600, output=FAKE_CREDENTIAL)):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.generate()
        record = json.loads((self.run / "model-call.json").read_text())
        self.assertEqual(record["error_code"], "timeout")
        self.assertNotIn(FAKE_CREDENTIAL, self.backend.usage_path.read_text())

    def test_usage_write_failure_keeps_successful_output_usable(self):
        self.api()
        opener = self.fake_transport(response())
        original_writer = models._write_json

        def fail_only_usage(path, value):
            if Path(path).name == "model-call.json":
                raise OSError("isolated test storage failure")
            return original_writer(path, value)

        with patch.object(models, "build_opener", return_value=opener), patch.object(models, "_write_json", side_effect=fail_only_usage):
            result = self.generate()
        self.assertEqual(json.loads((self.run / "answer.json").read_text()), result)
        self.assertTrue(self.backend.status()["usage"]["warning"])
        opener.open.assert_called_once()

    def test_usage_totals_accept_only_numbers(self):
        self.api()
        opener = self.fake_transport(response(model="sk-not-a-real-model", usage={"input_tokens": True, "output_tokens": -1, "total_tokens": "secret"}))
        with patch.object(models, "build_opener", return_value=opener):
            self.generate()
        usage = self.backend.status()["usage"]
        self.assertEqual(usage["token_usage_calls"], 0)
        self.assertEqual(usage["last_call"]["returned_model"], "")
        self.assertNotIn(FAKE_CREDENTIAL, self.backend.usage_path.read_text())

    def test_aggregate_usage_storage_failure_does_not_discard_output(self):
        self.api()
        opener = self.fake_transport(response())
        original_open = Path.open

        def fail_usage_append(path, *args, **kwargs):
            if path == self.backend.usage_path:
                raise OSError("isolated aggregate usage failure")
            return original_open(path, *args, **kwargs)

        with patch.object(models, "build_opener", return_value=opener), patch.object(Path, "open", new=fail_usage_append):
            result = self.generate()
        self.assertEqual(json.loads((self.run / "answer.json").read_text()), result)
        self.assertTrue(self.backend.status()["usage"]["warning"])
        self.assertEqual(json.loads((self.run / "model-call.json").read_text())["status"], "succeeded")
        opener.open.assert_called_once()

    def test_credential_echo_is_rejected_before_result_or_metadata_is_saved(self):
        self.api()
        cases = [response(model=FAKE_CREDENTIAL), response(json.dumps({"answer": FAKE_CREDENTIAL})),
                 response('{"answer":"' + FAKE_CREDENTIAL.replace("_", "\\u005f") + '"}')]
        for value in cases:
            with self.subTest(value_type="credential echo"):
                opener = self.fake_transport(value)
                with patch.object(models, "build_opener", return_value=opener):
                    with self.assertRaises(models.ModelBackendError) as caught:
                        self.generate()
                self.assertEqual(caught.exception.code, "credential_echo")
                self.assertNotIn(FAKE_CREDENTIAL, str(caught.exception))
                self.assertFalse((self.run / "answer.json").exists())
                self.assertNotIn(FAKE_CREDENTIAL, self.backend.usage_path.read_text())

    def test_redirect_handler_does_not_forward_request(self):
        request = Request("https://first.example.test/responses", headers={"Authorization": "Bearer " + FAKE_CREDENTIAL})
        self.assertIsNone(models.NoRedirects().redirect_request(request, None, 307, "redirect", {}, "https://other.example.test/responses"))

    def test_status_checks_do_not_scan_usage_when_only_availability_is_needed(self):
        self.api()
        with patch.object(self.backend, "_usage_summary", side_effect=AssertionError("usage must not be read")):
            self.assertTrue(self.backend.available())


if __name__ == "__main__":
    unittest.main()
