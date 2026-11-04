"""Protect source validation, saved work and backend switching without real model calls."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import Library, Handler, LocalServer, ANSWER_FIELDS
from library_features import ANSWER_FIELDS as COMPARISON_ANSWER_FIELDS
from study_generation import model_json, obj, S


class ModelIntegrationTests(unittest.TestCase):
    def setUp(self):
        # Keep generated call paths below the Windows path limit; retain fixtures for inspection.
        self.root = Path(tempfile.mkdtemp(prefix="reading-model-"))
        self.source = "Splicing evidence requires separate validation."
        articles = []
        for aid in ("paper-a", "paper-b"):
            with fitz.open() as pdf:
                pdf.new_page().insert_text((72, 72), self.source)
                (self.root / (aid + ".pdf")).write_bytes(pdf.tobytes())
            (self.root / (aid + ".json")).write_text(json.dumps({"overview": {}, "sections": [], "concepts": []}), encoding="utf-8")
            articles.append({"id": aid, "title": aid, "short_title": aid, "doi": "", "pages": 1,
                             "pdf": aid + ".pdf", "content": aid + ".json"})
        (self.root / "articles.json").write_text(json.dumps({"library_id": "model-tests", "articles": articles}), encoding="utf-8")
        with patch("server.find_codex", return_value=None):
            self.lib = Library(self.root)
        self.addCleanup(lambda: self.lib.close())
        self.environment = patch.dict(os.environ, {"READING_TEST_CREDENTIAL": "test-fixture-only"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.settings = {**self.lib.model_backend.settings(), "backend": "openai", "model": "test-model",
                         "key_env": "READING_TEST_CREDENTIAL"}

    def activate(self):
        self.lib.configure_model(self.settings)
        self.assertTrue(self.lib.ai_available)
        self.assertIsNone(self.lib.codex)

    def run_question(self, answer=None, error=None):
        question = self.lib.capture({"article_id": "paper-a", "question": "What does the source show?", "page": 1,
                                     "include_image": True, "generate": False})
        self.lib.ai_lock.acquire()
        self.lib.active_ai = ("question", "paper-a", question["id"])
        with patch.object(self.lib.model_backend, "generate", return_value=answer, side_effect=error) as model:
            self.lib.explain("paper-a", question)
        self.assertFalse(self.lib.ai_lock.locked())
        self.assertIsNone(self.lib.active_ai)
        return self.lib.question("paper-a", question["id"]), model

    def test_api_works_without_cli_and_question_failure_keeps_record(self):
        self.activate()
        answer = {**{name: "Source-linked explanation" for name in ANSWER_FIELDS}, "related_concepts": []}
        question, model = self.run_question(answer)
        self.assertEqual(question["status"], "completed")
        self.assertEqual(question["answer"], answer)
        self.assertEqual(model.call_args.kwargs["kind"], "question")
        self.assertEqual(model.call_args.kwargs["timeout"], 600)
        self.assertEqual(len(model.call_args.args[2]), 1)
        self.assertTrue(model.call_args.args[2][0].is_file())
        failed, model = self.run_question(error=RuntimeError("API request not completed"))
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["question"], "What does the source show?")
        model.assert_called_once()
        self.assertEqual(self.lib.state("paper-b")["questions"], [])

    def test_comparison_still_rejects_unsupported_citations(self):
        self.activate()
        answer = {key: "Evidence summary" for key in COMPARISON_ANSWER_FIELDS}
        answer["citations"] = [{"article_id": aid, "page": 1, "quote": self.source} for aid in ("paper-a", "paper-b")]
        for valid in (True, False):
            if not valid:
                answer["citations"][0]["quote"] = "An unsupported invented source statement."
            record = self.lib.capture_comparison({"article_ids": ["paper-a", "paper-b"], "question": "Compare evidence"})
            self.lib.ai_lock.acquire()
            self.lib.active_ai = ("comparison", record["id"])
            with patch.object(self.lib.model_backend, "generate", return_value=answer) as model:
                self.lib.explain_comparison(record)
            saved = self.lib.comparison(record["id"])
            self.assertEqual(saved["status"], "completed" if valid else "failed")
            self.assertEqual(model.call_args.kwargs["kind"], "comparison")
            self.assertFalse(self.lib.ai_lock.locked())
        self.assertEqual(self.lib.state("paper-a")["questions"], [])

    def test_study_checkpoint_reuses_success_without_available_backend(self):
        self.activate()
        schema = obj(answer=S)
        run = self.root / "study-output"
        run.mkdir()
        with patch.object(self.lib.model_backend, "generate", return_value={"answer": "checked"}) as model:
            first = model_json(self.lib, run, "guide", schema, "source text", [], lambda: False, lambda value: None)
            self.assertEqual(model.call_args.kwargs["kind"], "study")
            self.assertEqual(model.call_args.kwargs["timeout"], 900)
            self.assertEqual(model.call_args.kwargs["output_file"], "response.json")
            with patch.object(self.lib.model_backend, "available", return_value=False):
                second = model_json(self.lib, run, "guide", schema, "source text", [], lambda: False, lambda value: None)
            self.assertEqual(first, second)
            model.assert_called_once()
        self.assertFalse(self.lib.ai_lock.locked())
        self.assertIsNone(self.lib.active_ai)

    def test_model_switch_rejected_during_requests_and_between_study_calls(self):
        self.activate()
        before = (self.lib.data_root / "model-settings.json").read_bytes()
        for attribute, value in (("active_ai", ("question", "paper-a", "q")), ("study_active_job", "study-running")):
            setattr(self.lib, attribute, value)
            try:
                with self.assertRaisesRegex(ValueError, "运行"):
                    self.lib.configure_model({**self.settings, "backend": "codex"})
            finally:
                setattr(self.lib, attribute, None)
        self.lib.ai_lock.acquire()
        try:
            with self.assertRaises(ValueError):
                self.lib.configure_model({**self.settings, "backend": "codex"})
        finally:
            self.lib.ai_lock.release()
        self.assertEqual(before, (self.lib.data_root / "model-settings.json").read_bytes())

    def test_http_settings_require_token_and_missing_credentials_are_visible(self):
        http = LocalServer(("127.0.0.1", 0), Handler)
        http.lib = self.lib
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        endpoint = f"http://127.0.0.1:{http.server_port}"
        try:
            initial = json.loads(urlopen(endpoint + "/api/model-settings").read())
            self.assertEqual(initial["settings"]["backend"], "codex")
            self.assertFalse(initial["available"])
            body = json.dumps(self.settings).encode()
            request = Request(endpoint + "/api/model-settings", data=body, headers={"Content-Type": "application/json"})
            with self.assertRaises(HTTPError) as error:
                urlopen(request)
            self.assertEqual(error.exception.code, 403)
            self.assertFalse((self.lib.data_root / "model-settings.json").exists())
            request.add_header("X-Learning-Token", self.lib.token)
            configured = json.loads(urlopen(request).read())
            self.assertTrue(configured["available"])
            self.assertNotIn("test-fixture-only", json.dumps(configured))
            library = json.loads(urlopen(endpoint + "/api/library").read())
            self.assertTrue(library["ai_available"])
            self.assertEqual(library["model_backend"]["settings"]["model"], "test-model")
            with patch.dict(os.environ, {}, clear=True):
                status = json.loads(urlopen(endpoint + "/api/model-settings").read())
                self.assertFalse(status["available"])
                self.assertFalse(self.lib.ai_available)
        finally:
            http.shutdown()
            http.server_close()


if __name__ == "__main__":
    unittest.main()
