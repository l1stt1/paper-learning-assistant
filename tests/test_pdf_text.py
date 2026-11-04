"""PDF text extraction checks using original synthetic layout fixtures."""
import re
import sys
import unittest
from pathlib import Path
import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import extract_reading_text, join_reading_lines

class ReadingTextTests(unittest.TestCase):
    def test_compounds_and_soft_hyphens(self):
        self.assertEqual(join_reading_lines(["RNA-", "seq uses tissue- and tumor-specific samples."]), "RNA-seq uses tissue- and tumor-specific samples.")
        self.assertEqual(join_reading_lines(["identi\u00ad", "fied shapes"]), "identified shapes")

    def test_heading_and_caption_boundaries(self):
        with fitz.open() as pdf:
            p = pdf.new_page()
            p.insert_text((72, 72), "Methods", fontname="hebo")
            p.insert_text((72, 100), "We counted blue shapes.")
            p.insert_text((72, 150), "A. Shape counts")
            p.insert_text((72, 180), "B. Shape sizes")
            text = extract_reading_text(p)
            self.assertIn("Methods\n\nWe counted blue shapes.", text)
            self.assertIn("A. Shape counts", text)
            self.assertIn("B. Shape sizes", text)

    def test_columns_retain_source_content_and_order(self):
        with fitz.open() as pdf:
            p=pdf.new_page()
            p.insert_textbox((72, 72, 250, 300), "Left column first sentence.\nA second sentence retains RNA-seq.")
            p.insert_textbox((320, 72, 500, 300), "Right column follows in source order.\nThese observations are synthetic.")
            normalized = lambda value: re.sub(r"[\s\u00ad]", "", value)
            self.assertEqual(normalized(extract_reading_text(p)), normalized(p.get_text("text", sort=False)))

    def test_table_cells_do_not_merge_into_words(self):
        with fitz.open() as pdf:
            p=pdf.new_page()
            for y,left,right in [(72,"Group","Count"),(90,"Blue","4"),(108,"Green","2")]:
                p.insert_text((72,y),left)
                p.insert_text((180,y),right)
            text=extract_reading_text(p)
            self.assertIn("Blue",text)
            self.assertIn("4",text)
            self.assertNotIn("Blue4",text)

if __name__ == "__main__":
    unittest.main()
