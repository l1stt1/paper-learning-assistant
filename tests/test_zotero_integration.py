"""Keep Zotero linking, source identity and additive guide writes isolated and explicit."""
from copy import deepcopy
from http.client import IncompleteRead, RemoteDisconnected
import json
from pathlib import Path
import sys
import threading
import unittest
from legacy_fixtures import seed_note
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from urllib.parse import urlsplit
import uuid

import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import Handler, Library, LocalServer
from zotero_integration import LocalZotero, ZoteroError, attachment_title


def make_pdf(path, label, count=3):
    with fitz.open() as pdf:
        for index in range(count):
            pdf.new_page().insert_text((72, 72), f"{label} page {index + 1}. Distinct source evidence.")
        pdf.save(path)


class FakeZotero:
    server_id = "isolated-zotero-library"

    def __init__(self, root):
        self.paths = {}
        self.items = {}
        self.writes = []
        self.fail_after_write = False
        for key, doi, label in (("PAPERAAA", "10.1234/same", "Supplement source"), ("PAPERBBB", "10.1234/new", "New paper")):
            attachment = "ATTACAAA" if key == "PAPERAAA" else "ATTACBBB"
            path = root / (attachment + ".pdf")
            make_pdf(path, label)
            self.paths[attachment] = path
            self.items[key] = {"key": key, "itemType": "journalArticle", "title": label, "DOI": doi, "collections": ["COLLECAA"], "tags": []}
            self.items[attachment] = {"key": attachment, "itemType": "attachment", "parentItem": key, "title": label, "filename": path.name, "contentType": "application/pdf"}
        self.items["NOTESAAA"] = {"key": "NOTESAAA", "itemType": "note", "parentItem": "PAPERAAA", "note": "<h2>Private Zotero note</h2><p>Do not send automatically</p>"}
        self.items["ANNOTEAA"] = {"key": "ANNOTEAA", "itemType": "annotation", "parentItem": "ATTACAAA", "annotationPosition": json.dumps({"pageIndex": 1}), "annotationPageLabel": "S8", "annotationText": "Selected evidence", "annotationComment": "My question"}

    def get(self, route):
        if route == "collections":
            return [{"data": {"key": "COLLECAA", "name": "Papers", "parentCollection": False}}]
        if route == "items/top" or route == "collections/COLLECAA/items/top":
            return [{"key": value["key"], "data": deepcopy(value)} for value in self.items.values() if not value.get("parentItem")]
        if route.startswith("items/") and route.endswith("/children"):
            parent = route.split("/")[1]
            return [{"key": value["key"], "data": deepcopy(value)} for value in self.items.values() if value.get("parentItem") == parent]
        raise AssertionError("Unexpected fake GET: " + route)

    def get_all(self, route):
        return self.get(urlsplit(route).path)

    def get_item(self, key):
        return deepcopy(self.items[key])

    def local_file(self, key):
        return self.paths[key]

    def create_note(self, note, authorized):
        authorized()
        self.writes.append(deepcopy(note))
        self.items[note["key"]] = deepcopy(note)
        if self.fail_after_write:
            self.fail_after_write = False
            raise ZoteroError("模拟已保存但返回丢失")


