"""Check source fidelity, stable learning IDs, and safe continuation of model work."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import study_generation as generation
from model_backend import ModelBackend


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.run = ROOT / "work" / "validation" / ("generation-" + uuid.uuid4().hex)
        self.run.mkdir(parents=True)
        self.lib = SimpleNamespace(codex="model-cli", ai_available=True, lock=threading.RLock(), ai_lock=threading.Lock(), active_ai=None)
        self.lib.model_backend = ModelBackend(self.run / "learning", lambda: self.lib.codex)
        self.text = "The independent evaluation measured recognition of previously unseen antigens."

    def test_packaged_writing_rules_are_checked_before_model_work(self):
        lib = SimpleNamespace(app_root=self.run, root=self.run / "profile", content={"paper": {}},
                              articles={"paper": {"pages": 1}})
        with patch.object(generation, "read_materials") as read, patch.object(generation, "model_json") as model:
            with self.assertRaises(FileNotFoundError):
                generation.generate_article(lib, "paper", [], self.run / "missing-rules", lambda *a: None,
                                            lambda *a: None, lambda: False)
            read.assert_not_called()
            model.assert_not_called()
        lib.app_root = ROOT
        pages = [{"page": 1, "primary": True, "text": self.text}]
        with patch.object(generation, "source_pages", return_value=pages), \
             patch.object(generation, "read_materials", return_value=[]), \
             patch.object(generation, "model_json", side_effect=RuntimeError("guide reached")) as model:
            with self.assertRaisesRegex(RuntimeError, "guide reached"):
                generation.generate_article(lib, "paper", [], self.run / "valid-rules", lambda *a: None,
                                            lambda *a: None, lambda: False)
            self.assertIn("文章导读写作要求", model.call_args.args[4])

    def test_reading_requires_every_page_and_exact_excerpts(self):
        batch = [{"material_id": "reading", "page": 1, "text": self.text * 2}]
        value = {"pages": [{"material_id": "reading", "page": 1, "quotes": [self.text]}]}
        generation.validate_reading(value, batch)
        value["pages"][0]["quotes"] = ["The model confirmed clinical therapeutic benefit."]
        with self.assertRaises(ValueError):
            generation.validate_reading(value, batch)
        with self.assertRaises(ValueError):
            generation.validate_reading({"pages": []}, batch)

    def test_short_redundant_excerpt_does_not_discard_valid_page_evidence(self):
        batch = [{"material_id": "reading", "page": 1, "text": self.text * 2}]
        note = {"material_id": "reading", "page": 1, "quotes": ["The", self.text]}
        generation.validate_reading({"pages": [note]}, batch)
        self.assertEqual(note["quotes"], [self.text])
        note["quotes"] = ["The"]
        with self.assertRaises(ValueError):
            generation.validate_reading({"pages": [note]}, batch)

    def test_id_preservation_and_personal_namespace(self):
        old = [{"id": "existing"}]
        with self.assertRaisesRegex(ValueError, "既有ID"):
            generation.check_items([{"id": "changed", "pages": [1]}], old, 1, "章节")
        with self.assertRaisesRegex(ValueError, "ID重复或无效"):
            generation.check_items([{"id": "custom-personal", "pages": [1]}], [], 1, "概念")
        with self.assertRaises(ValueError):
            generation.check_items([{"id": "existing", "pages": [2]}], old, 1, "章节")

    def test_pdf_word_wrap_hyphen_is_not_a_changed_quote(self):
        self.assertTrue(generation.check_quote("embedding to a vector of all zeroes", "embedding to a vec-tor of all zeroes"))
        self.assertFalse(generation.check_quote("the difference is -0.5 between groups", "the difference is 0.5 between groups"))

    def test_each_learning_item_requires_its_own_source(self):
        items = [{"id": "one", "pages": [1]}, {"id": "two", "pages": [2]}]
        checks = [{"id": "one", "page": 1, "quote": self.text}]
        with self.assertRaisesRegex(ValueError, "每个条目"):
            generation.validate_checks(checks, items, {1: self.text, 2: self.text})
        checks.append({"id": "two", "page": 1, "quote": self.text})
        with self.assertRaises(ValueError):
            generation.validate_checks(checks, items, {1: self.text, 2: self.text})

    def test_cross_citations_cannot_escape_selected_articles(self):
        refs = [{"article_id": "paper-a", "page": 1, "quote": self.text}]
        sources = {("paper-a", 1): self.text, ("paper-b", 1): self.text}
        with self.assertRaisesRegex(ValueError, "所有指定文章"):
            generation.validate_refs(refs, ["paper-a", "paper-b"], sources)
        refs.append({"article_id": "paper-b", "page": 1, "quote": self.text})
        generation.validate_refs(refs, ["paper-a", "paper-b"], sources)

    def cross_library(self):
        articles, content = {}, {}
        for aid in ("paper-a", "paper-b"):
            with fitz.open() as pdf:
                pdf.new_page().insert_text((72, 72), self.text)
                (self.run / (aid + ".pdf")).write_bytes(pdf.tobytes())
            articles[aid] = {"title": aid, "pdf": aid + ".pdf", "pages": 1}
            content[aid] = {"overview": {}, "sections": [{"id": aid, "title": "A question", "pages": [1]}],
                            "comparison": {key: {"text": "Study evidence", "pages": [1]} for key in generation.COMPARISON_KEYS}}
        return SimpleNamespace(root=self.run, articles=articles, content=content, pages={aid: ["stale cached text"] for aid in articles},
                               check_reference=lambda aid, page: None, extract_page_text=lambda page: page.get_text())

    def test_cross_fingerprint_is_order_independent_and_reads_current_primary_pdf(self):
        lib = self.cross_library()
        first, source_text, identity = generation.cross_inputs(lib, ["paper-b", "paper-a"])
        self.assertEqual([row["article_id"] for row in first], ["paper-a", "paper-b"])
        self.assertIn("independent evaluation", source_text["paper-a", 1])
        self.assertNotIn("stale cached", source_text["paper-a", 1])
        self.assertEqual(identity, generation.cross_inputs(lib, ["paper-a", "paper-b"])[2])
        with fitz.open() as pdf:
            pdf.new_page().insert_text((72, 72), "Revised evidence uses a different evaluation cohort.")
            (self.run / "paper-a.pdf").write_bytes(pdf.tobytes())
        _, revised_text, changed = generation.cross_inputs(lib, ["paper-a", "paper-b"])
        self.assertNotEqual(identity, changed)
        self.assertIn("Revised evidence", revised_text["paper-a", 1])

    def test_cross_generator_only_calls_missing_module_and_rejects_input_race(self):
        lib = self.cross_library()
        identity = generation.cross_inputs(lib, ["paper-a", "paper-b"])[2]
        signature = hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()
        summary = {**{key: "An answer" for key in ("summary", "agreements", "differences", "boundaries", "hypotheses")},
                   "citations": [{"article_id": aid, "page": 1, "quote": self.text} for aid in ("paper-a", "paper-b")]}
        published = []
        with patch("study_generation.model_json", return_value=summary) as model:
            generation.generate_cross(lib, ["paper-b", "paper-a"], self.run / "cross", lambda *args: None,
                                      lambda module, result: published.append((module, result)), lambda: False,
                                      modules=["summary"], expected_signature=signature)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(model.call_args.args[2], "summary")
        self.assertEqual([module for module, _ in published], ["summary"])
        lib.content["paper-a"]["overview"]["question"] = "Changed between scheduling and generation"
        with patch("study_generation.model_json") as model:
            with self.assertRaisesRegex(ValueError, "生成前已变化"):
                generation.generate_cross(lib, ["paper-a", "paper-b"], self.run / "race", lambda *args: None,
                                          lambda *args: self.fail("Changed input must not publish"), lambda: False,
                                          expected_signature=signature)
        model.assert_not_called()

    def test_validated_checkpoint_prevents_repeated_model_request(self):
        schema = generation.obj(answer=generation.S)
        calls = []
        def cli(command, **kwargs):
            calls.append(command)
            Path(command[command.index("--output-last-message") + 1]).write_text('{"answer":"ok"}', encoding="utf-8")
            return SimpleNamespace(returncode=0)
        with patch("model_backend.subprocess.run", side_effect=cli):
            first = generation.model_json(self.lib, self.run, "guide", schema, "prompt", [], lambda: False, lambda x: None)
            second = generation.model_json(self.lib, self.run, "guide", schema, "prompt", [], lambda: False, lambda x: None)
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.lib.ai_lock.locked())
        self.assertIsNone(self.lib.active_ai)

    def test_failed_checkpoint_save_does_not_hold_ai_lock(self):
        with patch.object(self.lib.model_backend, "generate", return_value={"answer": "ok"}), patch("study_generation.save", side_effect=OSError("storage unavailable")):
            with self.assertRaises(OSError):
                generation.model_json(self.lib, self.run, "guide", generation.obj(answer=generation.S), "prompt", [], lambda: False, lambda x: None)
        self.assertFalse(self.lib.ai_lock.locked())
        self.assertIsNone(self.lib.active_ai)

    def test_stop_before_request_does_not_call_cli(self):
        with patch.object(self.lib.model_backend, "generate") as cli:
            with self.assertRaises(generation.StudyStopped):
                generation.model_json(self.lib, self.run, "guide", generation.obj(answer=generation.S), "prompt", [], lambda: True, lambda x: None)
        cli.assert_not_called()

    def test_reject_bad_schema_and_bool_page(self):
        with self.assertRaises(ValueError):
            generation.validate_schema({"page": True}, generation.obj(page=generation.N))
        with self.assertRaises(ValueError):
            generation.validate_schema({"page": 1, "personal_note": "private"}, generation.obj(page=generation.N))


if __name__ == "__main__":
    unittest.main()
