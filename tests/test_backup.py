"""Offline backup/restore checks using synthetic papers and private-record fixtures."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid
import warnings
import zipfile

import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import backup
from server import Library


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def pdf(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), text)
        document.save(path)


class BackupTests(unittest.TestCase):
    def setUp(self):
        # Keep task-generated fixtures for inspection; no automatic file deletion.
        base = Path(os.environ.get("PLA_TEST_ARTIFACTS", tempfile.gettempdir()))
        self.folder = base / ("pla-backup-" + uuid.uuid4().hex)
        self.root = self.folder / "profile"
        self.root.mkdir(parents=True)
        self.external = self.folder / "external"
        self.external.mkdir()
        self.archive = self.folder / "backup.zip"
        self.restored = self.folder / "restored"
        external_pdf = self.external / "source.pdf"
        attachment = self.external / "supplement.pdf"
        pdf(external_pdf, "Synthetic external reading paper")
        pdf(attachment, "Synthetic external supplement")
        pdf(self.root / "articles/paper-b/source.pdf", "Synthetic internal paper")
        content = {"overview": {}, "sections": [], "concepts": [], "comparison": {}}
        write_json(self.root / "articles/paper-a/learning.json", content)
        write_json(self.root / "old-content.json", content)
        self.manifest = {"library_id": "synthetic-backup-library", "articles": [
            {"id": "paper-a", "title": "Paper A", "short_title": "A", "pages": 1,
             "pdf": str(external_pdf), "content": "articles/paper-a/learning.json",
             "assets": [{"id": "supplement", "label": "Supplement", "kind": "pdf", "pages": 1, "path": str(attachment)}]},
            {"id": "paper-b", "title": "Paper B", "short_title": "B", "pages": 1,
             "pdf": "articles/paper-b/source.pdf", "content": "old-content.json"}]}
        write_json(self.root / "articles.json", self.manifest)
        relation = {"id": "curated", "source": "paper-a", "target": "paper-b", "title": "Synthetic relation",
                    "evidence": [{"article_id": "paper-a", "page": 1}, {"article_id": "paper-b", "page": 1}]}
        write_json(self.root / "relations.json", {"relations": [relation]})
        library = Library(ROOT, data_root=self.root)
        try:
            with library.db("paper-a") as connection:
                connection.execute("INSERT INTO notes(created_at, body) VALUES(?, ?)", ("2026-01-01", "Synthetic personal note"))
            self.question = library.capture({"article_id": "paper-a", "question": "Synthetic question", "page": 1})
            self.topic = library.save_topic({"name": "Synthetic topic", "notes": "Topic note", "article_ids": ["paper-a", "paper-b"]})
            library.add_relation({"source": "paper-a", "target": "paper-b", "title": "Personal relation", "explanation": "Synthetic hypothesis", "source_page": 1, "target_page": 1})
            library.zotero_links = {"server_id": "synthetic-server", "articles": {"paper-a": {"item_key": "ABCD1234", "attachment_key": "EFGH5678"}}}
            library.save_zotero_links()
        finally:
            library.close()
        write_json(self.root / "learning/model-settings.json", {"backend": "codex", "key_env": "TEST_UNUSED_ENV_NAME"})
        write_json(self.root / "learning/study-runs/job/paper-a/answer.json", {"answer": "Synthetic study answer"})
        write_json(self.root / "learning/study-jobs.json", {"jobs": []})
        write_json(self.root / "work/explanations/question/answer.json", {"answer": "Synthetic answer"})
        write_json(self.root / "work/comparisons/comparison/answer.json", {"summary": "Synthetic cross-paper summary"})
        write_json(self.root / "work/import-writes/pending.json", {"synthetic": "pending import"})
        pdf(self.root / "work/validation/test.pdf", "Synthetic development-only paper")
        (self.root / "server.py").write_text("# excluded program", encoding="utf-8")
        (self.root / "learning/auth.json").write_text('{"placeholder":"not a credential"}', encoding="utf-8")
        (self.root / "learning/.env").write_text("SYNTHETIC_PLACEHOLDER=not-a-credential", encoding="utf-8")
        (self.root / "work/helper.py").write_text("# excluded program", encoding="utf-8")
        (self.root / "learning/cache").mkdir()
        (self.root / "learning/cache/unused.txt").write_text("cache", encoding="utf-8")

    def make_backup(self):
        return backup.create_backup(self.root, self.archive)

    def make_archive(self, entries, name="crafted.zip", article_count=0):
        """Build self-consistent archive indexes, allowing targeted hostile members."""
        destination = self.folder / name
        records = [{"path": (key.filename if isinstance(key, zipfile.ZipInfo) else key).removeprefix("profile/"),
                    "size": len(value), "sha256": hashlib.sha256(value).hexdigest()} for key, value in entries]
        metadata = {"format": backup.FORMAT, "version": backup.VERSION, "created_at": "2026-01-01T00:00:00Z",
                    "article_count": article_count, "file_count": len(records), "size": sum(row["size"] for row in records), "files": records}
        with warnings.catch_warnings(), zipfile.ZipFile(destination, "x") as archive:
            warnings.simplefilter("ignore", UserWarning)
            for key, value in entries:
                archive.writestr(key, value)
            archive.writestr(backup.MANIFEST, json.dumps(metadata))
        return destination

    @staticmethod
    def empty_articles():
        return json.dumps({"library_id": "synthetic-empty", "articles": []}).encode()

    def test_complete_backup_restores_loadable_profile_and_external_files(self):
        before_manifest = (self.root / "articles.json").read_bytes()
        before_pdf = (self.external / "source.pdf").read_bytes()
        summary = self.make_backup()
        self.assertEqual(summary["article_count"], 2)
        self.assertEqual(summary["archive"], str(self.archive.resolve()))
        self.assertNotIn("files", backup.inspect_backup(self.archive))
        with zipfile.ZipFile(self.archive) as archive:
            names = archive.namelist()
        for suffix in ("auth.json", "/.env", "helper.py", "/cache/unused.txt", ".profile.lock", ".service.json", "server.py", "/validation/test.pdf"):
            self.assertFalse(any(name.endswith(suffix) for name in names), suffix)
        result = backup.restore_backup(self.archive, self.restored)
        self.assertEqual(result["article_count"], 2)
        self.assertEqual((self.root / "articles.json").read_bytes(), before_manifest)
        self.assertEqual((self.external / "source.pdf").read_bytes(), before_pdf)
        restored_manifest = json.loads((self.restored / "articles.json").read_text(encoding="utf-8"))
        for article in restored_manifest["articles"]:
            for name in (article["pdf"], article["content"], *(asset["path"] for asset in article.get("assets", []))):
                self.assertFalse(Path(name).is_absolute())
                self.assertTrue((self.restored / name).is_file())
        self.assertEqual((self.restored / restored_manifest["articles"][0]["pdf"]).read_bytes(), before_pdf)
        library = Library(ROOT, data_root=self.restored)
        try:
            self.assertEqual(library.state("paper-a")["note"], "Synthetic personal note")
            self.assertEqual(library.question("paper-a", self.question["id"])["question"], "Synthetic question")
            self.assertEqual(library.topic(self.topic["id"])["notes"], "Topic note")
            self.assertEqual(len(library.relations()), 2)
            self.assertEqual(library.zotero_links["server_id"], "synthetic-server")
            self.assertEqual(library.model_backend._settings["key_env"], "TEST_UNUSED_ENV_NAME")
            self.assertFalse(library.model_backend.available())
            self.assertIn("来自备份", library.model_backend.status()["message"])
            self.assertEqual(len(library.pages["paper-b"]), 1)
        finally:
            library.close()
        for name in ("learning/study-runs/job/paper-a/answer.json", "work/explanations/question/answer.json", "work/comparisons/comparison/answer.json", "work/import-writes/pending.json"):
            self.assertEqual((self.root / name).read_bytes(), (self.restored / name).read_bytes())

    def test_manifest_reference_under_old_work_is_still_included(self):
        self.manifest["articles"][0]["assets"].append({"id": "explicit-source", "path": "work/validation/test.pdf", "kind": "pdf", "pages": 1})
        write_json(self.root / "articles.json", self.manifest)
        self.make_backup()
        backup.restore_backup(self.archive, self.restored)
        self.assertEqual((self.root / "work/validation/test.pdf").read_bytes(), (self.restored / "work/validation/test.pdf").read_bytes())

    def test_restoration_never_trusts_saved_review_marker(self):
        write_json(self.root / backup.MODEL_REVIEW_PATH, {"requires_review": False})
        self.make_backup()
        backup.restore_backup(self.archive, self.restored)
        self.assertEqual(json.loads((self.restored / backup.MODEL_REVIEW_PATH).read_text()), {"requires_review": True})
        self.assertEqual(json.loads((self.root / backup.MODEL_REVIEW_PATH).read_text()), {"requires_review": False})

    def test_running_library_is_rejected_without_archive(self):
        library = Library(ROOT, data_root=self.root)
        try:
            with self.assertRaisesRegex(backup.BackupError, "正在使用"):
                self.make_backup()
        finally:
            library.close()
        self.assertFalse(self.archive.exists())

    def test_existing_backup_and_profile_are_preserved(self):
        self.archive.write_bytes(b"existing archive")
        with self.assertRaisesRegex(backup.BackupError, "已存在"):
            self.make_backup()
        self.assertEqual(self.archive.read_bytes(), b"existing archive")
        other = self.folder / "new.zip"
        backup.create_backup(self.root, other)
        self.restored.mkdir()
        existing = self.restored / "personal-note.txt"
        existing.write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(backup.BackupError, "完全空目录"):
            backup.restore_backup(other, self.restored)
        self.assertEqual(existing.read_text(), "keep")
        self.assertEqual(len(list(self.restored.iterdir())), 1)

    def test_empty_target_is_allowed(self):
        self.make_backup()
        self.restored.mkdir()
        backup.restore_backup(self.archive, self.restored)
        self.assertTrue((self.restored / "articles.json").is_file())

    def test_restore_accepts_existing_parent_alias_and_uses_canonical_destination(self):
        self.make_backup()
        parent = self.folder / "canonical-parent"
        parent.mkdir()
        alias = self.folder / "parent-alias"
        try:
            alias.symlink_to(parent.resolve(), target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Directory symlinks unavailable: {type(exc).__name__}")
        (parent / "empty").mkdir()
        for relative in (Path("nested/new"), Path("empty")):
            with self.subTest(destination=str(relative)):
                destination = alias / relative
                result = backup.restore_backup(self.archive, destination)
                canonical = (parent / relative).resolve()
                self.assertEqual(result["destination"], str(canonical))
                self.assertEqual((destination / "articles.json").read_bytes(), (canonical / "articles.json").read_bytes())
                self.assertTrue((canonical / backup.MODEL_REVIEW_PATH).is_file())
        self.assertTrue(alias.is_symlink())

    def test_restore_rejects_nonempty_destination_under_parent_alias(self):
        self.make_backup()
        parent = self.folder / "canonical-parent"
        destination = parent / "restored"
        destination.mkdir(parents=True)
        existing = destination / "personal-note.txt"
        existing.write_text("keep", encoding="utf-8")
        alias = self.folder / "parent-alias"
        try:
            alias.symlink_to(parent.resolve(), target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Directory symlinks unavailable: {type(exc).__name__}")
        with self.assertRaisesRegex(backup.BackupError, "完全空目录"):
            backup.restore_backup(self.archive, alias / "restored")
        self.assertEqual(existing.read_text(encoding="utf-8"), "keep")
        self.assertEqual(list(destination.iterdir()), [existing])

    def test_restore_target_itself_cannot_be_live_or_dangling_link(self):
        self.make_backup()
        for kind in ("empty", "nonempty", "dangling"):
            with self.subTest(kind=kind):
                destination = self.folder / ("linked-target-" + kind)
                actual = self.folder / ("actual-target-" + kind)
                if kind != "dangling":
                    actual.mkdir()
                if kind == "nonempty":
                    (actual / "personal-note.txt").write_text("keep", encoding="utf-8")
                try:
                    destination.symlink_to(actual.resolve(), target_is_directory=True)
                except (OSError, NotImplementedError) as exc:
                    self.skipTest(f"Directory symlinks unavailable: {type(exc).__name__}")
                with self.assertRaisesRegex(backup.BackupError, "链接"):
                    backup.restore_backup(self.archive, destination)
                self.assertTrue(destination.is_symlink())
                if kind == "dangling":
                    self.assertFalse(actual.exists())
                elif kind == "empty":
                    self.assertEqual(list(actual.iterdir()), [])
                else:
                    self.assertEqual((actual / "personal-note.txt").read_text(encoding="utf-8"), "keep")
                    self.assertEqual(len(list(actual.iterdir())), 1)

    def test_committed_wal_is_snapshotted_without_wal_files(self):
        path = self.root / "learning/additional.sqlite"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE records(value TEXT)")
            connection.execute("INSERT INTO records VALUES('committed WAL record')")
            connection.commit()
            self.assertTrue(Path(str(path) + "-wal").is_file())
            self.make_backup()
            backup.restore_backup(self.archive, self.restored)
            with closing(sqlite3.connect(self.restored / "learning/additional.sqlite")) as restored:
                self.assertEqual(restored.execute("SELECT value FROM records").fetchone()[0], "committed WAL record")
        with zipfile.ZipFile(self.archive) as archive:
            self.assertFalse(any(name.endswith(("-wal", "-shm")) for name in archive.namelist()))

    def test_missing_external_file_is_reported_before_zip_creation(self):
        self.manifest["articles"][0]["pdf"] = str(self.external / "missing.pdf")
        write_json(self.root / "articles.json", self.manifest)
        with self.assertRaises(backup.BackupError):
            self.make_backup()
        self.assertFalse(self.archive.exists())

    def test_backup_cannot_be_inside_profile_or_nonexistent_profile(self):
        with self.assertRaisesRegex(backup.BackupError, "之外"):
            backup.create_backup(self.root, self.root / "backup.zip")
        with self.assertRaises(backup.BackupError):
            backup.create_backup(self.folder / "missing", self.archive)

    def test_secret_field_in_model_settings_is_rejected(self):
        write_json(self.root / "learning/model-settings.json", {"backend": "codex", "api_key": "SYNTHETIC_NOT_A_CREDENTIAL"})
        with self.assertRaisesRegex(backup.BackupError, "凭据字段") as caught:
            self.make_backup()
        self.assertNotIn("SYNTHETIC_NOT_A_CREDENTIAL", str(caught.exception))
        self.assertFalse(self.archive.exists())

    def test_archive_traversal_absolute_backslash_and_windows_aliases_rejected(self):
        invalid = ["profile/../escape.txt", "/absolute", "profile/C:/drive", "profile/learning\\escape", "profile/learning/file:stream", "profile/learning/NUL.txt", "profile/learning/COM¹", "profile/learning/CONIN$", "profile/learning/trailing."]
        for index, name in enumerate(invalid):
            archive = self.make_archive([("profile/articles.json", self.empty_articles()), (name, b"bad")], f"attack-{index}.zip")
            target = self.folder / f"never-created-{index}"
            with self.subTest(name=name), self.assertRaises(backup.BackupError):
                backup.restore_backup(archive, target)
            self.assertFalse(target.exists())

    def test_duplicate_case_collisions_symlinks_and_file_parent_conflicts_rejected(self):
        symlink = zipfile.ZipInfo("profile/learning/link")
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        cases = [[("profile/learning/a.txt", b"1"), ("profile/learning/a.txt", b"2")],
                 [("profile/learning/A.txt", b"1"), ("profile/learning/a.txt", b"2")],
                 [(symlink, b"../../outside")],
                 [("profile/learning/parent", b"file"), ("profile/learning/parent/child", b"child")],
                 [("profile/learning/helper.py", b"code")]]
        for index, entries in enumerate(cases):
            archive = self.make_archive([("profile/articles.json", self.empty_articles()), *entries], f"unsafe-{index}.zip")
            with self.subTest(index=index), self.assertRaises(backup.BackupError):
                backup.inspect_backup(archive)

    def test_missing_required_file_bad_format_hash_and_limits_rejected(self):
        missing = self.make_archive([("profile/learning/note.txt", b"note")], "missing.zip")
        with self.assertRaisesRegex(backup.BackupError, "必要文件"):
            backup.inspect_backup(missing)
        wrong = self.folder / "not-zip.zip"
        wrong.write_bytes(b"not a zip")
        with self.assertRaises(backup.BackupError):
            backup.inspect_backup(wrong)
        self.make_backup()
        with patch.object(backup, "MAX_FILES", 1), self.assertRaisesRegex(backup.BackupError, "数量"):
            backup.inspect_backup(self.archive)
        with patch.object(backup, "MAX_FILE_BYTES", 8), self.assertRaisesRegex(backup.BackupError, "大小"):
            backup.inspect_backup(self.archive)
        with patch.object(backup, "MAX_TOTAL_BYTES", 8), self.assertRaisesRegex(backup.BackupError, "大小"):
            backup.inspect_backup(self.archive)
        corrupted = self.folder / "corrupted.zip"
        with zipfile.ZipFile(self.archive) as source, zipfile.ZipFile(corrupted, "x") as target:
            for info in source.infolist():
                data = source.read(info)
                if info.filename == "profile/work/explanations/question/answer.json":
                    data = data.replace(b"Synthetic", b"Changed!!")
                target.writestr(info, data)
        with self.assertRaisesRegex(backup.BackupError, "完整性"):
            backup.inspect_backup(corrupted)

    def test_partial_backup_is_retained_and_named(self):
        with patch.object(backup, "_sqlite_snapshot", side_effect=OSError("synthetic failure")):
            with self.assertRaisesRegex(backup.BackupError, "不完整备份已保留") as caught:
                self.make_backup()
        self.assertTrue(self.archive.exists())
        self.assertIn(str(self.archive), str(caught.exception))

    def test_sqlite_snapshot_limit_is_explicit(self):
        with patch.object(backup, "MAX_SQLITE_BYTES", 1), self.assertRaisesRegex(backup.BackupError, "256 MiB"):
            self.make_backup()

    def test_partial_restore_is_retained_and_named(self):
        self.make_backup()
        original_open = Path.open
        canonical_target = self.restored.resolve()

        def fail_second(path, *args, **kwargs):
            if path == canonical_target / "relations.json" and args and args[0] == "xb":
                raise OSError("synthetic disk failure")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", fail_second), self.assertRaisesRegex(backup.BackupError, "已写入内容保留") as caught:
            backup.restore_backup(self.archive, self.restored)
        self.assertTrue((self.restored / "articles.json").exists())
        self.assertIn(str(canonical_target), str(caught.exception))
        with self.assertRaisesRegex(backup.BackupError, "完全空目录"):
            backup.restore_backup(self.archive, self.restored)


if __name__ == "__main__":
    unittest.main()
