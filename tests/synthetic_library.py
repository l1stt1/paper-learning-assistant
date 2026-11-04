"""Copyright-free documents created specifically for automated checks."""
import json
from pathlib import Path

import fitz


def seed_library(root, extra_id=None):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    aid = "synthetic-paper"
    with fitz.open() as pdf:
        for number in range(1, 54):
            page = pdf.new_page()
            page.insert_text((72, 72), f"Synthetic reading fixture. Page {number}.")
            page.insert_text((72, 100), "More than 50% of these shapes are blue. This is a test, not a research finding.")
            if number == 40:
                page.insert_text((72, 140), "Data availability. Synthetic measurements are available in the example archive.")
        pdf.save(root / "source.pdf")
    content = {"overview": {"question": "How are the example shapes counted?", "findings": [], "limits": ["Synthetic test material"]},
               "sections": [{"id": "example-method", "title": "Counting shapes", "pages": [25], "summary": "A synthetic method"}],
               "concepts": [{"id": f"example-concept-{i}", "term": f"Example term {i}", "pages": [25]} for i in range(25)]}
    (root / "content.json").write_text(json.dumps(content), encoding="utf-8")
    first = {"id": aid, "title": "Synthetic reading fixture", "short_title": "Example", "pages": 53,
             "pdf": "source.pdf", "content": "content.json", "doi": ""}
    manifest = {"library_id": "synthetic-" + root.name, "articles": [first]}
    if extra_id:
        manifest["articles"].append({**first, "id": extra_id})
    (root / "articles.json").write_text(json.dumps(manifest), encoding="utf-8")
    return first
