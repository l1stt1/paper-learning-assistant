"""Learning topics reference articles without duplicating their reading records."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
import time
import uuid


def topic_time():
    return datetime.now(timezone.utc).isoformat()


def next_topic_time(previous=None):
    current = topic_time()
    if previous and datetime.fromisoformat(current) <= datetime.fromisoformat(previous):
        return (datetime.fromisoformat(previous) + timedelta(microseconds=1)).isoformat()
    return current


def source_identity(source):
    if not source:
        return None
    return tuple(source.get(key) for key in ("server_id", "kind", "collection_key", "tag", "include_children"))


class TopicFeatures:
    def init_topics(self):
        self.topic_path = self.data_root / "topics.json"
        self.topic_data = json.loads(self.topic_path.read_text(encoding="utf-8")) if self.topic_path.exists() else {"topics": []}
        for topic in self.topic_data["topics"]:
            self.validate_topic_articles(topic["article_ids"])
        ids = [topic["id"] for topic in self.topic_data["topics"]]
        if len(ids) != len(set(ids)):
            raise ValueError("主题列表存在重复标识，请检查主题文件")

    def topics(self):
        with self.lock:
            return deepcopy(self.topic_data["topics"])

    def topic(self, topic_id):
        with self.lock:
            topic = next((value for value in self.topic_data["topics"] if value["id"] == topic_id), None)
            if topic is None:
                raise ValueError("学习主题不存在，请刷新主题列表")
            return deepcopy(topic)

    def validate_topic_articles(self, article_ids):
        if not isinstance(article_ids, list) or any(not isinstance(aid, str) for aid in article_ids):
            raise ValueError("主题文章列表格式不正确")
        if len(article_ids) != len(set(article_ids)):
            raise ValueError("同一主题中的文章不能重复")
        for aid in article_ids:
            self.article(aid)
        return list(article_ids)

    def write_topics(self, values):
        # Caller holds self.lock. Publish memory only after the complete file is saved.
        pending = self.data_root / ("topics.pending-" + uuid.uuid4().hex + ".json")
        pending.write_text(json.dumps(values, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # Windows can briefly deny replacement even when the file is writable.
        # Keep the previous complete file and in-memory state if access stays denied.
        for attempt in range(4):
            try:
                os.replace(pending, self.topic_path)
                break
            except PermissionError as exc:
                if os.name != "nt" or exc.winerror not in (5, 32, 33) or attempt == 3:
                    raise
                time.sleep(0.05 * (attempt + 1))
        self.topic_data = values

    def save_topic(self, body):
        name = body.get("name", "")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
            raise ValueError("请填写主题名称（最多120字符）")
        with self.lock:
            old = self.topic(body["id"]) if body.get("id") else None
            expected = body.get("expected_updated_at")
            if old and expected is not None and expected != old["updated_at"]:
                raise ValueError("主题已更新，请重新打开编辑窗口后保存，已有内容保留")
            notes = body.get("notes", old["notes"] if old else "")
            if not isinstance(notes, str) or len(notes) > 180000:
                raise ValueError("主题笔记需为文字，最多180000字符")
            article_ids = self.validate_topic_articles(body.get("article_ids", old["article_ids"] if old else []))
            current_time = next_topic_time(old["updated_at"] if old else None)
            value = {**(old or {}), "id": old["id"] if old else "topic-" + uuid.uuid4().hex,
                     "name": name.strip(), "notes": notes, "article_ids": article_ids,
                     "source": old.get("source") if old else None,
                     "source_items": {key: aid for key, aid in (old.get("source_items", {}) if old else {}).items() if aid in article_ids},
                     "created_at": old["created_at"] if old else current_time, "updated_at": current_time}
            values = deepcopy(self.topic_data)
            if old:
                values["topics"] = [value if item["id"] == old["id"] else item for item in values["topics"]]
            else:
                values["topics"].append(value)
            self.write_topics(values)
            return deepcopy(value)

    def add_topic_articles(self, topic_id, article_ids, source_items=None, source=None):
        with self.lock:
            added = self.validate_topic_articles(article_ids)
            value = self.topic(topic_id)
            value["article_ids"].extend(aid for aid in added if aid not in value["article_ids"])
            if source is not None:
                if source_identity(source) != source_identity(value.get("source")):
                    # Rebinding preserves existing articles as manual topic members.
                    value["source_items"] = {}
                value["source"] = deepcopy(source)
            if source_items is not None:
                if not isinstance(source_items, dict) or any(not isinstance(key, str) or aid not in value["article_ids"] for key, aid in source_items.items()):
                    raise ValueError("来源文章与主题成员不对应")
                value["source_items"].update(source_items)
            value["updated_at"] = next_topic_time(value["updated_at"])
            values = deepcopy(self.topic_data)
            values["topics"] = [value if item["id"] == topic_id else item for item in values["topics"]]
            self.write_topics(values)
            return deepcopy(value)

    def comparison_topic(self, topic_id, article_ids):
        if not topic_id:
            return {"topic_id": "", "topic_name": ""}
        topic = self.topic(topic_id)
        if not set(article_ids).issubset(topic["article_ids"]):
            raise ValueError("所选文章不全属于当前主题，请明确选择跨主题比较")
        return {"topic_id": topic["id"], "topic_name": topic["name"]}
