"""Topic membership must not duplicate or reset article learning records."""
from datetime import datetime
from pathlib import Path
import sys
import unittest
from legacy_fixtures import seed_note
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import test_library_features as feature_fixtures
from server import Library


class TopicTests(unittest.TestCase):
    def setUp(self):
        fixture = feature_fixtures.LibraryFeatureTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.lib = fixture.lib
        self.source = {"kind": "collection", "collection_key": "COLLECTA", "include_children": False,
                       "label": "Original group", "server_id": "test-library"}

    def test_two_topics_reference_the_same_learning_record(self):
        seed_note(self.lib, "paper-a", "保留同一篇文章的笔记")
        before = self.lib.state("paper-a")
        first = self.lib.save_topic({"name": "主题甲", "article_ids": ["paper-a"]})
        second = self.lib.save_topic({"name": "主题乙"})
        self.lib.add_topic_articles(second["id"], ["paper-a"])
        self.lib.add_topic_articles(second["id"], ["paper-a"])
        self.assertEqual(self.lib.topic(first["id"])["article_ids"], ["paper-a"])
        self.assertEqual(self.lib.topic(second["id"])["article_ids"], ["paper-a"])
        self.assertEqual(self.lib.state("paper-a"), before)
        self.assertEqual(len(self.lib.articles), 2)

    def test_order_notes_and_binding_survive_rename_and_restart(self):
        topic = self.lib.save_topic({"name": "初始主题", "article_ids": ["paper-a", "paper-b"]})
        self.lib.add_topic_articles(topic["id"], [], source=self.source, source_items={"ITEMAAAA": "paper-a"})
        changed = self.lib.save_topic({"id": topic["id"], "name": "新名称", "notes": "主题问题", "article_ids": ["paper-b", "paper-a"]})
        self.lib.close()
        reopened = Library(self.lib.root)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.topic(topic["id"]), changed)
        self.assertEqual(changed["source"]["collection_key"], "COLLECTA")

    def test_rebinding_preserves_articles_but_resets_source_members(self):
        topic = self.lib.save_topic({"name": "主题", "article_ids": ["paper-a"]})
        self.lib.add_topic_articles(topic["id"], [], source=self.source, source_items={"ITEMAAAA": "paper-a"})
        renamed_source = {**self.source, "label": "Renamed group"}
        self.lib.add_topic_articles(topic["id"], ["paper-b"], source=renamed_source, source_items={"ITEMBBBB": "paper-b"})
        self.assertEqual(len(self.lib.topic(topic["id"])["source_items"]), 2)
        rebound = self.lib.add_topic_articles(topic["id"], [], source={**self.source, "collection_key": "COLLECTB"}, source_items={})
        self.assertEqual(rebound["article_ids"], ["paper-a", "paper-b"])
        self.assertEqual(rebound["source_items"], {})

    def test_failed_save_does_not_publish_partial_memory(self):
        topic = self.lib.save_topic({"name": "原主题", "article_ids": ["paper-a"]})
        before = self.lib.topic_path.read_bytes()
        with patch("topic_features.os.replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.lib.save_topic({"id": topic["id"], "name": "未写入的名称"})
        self.assertEqual(self.lib.topic_path.read_bytes(), before)
        self.assertEqual(self.lib.topic(topic["id"]), topic)

    def test_stale_editor_does_not_overwrite_new_import(self):
        topic = self.lib.save_topic({"name": "主题", "article_ids": ["paper-a"]})
        self.lib.add_topic_articles(topic["id"], ["paper-b"])
        with self.assertRaisesRegex(ValueError, "主题已更新"):
            self.lib.save_topic({"id": topic["id"], "name": "旧表单", "article_ids": ["paper-a"], "expected_updated_at": topic["updated_at"]})
        self.assertEqual(self.lib.topic(topic["id"])["article_ids"], ["paper-a", "paper-b"])

    def test_frozen_clock_keeps_mutations_distinct_and_rejects_stale_editor(self):
        with patch("topic_features.topic_time", return_value="2026-10-31T08:30:00+00:00"):
            topic = self.lib.save_topic({"name": "主题", "article_ids": ["paper-a"]})
            added = self.lib.add_topic_articles(topic["id"], ["paper-b"])
            with self.assertRaisesRegex(ValueError, "主题已更新"):
                self.lib.save_topic({"id": topic["id"], "name": "旧表单", "article_ids": ["paper-a"], "expected_updated_at": topic["updated_at"]})
            edited = self.lib.save_topic({"id": topic["id"], "name": "新名称", "expected_updated_at": added["updated_at"]})
            bound = self.lib.add_topic_articles(topic["id"], [], source=self.source, source_items={"ITEMAAAA": "paper-a"})
        timestamps = [datetime.fromisoformat(row["updated_at"]) for row in (topic, added, edited, bound)]
        self.assertTrue(all(before < after for before, after in zip(timestamps, timestamps[1:])))
        self.assertEqual(bound["created_at"], topic["created_at"])
        self.assertEqual(bound["article_ids"], ["paper-a", "paper-b"])

    def test_comparison_scope_and_history_remain_explicit(self):
        topic = self.lib.save_topic({"name": "当时的主题", "article_ids": ["paper-a"]})
        with self.assertRaisesRegex(ValueError, "跨主题"):
            self.lib.capture_comparison({"article_ids": ["paper-a", "paper-b"], "topic_id": topic["id"], "question": "比较分析单位"})
        self.assertEqual(self.lib.comparisons(), [])
        self.lib.add_topic_articles(topic["id"], ["paper-b"])
        selected = ["paper-a", "paper-b"]
        record = self.lib.capture_comparison({"article_ids": selected, "topic_id": topic["id"], "question": "比较分析单位"})
        selected.reverse()
        self.lib.save_topic({"id": topic["id"], "name": "后来改名", "article_ids": ["paper-a"]})
        saved = self.lib.comparison(record["id"])
        self.assertEqual(saved["article_ids"], ["paper-a", "paper-b"])
        self.assertEqual(saved["topic_name"], "当时的主题")
        cross = self.lib.capture_comparison({"article_ids": ["paper-a", "paper-b"], "question": "跨主题比较"})
        self.assertEqual(cross["topic_id"], "")


if __name__ == "__main__":
    unittest.main()
