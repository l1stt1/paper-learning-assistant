"""Exercise source completeness and recoverable topic imports with isolated PDFs."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
import threading
import time
import unittest
from legacy_fixtures import seed_note
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, build_opener
import uuid

import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import Handler, Library, LocalServer
from zotero_integration import LocalZotero, ZoteroError


def make_pdf(path, title):
    with fitz.open() as pdf:
        pdf.new_page().insert_text((72, 72), title + ". Original scientific text for isolated testing.")
        pdf.set_metadata({"title": title})
        pdf.save(path)


class PagedZotero(LocalZotero):
    def __init__(self, root):
        self.root = root
        self.server_id = "isolated-topic-library"
        self.page_size = 2
        self.version = 1
        self.calls, self.writes = [], []
        self.items, self.paths = {}, {}
        self.unavailable = set()
        self.collections = {
            "CROOT000": {"key": "CROOT000", "name": "Example papers", "parentCollection": False},
            "CCHILD00": {"key": "CCHILD00", "name": "TCR", "parentCollection": "CROOT000"},
            "COTHER00": {"key": "COTHER00", "name": "Other", "parentCollection": False},
        }
        self.items["NOTE0000"] = {"key": "NOTE0000", "itemType": "note", "note": "PRIVATE CONTENT MUST NOT BE REQUESTED",
                                  "collections": ["CROOT000"], "tags": [{"tag": "private"}]}

    def add_paper(self, number, *, collection="CROOT000", doi=None, pdfs=1, tags=()):
        key = f"P{number:07d}"
        collections = [collection] if isinstance(collection, str) else collection
        self.items[key] = {"key": key, "itemType": "journalArticle", "title": f"Paper {number}",
                           "DOI": f"10.1234/paper-{number}" if doi is None else doi, "date": "2024",
                           "collections": collections, "tags": [{"tag": tag} for tag in tags]}
        for index in range(pdfs):
            attachment = f"A{number * 10 + index:07d}"
            path = self.root / (attachment + ".pdf")
            make_pdf(path, f"Paper {number} attachment {index}")
            self.paths[attachment] = path
            self.items[attachment] = {"key": attachment, "itemType": "attachment", "parentItem": key,
                                      "title": f"PDF {index}", "filename": path.name, "contentType": "application/pdf"}
        self.items[f"N{number:07d}"] = {"key": f"N{number:07d}", "itemType": "note", "parentItem": key, "note": "PRIVATE NOTE"}
        return key

    def request(self, method, route, body=None, **kwargs):
        if method != "GET":
            raise AssertionError("A topic import must never write to Zotero")
        parsed = urlsplit(route)
        path = parsed.path.removeprefix("users/0/")
        query = parse_qs(parsed.query)
        self.calls.append((path, deepcopy(query)))
        if path == "collections":
            rows = [{"data": row} for row in self.collections.values()]
        else:
            parts = path.split("/")
            if path.startswith("collections/"):
                if parts[1] not in self.collections:
                    raise ZoteroError("Zotero分类不存在")
                items = [value for value in self.items.values() if parts[1] in value.get("collections", []) and not value.get("parentItem")]
            elif path.startswith("items/") and parts[-1] == "children":
                items = [value for value in self.items.values() if value.get("parentItem") == parts[1]]
            elif path.startswith("items/top"):
                items = [value for value in self.items.values() if not value.get("parentItem")]
            else:
                raise AssertionError("Unexpected fake API path: " + path)
            for condition in query.get("itemType", []):
                items = [value for value in items if value["itemType"] != condition[1:]] if condition.startswith("-") else [value for value in items if value["itemType"] == condition]
            if any(value["itemType"] == "note" for value in items):
                raise AssertionError("Topic source listing requested private notes")
            if path.endswith("/tags"):
                rows = [{"tag": tag} for tag in sorted({tag["tag"] for value in items for tag in value.get("tags", [])})]
            else:
                rows = [{"key": value["key"], "data": value} for value in items if not value.get("deleted")]
        start = int(query.get("start", [0])[0])
        limit = min(self.page_size, int(query.get("limit", [100])[0]))
        return deepcopy(rows[start:start + limit]), {"Total-Results": str(len(rows)), "Last-Modified-Version": str(self.version)}

    def get_item(self, key):
        if key not in self.items or self.items[key].get("deleted"):
            raise ZoteroError("Zotero条目已不存在")
        return deepcopy(self.items[key])

    def local_file(self, key):
        if key in self.unavailable or key not in self.paths:
            raise ZoteroError("附件尚未下载到本机")
        return self.paths[key]


class ZoteroTopicTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / "work" / "validation" / ("topics-zotero-" + uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        make_pdf(self.root / "reading.pdf", "Existing reading version")
        (self.root / "learning.json").write_text(json.dumps({"overview": {}, "sections": [], "concepts": [], "comparison": {}}), encoding="utf-8")
        self.article = {"id": "existing-paper", "title": "Existing paper", "short_title": "Existing", "doi": "10.1234/existing",
                        "pdf": "reading.pdf", "pages": 1, "content": "learning.json"}
        (self.root / "articles.json").write_text(json.dumps({"library_id": "isolated-topic-test", "articles": [self.article]}), encoding="utf-8")
        with patch("server.find_codex", return_value=None):
            self.lib = Library(self.root)
        self.addCleanup(lambda: self.lib.close())
        self.api = PagedZotero(self.root)
        self.lib.zotero_client_class = lambda: self.api

    def preview(self, **kwargs):
        return self.lib.zotero_topic_preview({"source": {"kind": "collection", "collection_key": "CROOT000"}, **kwargs})

    def submit(self, preview, selections=None, name="Topic"):
        if selections is None:
            selections = [{"item_key": row["item_key"]} for row in preview["items"] if row["status"] in {"ready", "existing"}]
        response = self.lib.zotero_topic_import({"preview_id": preview["preview_id"], "name": name, "selections": selections})
        return self.wait(response["job"]["id"])

    def wait(self, job_id):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = self.lib.zotero_topic_job(job_id)["job"]
            if job["status"] != "running":
                return job
            time.sleep(0.01)
        self.fail("Isolated import did not finish")

    def test_paginated_subtrees_exact_tags_and_no_private_note_or_pdf_reads(self):
        direct = self.api.add_paper(1, tags=["A || B", "alpha"])
        child = self.api.add_paper(2, collection="CCHILD00", tags=["A || B"])
        both = self.api.add_paper(3, collection=["CROOT000", "CCHILD00"], tags=["-literal"])
        self.api.add_paper(4, collection="COTHER00", tags=["outside"])
        with patch("zotero_integration.file_digest", side_effect=AssertionError("No preview hashing")), patch("fitz.open", side_effect=AssertionError("No preview PDF parsing")):
            self.assertEqual({row["item_key"] for row in self.preview()["items"]}, {direct, both})
            recursive = self.preview(source={"kind": "collection", "collection_key": "CROOT000", "include_children": True})
            self.assertEqual({row["item_key"] for row in recursive["items"]}, {direct, child, both})
            tagged = self.preview(source={"kind": "tag", "tag": "A || B", "collection_key": "CROOT000", "include_children": True})
            self.assertEqual({row["item_key"] for row in tagged["items"]}, {direct, child})
            self.assertEqual(self.lib.zotero_tags("CROOT000")["tags"], ["-literal", "A || B", "alpha"])
        self.assertTrue(any(query.get("start") == ["2"] for _, query in self.api.calls))
        self.assertEqual(self.api.writes, [])

    def test_preview_states_and_missing_or_renamed_source_are_explicit(self):
        existing = self.api.add_paper(1, doi=self.article["doi"], pdfs=0)
        ready = self.api.add_paper(2)
        choose = self.api.add_paper(3, pdfs=2)
        missing = self.api.add_paper(4, pdfs=0)
        states = {row["item_key"]: row for row in self.preview()["items"]}
        self.assertEqual({key: row["status"] for key, row in states.items()}, {existing: "existing", ready: "ready", choose: "choose_pdf", missing: "missing_pdf"})
        self.assertEqual(states[existing]["article_id"], "existing-paper")
        self.lib.zotero_links = {"server_id": self.api.server_id, "articles": {"existing-paper": {"item_key": "OTHERKEY", "attachment_key": "OTHERATT"}}}
        self.assertEqual(self.preview()["items"][0]["status"], "conflict")
        with self.assertRaisesRegex(ZoteroError, "分类已不存在"):
            self.preview(source={"kind": "collection", "collection_key": "MISSING0"})
        with self.assertRaisesRegex(ZoteroError, "标签已不存在"):
            self.preview(source={"kind": "tag", "tag": "missing"})

    def test_batch_more_than_three_partial_failure_idempotence_and_restart_resume(self):
        for number in range(1, 6):
            self.api.add_paper(number)
        seed_note(self.lib, "existing-paper", "Keep this learning note")
        preview = self.preview()
        self.api.unavailable.add("A0000030")
        first = self.submit(preview)
        self.assertEqual((first["status"], first["processed"], first["added_count"], len(first["failed"])), ("completed", 5, 4, 1))
        again = self.lib.zotero_topic_import({"preview_id": preview["preview_id"], "selections": []})["job"]
        self.assertEqual(again["id"], first["id"])
        self.lib.close()
        with patch("server.find_codex", return_value=None):
            reopened = Library(self.root)
        self.addCleanup(reopened.close)
        reopened.zotero_client_class = lambda: self.api
        self.lib = reopened
        with self.assertRaisesRegex(ZoteroError, "服务已重启"):
            self.lib.zotero_topic_job(first["id"])
        self.api.unavailable.clear()
        second = self.submit(self.lib.zotero_topic_preview({"topic_id": first["topic_id"]}))
        self.assertEqual((second["added_count"], second["reused_count"], len(second["failed"])), (1, 4, 0))
        self.assertEqual(len(self.lib.articles), 6)
        self.assertEqual(len(second["topic"]["article_ids"]), 5)
        self.assertEqual(self.lib.state("existing-paper")["note"], "Keep this learning note")
        self.assertEqual(self.api.writes, [])

    def test_confirmed_source_mapping_survives_changed_doi_in_batch_and_single_import(self):
        key = self.api.add_paper(1, doi=self.article["doi"], pdfs=0)
        first = self.submit(self.preview())
        self.assertEqual(first["reused_count"], 1)
        self.assertEqual(self.lib.zotero_links["articles"], {})
        self.api.items[key]["DOI"] = ""
        attachment = "NEWPDF00"
        path = self.root / "new-reading.pdf"
        make_pdf(path, "A changed PDF for the same catalog item")
        self.api.paths[attachment] = path
        self.api.items[attachment] = {"key": attachment, "parentItem": key, "itemType": "attachment", "contentType": "application/pdf", "title": "New PDF"}
        new_preview = self.lib.zotero_topic_preview({"topic_id": first["topic_id"]})
        self.assertEqual(new_preview["items"][0]["article_id"], "existing-paper")
        single = self.lib.zotero_import_preview({"item_key": key, "attachment_key": attachment})
        self.assertEqual(single["existing_article_id"], "existing-paper")
        result = self.lib.zotero_import({"preview_id": single["preview_id"]})
        self.assertTrue(result["linked_existing"])
        self.assertEqual(result["article"]["pdf"], "reading.pdf")
        self.assertEqual(len(self.lib.articles), 1)

    def test_disagreeing_confirmed_item_mappings_are_not_silently_reused(self):
        key = self.api.add_paper(1, doi=self.article["doi"])
        self.api.add_paper(2)
        first = self.submit(self.preview())
        other_aid = next(row["article_id"] for row in first["results"] if row["item_key"] != key)
        another = self.lib.save_topic({"name": "Conflicting mapping", "article_ids": [other_aid]})
        self.lib.add_topic_articles(another["id"], [], source=first["topic"]["source"], source_items={key: other_aid})
        row = next(row for row in self.preview()["items"] if row["item_key"] == key)
        self.assertEqual(row["status"], "conflict")
        with self.assertRaisesRegex(ZoteroError, "多篇文章"):
            self.lib.zotero_import_preview({"item_key": key, "attachment_key": "A0000010"})

    def test_removed_and_changed_do_not_remove_manual_or_source_members(self):
        first_key = self.api.add_paper(1)
        second_key = self.api.add_paper(2)
        job = self.submit(self.preview())
        self.lib.add_topic_articles(job["topic_id"], ["existing-paper"])
        self.api.items[first_key]["collections"] = ["COTHER00"]
        self.api.items[second_key]["title"] = "Updated catalog title"
        preview = self.lib.zotero_topic_preview({"topic_id": job["topic_id"]})
        self.assertEqual([row["item_key"] for row in preview["removed"]], [first_key])
        self.assertEqual([row["item_key"] for row in preview["changed"]], [second_key])
        after = self.submit(preview, selections=[])
        self.assertEqual(len(after["topic"]["article_ids"]), 3)
        self.assertIn("existing-paper", after["topic"]["article_ids"])
        self.api.collections.pop("CROOT000")
        with self.assertRaisesRegex(ZoteroError, "分类已不存在"):
            self.lib.zotero_topic_preview({"topic_id": job["topic_id"]})
        self.assertEqual(len(self.lib.topic(job["topic_id"])["article_ids"]), 3)

    def test_empty_selection_binds_and_rebinding_retains_articles(self):
        self.api.add_paper(1, doi=self.article["doi"], pdfs=0)
        first = self.submit(self.preview())
        rebind = self.preview(topic_id=first["topic_id"], source={"kind": "collection", "collection_key": "COTHER00"})
        self.assertTrue(rebind["warnings"])
        result = self.submit(rebind, selections=[], name="Must not rename existing")
        self.assertEqual(result["topic"]["name"], "Topic")
        self.assertEqual(result["topic"]["article_ids"], ["existing-paper"])
        self.assertEqual(result["topic"]["source_items"], {})
        self.assertEqual(result["topic"]["source"]["collection_key"], "COTHER00")

    def test_multiple_pdf_needs_selection_and_imports_exact_selected_source(self):
        key = self.api.add_paper(1, pdfs=2)
        preview = self.preview()
        with self.assertRaisesRegex(ZoteroError, "选择预览中可用"):
            self.submit(preview, selections=[{"item_key": key}])
        self.assertEqual(self.lib.topics(), [])
        result = self.submit(preview, selections=[{"item_key": key, "attachment_key": "A0000011"}])
        self.assertEqual(result["added_count"], 1)
        article = self.lib.article(result["results"][0]["article_id"])
        self.assertEqual((self.root / article["pdf"]).read_bytes(), self.api.paths["A0000011"].read_bytes())

    def test_changed_library_or_topic_binding_stops_before_adding(self):
        self.api.add_paper(1)
        preview = self.preview()
        self.api.server_id = "different-library"
        job = self.submit(preview)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(self.lib.topics(), [])
        self.api.server_id = "isolated-topic-library"
        topic = self.lib.save_topic({"name": "Topic"})
        preview = self.preview(topic_id=topic["id"])
        other_source = self.preview(source={"kind": "collection", "collection_key": "COTHER00"})["source"]
        self.lib.add_topic_articles(topic["id"], [], source=other_source)
        job = self.submit(preview)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["processed"], 0)
        self.assertEqual(self.lib.topic(topic["id"])["source"], other_source)

    def test_sqlite_failure_finishes_and_retry_recovers_published_article(self):
        self.api.add_paper(1, doi="")
        preview = self.preview()
        original = self.lib.initialize_article_db
        def fail_new_once(aid, recover_running=True):
            if aid != "existing-paper":
                raise sqlite3.OperationalError("injected SQLite initialization failure")
            return original(aid, recover_running=recover_running)
        with patch.object(self.lib, "initialize_article_db", side_effect=fail_new_once):
            first = self.submit(preview)
        self.assertEqual((first["status"], first["processed"], len(first["failed"])), ("completed", 1, 1))
        self.assertEqual(len(self.lib.articles), 2)
        retry = self.submit(self.lib.zotero_topic_preview({"topic_id": first["topic_id"]}))
        self.assertEqual((retry["reused_count"], retry["failed"]), (1, []))
        self.assertEqual(len(self.lib.articles), 2)
        self.assertEqual(self.lib.state(retry["results"][0]["article_id"])["note"], "")

    def test_thread_start_failure_does_not_leave_active_job(self):
        preview = self.preview()
        with patch("zotero_topic_import.threading.Thread.start", side_effect=RuntimeError("injected start failure")):
            failed = self.lib.zotero_topic_import({"preview_id": preview["preview_id"], "name": "Topic", "selections": []})["job"]
        self.assertEqual(failed["status"], "failed")
        self.assertIsNone(self.lib.zotero_topic_active_job)
        self.assertEqual(self.lib.topics(), [])

    def test_changing_paginated_source_is_rejected_instead_of_truncated(self):
        self.api.add_paper(1)
        original = self.api.request
        def changing_request(method, route, **kwargs):
            if parse_qs(urlsplit(route).query).get("start") == ["2"]:
                self.api.version += 1
            return original(method, route, **kwargs)
        with patch.object(self.api, "request", side_effect=changing_request):
            with self.assertRaisesRegex(ZoteroError, "来源已变化"):
                self.preview()
        self.assertEqual(self.lib.topics(), [])

    def test_job_http_response_uses_contract_envelope(self):
        job = self.submit(self.preview(), selections=[])
        http = LocalServer(("127.0.0.1", 0), Handler)
        http.lib = self.lib
        worker = threading.Thread(target=http.serve_forever, daemon=True)
        worker.start()
        try:
            with build_opener(ProxyHandler({})).open(f"http://127.0.0.1:{http.server_port}/api/zotero/topic-job?id={job['id']}") as response:
                self.assertEqual(json.load(response)["job"]["topic_id"], job["topic_id"])
        finally:
            http.shutdown()
            http.server_close()


if __name__ == "__main__":
    unittest.main()