class ZoteroIntegrationTests(unittest.TestCase):
    def setUp(self):
        # Deliberately retain generated fixtures; no cleanup of existing or test-owned paths.
        self.root = ROOT / "work" / "validation" / ("zotero-" + uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        make_pdf(self.root / "reading.pdf", "Original reading version")
        content = {"page_convention": "阅读版PDF页码", "overview": {"question": "A scientific question", "findings": ["One finding"], "limits": []},
                   "sections": [{"id": "one", "title": "First section", "pages": [1], "summary": "Curated finding", "questions": []}], "concepts": [], "comparison": {}}
        (self.root / "learning.json").write_text(json.dumps(content), encoding="utf-8")
        self.article = {"id": "existing-paper", "title": "Existing paper", "short_title": "Existing", "doi": "10.1234/same", "pages": 3, "pdf": "reading.pdf", "content": "learning.json"}
        (self.root / "articles.json").write_text(json.dumps({"library_id": "zotero-isolated-test", "articles": [self.article]}), encoding="utf-8")
        self.lib = Library(self.root)
        self.addCleanup(lambda: self.lib.close())
        self.api = FakeZotero(self.root)
        self.lib.zotero_client_class = lambda: self.api

    def link(self):
        preview = self.lib.zotero_import_preview({"item_key": "PAPERAAA", "attachment_key": "ATTACAAA"})
        return self.lib.zotero_import({"preview_id": preview["preview_id"]})

    def export_sync(self, preview=None):
        preview = preview or self.lib.zotero_guide_preview("existing-paper")
        job = {"id": uuid.uuid4().hex, "status": "waiting_authorization"}
        self.lib.zotero_export_lock.acquire()
        self.lib.zotero_export_guide("existing-paper", preview, job)
        return job

    def test_same_doi_links_without_replacing_reader_or_learning(self):
        pdf_before = (self.root / "reading.pdf").read_bytes()
        manifest_before = (self.root / "articles.json").read_bytes()
        seed_note(self.lib, "existing-paper", "Original user note")
        question = self.lib.capture({"article_id": "existing-paper", "question": "Original question"})
        result = self.link()
        self.assertTrue(result["linked_existing"])
        self.assertEqual(result["article"], self.article)
        self.assertEqual((self.root / "reading.pdf").read_bytes(), pdf_before)
        self.assertEqual((self.root / "articles.json").read_bytes(), manifest_before)
        self.assertFalse(self.lib.zotero_links["articles"]["existing-paper"]["reader_matches_attachment"])
        self.assertEqual(self.lib.state("existing-paper")["note"], "Original user note")
        self.assertEqual(self.lib.question("existing-paper", question["id"])["question"], "Original question")
        self.lib.close()
        reopened = Library(self.root)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.zotero_links, self.lib.zotero_links)
        self.assertEqual(self.api.writes, [])

    def test_new_article_import_is_snapshot_and_idempotent(self):
        preview = self.lib.zotero_import_preview({"item_key": "PAPERBBB", "attachment_key": "ATTACBBB"})
        self.assertIsNone(preview["existing_article_id"])
        result = self.lib.zotero_import({"preview_id": preview["preview_id"]})
        aid = result["article"]["id"]
        self.assertEqual(self.lib.zotero_import({"preview_id": preview["preview_id"]})["article"]["id"], aid)
        self.assertEqual(len(self.lib.articles), 2)
        self.assertTrue(self.lib.zotero_links["articles"][aid]["reader_matches_attachment"])
        self.assertEqual((self.root / result["article"]["pdf"]).read_bytes(), self.api.paths["ATTACBBB"].read_bytes())
        self.assertEqual(self.lib.content[aid]["sections"], [])

    def test_preview_file_change_stops_before_import(self):
        preview = self.lib.zotero_import_preview({"item_key": "PAPERBBB", "attachment_key": "ATTACBBB"})
        (self.root / "replacement.pdf").write_bytes(self.api.paths["ATTACAAA"].read_bytes())
        self.api.paths["ATTACBBB"] = self.root / "replacement.pdf"
        with self.assertRaisesRegex(ZoteroError, "变化"):
            self.lib.zotero_import({"preview_id": preview["preview_id"]})
        self.assertEqual(len(self.lib.articles), 1)
        self.assertEqual(self.lib.zotero_links["articles"], {})

    def test_import_preview_separates_catalog_metadata_and_pdf_identity(self):
        source = self.root / "different-identity.pdf"
        with fitz.open() as pdf:
            pdf.new_page().insert_text((72, 72), "DOI:10.5555/pdf-identity")
            pdf.set_metadata({"title": "Embedded PDF title", "creationDate": "D:20000101000000", "subject": "Not a catalog abstract"})
            pdf.save(source)
        self.api.paths["ATTACAAA"] = source
        self.api.items["PAPERAAA"].update(publicationTitle="Example Journal", date="2024-06",
            abstractNote="<p>RNA-\nseq &amp; TE<sup>2</sup>.</p><p>Splic\u00ad\ning evidence.</p>")
        preview = self.lib.zotero_import_preview({"item_key": "PAPERAAA", "attachment_key": "ATTACAAA"})
        self.assertEqual(preview["metadata"], {"source": "Zotero 书目", "journal": "Example Journal", "date": "2024-06", "abstract": "RNA-seq & TE^(2). Splicing evidence."})
        self.assertEqual(preview["title"], "Supplement source")
        self.assertEqual(preview["pdf_identity"], {"title": "Embedded PDF title", "title_source": "PDF 内嵌标题", "doi": "10.5555/pdf-identity"})
        self.assertEqual(preview["doi"], "10.1234/same")
        self.assertEqual(preview["existing_article_id"], "existing-paper")
        self.assertEqual(preview["identity_warnings"][0]["pdf_value"], "10.5555/pdf-identity")
        self.assertEqual(preview["identity_warnings"][0]["catalog_value"], "10.1234/same")
        self.assertNotIn("first_page_excerpt", preview)
        self.assertEqual(preview["filename"], source.name)
        missing = self.lib.zotero_import_preview({"item_key": "PAPERBBB", "attachment_key": "ATTACBBB"})
        self.assertEqual(missing["metadata"], {"source": "Zotero 书目", "journal": "", "date": "", "abstract": ""})
        self.assertEqual(missing["identity_warnings"], [])
        self.assertEqual(self.api.writes, [])

    def test_attachment_identity_physical_page_and_source_only_prompt(self):
        self.link()
        details = self.lib.zotero_article("existing-paper")
        annotation = details["annotations"][0]
        self.assertEqual(annotation["page"], 2)
        self.assertEqual(annotation["page_label"], "S8")
        with self.assertRaisesRegex(ZoteroError, "不属于"):
            self.lib.zotero_file("existing-paper", "ATTACBBB")
        with self.assertRaisesRegex(ZoteroError, "页码"):
            self.lib.capture({"article_id": "existing-paper", "question": "Outside source", "zotero_source": {"attachment_key": "ATTACAAA", "page": 30}})
        q = self.lib.capture({"article_id": "existing-paper", "question": "Explain this annotation", "page": 1,
                              "zotero_source": {"attachment_key": "ATTACAAA", "page": 2}})
        prompt = self.lib.prompt("existing-paper", q)
        payload = json.loads(prompt.split("<reading_data>\n")[1].split("\n</reading_data>")[0])
        self.assertEqual(payload["current_sections"], [])
        self.assertTrue(all("Supplement source" in row["text"] for row in payload["source_pages"]))
        self.assertNotIn("Original reading version", prompt)
        self.assertNotIn("Do not send automatically", prompt)
        self.assertIn("Supplement source · PDF第2页", self.lib.export("existing-paper"))
        self.api.paths["ATTACAAA"] = self.api.paths["ATTACBBB"]
        with self.assertRaisesRegex(ZoteroError, "已改变"):
            self.lib.prompt("existing-paper", q)
        with self.assertRaisesRegex(ZoteroError, "已改变"):
            self.lib.capture({"article_id": "existing-paper", "question": "Follow up after a source change", "parent_id": q["id"]})

    def test_source_followups_and_concepts_do_not_claim_reader_page(self):
        self.link()
        parent = self.lib.capture({"article_id": "existing-paper", "question": "First", "quote": "Selected evidence", "zotero_source": {"attachment_key": "ATTACAAA", "page": 2}})
        child = self.lib.capture({"article_id": "existing-paper", "question": "Why?", "parent_id": parent["id"], "capture_concept": True, "term": "Source concept"})
        self.assertEqual(child["zotero_source"]["page"], 2)
        concept = next(c for c in self.lib.contents("existing-paper")["concepts"] if c["id"] == child["concept_id"])
        self.assertEqual(concept["pages"], [])
        self.assertEqual(concept["zotero_source"]["attachment_key"], "ATTACAAA")
        native = self.lib.capture({"article_id": "existing-paper", "question": "Back to reading", "parent_id": parent["id"], "zotero_source": None})
        self.assertNotIn("zotero_source", native)

    def test_guide_adds_one_child_note_and_recovers_lost_response(self):
        self.link()
        original_note = deepcopy(self.api.items["NOTESAAA"])
        seed_note(self.lib, "existing-paper", "Private assistant note")
        self.lib.content["existing-paper"].update(
            comparison_notes=[{"topic": "Comparison topic", "type": "Interpretive comparison only", "note": "Keep the distinct analysis units", "pages": [2]}],
            source_coverage={"included_assets": ["Main figures and methods"], "missing_assets": ["Independent Table S1 is missing"], "reading_notes": ["Physical PDF pages, not printed page labels"]},
            numeric_audit=[{"note": "Internal numeric audit is not a reading note"}],
        )
        preview = self.lib.zotero_guide_preview("existing-paper")
        self.assertTrue(preview["can_export"])
        self.assertNotIn("Private assistant note", preview["note_html"])
        self.assertIn("不能直接套用", preview["note_html"])
        for text in ("Comparison topic", "Interpretive comparison only", "Keep the distinct analysis units", "Main figures and methods", "Independent Table S1 is missing", "Physical PDF pages, not printed page labels"):
            self.assertIn(text, preview["note_html"])
        self.assertIn("阅读版PDF页码：</strong> 2", preview["note_html"])
        self.assertNotIn("Internal numeric audit", preview["note_html"])
        self.api.fail_after_write = True
        self.assertEqual(self.export_sync(preview)["status"], "failed")
        done = self.export_sync(preview)
        self.assertEqual(done["status"], "completed")
        self.assertEqual(len(self.api.writes), 1)
        self.assertEqual(self.api.items["NOTESAAA"], original_note)
        self.assertEqual(self.api.writes[0]["parentItem"], "PAPERAAA")
        self.assertTrue(self.lib.zotero_guide_preview("existing-paper")["already_exported"])
        self.assertEqual(self.export_sync(preview)["status"], "completed")
        self.assertEqual(len(self.api.writes), 1)

    def test_guide_escapes_html_and_preserves_user_edits(self):
        self.link()
        self.lib.content["existing-paper"]["sections"][0]["summary"] = "<script>untrusted()</script>"
        preview = self.lib.zotero_guide_preview("existing-paper")
        self.assertNotIn("<script>", preview["note_html"])
        self.assertIn("&lt;script&gt;", preview["note_html"])
        done = self.export_sync(preview)
        self.api.items[done["note_key"]]["note"] += "<p>User changed the note</p>"
        failed = self.export_sync(preview)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(len(self.api.writes), 1)
        self.assertIn("User changed", self.api.items[done["note_key"]]["note"])

    def test_guide_exports_new_coverage_and_review_without_changing_learning(self):
        self.link()
        self.lib.content["existing-paper"].update(
            source_coverage={"main_text": ["Main <article>"], "figures": ["Figures & captions"],
                             "supplementary": ["Supplement <S1>"], "missing": ["Missing <S2>"],
                             "notes": ["Read <script>source</script>"]},
            study_review={"status": "independent_source_reviewed", "label": "Checked <source>",
                          "scope": "Methods & results", "limitations": "No <reanalysis>",
                          "checked_at": "test-only internal timestamp"},
        )
        content_before = deepcopy(self.lib.content["existing-paper"])
        learning_before = (self.root / "learning.json").read_bytes()
        preview = self.lib.zotero_guide_preview("existing-paper")
        for text in ("资料范围与阅读说明", "正文", "图表", "补充材料", "缺件与尚未核查部分", "阅读说明",
                     "Main &lt;article&gt;", "Figures &amp; captions", "Supplement &lt;S1&gt;", "Missing &lt;S2&gt;",
                     "Read &lt;script&gt;source&lt;/script&gt;", "核查说明", "Checked &lt;source&gt;",
                     "Methods &amp; results", "No &lt;reanalysis&gt;"):
            self.assertIn(text, preview["note_html"])
        self.assertNotIn("<script>", preview["note_html"])
        self.assertNotIn("independent_source_reviewed", preview["note_html"])
        self.assertNotIn("test-only internal timestamp", preview["note_html"])
        self.assertEqual(self.export_sync(preview)["status"], "completed")
        self.assertEqual(self.export_sync()["status"], "completed")
        self.assertEqual(len(self.api.writes), 1)
        self.assertEqual(self.api.writes[0]["note"], preview["note_html"])
        self.assertEqual(self.lib.content["existing-paper"], content_before)
        self.assertEqual((self.root / "learning.json").read_bytes(), learning_before)

    def test_guide_review_change_adds_snapshot_and_preserves_edited_note(self):
        self.link()
        self.lib.content["existing-paper"]["study_review"] = {"label": "Source checked"}
        first = self.export_sync()
        self.assertEqual(first["status"], "completed")
        edited = self.api.items[first["note_key"]]["note"] + "<p>User clarification</p>"
        self.api.items[first["note_key"]]["note"] = edited
        self.lib.content["existing-paper"]["study_review"]["scope"] = "Main figures checked"
        second = self.export_sync()
        self.assertEqual(second["status"], "completed")
        self.assertNotEqual(first["note_key"], second["note_key"])
        self.assertEqual(self.api.items[first["note_key"]]["note"], edited)
        self.assertIn("Main figures checked", self.api.items[second["note_key"]]["note"])
        self.assertEqual(len(self.api.writes), 2)

    def test_guide_links_only_identical_reader_and_rechecks_after_authorization(self):
        self.api.paths["ATTACAAA"] = self.root / "reading.pdf"
        self.link()
        preview = self.lib.zotero_guide_preview("existing-paper")
        self.assertIn('href="zotero://open-pdf/library/items/ATTACAAA?page=1"', preview["note_html"])
        self.assertEqual(preview["source_attachment_key"], "ATTACAAA")
        original_create = self.api.create_note
        def replace_before_authorizing(note, authorized):
            self.api.paths["ATTACAAA"] = self.api.paths["ATTACBBB"]
            original_create(note, authorized)
        self.api.create_note = replace_before_authorizing
        job = self.export_sync(preview)
        self.assertEqual(job["status"], "failed")
        self.assertIn("已变化", job["message"])
        self.assertEqual(self.api.writes, [])
        changed = self.lib.zotero_guide_preview("existing-paper")
        self.assertNotIn("zotero://open-pdf", changed["note_html"])
        self.assertIn("不能直接套用", changed["note_html"])

    def test_failed_thread_start_does_not_leave_waiting_job(self):
        self.link()
        with patch("zotero_integration.threading.Thread.start", side_effect=RuntimeError("injected start failure")):
            with self.assertRaises(RuntimeError):
                self.lib.zotero_start_guide_export("existing-paper")
        self.assertFalse(self.lib.zotero_export_lock.locked())
        self.assertEqual({job["status"] for job in self.lib.zotero_export_jobs.values()}, {"failed"})

    def test_attachment_titles_preserve_meaning_without_paperlib_suffix(self):
        self.assertEqual(attachment_title({"title": "Supplementary methods"}, "paper.pdf"), "Supplementary methods")
        self.assertEqual(attachment_title({"title": "PDF"}, "A_Paper_0123456789abcdef01234567_main.pdf"), "A Paper")
        self.assertEqual(attachment_title({"title": "PDF"}, ""), "PDF附件")

    def test_changed_library_cannot_reuse_links(self):
        self.link()
        self.api.server_id = "different-database"
        self.assertFalse(self.lib.zotero_status()["available"])
        with self.assertRaisesRegex(ZoteroError, "文库"):
            self.lib.zotero_article("existing-paper")

    def test_http_reads_files_and_rejects_unapproved_write(self):
        self.link()
        http = LocalServer(("127.0.0.1", 0), Handler)
        http.lib = self.lib
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{http.server_port}"
        opener = build_opener(ProxyHandler({}))
        try:
            with opener.open(url + "/api/zotero/article?id=existing-paper") as response:
                self.assertTrue(json.load(response)["linked"])
            with opener.open(url + "/api/zotero/file?id=existing-paper&attachment=ATTACAAA") as response:
                self.assertIn("frame-ancestors 'self'", response.headers["Content-Security-Policy"])
                self.assertTrue(response.read().startswith(b"%PDF-"))
            with self.assertRaises(HTTPError) as caught:
                opener.open(Request(url + "/api/zotero/guide-export", data=b'{"article_id":"existing-paper"}', headers={"Content-Type": "application/json"}))
            self.assertEqual(caught.exception.code, 403)
            self.assertEqual(self.api.writes, [])
            with patch.object(self.lib, "zotero_client_class", side_effect=ZoteroError("Zotero未打开")):
                with opener.open(url + "/api/zotero/status") as response:
                    self.assertFalse(json.load(response)["available"])
        finally:
            http.shutdown()
            http.server_close()

    def test_authorization_reuse_requires_remember(self):
        # The opaque marker is not a credential and never reaches a real network request.
        marker = object()
        for remember, expected_authorizations in ((True, 1), (False, 2)):
            with self.subTest(remember=remember), patch.object(LocalZotero, "_authorization", None):
                api = object.__new__(LocalZotero)
                api.server_id = "test-only"
                seen = []
                def request(method, route, body=None, key=None, timeout=15):
                    seen.append(route)
                    if route == "local/authorize":
                        return {"key": marker, "remember": remember}, {}
                    self.assertIs(key, marker)
                    self.assertEqual(route, "users/0/items")
                    return {"successful": {"0": {}}}, {}
                api.request = request
                for _ in range(2):
                    api.create_note({"key": "TESTNOTE", "itemType": "note"}, lambda: None)
                self.assertEqual(seen.count("local/authorize"), expected_authorizations)
                self.assertEqual(seen.count("users/0/items"), 2)
                self.assertFalse(any(value is marker for value in vars(api).values()))
                self.assertEqual(LocalZotero._authorization, ("test-only", marker) if remember else None)

    def test_authorization_401_clears_cache_without_replay(self):
        marker = object()
        api = object.__new__(LocalZotero)
        api.server_id = "test-only"
        class RejectingOpener:
            calls = 0
            def open(self, *args, **kwargs):
                self.calls += 1
                raise HTTPError(LocalZotero.base, 401, "Unauthorized", {}, None)
        api.opener = RejectingOpener()
        with patch.object(LocalZotero, "_authorization", (api.server_id, marker)):
            with self.assertRaisesRegex(ZoteroError, "授权失效"):
                api.create_note({"key": "TESTNOTE", "itemType": "note"}, lambda: None)
            self.assertEqual(api.opener.calls, 1)
            self.assertIsNone(LocalZotero._authorization)

    def test_authorization_clears_when_database_changes(self):
        marker = object()
        with patch.object(LocalZotero, "_authorization", ("first-library", marker)), patch.object(LocalZotero, "request", return_value=(b"", {"Zotero-Server-ID": "second-library"})):
            api = LocalZotero()
            self.assertEqual(api.server_id, "second-library")
            self.assertIsNone(LocalZotero._authorization)

    def test_interrupted_connection_becomes_readable_status_error(self):
        api = object.__new__(LocalZotero)
        api.server_id = None
        class BrokenResponse:
            headers = {"Content-Type": "application/json"}
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
            def read(self):
                raise IncompleteRead(b"{", 20)
        class BrokenOpener:
            def open(self, *args, **kwargs):
                return BrokenResponse()
        api.opener = BrokenOpener()
        with self.assertRaisesRegex(ZoteroError, "无法连接"):
            api.request("GET", "")
        with patch.object(api.opener, "open", side_effect=RemoteDisconnected("response ended")):
            with self.assertRaisesRegex(ZoteroError, "无法连接"):
                api.request("GET", "")


if __name__ == "__main__":
    unittest.main()
