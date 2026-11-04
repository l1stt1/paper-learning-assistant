"""Local PDF module delivery and path-boundary checks."""
from synthetic_library import seed_library
import http.client
import sys
import threading
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import Handler, Library, LocalServer


class PdfAssetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = LocalServer(("127.0.0.1", 0), Handler)
        cls.profile = ROOT / "work" / "validation" / uuid.uuid4().hex
        seed_library(cls.profile)
        cls.http.lib = Library(ROOT, cls.profile)
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.http.lib.close()

    def get(self, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.http.server_port, timeout=10)
        connection.request("GET", path)
        response = connection.getresponse()
        result = response.status, response.getheader("Content-Type"), response.read()
        connection.close()
        return result

    def test_local_pdf_modules_and_styles(self):
        for filename in ("pdf.mjs", "pdf.worker.mjs"):
            with self.subTest(filename=filename):
                status, mime, body = self.get("/vendor/pdfjs/build/" + filename)
                self.assertEqual(status, 200)
                self.assertEqual(mime, "text/javascript")
                self.assertEqual(body, (ROOT / "app" / "vendor" / "pdfjs" / "build" / filename).read_bytes())
        status, mime, body = self.get("/vendor/pdfjs/web/pdf_viewer.css")
        self.assertEqual((status, mime), (200, "text/css"))
        self.assertIn(b".textLayer", body)

    def test_vendor_requests_cannot_read_application_or_learning_files(self):
        for path in ("/vendor/pdfjs/../../../server.py", "/vendor/pdfjs/../../../learning/synthetic-paper.sqlite",
                     "/vendor/pdfjs/..\\..\\..\\server.py", "/vendor/pdfjs/missing.mjs"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0], 404)

    def test_original_pdf_is_served_unchanged(self):
        status, mime, body = self.get("/api/pdf?id=synthetic-paper")
        self.assertEqual((status, mime), (200, "application/pdf"))
        self.assertEqual(body, (self.profile / self.http.lib.article("synthetic-paper")["pdf"]).read_bytes())


if __name__ == "__main__":
    unittest.main(verbosity=2)
