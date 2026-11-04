"""Synthetic complete-library isolation, process locking, and safe server lifecycle."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import uuid

import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_version import VERSION
from profile_store import ProfileInUseError, ProfileLock, profile_run_path, read_service_info, resolve_profile_root
from server import ANSWER_FIELDS, Handler, Library, find_codex, serve
from library_features import ANSWER_FIELDS as COMPARISON_ANSWER_FIELDS


def synthetic_pdf(text="A synthetic paper for library isolation."):
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), text)
        return document.tobytes()


class ProfileStoreTests(unittest.TestCase):
    def setUp(self):
        self.work = ROOT / "work" / "validation" / ("profiles-" + uuid.uuid4().hex)
        self.work.mkdir(parents=True)
        self.app = self.work / "program"
        self.app.mkdir()

    def library(self, profile):
        library = Library(root=self.app, data_root=profile)
        self.addCleanup(library.close)
        return library

    def import_pair(self, library):
        articles = []
        for label in ("first", "second"):
            preview = library.preview_import(synthetic_pdf(f"The {label} synthetic paper provides source evidence."))
            articles.append(library.commit_import({"preview_id": preview["preview_id"], "title": label}))
        return [article["id"] for article in articles]

    def test_run_paths_reject_path_syntax_and_accept_generated_ids(self):
        profile = self.work / "run-validation"
        identifier = uuid.uuid4().hex
        self.assertEqual(profile_run_path(profile, "work", "explanations", identifier),
                         profile.resolve() / "work" / "explanations" / identifier)
        self.assertEqual(profile_run_path(profile, "learning", "study-runs", "study-" + identifier),
                         profile.resolve() / "learning" / "study-runs" / ("study-" + identifier))
        for value in ("../escape", "../../../escape", "a/b", "a\\b", "/absolute", "C:escape", "", None, "x" * 121):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "任务标识"):
                profile_run_path(profile, "work", value)
        self.assertFalse(profile.exists())

    def test_run_paths_reject_existing_directory_link_outside_profile(self):
        profile = self.work / "linked-profile"
        profile.mkdir()
        outside = self.work / "outside"
        outside.mkdir()
        try:
            (profile / "work").symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Directory symlink creation unavailable: {exc}")
        with self.assertRaisesRegex(ValueError, "不在当前文库"):
            profile_run_path(profile, "work", "explanations", uuid.uuid4().hex)
        self.assertEqual(list(outside.iterdir()), [])

    def test_actual_question_comparison_and_study_producers_use_profile_paths(self):
        library = self.library(self.work / "valid-runs")
        aids = self.import_pair(library)
        question = library.capture({"article_id": aids[0], "page": 1, "question": "Synthetic question"})
        answer = {**{key: "Synthetic answer" for key in ANSWER_FIELDS}, "related_concepts": []}
        with patch.object(library.model_backend, "generate", return_value=answer) as generate:
            library.ai_lock.acquire()
            library.active_ai = ("question", aids[0], question["id"])
            library.explain(aids[0], question)
            run = generate.call_args.args[3]
        self.assertTrue(run.is_dir())
        self.assertEqual(run.parent, profile_run_path(library.root, "work", "explanations", question["id"]))
        self.assertEqual(library.question(aids[0], question["id"])["status"], "completed")

        comparison = library.capture_comparison({"article_ids": aids, "question": "Compare synthetic papers"})
        comparison_answer = {key: "Synthetic comparison" for key in COMPARISON_ANSWER_FIELDS}
        comparison_answer["citations"] = [{"article_id": aid, "page": 1, "quote": library.pages[aid][0].strip()} for aid in aids]
        with patch.object(library.model_backend, "generate", return_value=comparison_answer) as generate:
            library.ai_lock.acquire()
            library.active_ai = ("comparison", comparison["id"])
            library.explain_comparison(comparison)
            run = generate.call_args.args[3]
        self.assertTrue(run.is_dir())
        self.assertEqual(run.parent, profile_run_path(library.root, "work", "comparisons", comparison["id"]))
        self.assertEqual(library.comparison(comparison["id"])["status"], "completed")

        with patch.object(library.model_backend, "available", return_value=True), patch("study_features.threading.Thread"):
            job_id = library.study_start({"article_ids": [aids[0]]})["job"]["id"]
        with patch("study_generation.generate_article", side_effect=RuntimeError("Synthetic stop after directory creation")) as generate:
            library._study_run(job_id)
            run = generate.call_args.args[3]
        self.assertTrue(run.is_dir())
        self.assertEqual(run, profile_run_path(library.root, "learning", "study-runs", job_id, aids[0]))
        self.assertFalse(library.busy)

    def test_restored_forged_ids_are_rejected_before_workers_and_model_calls(self):
        profile = self.work / "restored"
        library = self.library(profile)
        aids = self.import_pair(library)
        question = library.capture({"article_id": aids[0], "page": 1, "question": "Restored question"})
        comparison = library.capture_comparison({"article_ids": aids, "question": "Restored comparison"})
        with patch.object(library.model_backend, "available", return_value=True), patch("study_features.threading.Thread"):
            job_id = library.study_start({"article_ids": [aids[0]]})["job"]["id"]
        library.study_active_job = None  # No worker was started in this fixture.
        forged_question = {**question, "id": "../../../escaped-question"}
        forged_comparison = {**comparison, "id": "../../../escaped-comparison"}
        library.save_question(aids[0], forged_question)
        library.save_comparison(forged_comparison)
        job = library.study_jobs[job_id]
        job.update(id="../../../escaped-study", status="failed")
        library._study_write_json(library.study_job_path, {"jobs": [job]})
        library.close()
        restored = self.library(profile)
        original_study_jobs = restored.study_job_path.read_bytes()
        with patch.object(restored.model_backend, "generate") as model, \
             patch("study_generation.generate_article") as article_model, \
             patch("study_generation.generate_cross") as cross_model, \
             patch("server.threading.Thread") as worker:
            with self.assertRaisesRegex(ValueError, "任务标识"):
                restored.start_explanation(aids[0], forged_question["id"])
            with self.assertRaisesRegex(ValueError, "任务标识"):
                restored.start_comparison(forged_comparison["id"])
            with self.assertRaisesRegex(ValueError, "任务标识"):
                restored.study_resume(job["id"])
            worker.assert_not_called()
            self.assertEqual(restored.study_job_path.read_bytes(), original_study_jobs)
            self.assertFalse(restored.busy)

            # The producers also check paths if invoked directly after restoration.
            restored.ai_lock.acquire()
            restored.active_ai = ("question", aids[0], forged_question["id"])
            restored.explain(aids[0], forged_question)
            restored.ai_lock.acquire()
            restored.active_ai = ("comparison", forged_comparison["id"])
            restored.explain_comparison(forged_comparison)
            restored._study_run(job["id"])
            model.assert_not_called()
            article_model.assert_not_called()
            cross_model.assert_not_called()
        self.assertFalse(restored.busy)
        for name in ("escaped-question", "escaped-comparison", "escaped-study"):
            self.assertFalse((self.work / name).exists())
        self.assertFalse((profile / "work" / "explanations").exists())
        self.assertFalse((profile / "work" / "comparisons").exists())
        self.assertFalse((profile / "learning" / "study-runs").exists())

    def test_explicit_profiles_isolate_articles_records_and_program(self):
        # An invalid private program manifest must never be read by explicit profiles.
        private = self.app / "articles.json"
        private.write_text("private, intentionally invalid JSON", encoding="utf-8")
        a = self.library(self.work / "a")
        b = self.library(self.work / "b")
        self.assertNotEqual(a.manifest["library_id"], b.manifest["library_id"])
        upload = synthetic_pdf()
        preview = a.preview_import(upload)
        with patch("os.link", side_effect=AssertionError("Hard links are unavailable")):
            article = a.commit_import({"preview_id": preview["preview_id"], "title": "Synthetic paper"})
        a.capture({"article_id": article["id"], "page": 1, "question": "Saved in profile A"})
        a.save_topic({"name": "Profile A topic"})
        a.configure_model({"backend": "openai", "model": "test-model", "key_env": "TEST_UNUSED_KEY"})
        self.assertEqual(a.root, (self.work / "a").resolve())
        self.assertEqual(a.data_root, a.root / "learning")
        self.assertEqual(a.app_root, self.app.resolve())
        self.assertEqual(b.articles, {})
        self.assertEqual(b.topics(), [])
        self.assertEqual(b.model_backend.status()["settings"]["backend"], "codex")
        self.assertEqual(private.read_text(encoding="utf-8"), "private, intentionally invalid JSON")
        self.assertEqual(sorted(p.name for p in self.app.iterdir()), ["articles.json"])
        library_id = a.manifest["library_id"]
        a.close()
        reopened = self.library(self.work / "a")
        self.assertEqual(reopened.manifest["library_id"], library_id)
        self.assertEqual(reopened.state(article["id"])["questions"][0]["question"], "Saved in profile A")
        self.assertEqual((reopened.root / article["pdf"]).read_bytes(), upload)

    def test_default_resolution_preserves_only_implicit_legacy_library(self):
        default = self.work / "os-profile"
        with patch("profile_store.default_profile_root", return_value=default):
            self.assertEqual(resolve_profile_root(self.app), default)
            (self.app / "articles.json").write_text(json.dumps({"library_id": "legacy-example-library", "articles": []}), encoding="utf-8")
            self.assertEqual(resolve_profile_root(self.app), self.app.resolve())
            self.assertEqual(resolve_profile_root(self.app, self.work / "explicit"), (self.work / "explicit").resolve())
        library = Library(root=self.app)
        self.addCleanup(library.close)
        self.assertEqual(library.root, self.app.resolve())
        self.assertEqual(library.manifest["library_id"], "legacy-example-library")

    def test_lock_blocks_process_before_job_recovery_and_releases(self):
        profile = self.work / "locked"
        library = self.library(profile)
        preview = library.preview_import(synthetic_pdf())
        article = library.commit_import({"preview_id": preview["preview_id"], "title": "Lock test"})
        question = library.capture({"article_id": article["id"], "page": 1, "question": "Keep running status"})
        question["status"] = "running"
        library.save_question(article["id"], question)
        script = "from server import Library; import sys; Library(data_root=sys.argv[1])"
        child = subprocess.run([sys.executable, "-c", script, str(profile)], cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertNotEqual(child.returncode, 0)
        self.assertIn("ProfileInUseError", child.stderr)
        with library.db(article["id"]) as db:
            raw = json.loads(db.execute("SELECT payload FROM questions WHERE id=?", (question["id"],)).fetchone()[0])
        self.assertEqual(raw["status"], "running")
        with self.assertRaises(ProfileInUseError):
            ProfileLock(profile).acquire()
        library.close()
        with ProfileLock(profile):
            pass
        self.assertTrue((profile / ".profile.lock").exists())
        reopened = self.library(profile)
        self.assertEqual(reopened.question(article["id"], question["id"])["status"], "failed")

    def test_invalid_manifest_releases_lock_without_overwriting(self):
        profile = self.work / "invalid"
        profile.mkdir()
        manifest = profile / "articles.json"
        manifest.write_text("invalid", encoding="utf-8")
        with self.assertRaises(ValueError):
            Library(root=self.app, data_root=profile)
        with ProfileLock(profile):
            pass
        self.assertEqual(manifest.read_text(encoding="utf-8"), "invalid")
        self.assertFalse((profile / "learning").exists())

    def test_codex_path_lookup_supports_non_windows_binary_name(self):
        with patch.dict(os.environ, {}, clear=True), patch("server.shutil.which", side_effect=lambda name: "/usr/bin/codex" if name == "codex" else None):
            self.assertEqual(find_codex(), "/usr/bin/codex")

    def test_port_conflict_does_not_initialize_profile(self):
        profile = self.work / "not-created"
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen(1)
            with self.assertRaises(OSError):
                serve(data_dir=profile, port=occupied.getsockname()[1], app_root=self.app)
        self.assertFalse(profile.exists())

    def test_http_health_static_assets_busy_and_token_guarded_shutdown(self):
        app = self.app / "app"
        app.mkdir()
        (app / "index.html").write_text("synthetic static application", encoding="utf-8")
        profile = self.work / "server-profile"
        ready = threading.Event()
        state = {}

        def started(server):
            state["server"] = server
            ready.set()

        def run():
            try:
                serve(data_dir=profile, port=0, ready_callback=started, app_root=self.app)
            except BaseException as exc:
                state["error"] = exc
                ready.set()

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(ready.wait(10))
        if "error" in state:
            raise state["error"]
        server = state["server"]
        base = f"http://127.0.0.1:{server.server_port}"

        def request(path, token=None, body=None):
            headers = {"Content-Type": "application/json"}
            if token is not None:
                headers["X-Learning-Token"] = token
            req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=5) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read()

        try:
            health = json.loads(request("/health")[1])
            self.assertEqual(health["root"], str(profile.resolve()))
            self.assertEqual(health["app_root"], str(self.app.resolve()))
            self.assertEqual(health["version"], VERSION)
            self.assertFalse(health["busy"])
            self.assertEqual(request("/")[1], b"synthetic static application")
            library = json.loads(request("/api/library")[1])
            self.assertFalse(library["legacy_drafts"])
            self.assertEqual(library["library_id"], health["library_id"])
            self.assertEqual(request("/api/shutdown", body={})[0], 403)
            for field in ("active_ai", "study_active_job", "zotero_topic_active_job"):
                setattr(server.lib, field, "synthetic-active-job")
                self.assertTrue(json.loads(request("/health")[1])["busy"])
                self.assertEqual(request("/api/shutdown", library["token"], {})[0], 409)
                setattr(server.lib, field, None)
            server.lib.zotero_export_lock.acquire()
            self.assertEqual(request("/api/shutdown", library["token"], {})[0], 409)
            server.lib.zotero_export_lock.release()
            info = read_service_info(profile)
            self.assertEqual(info["status"], "running")
            self.assertNotIn(library["token"], (profile / ".service.json").read_text(encoding="utf-8"))
            self.assertEqual(request("/api/shutdown", library["token"], {})[0], 200)
        finally:
            server.lib.active_ai = server.lib.study_active_job = server.lib.zotero_topic_active_job = None
            if server.lib.zotero_export_lock.locked():
                server.lib.zotero_export_lock.release()
            server.request_shutdown()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", state)
        self.assertEqual(read_service_info(profile)["status"], "stopped")
        with ProfileLock(profile):
            pass

    def test_half_http_request_cannot_hold_profile_open_indefinitely(self):
        profile = self.work / "half-request"
        ready = threading.Event()
        connected = threading.Event()
        state = {}

        class ObservedHandler(Handler):
            def setup(self):
                super().setup()
                connected.set()

        def started(server):
            state["server"] = server
            ready.set()

        def run():
            try:
                serve(data_dir=profile, port=0, ready_callback=started, app_root=self.app)
            except BaseException as exc:
                state["error"] = exc
                ready.set()

        connection = None
        with patch("server.Handler", ObservedHandler):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(ready.wait(10))
                if "error" in state:
                    raise state["error"]
                server = state["server"]
                connection = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
                connection.sendall(b"GET /health HTTP/1.1\r\nHost: 127.0.0.1")
                self.assertTrue(connected.wait(5))
                self.assertTrue(server.request_shutdown())
                thread.join(7)
                self.assertFalse(thread.is_alive(), "An incomplete HTTP request blocked shutdown")
                self.assertNotIn("error", state)
                self.assertEqual(read_service_info(profile)["status"], "stopped")
                with ProfileLock(profile):
                    pass
            finally:
                if connection is not None:
                    connection.close()
                if "server" in state:
                    state["server"].request_shutdown()
                thread.join(10)


if __name__ == "__main__":
    unittest.main()
