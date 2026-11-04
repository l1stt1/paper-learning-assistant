"""Protect import identity, per-paper state, and source-linked comparison records."""
import json
import sys
import threading
import unittest
from legacy_fixtures import seed_note
import urllib.error
import urllib.request
import uuid
from unittest.mock import patch
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import Handler, Library, LocalServer


def pdf_bytes(title):
    with fitz.open() as pdf:
        for page in range(3):
            pdf.new_page().insert_text((72, 72), f"{title}. Page {page + 1}. Splicing evidence requires separate validation.")
        pdf.set_metadata({"title": title})
        return pdf.tobytes()


class LibraryFeatureTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / "work" / "validation" / ("library-" + uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        articles = []
        for aid, doi in [("paper-a", "10.1234/a"), ("paper-b", "10.1234/b")]:
            (self.root / (aid + ".pdf")).write_bytes(pdf_bytes(aid))
            content = {"overview": {}, "sections": [], "concepts": [], "comparison": {"definition": {"text": "不同结构定义", "pages": [2]}}}
            (self.root / (aid + ".json")).write_text(json.dumps(content), encoding="utf-8")
            articles.append({"id": aid, "title": aid, "short_title": aid, "doi": doi, "pages": 3, "pdf": aid + ".pdf", "content": aid + ".json"})
        (self.root / "articles.json").write_text(json.dumps({"library_id": "isolated-feature-test", "articles": articles}), encoding="utf-8")
        self.lib = Library(self.root)
        self.addCleanup(lambda: self.lib.close())

    def test_import_preview_commit_duplicate_and_restart(self):
        seed_note(self.lib, "paper-a", "保留既有笔记")
        original = self.lib.capture({"article_id": "paper-a", "question": "原问题", "page": 1})
        upload = pdf_bytes("A new uploaded paper")
        preview = self.lib.preview_import(upload, "a-new-paper.pdf")
        self.assertEqual(len(self.lib.articles), 2)
        self.assertEqual(preview["duplicates"], [])
        self.assertTrue(preview["text_available"])
        request = {"preview_id": preview["preview_id"], "title": "新文章", "doi": "https://doi.org/10.1234/new"}
        article = self.lib.commit_import(request)
        self.assertEqual(self.lib.commit_import(request)["id"], article["id"])
        self.assertEqual(len(self.lib.articles), 3)
        self.assertEqual((self.root / article["pdf"]).read_bytes(), upload)
        self.assertEqual(self.lib.state(article["id"])["questions"], [])

        self.assertEqual(self.lib.state(article["id"])["note"], "")
        self.assertEqual(self.lib.state("paper-a")["note"], "保留既有笔记")
        self.assertEqual(self.lib.question("paper-a", original["id"])["question"], "原问题")
        self.assertEqual(self.lib.preview_import(upload)["duplicates"][0]["id"], article["id"])
        other = self.lib.preview_import(pdf_bytes("Different bytes"))
        with self.assertRaisesRegex(ValueError, "已在资料库"):
            self.lib.commit_import({"preview_id": other["preview_id"], "title": "同DOI", "doi": "10.1234/new"})
        self.lib.close()
        reopened = Library(self.root)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.state(article["id"])["note"], "")
        self.assertEqual(reopened.state("paper-a")["note"], "保留既有笔记")

    def test_import_recovers_after_source_publication_failure(self):
        preview = self.lib.preview_import(pdf_bytes("Interrupted initial write"))
        request = {"preview_id": preview["preview_id"], "title": "再次导入"}
        with patch("library_features.os.rename", side_effect=OSError("temporary publication failure")):
            with self.assertRaises(OSError):
                self.lib.commit_import(request)
        self.assertEqual(len(self.lib.articles), 2)
        article = self.lib.commit_import(request)
        self.assertEqual(self.lib.state(article["id"])["questions"], [])

    def test_pdf_preview_keeps_identity_without_inventing_publication_metadata(self):
        with fitz.open(stream=pdf_bytes("Embedded identity"), filetype="pdf") as pdf:
            pdf.set_metadata({"title": "Embedded identity", "subject": "This subject is not a reliable abstract",
                              "creationDate": "D:20000102030405", "modDate": "D:20251231000000"})
            data = pdf.tobytes()
            pdf.set_metadata({"title": ""})
            without_title = pdf.tobytes()
        preview = self.lib.preview_import(data, "some/folder/article.pdf")
        self.assertNotIn("first_page_excerpt", preview)
        self.assertEqual(preview["filename"], "article.pdf")
        self.assertEqual(preview["metadata"], {"source": "PDF 内嵌信息", "journal": "", "date": "", "abstract": ""})
        self.assertEqual(preview["pdf_identity"], {"title": "Embedded identity", "doi": "", "title_source": "PDF 内嵌标题"})
        self.assertEqual(preview["identity_warnings"], [])
        fallback = self.lib.preview_import(without_title, "fallback.pdf")
        self.assertEqual(fallback["pdf_identity"]["title"], "fallback")
        self.assertEqual(fallback["pdf_identity"]["title_source"], "文件名")

    def test_invalid_pdf_and_preview_expiry_do_not_register(self):
        for data in (b"<html>not a pdf</html>", b"%PDF-broken"):
            with self.assertRaises(ValueError):
                self.lib.preview_import(data)
        preview = self.lib.preview_import(pdf_bytes("Expired file"))
        self.lib.import_previews[preview["preview_id"]]["time"] -= 1801
        with self.assertRaisesRegex(ValueError, "过期"):
            self.lib.commit_import({"preview_id": preview["preview_id"], "title": "过期"})
        self.assertEqual(len(self.lib.articles), 2)

    def test_import_recovers_after_manifest_write_failure(self):
        preview = self.lib.preview_import(pdf_bytes("Recoverable upload"))
        request = {"preview_id": preview["preview_id"], "title": "恢复导入"}
        with patch("library_features.os.replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.lib.commit_import(request)
        self.assertEqual(len(self.lib.articles), 2)
        article = self.lib.commit_import(request)
        self.assertEqual(len(self.lib.articles), 3)
        self.assertEqual(self.lib.state(article["id"])["questions"], [])

    def test_comparison_save_failure_releases_ai_lock(self):
        record = self.lib.capture_comparison({"article_ids": ["paper-a", "paper-b"], "question": "保存失败验证"})
        self.lib.ai_lock.acquire()
        with patch.object(self.lib, "comparison_sources", side_effect=ValueError("injected test failure")), patch.object(self.lib, "save_comparison", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.lib.explain_comparison(record)
        self.assertFalse(self.lib.ai_lock.locked())
        self.lib.codex = "test-only-cli"
        with patch.object(self.lib, "save_comparison", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.lib.start_comparison(record["id"])
        self.assertFalse(self.lib.ai_lock.locked())

    def test_relations_comparisons_isolated_persistent_and_exported(self):
        for ids in (["paper-a"], ["paper-a", "paper-a"], ["paper-a", "missing"]):
            with self.assertRaises(ValueError):
                self.lib.compare(ids)
        relation = self.lib.add_relation({"source": "paper-a", "target": "paper-b", "title": "我的联想", "explanation": "可能共享来源，需要验证", "source_page": 1, "target_page": 2})
        self.assertEqual(relation["origin"], "personal")
        self.assertEqual(relation["type"], "hypothesis")
        record = self.lib.capture_comparison({"article_ids": ["paper-a", "paper-b"], "question": "如何区别定义？", "generate": False})
        self.assertEqual(record["status"], "saved")
        self.assertEqual(self.lib.state("paper-a")["questions"], [])
        self.assertIn("我的联想", self.lib.export_network())
        self.assertIn("如何区别定义", self.lib.export_network())
        self.lib.close()
        reopened = Library(self.root)
        self.addCleanup(reopened.close)
        self.assertEqual(len(reopened.relations()), 1)
        self.assertEqual(reopened.comparison(record["id"])["question"], record["question"])

    def test_final_save_failure_is_visible_and_can_be_retried(self):
        self.lib.codex = "test-only-cli"
        record = self.lib.capture_comparison({"article_ids": ["paper-a", "paper-b"], "question": "比较保存失败"})
        record["status"] = "running"
        self.lib.save_comparison(record)
        self.lib.active_ai = ("comparison", record["id"])
        self.lib.ai_lock.acquire()
        self.assertEqual(self.lib.comparisons()[0]["status"], "running")
        with patch.object(self.lib, "comparison_sources", side_effect=ValueError("test-only failure")), patch.object(self.lib, "save_comparison", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.lib.explain_comparison(record)
        self.assertFalse(self.lib.ai_lock.locked())
        self.assertEqual(self.lib.comparisons()[0]["status"], "failed")
        with patch("library_features.threading.Thread") as worker:
            self.assertEqual(self.lib.start_comparison(record["id"])["status"], "running")
            self.lib.start_comparison(record["id"])
            worker.return_value.start.assert_called_once()
        self.lib.active_ai = None
        self.lib.ai_lock.release()

        q = self.lib.capture({"article_id": "paper-a", "question": "单篇保存失败", "page": 1})
        q["status"] = "running"
        self.lib.save_question("paper-a", q)
        self.lib.active_ai = ("question", "paper-a", q["id"])
        self.lib.ai_lock.acquire()
        self.assertEqual(self.lib.state("paper-a")["questions"][0]["status"], "running")
        with patch.object(self.lib, "prompt", side_effect=ValueError("test-only failure")), patch.object(self.lib, "save_question", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.lib.explain("paper-a", q)
        self.assertFalse(self.lib.ai_lock.locked())
        self.assertEqual(self.lib.state("paper-a")["questions"][0]["status"], "failed")
        with patch("server.threading.Thread") as worker:
            self.assertEqual(self.lib.start_explanation("paper-a", q["id"])["status"], "running")
            self.lib.start_explanation("paper-a", q["id"])
            worker.return_value.start.assert_called_once()
        self.lib.active_ai = None
        self.lib.ai_lock.release()

    def test_citations_must_match_supplied_original_pages(self):
        record = self.lib.capture_comparison({"article_ids": ["paper-a", "paper-b"], "question": "Splicing"})
        sources = self.lib.comparison_sources(record)
        answer = {key: "保留解释边界" for key in ("summary", "agreements", "differences", "boundaries", "hypotheses")}
        answer["citations"] = [{"article_id": aid, "page": 1, "quote": "Splicing evidence requires separate validation."} for aid in record["article_ids"]]
        self.lib.validate_comparison_answer(answer, record, sources)
        answer["citations"][0]["quote"] = "An invented claim with no support on the source page."
        with self.assertRaisesRegex(ValueError, "无法在"):
            self.lib.validate_comparison_answer(answer, record, sources)
        prompt = self.lib.comparison_prompt(record, sources)
        seed_note(self.lib, "paper-a", "PRIVATE_NOTE_NOT_FOR_COMPARISON")
        self.assertNotIn("PRIVATE_NOTE_NOT_FOR_COMPARISON", self.lib.comparison_prompt(record, sources))
        self.assertIn("source_pages", prompt)

    def test_ai_busy_and_restart_do_not_replay(self):
        self.lib.codex = "test-only-cli"
        record = self.lib.capture_comparison({"article_ids": ["paper-a", "paper-b"], "question": "比较"})
        self.lib.ai_lock.acquire()
        try:
            self.assertEqual(self.lib.start_comparison(record["id"])["status"], "saved")
        finally:
            self.lib.ai_lock.release()
        record["status"] = "running"
        self.lib.save_comparison(record)
        self.lib.close()
        reopened = Library(self.root)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.comparison(record["id"])["status"], "failed")

    def test_http_import_auth_and_asset_allowlist(self):
        http = LocalServer(("127.0.0.1", 0), Handler)
        http.lib = self.lib
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        url = "http://127.0.0.1:" + str(http.server_port)
        try:
            request = urllib.request.Request(url + "/api/import/preview", data=pdf_bytes("HTTP upload"), headers={"Content-Type": "application/pdf"})
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(request)
            self.assertEqual(ctx.exception.code, 403)
            request.add_header("X-Learning-Token", self.lib.token)
            preview = json.loads(urllib.request.urlopen(request).read())
            self.assertEqual(preview["pages"], 3)
            self.assertEqual(len(self.lib.articles), 2)
            for suffix in ("&asset=../../articles.json", "&asset=unknown"):
                with self.assertRaises(urllib.error.HTTPError):
                    urllib.request.urlopen(url + "/api/asset?id=paper-a" + suffix)
            asset = urllib.request.urlopen(url + "/api/asset?id=paper-a&asset=reading").read()
            self.assertEqual(asset, (self.root / "paper-a.pdf").read_bytes())
        finally:
            http.shutdown()
            http.server_close()


if __name__ == "__main__":
    unittest.main()
