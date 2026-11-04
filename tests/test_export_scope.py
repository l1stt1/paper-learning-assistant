"""Exports follow explicit reading scope and retain historical personal records."""
import json
from pathlib import Path
import sys
import threading
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import test_library_features as fixtures
from server import Handler, LocalServer


class ExportScopeTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.LibraryFeatureTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.lib = fixture.lib
        preview = self.lib.preview_import(fixtures.pdf_bytes("third paper"))
        self.cid = self.lib.commit_import({"preview_id": preview["preview_id"], "title": "Third", "doi": "10.1234/c"})["id"]
        self.topic = self.lib.save_topic({"name": "主题甲", "article_ids": ["paper-a", "paper-b"]})
        other = self.lib.save_topic({"name": "主题乙", "article_ids": ["paper-b", self.cid]})
        self.ab = self.lib.capture_comparison({"article_ids": ["paper-a", "paper-b"], "topic_id": self.topic["id"], "question": "甲主题问题"})
        self.bc = self.lib.capture_comparison({"article_ids": ["paper-b", self.cid], "topic_id": other["id"], "question": "乙主题问题"})
        for a, b, title in [("paper-a", "paper-b", "甲关系"), ("paper-b", self.cid, "乙关系")]:
            self.lib.add_relation({"source": a, "target": b, "source_page": 1, "target_page": 1,
                                   "title": title, "explanation": "待验证联系"})

    def test_topic_selected_and_all_scopes_do_not_leak_each_other(self):
        topic = self.lib.network_records("topic", self.topic["id"])
        self.assertEqual([r["id"] for r in topic["comparisons"]], [self.ab["id"]])
        self.assertEqual([r["title"] for r in topic["relations"]], ["甲关系"])
        markdown = self.lib.export_network(topic)
        self.assertIn("范围：学习主题：主题甲", markdown)
        self.assertNotIn("乙主题问题", markdown)
        selected = self.lib.network_records("selected", article_ids=["paper-b", self.cid])
        self.assertEqual([r["id"] for r in selected["comparisons"]], [self.bc["id"]])
        self.assertEqual(len(self.lib.network_records()["comparisons"]), 2)
        with self.assertRaises(ValueError):
            self.lib.network_records("selected", article_ids=[])
        with self.assertRaises(ValueError):
            self.lib.network_records("topic", "missing")
        # Regrouping changes current members, never the saved comparison identity.
        self.lib.save_topic({"id": self.topic["id"], "name": "改名", "article_ids": ["paper-a"]})
        saved = self.lib.network_records("topic", self.topic["id"])["comparisons"][0]
        self.assertEqual(saved["topic_name"], "主题甲")
        self.assertEqual(saved["article_ids"], ["paper-a", "paper-b"])

    def test_http_export_scope_and_legacy_history_survive(self):
        annotation = {"id": "old-annotation", "page": 1, "quote": "old quote", "note": "old note", "rects": [{"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.02}]}
        with self.lib.db("paper-a") as db:
            db.executemany("INSERT INTO notes(created_at,body) VALUES(?,?)", [("2026-01-01", "first note"), ("2026-01-02", "latest note")])
            db.execute("INSERT INTO annotations VALUES(?,?,?,?,?)", (annotation["id"], 1, "2026-01-01", json.dumps(annotation), 1))
        http = LocalServer(("127.0.0.1", 0), Handler)
        http.lib = self.lib
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:" + str(http.server_port)
        try:
            payload = json.load(urllib.request.urlopen(base + "/api/network/export?format=json&scope=topic&topic_id=" + self.topic["id"]))
            self.assertEqual([r["id"] for r in payload["comparisons"]], [self.ab["id"]])
            self.assertEqual(payload["scope"]["kind"], "topic")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(base + "/api/network/export?scope=selected&ids=")
            self.assertEqual(ctx.exception.code, 400)
            snapshot = json.load(urllib.request.urlopen(base + "/api/export?id=paper-a&format=json"))
            self.assertEqual(snapshot["state"]["note"], "latest note")
            self.assertEqual(snapshot["state"]["annotations"], [])
            self.assertEqual([r["body"] for r in snapshot["legacy_records"]["notes"]], ["latest note", "first note"])
            self.assertEqual(snapshot["legacy_records"]["annotations"], [{**annotation, "archived": True}])
            self.assertEqual(self.lib.legacy_records("paper-b"), {"notes": [], "annotations": []})
        finally:
            http.shutdown()
            http.server_close()


if __name__ == "__main__":
    unittest.main()
