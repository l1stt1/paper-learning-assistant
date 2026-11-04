"""Install only into an empty destination; invalid bundles cannot escape it."""
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from installer import install_bundle


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="pla-install-test-")).resolve()

    def bundle(self, names):
        archive = self.root / f"bundle-{len(list(self.root.glob('*.zip')))}.zip"
        with zipfile.ZipFile(archive, "x") as z:
            for name, value in names:
                z.writestr(name, value)
        return archive

    def test_install_leaves_existing_libraries_and_old_versions_untouched(self):
        old = self.root / "old"
        old.mkdir()
        (old / "personal.json").write_text("private fixture")
        bundle = self.bundle([("PaperLearningAssistant.exe", b"synthetic executable"), ("_internal/app/index.html", b"app")])
        result = install_bundle(bundle, self.root / "new")
        self.assertEqual(result.read_bytes(), b"synthetic executable")
        self.assertEqual((old / "personal.json").read_text(), "private fixture")
        with self.assertRaises(ValueError):
            install_bundle(bundle, old)
        with self.assertRaises(ValueError):
            install_bundle(bundle, self.root / "new")

    def test_traversal_and_case_collisions_fail_before_destination_creation(self):
        for bad in ("../escape", "C:/escape", "\\escape", "_internal/../../escape", "paperlearningassistant.EXE"):
            with self.subTest(bad=bad):
                bundle = self.bundle([("PaperLearningAssistant.exe", b"stub"), (bad, b"bad")])
                target = self.root / "rejected"
                with self.assertRaises(ValueError):
                    install_bundle(bundle, target)
                self.assertFalse(target.exists())

    def test_missing_program_is_rejected(self):
        bundle = self.bundle([("readme.txt", b"empty")])
        with self.assertRaises(ValueError):
            install_bundle(bundle, self.root / "missing")


if __name__ == "__main__":
    unittest.main()
