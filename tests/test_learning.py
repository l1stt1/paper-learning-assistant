"""Focused persistence, article-isolation and local HTTP contract checks."""
import importlib.util
import json
from synthetic_library import seed_library
import subprocess
import sys
import threading
import unittest
from legacy_fixtures import seed_note
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("learning_server", ROOT / "server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class LearningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_root = ROOT / "work" / "validation" / uuid.uuid4().hex
        cls.test_root.mkdir(parents=True)
        first = seed_library(cls.test_root, "second-test-article")
        cls.lib = server.Library(cls.test_root)
        cls.aid = first["id"]
        cls.http = server.LocalServer(("127.0.0.1", 0), server.Handler)
        cls.http.lib = cls.lib
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = "http://127.0.0.1:" + str(cls.http.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.lib.close()

    def test_01_seed_cards_do_not_mark_learning(self):
        self.assertGreaterEqual(len(self.lib.contents(self.aid)["concepts"]), 25)
        self.assertEqual(self.lib.state(self.aid)["concepts"], {})
        self.assertEqual(self.lib.state(self.aid)["questions"], [])

    def test_02_custom_concept_and_isolation(self):
        payload = {"article_id": self.aid, "question": "这个概念如何解释？", "quote": "a selected test term", "page": 25,
                   "capture_concept": True, "term": "Validation-only concept", "generate": False}
        first = self.lib.capture(payload)
        second = self.lib.capture(payload)
        self.assertEqual(first["concept_id"], second["concept_id"])
        self.assertEqual(self.lib.state(self.aid)["concepts"][first["concept_id"]]["interactions"], 2)
        self.assertEqual(self.lib.state("second-test-article")["questions"], [])
        self.assertNotIn(first["concept_id"], {c["id"] for c in self.lib.contents("second-test-article")["concepts"]})
        # Reopening the same library must retain the selected concept and question.
        self.lib.close()
        reopened = server.Library(self.test_root)
        self.__class__.lib = reopened
        self.http.lib = reopened
        self.assertEqual(reopened.question(self.aid, first["id"])["quote"], payload["quote"])

    def test_03_notes_revisions_and_export(self):
        for note in ["第一版个人想法", "第二版个人想法"]:
            seed_note(self.lib, self.aid, note)
        with self.lib.db(self.aid) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM notes").fetchone()[0], 2)
        self.assertIn("第二版个人想法", self.lib.export(self.aid))
        self.assertEqual(self.lib.state("second-test-article")["note"], "")

    def test_04_prompt_has_original_pages_and_followup(self):
        parent = self.lib.capture({"article_id": self.aid, "question": "示例中的比例怎样计算？", "page": 25, "quote": "More than 50%"})
        child = self.lib.capture({"article_id": self.aid, "question": "请再举一个例子", "page": 25, "parent_id": parent["id"]})
        prompt = self.lib.prompt(self.aid, child)
        self.assertIn('"pdf_page": 25', prompt)
        supplied = json.loads(prompt.split("<reading_data>\n", 1)[1].split("\n</reading_data>", 1)[0])
        original = next(p["text"] for p in supplied["source_pages"] if p["pdf_page"] == 25)
        self.assertIn("More than 50%", original)
        self.assertIn("示例中的比例怎样计算？", prompt)
        self.assertEqual(child["quote"], parent["quote"])

    def test_05_invalid_requests_do_not_write(self):
        with self.assertRaises(ValueError):
            self.lib.capture({"article_id": "../../other", "question": "test"})
        with self.assertRaises(ValueError):
            self.lib.capture({"article_id": self.aid, "question": "test", "page": 54})
        with self.assertRaises(ValueError):
            self.lib.update_state({"article_id": self.aid, "action": "concept", "concept_id": "missing", "status": "mastered"})

    def test_followup_can_keep_discussion_without_inheriting_a_citation(self):
        parent = self.lib.capture({"article_id": self.aid, "question": "Where are the data?", "page": 25,
                                   "quote": "example selected text", "source_scope": "page", "reading_page": 2})
        body = {"article_id": self.aid, "question": "哪些数据可以公开获取？", "parent_id": parent["id"],
                "source_scope": "article", "reading_page": 40, "page": 25,
                "quote": parent["quote"], "include_image": True, "zotero_source": {"attachment_key": "stale"}}
        whole = self.lib.capture(body)
        self.assertEqual((whole["source_scope"], whole["page"], whole["quote"], whole["include_image"]),
                         ("article", 40, "", False))
        self.assertNotIn("zotero_source", whole)
        prompt = self.lib.prompt(self.aid, whole)
        payload = json.loads(prompt.split("<reading_data>\n")[1].split("\n</reading_data>")[0])
        self.assertNotIn("page", payload["question"])
        self.assertNotIn("reading_page", payload["question"])
        self.assertEqual(payload["followup_history"][-1]["question"], parent["question"])
        same = self.lib.capture({**body, "reading_page": 3})
        second = json.loads(self.lib.prompt(self.aid, same).split("<reading_data>\n")[1].split("\n</reading_data>")[0])
        self.assertEqual(second["source_pages"], payload["source_pages"])
        self.assertIn("整篇提问 · 提问时阅读位置：第40页", self.lib.export(self.aid))
        focused = self.lib.capture({"article_id": self.aid, "question": "Another page", "parent_id": parent["id"],
                                    "source_scope": "page", "reading_page": 3, "page": 4, "quote": ""})
        self.assertEqual((focused["reading_page"], focused["page"], focused["quote"]), (3, 4, ""))

    def test_whole_article_finds_data_statement_and_named_source(self):
        from question_evidence import article_evidence
        pages = ["General introduction."] * 20
        pages[5] = "Archived XQZ data were downloaded from the repository under accession XQZ123."
        pages[16] = "Data and code availability. Our newly generated data can be requested from the authors."
        rows, _ = article_evidence(pages, {}, "XQZ 数据能否开放获取？")
        self.assertTrue({6, 17}.issubset({r["pdf_page"] for r in rows}))

    def test_uncurated_overview_includes_later_text_without_keyword_hits(self):
        from question_evidence import article_evidence
        rows, _ = article_evidence(["Body text."] * 20, {}, "这篇文章的主要发现是什么？")
        self.assertIn(20, [r["pdf_page"] for r in rows])

    def test_06_http_security_and_source_render(self):
        health = json.loads(urllib.request.urlopen(self.url + "/health").read())
        self.assertEqual(health["root"], str(self.test_root.resolve()))
        self.assertEqual(health["data_root"], str(self.lib.data_root.resolve()))
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(urllib.request.Request(self.url + "/api/library", headers={"Host": "evil.example"}))
        self.assertEqual(ctx.exception.code, 403)
        sid = self.lib.contents(self.aid)["sections"][0]["id"]
        body = json.dumps({"article_id": self.aid, "action": "progress", "section_id": sid, "completed": True}).encode()
        request = urllib.request.Request(self.url + "/api/state", data=body, headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request)
        self.assertEqual(ctx.exception.code, 403)
        request.add_header("X-Learning-Token", self.lib.token)
        response = json.loads(urllib.request.urlopen(request).read())
        self.assertTrue(response["state"]["progress"][sid])
        page = json.loads(urllib.request.urlopen(self.url + "/api/page?id=" + self.aid + "&page=25").read())
        self.assertEqual(page["total"], 53)
        image = urllib.request.urlopen(self.url + "/api/page-image?id=" + self.aid + "&page=3").read()
        self.assertTrue(image.startswith(b"\x89PNG"))

    def test_07_retired_note_editor_and_duplicate_start(self):
        note = "文" * 40000
        payload = json.dumps({"article_id": self.aid, "action": "note", "note": note}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.url + "/api/state", data=payload, headers={"Content-Type": "application/json", "X-Learning-Token": self.lib.token})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request)
        self.assertEqual(ctx.exception.code, 400)
        self.assertNotEqual(self.lib.state(self.aid)["note"], note)
        q = self.lib.capture({"article_id": self.aid, "question": "duplicate launch test", "page": 1})
        q["status"] = "running"
        self.lib.save_question(self.aid, q)
        self.lib.active_ai = ("question", self.aid, q["id"])
        self.lib.ai_lock.acquire()
        try:
            process = subprocess.run([sys.executable, str(ROOT / "server.py"), "--port", str(self.http.server_port), "--data-dir", str(self.lib.data_root)], capture_output=True, timeout=15)
            self.assertNotEqual(process.returncode, 0)
            self.assertEqual(self.lib.question(self.aid, q["id"])["status"], "running")
        finally:
            self.lib.active_ai = None
            self.lib.ai_lock.release()


if __name__ == "__main__":
    unittest.main(verbosity=2)
