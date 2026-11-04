"""Validate durable study tasks using generated fixtures and a fake generator."""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
import threading
import time
import types
import unittest
from legacy_fixtures import seed_note
from unittest.mock import patch
import uuid

import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from library_features import ANSWER_FIELDS, COMPARISON_FIELDS
from server import Library
from study_features import ARTICLE_MODULES, DONE, StudyFeatures, module_complete
from study_generation import cross_inputs
from zotero_integration import ZoteroError


class StudyLibrary(Library, StudyFeatures):
    def __init__(self, root):
        super().__init__(root)
        if not hasattr(self, "study_jobs"):
            self.init_study()


def complete_content(aid):
    return {"paper_id": "local:" + aid, "page_convention": "Physical PDF pages", "retained_field": {"keep": True},
            "overview": {"question": "What does this study ask?", "findings": ["A finding"], "limits": []},
            "sections": [{"id": aid + "-section", "title": "A research question", "pages": [1], "summary": "A finding"}],
            "concepts": [{"id": aid + "-concept", "term": "Specificity", "pages": [1]}],
            "comparison": {key: {"text": "Known study context", "pages": [1]} for key in COMPARISON_FIELDS}}


class FakeStopped(RuntimeError):
    pass


class FakeGeneration(types.ModuleType):
    StudyStopped = FakeStopped
    cross_inputs = staticmethod(cross_inputs)

    def __init__(self):
        super().__init__("study_generation")
        self.calls = Counter()
        self.fail_once = set()
        self.block = None
        self.entered = threading.Event()
        self.release = threading.Event()
        self.bad_guide = False
        self.empty_relations = False

    def payload(self, aid, module):
        complete = complete_content(aid)
        if module == "sources":
            return {"source_coverage": {"main_text": ["Main PDF page 1"], "figures": ["Page image viewed"], "missing": []},
                    "study_review": {"status": "ai_source_checked", "label": "AI整理；尚未人工逐项核查", "checked_at": "2026-10-31"}}
        if module == "guide":
            if self.bad_guide:
                complete["sections"][0]["id"] = "different-section"
            return {"overview": complete["overview"], "sections": complete["sections"]}
        return {module: complete[module]}

    def _stage(self, aid, module, run_dir, payload, report, publish, should_stop):
        if should_stop():
            raise self.StudyStopped()
        report(module, "running", "Fake generation " + module)
        if (aid, module) in self.fail_once:
            self.fail_once.remove((aid, module))
            raise RuntimeError("Injected " + module + " failure")
        checkpoint = run_dir / (module + ".json")
        if checkpoint.exists():
            payload = json.loads(checkpoint.read_text(encoding="utf-8"))
        else:
            self.calls[(aid, module)] += 1
            checkpoint.write_text(json.dumps(payload), encoding="utf-8")
        if self.block == (aid, module):
            self.entered.set()
            if not self.release.wait(10):
                raise RuntimeError("Fake generator release timeout")
        publish(module, payload)
        report(module, "completed", "Fake module saved")

    def generate_article(self, lib, aid, materials, run_dir, report, publish, should_stop, regenerate=False):
        if any(not isinstance(row["path"], Path) or not row["path"].is_file() for row in materials):
            raise AssertionError("Materials must be existing whitelisted paths")
        needs_content = regenerate or any(not module_complete(lib.content[aid], key) for key in ARTICLE_MODULES[1:])
        for module in ARTICLE_MODULES:
            if not regenerate and module_complete(lib.content[aid], module) and not (module == "sources" and needs_content):
                report(module, "existing", "Existing module")
                continue
            payload = self.payload(aid, module)
            if module == "sources":
                payload["source_coverage"]["materials"] = [row["id"] for row in materials]
            self._stage(aid, module, run_dir, payload, report, publish, should_stop)
        return {"content": deepcopy(lib.content[aid]), "coverage": {"main_text": [1]}}

    def generate_cross(self, lib, article_ids, run_dir, report, publish, should_stop, modules=("relations", "summary"), expected_signature=None):
        evidence = [{"article_id": aid, "page": 1, "quote": "Scientific evidence for " + aid} for aid in article_ids]
        relations = [{"source": article_ids[0], "target": article_ids[1], "type": "method", "title": "Related study designs",
                      "explanation": "A method comparison", "boundary": "Generated comparison requires review", "evidence": evidence[:2]}]
        if self.empty_relations:
            relations = []
        summary = {**{key: "A source-grounded " + key for key in ANSWER_FIELDS}, "citations": evidence}
        if "relations" in modules:
            self._stage("", "relations", run_dir, {"relations": relations}, report, publish, should_stop)
        if "summary" in modules:
            self._stage("", "summary", run_dir, {"summary": summary}, report, publish, should_stop)
        return {"relations": relations, "summary": summary}


