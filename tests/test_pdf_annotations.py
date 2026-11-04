"""Retained PDF annotations remain readable and cannot be edited by retired APIs."""
import json
from synthetic_library import seed_library
import sys
import unittest
from legacy_fixtures import seed_annotation
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import Library


class PdfAnnotationTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / "work" / "validation" / uuid.uuid4().hex
        self.root.mkdir(parents=True)
        first = seed_library(self.root, "other-paper")
        self.lib = Library(self.root)
        self.addCleanup(lambda: self.lib.close())
        self.aid = first["id"]
        self.payload = {"article_id": self.aid, "action": "create", "page": 5,
                        "quote": "The example contains several geometric shapes", "note": "需要对照图注",
                        "rects": [{"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.02}]}

    def test_highlight_note_persistence_export_and_article_isolation(self):
        existing = self.lib.capture({"article_id": self.aid, "question": "已有问题保留", "page": 5})
        note = "已有批注：高亮保留位置。"
        annotation = seed_annotation(self.lib, self.aid, {**self.payload, "note": note})
        self.lib.close()
        reopened = Library(self.root)
        self.lib = reopened
        saved = reopened.annotations(self.aid, 5)[0]
        self.assertEqual(saved["note"], note)
        self.assertEqual(saved["rects"], self.payload["rects"])
        self.assertEqual(reopened.annotations(self.aid, 6), [])
        self.assertEqual(reopened.annotations("other-paper"), [])
        self.assertEqual(reopened.question(self.aid, existing["id"])["question"], "已有问题保留")
        self.assertIn(note, reopened.export(self.aid))
        self.assertEqual(reopened.state(self.aid)["annotations"][0]["quote"], self.payload["quote"])
        with self.assertRaises(ValueError):
            reopened.update_annotation({"article_id": "other-paper", "action": "note", "annotation_id": annotation["id"], "note": "wrong article"})

    def test_cancel_is_recoverable_and_not_exported(self):
        annotation = seed_annotation(self.lib, self.aid, self.payload, archived=True)
        self.assertTrue(self.lib.legacy_records(self.aid)["annotations"][0]["archived"])
        self.assertEqual(self.lib.annotations(self.aid), [])
        self.assertNotIn(self.payload["quote"], self.lib.export(self.aid))
        with self.lib.db(self.aid) as db:
            row = db.execute("SELECT payload,archived FROM annotations WHERE id=?", (annotation["id"],)).fetchone()
            self.assertEqual(row[1], 1)
            self.assertEqual(json.loads(row[0])["note"], self.payload["note"])

    def test_retired_annotation_editor_cannot_modify_records(self):
        annotation = seed_annotation(self.lib, self.aid, self.payload)
        before = self.lib.legacy_records(self.aid)
        for action in ("create", "note", "archive"):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, "只读"):
                self.lib.update_annotation({**self.payload, "action": action, "annotation_id": annotation["id"], "note": "must not write"})
        self.assertEqual(self.lib.legacy_records(self.aid), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