class StudyFeatureTests(unittest.TestCase):
    def setUp(self):
        # Keep nested run checkpoints below the native Windows path-length boundary.
        self.root = ROOT / "work" / ("s-" + uuid.uuid4().hex[:12])
        self.root.mkdir(parents=True)
        articles = []
        for aid in ("paper-a", "paper-b", "paper-c"):
            folder = self.root / "articles" / aid
            folder.mkdir(parents=True)
            with fitz.open() as pdf:
                pdf.new_page().insert_text((72, 72), "Scientific evidence for " + aid)
                pdf.save(folder / "source.pdf")
            content = complete_content(aid)
            if aid != "paper-c":
                content.update(sections=[], concepts=[], comparison={key: {"text": "尚未整理，请回原文核对。", "pages": []} for key in COMPARISON_FIELDS})
            (folder / "learning.json").write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
            articles.append({"id": aid, "title": aid, "short_title": aid, "doi": "10.1234/" + aid, "pages": 1,
                             "pdf": f"articles/{aid}/source.pdf", "content": f"articles/{aid}/learning.json"})
        with fitz.open() as pdf:
            pdf.new_page().insert_text((72, 72), "Supplementary material")
            pdf.save(self.root / "supplement.pdf")
        (self.root / "table.xlsx").write_bytes(b"Not a PDF; never parsed")
        articles[0]["assets"] = [{"id": "supplement", "label": "Supplement PDF", "kind": "pdf", "path": "supplement.pdf", "pages": 1},
                                  {"id": "table", "label": "Table spreadsheet", "kind": "xlsx", "path": "table.xlsx"}]
        (self.root / "articles.json").write_text(json.dumps({"library_id": "isolated-study-test", "articles": articles}), encoding="utf-8")
        self.fake = FakeGeneration()
        self.module_patch = patch.dict(sys.modules, {"study_generation": self.fake})
        self.module_patch.start()
        self.lib = self.new_library()
        seed_note(self.lib, "paper-c", "Private learning note must remain local")
        with self.lib.db("paper-c") as db:
            db.execute("INSERT INTO concepts VALUES(?,?,?,?,?)", ("paper-c-concept", "mastered", "before", 3, "Personal answer"))
            db.execute("INSERT INTO progress VALUES(?,?)", ("paper-c-section", 1))
            db.execute("INSERT INTO custom_concepts VALUES(?,?)", ("custom-private", json.dumps({"id": "custom-private", "term": "Private concept"})))
        with self.lib.network_db() as db:
            db.execute("INSERT INTO relations VALUES(?,?,?)", ("personal-relation", "before", json.dumps({"id": "personal-relation", "source": "paper-a", "target": "paper-c"})))
        self.personal_before = self.personal_snapshot()

    def tearDown(self):
        self.fake.release.set()
        deadline = time.monotonic() + 5
        while self.lib.study_active_job and time.monotonic() < deadline:
            time.sleep(0.01)
        self.lib.close()
        self.module_patch.stop()

    def new_library(self):
        if hasattr(self, "lib"):
            self.lib.close()
        with patch("server.find_codex", return_value="fake-generator-only"):
            return StudyLibrary(self.root)

    def personal_snapshot(self):
        rows = {}
        for path in (self.root / "learning").glob("*.sqlite"):
            with sqlite3.connect(path) as db:
                rows[path.name] = list(db.iterdump())
        return rows

    def wait(self, job_id):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = self.lib.study_job(job_id)["job"]
            if self.lib.study_active_job != job_id:
                return job
            time.sleep(0.01)
        self.fail("Fake study task did not finish")

    def start(self, **body):
        return self.wait(self.lib.study_start({"article_ids": ["paper-a"], **body})["job"]["id"])

    def row(self, job, aid="paper-a"):
        return next(row for row in job["articles"] if row["article_id"] == aid)

    def content_path(self, aid="paper-a"):
        return self.root / self.lib.article(aid)["content"]

    def test_complete_publication_updates_cache_preserves_ids_and_personal_databases(self):
        original = self.content_path().read_bytes()
        job = self.start(article_ids=["paper-a", "paper-b"], cross_article_ids=["paper-a", "paper-b"])
        self.assertEqual(job["status"], "completed")
        self.assertTrue(all(state["status"] == "completed" for row in job["articles"] for state in row["modules"].values()))
        content = json.loads(self.content_path().read_text(encoding="utf-8"))
        self.assertEqual(content, self.lib.content["paper-a"])
        self.assertEqual(content["paper_id"], "local:paper-a")
        self.assertEqual(content["retained_field"], {"keep": True})
        self.assertEqual((self.lib.data_root / "study-runs" / job["id"] / "paper-a" / "original-learning.json").read_bytes(), original)
        self.assertEqual(self.lib.study_relations()[0]["origin"], "generated")
        self.assertEqual(self.lib.study_comparisons()[0]["origin"], "study")
        self.assertEqual(self.personal_snapshot(), self.personal_before)
        self.assertEqual(self.lib.study_status()["articles"][0]["review_status"], "AI整理；尚未人工逐项核查")

    def test_old_complete_content_is_existing_without_new_source_fields_or_model(self):
        original = self.content_path("paper-c").read_bytes()
        self.lib.codex = None
        job = self.start(article_ids=["paper-c"])
        self.assertEqual(job["status"], "completed")
        self.assertEqual({state["status"] for state in self.row(job, "paper-c")["modules"].values()}, {"existing"})
        self.assertEqual(self.fake.calls, Counter())
        self.assertEqual(self.content_path("paper-c").read_bytes(), original)
        self.assertEqual(self.personal_snapshot(), self.personal_before)

    def test_missing_content_refreshes_existing_source_coverage_for_selected_materials(self):
        content = deepcopy(self.lib.content["paper-a"])
        content["source_coverage"] = {"main_text": ["Previous reading scope"], "legacy": True}
        self.content_path().write_text(json.dumps(content), encoding="utf-8")
        self.lib.content["paper-a"] = content
        job = self.start(materials={"paper-a": ["reading", "supplement"]})
        self.assertEqual(job["status"], "completed")
        self.assertEqual(self.row(job)["modules"]["sources"]["status"], "completed")
        self.assertEqual(self.lib.content["paper-a"]["source_coverage"]["materials"], ["reading", "supplement"])
        self.assertNotIn("legacy", self.lib.content["paper-a"]["source_coverage"])
        self.assertEqual(self.fake.calls[("paper-a", "sources")], 1)

    def test_partial_article_failure_continues_other_articles_and_resume_skips_success(self):
        self.fake.fail_once.add(("paper-a", "concepts"))
        first = self.start(article_ids=["paper-a", "paper-b"], cross_article_ids=["paper-a", "paper-b"])
        self.assertEqual(first["status"], "partial")
        self.assertEqual(self.row(first)["modules"]["concepts"]["status"], "failed")
        self.assertTrue(all(state["status"] == "completed" for state in self.row(first, "paper-b")["modules"].values()))
        self.assertEqual(self.lib.study_relations(), [])
        before_calls = self.fake.calls.copy()
        second = self.wait(self.lib.study_resume(first["id"])["job"]["id"])
        self.assertEqual(second["status"], "completed")
        for key in [("paper-a", "sources"), ("paper-a", "guide"), *[("paper-b", module) for module in ARTICLE_MODULES]]:
            self.assertEqual(self.fake.calls[key], before_calls[key])
        self.assertEqual(self.personal_snapshot(), self.personal_before)

    def test_stop_keeps_current_result_and_resume_continues_later_modules(self):
        self.fake.block = ("paper-a", "guide")
        job_id = self.lib.study_start({"article_ids": ["paper-a", "paper-b"]})["job"]["id"]
        self.assertTrue(self.fake.entered.wait(5))
        self.assertTrue(self.lib.study_stop(job_id)["job"]["stop_requested"])
        self.fake.release.set()
        stopped = self.wait(job_id)
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(self.row(stopped)["modules"]["guide"]["status"], "completed")
        self.assertEqual(self.fake.calls[("paper-a", "concepts")], 0)
        self.assertEqual(self.fake.calls[("paper-b", "sources")], 0)
        resumed = self.wait(self.lib.study_resume(job_id)["job"]["id"])
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(self.fake.calls[("paper-a", "guide")], 1)

    def test_one_running_job_and_same_job_resume_never_double_start(self):
        self.fake.block = ("paper-a", "sources")
        job_id = self.lib.study_start({"article_ids": ["paper-a"]})["job"]["id"]
        self.assertTrue(self.fake.entered.wait(5))
        self.assertEqual(self.lib.study_resume(job_id)["job"]["id"], job_id)
        with self.assertRaisesRegex(ValueError, "正在运行"):
            self.lib.study_start({"article_ids": ["paper-b"]})
        self.assertEqual(len(self.lib.study_jobs), 1)
        self.fake.release.set()
        self.assertEqual(self.wait(job_id)["status"], "completed")
        self.assertEqual(self.fake.calls[("paper-a", "sources")], 1)

    def test_restart_marks_running_interrupted_without_replay(self):
        with patch("study_features.threading.Thread.start", return_value=None):
            job_id = self.lib.study_start({"article_ids": ["paper-a"]})["job"]["id"]
        self.lib._study_update_job(job_id, lambda job: job.update(status="running"))
        self.lib.study_active_job = None
        self.lib = self.new_library()
        self.assertEqual(self.lib.study_job(job_id)["job"]["status"], "interrupted")
        self.assertEqual(self.fake.calls, Counter())
        self.assertEqual(self.wait(self.lib.study_resume(job_id)["job"]["id"])["status"], "completed")
        self.assertEqual(self.personal_snapshot(), self.personal_before)

    def test_content_save_failure_does_not_publish_cache_and_checkpoint_avoids_regeneration(self):
        original_write = self.lib._study_write_json
        failed = False
        def write(path, value):
            nonlocal failed
            if Path(path) == self.content_path() and value.get("sections") and not failed:
                failed = True
                raise OSError("Injected content publication failure")
            return original_write(path, value)
        with patch.object(self.lib, "_study_write_json", side_effect=write):
            first = self.start()
        self.assertEqual(first["status"], "partial")
        self.assertEqual(self.lib.content["paper-a"]["sections"], [])
        self.assertEqual(json.loads(self.content_path().read_text(encoding="utf-8"))["sections"], [])
        self.assertEqual(self.row(first)["modules"]["guide"]["status"], "failed")
        self.assertEqual(self.wait(self.lib.study_resume(first["id"])["job"]["id"])["status"], "completed")
        self.assertEqual(self.fake.calls[("paper-a", "guide")], 1)

    def test_job_status_failure_cannot_claim_completion_and_is_resumable(self):
        original_write = self.lib._study_write_json
        content_saved = False
        def write(path, value):
            nonlocal content_saved
            if Path(path) == self.lib.study_job_path and content_saved:
                raise OSError("Injected task state failure")
            original_write(path, value)
            if Path(path) == self.content_path():
                content_saved = True
        with patch.object(self.lib, "_study_write_json", side_effect=write):
            first = self.start()
        self.assertEqual(first["status"], "interrupted")
        self.assertIn("source_coverage", self.lib.content["paper-a"])
        self.assertNotEqual(self.row(first)["modules"]["sources"]["status"], "completed")
        self.assertEqual(self.wait(self.lib.study_resume(first["id"])["job"]["id"])["status"], "completed")
        self.assertEqual(self.fake.calls[("paper-a", "sources")], 1)

    def test_cross_partial_save_is_independent_and_resume_does_not_duplicate_relations(self):
        self.fake.fail_once.add(("", "summary"))
        first = self.start(article_ids=["paper-a"], cross_article_ids=["paper-a", "paper-c"])
        self.assertEqual(first["status"], "partial")
        self.assertEqual(first["cross"]["relations"]["status"], "completed")
        relation_id = self.lib.study_relations()[0]["id"]
        self.assertEqual(self.lib.study_comparisons(), [])
        self.lib = self.new_library()
        second = self.wait(self.lib.study_resume(first["id"])["job"]["id"])
        self.assertEqual(second["status"], "completed")
        self.assertEqual([row["id"] for row in self.lib.study_relations()], [relation_id])
        self.assertEqual(len(self.lib.study_comparisons()), 1)
        self.assertEqual(self.fake.calls[("", "relations")], 1)
        self.assertEqual(self.personal_snapshot(), self.personal_before)

    def test_default_cross_reuses_same_inputs_with_reversed_order_without_model(self):
        first = self.start(article_ids=["paper-a"], cross_article_ids=["paper-a", "paper-c"])
        before = deepcopy(self.lib.study_network)
        calls = self.fake.calls.copy()
        self.lib.codex = None
        second = self.start(article_ids=[], cross_article_ids=["paper-c", "paper-a"])
        self.assertEqual(second["status"], "completed")
        self.assertEqual({value["status"] for value in second["cross"].values()}, {"existing"})
        self.assertEqual({value["source_job_id"] for value in second["cross"].values()}, {first["id"]})
        self.assertEqual(self.fake.calls, calls)
        self.assertEqual(self.lib.study_network, before)
        self.assertEqual(self.personal_snapshot(), self.personal_before)

    def test_cross_regeneration_keeps_history_and_empty_relations_supersede_old_rows(self):
        first = self.start(article_ids=["paper-a"], cross_article_ids=["paper-a", "paper-c"])
        old_relations = deepcopy(self.lib.study_network["relations"])
        old_summary = deepcopy(self.lib.study_network["comparisons"])
        self.fake.empty_relations = True
        second = self.start(article_ids=[], cross_article_ids=["paper-c", "paper-a"], regenerate=True)
        self.assertEqual(second["status"], "completed")
        self.assertEqual(self.lib.study_relations(), [])
        self.assertEqual(self.lib.study_network["relations"], old_relations)
        self.assertEqual(self.lib.study_network["comparisons"][:1], old_summary)
        self.assertEqual([row["study_job_id"] for row in self.lib.study_comparisons()], [second["id"]])
        self.assertEqual(len(self.lib.study_comparisons(include_history=True)), 2)
        self.assertFalse(self.lib.study_relations(include_history=True)[0]["is_current"])
        self.lib = self.new_library()
        self.lib.codex = None
        calls = self.fake.calls.copy()
        reused = self.start(article_ids=[], cross_article_ids=["paper-a", "paper-c"])
        self.assertEqual(reused["status"], "completed")
        self.assertEqual(reused["cross"]["relations"]["source_job_id"], second["id"])
        self.assertEqual(self.lib.study_relations(), [])
        self.assertEqual(self.fake.calls, calls)
        self.assertNotEqual(first["id"], second["id"])

    def test_queued_full_reuse_cannot_succeed_after_input_changes(self):
        self.start(article_ids=["paper-a"], cross_article_ids=["paper-a", "paper-c"])
        before, calls = deepcopy(self.lib.study_network), self.fake.calls.copy()
        with patch.object(self.lib, "_study_launch"):
            queued = self.lib.study_start({"article_ids": [], "cross_article_ids": ["paper-a", "paper-c"]})["job"]
        self.assertEqual({value["status"] for value in queued["cross"].values()}, {"existing"})
        self.lib.content["paper-a"]["overview"]["question"] = "Changed while the task was queued"
        self.lib._study_run(queued["id"])
        result = self.lib.study_job(queued["id"])["job"]
        self.assertNotEqual(result["status"], "completed")
        self.assertIn("输入已变化", result["error"])
        self.assertEqual({value["status"] for value in result["cross"].values()}, {"failed"})
        self.assertEqual(self.fake.calls, calls)
        self.assertEqual(self.lib.study_network, before)

    def test_cross_content_and_pdf_changes_invalidate_reuse_and_old_inputs_can_be_selected_again(self):
        first = self.start(article_ids=["paper-a"], cross_article_ids=["paper-a", "paper-c"])
        original = deepcopy(self.lib.content["paper-a"])
        self.lib.content["paper-a"]["overview"]["question"] = "A revised scientific question"
        second = self.start(article_ids=[], cross_article_ids=["paper-a", "paper-c"])
        self.assertEqual(second["status"], "completed")
        self.assertEqual(self.fake.calls[("", "relations")], 2)
        self.assertEqual(len(self.lib.study_relations(include_history=True)), 2)
        self.lib.content["paper-a"] = original
        reused = self.start(article_ids=[], cross_article_ids=["paper-c", "paper-a"])
        self.assertEqual(reused["cross"]["summary"]["source_job_id"], first["id"])
        self.assertEqual(self.lib.study_comparisons()[0]["study_job_id"], first["id"])
        self.assertEqual(self.fake.calls[("", "summary")], 2)
        with fitz.open() as pdf:
            pdf.new_page().insert_text((72, 72), "Changed scientific evidence for paper-a")
            (self.root / self.lib.article("paper-a")["pdf"]).write_bytes(pdf.tobytes())
        changed_pdf = self.start(article_ids=[], cross_article_ids=["paper-a", "paper-c"])
        self.assertEqual(changed_pdf["status"], "completed")
        self.assertEqual(self.fake.calls[("", "relations")], 3)
        self.assertEqual(self.personal_snapshot(), self.personal_before)

    def test_cross_scope_preserves_topic_identity_and_does_not_merge_other_article_sets(self):
        self.start()
        topic = self.lib.save_topic({"name": "First question", "article_ids": ["paper-a", "paper-c"]})
        other = self.lib.save_topic({"name": "Another question", "article_ids": ["paper-a", "paper-c"]})
        first = self.start(article_ids=[], cross_article_ids=["paper-a", "paper-c"], topic_id=topic["id"])
        second = self.start(article_ids=[], cross_article_ids=["paper-c", "paper-a"], topic_id=other["id"])
        self.assertEqual(self.fake.calls[("", "summary")], 2)
        self.assertEqual(len(self.lib.study_comparisons()), 2)
        self.lib.save_topic({"id": topic["id"], "name": "Renamed question", "article_ids": ["paper-c", "paper-a"]})
        reused = self.start(article_ids=[], cross_article_ids=["paper-c", "paper-a"], topic_id=topic["id"])
        self.assertEqual(reused["cross"]["summary"]["source_job_id"], first["id"])
        self.assertNotEqual(reused["cross"]["summary"]["source_job_id"], second["id"])
        third = self.start(article_ids=["paper-b"], cross_article_ids=["paper-a", "paper-b"])
        self.assertEqual(third["status"], "completed")
        self.assertEqual(self.fake.calls[("", "summary")], 3)
        self.assertEqual(len(self.lib.study_comparisons()), 3)

    def test_new_cross_job_reuses_successful_partial_module(self):
        self.fake.fail_once.add(("", "summary"))
        first = self.start(article_ids=["paper-a"], cross_article_ids=["paper-a", "paper-c"])
        relation_id = self.lib.study_relations()[0]["id"]
        second = self.start(article_ids=[], cross_article_ids=["paper-c", "paper-a"])
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["cross"]["relations"]["source_job_id"], first["id"])
        self.assertEqual(self.fake.calls[("", "relations")], 1)
        self.assertEqual([row["id"] for row in self.lib.study_relations()], [relation_id])
        self.assertEqual(self.lib.study_comparisons()[0]["study_job_id"], second["id"])

    def test_resume_rejects_changed_cross_inputs_without_replaying_saved_module(self):
        self.fake.fail_once.add(("", "summary"))
        first = self.start(article_ids=["paper-a"], cross_article_ids=["paper-a", "paper-c"])
        before = deepcopy(self.lib.study_network)
        calls = self.fake.calls.copy()
        self.lib.content["paper-a"]["comparison"]["limits"]["text"] = "A changed evidence boundary"
        resumed = self.wait(self.lib.study_resume(first["id"])["job"]["id"])
        self.assertEqual(resumed["status"], "partial")
        self.assertIn("输入已变化", resumed["cross"]["summary"]["message"])
        self.assertEqual(self.fake.calls, calls)
        self.assertEqual(self.lib.study_network, before)

    def test_legacy_results_require_explicit_adoption_and_keep_scientific_payload_unchanged(self):
        first = self.start(article_ids=["paper-a"], cross_article_ids=["paper-a", "paper-c"])
        legacy = {key: deepcopy(self.lib.study_network[key]) for key in ("relations", "comparisons")}
        self.lib._study_write_json(self.lib.study_network_path, legacy)
        # Earlier accepted manual revisions need not equal the generator's old hash.
        old_inputs = self.lib.data_root / "study-runs" / first["id"] / "cross" / "inputs.json"
        old_inputs.write_text(json.dumps({"article_ids": ["paper-a", "paper-c"], "content_sha256": "before-reviewed-corrections"}), encoding="utf-8")
        self.lib = self.new_library()
        self.lib.codex = None
        self.assertEqual(self.lib.study_relations(), [])
        self.assertEqual(len(self.lib.study_relations(include_history=True)), 1)
        with self.assertRaisesRegex(ValueError, "智能整理暂不可用"):
            self.lib.study_start({"article_ids": [], "cross_article_ids": ["paper-a", "paper-c"]})
        signature = self.lib.study_cross_fingerprint(["paper-c", "paper-a"])
        adopted = self.lib.study_adopt_cross(first["id"])
        self.assertEqual(adopted["input_signature"], signature)
        self.assertTrue(adopted["adopted_legacy"])
        for key in ("relations", "comparisons"):
            self.assertEqual(self.lib.study_network[key], legacy[key])
        calls = self.fake.calls.copy()
        reused = self.start(article_ids=[], cross_article_ids=["paper-a", "paper-c"])
        self.assertEqual(reused["status"], "completed")
        self.assertEqual(self.fake.calls, calls)
        self.lib.content["paper-a"]["overview"]["question"] = "Changed after adoption"
        with self.assertRaisesRegex(ValueError, "已有输入身份"):
            self.lib.study_adopt_cross(first["id"])
        self.assertEqual(self.personal_snapshot(), self.personal_before)

    def test_material_whitelist_primary_default_and_zotero_offline(self):
        self.lib.zotero_links = {"server_id": "test", "articles": {"paper-a": {"item_key": "PAPERAAA", "attachment_key": "ATTACAAA"}}}
        with patch.object(self.lib, "zotero_client", side_effect=ZoteroError("Offline")):
            preview = self.lib.study_preview({"article_ids": ["paper-a"]})["articles"][0]
            self.assertEqual([row["id"] for row in preview["materials"] if row["selected"]], ["reading"])
            self.assertTrue(any("Zotero当前不可用" in value for value in preview["warnings"]))
            self.assertEqual([row["id"] for row in self.lib.study_materials("paper-a")], ["reading"])
        for selected in (["supplement"], ["reading", "../private.pdf"], ["reading", "table"], ["reading", "reading"]):
            with self.subTest(selected=selected), self.assertRaises(ValueError):
                self.lib.study_materials("paper-a", selected)
        self.assertEqual([row["id"] for row in self.lib.study_materials("paper-a", ["reading", "supplement"])], ["reading", "supplement"])

    def test_zotero_materials_only_use_linked_parent_pdf_and_require_explicit_selection(self):
        self.lib.zotero_links = {"server_id": "test", "articles": {"paper-a": {"item_key": "PAPERAAA", "attachment_key": "ATTACAAA"}}}
        calls = []
        class Catalog:
            def get_all(inner, route):
                calls.append(route)
                return [{"data": {"key": "ATTACAAA", "itemType": "attachment", "parentItem": "PAPERAAA", "contentType": "application/pdf", "title": "Alternate PDF"}},
                        {"data": {"key": "OTHERPDF", "itemType": "attachment", "parentItem": "OTHERKEY", "contentType": "application/pdf", "title": "Other article"}},
                        {"data": {"key": "NOTESAAA", "itemType": "note", "parentItem": "PAPERAAA", "contentType": "text/html", "note": "PRIVATE"}}]
            def local_file(inner, key):
                if key != "ATTACAAA":
                    raise AssertionError("Only a PDF belonging to the linked paper may be read")
                return self.root / "supplement.pdf"
        with patch.object(self.lib, "zotero_client", return_value=Catalog()):
            preview = self.lib.study_preview({"article_ids": ["paper-a"]})["articles"][0]
            extra = next(row for row in preview["materials"] if row["id"] == "zotero:ATTACAAA")
            self.assertFalse(extra["selected"])
            self.assertNotIn("zotero:OTHERPDF", {row["id"] for row in preview["materials"]})
            selected = self.lib.study_materials("paper-a", ["reading", "zotero:ATTACAAA"])
            self.assertEqual([row["id"] for row in selected], ["reading", "zotero:ATTACAAA"])
            self.assertTrue(all(route == "items/PAPERAAA/children?itemType=attachment" for route in calls))

    def test_network_save_failure_keeps_prior_module_and_resumes_without_model_replay(self):
        original_write = self.lib._study_write_json
        failed = False
        def write(path, value):
            nonlocal failed
            if Path(path) == self.lib.study_network_path and value.get("comparisons") and not failed:
                failed = True
                raise OSError("Injected network publication failure")
            return original_write(path, value)
        with patch.object(self.lib, "_study_write_json", side_effect=write):
            first = self.start(article_ids=["paper-a"], cross_article_ids=["paper-a", "paper-c"])
        self.assertEqual(first["status"], "partial")
        self.assertEqual(len(self.lib.study_relations()), 1)
        self.assertEqual(self.lib.study_comparisons(), [])
        self.assertEqual(json.loads(self.lib.study_network_path.read_text(encoding="utf-8"))["comparisons"], [])
        second = self.wait(self.lib.study_resume(first["id"])["job"]["id"])
        self.assertEqual(second["status"], "completed")
        self.assertEqual(len(self.lib.study_relations()), 1)
        self.assertEqual(len(self.lib.study_comparisons()), 1)
        self.assertEqual(self.fake.calls[("", "summary")], 1)
        self.assertEqual(self.personal_snapshot(), self.personal_before)

    def test_regeneration_cannot_drop_existing_section_ids_or_personal_state(self):
        original = deepcopy(self.lib.content["paper-c"]["sections"])
        self.fake.bad_guide = True
        job = self.start(article_ids=["paper-c"], regenerate=True)
        self.assertEqual(job["status"], "partial")
        self.assertEqual(self.row(job, "paper-c")["modules"]["guide"]["status"], "failed")
        self.assertEqual(self.lib.content["paper-c"]["sections"], original)
        self.assertEqual(self.personal_snapshot(), self.personal_before)

    def test_cross_only_scope_and_saved_topic_identity_are_fixed(self):
        self.start()
        topic = self.lib.save_topic({"name": "Original theme", "article_ids": ["paper-a", "paper-c"]})
        with self.assertRaisesRegex(ValueError, "不全属于"):
            self.lib.study_start({"article_ids": [], "cross_article_ids": ["paper-a", "paper-b"], "topic_id": topic["id"]})
        calls = self.fake.calls.copy()
        job = self.start(article_ids=[], cross_article_ids=["paper-a", "paper-c"], topic_id=topic["id"])
        self.assertEqual(job["status"], "completed")
        self.lib.save_topic({"id": topic["id"], "name": "Renamed", "article_ids": ["paper-c"]})
        self.assertEqual(self.lib.study_comparisons()[0]["topic_name"], "Original theme")
        self.assertEqual(self.lib.study_comparisons()[0]["article_ids"], ["paper-a", "paper-c"])
        for key in [("paper-a", module) for module in ARTICLE_MODULES]:
            self.assertEqual(self.fake.calls[key], calls[key])

    def test_initial_job_save_and_thread_start_failures_do_not_run_generator(self):
        with patch.object(self.lib, "_study_write_json", side_effect=OSError("Injected initial save failure")):
            with self.assertRaises(OSError):
                self.lib.study_start({"article_ids": ["paper-a"]})
        self.assertIsNone(self.lib.study_active_job)
        self.assertEqual(self.lib.study_jobs, {})
        with patch("study_features.threading.Thread.start", side_effect=RuntimeError("Injected launch failure")):
            job = self.lib.study_start({"article_ids": ["paper-a"]})["job"]
        self.assertEqual(job["status"], "failed")
        self.assertEqual(self.fake.calls, Counter())
        self.assertIsNone(self.lib.study_active_job)


if __name__ == "__main__":
    unittest.main()
