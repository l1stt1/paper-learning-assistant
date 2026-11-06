"""Render small desktop theme images from vectors with the existing PDF engine."""
from pathlib import Path
import pymupdf

ROOT = Path(__file__).resolve().parents[1]


def render(svg, output, scale):
    with pymupdf.open(stream=svg.encode("utf-8"), filetype="svg") as document:
        document[0].get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=True).save(output)


def main():
    logo = (ROOT / "assets/app-icon.svg").read_text(encoding="utf-8")
    # The compact SVG renderer does not resolve gradient paint servers.
    logo = logo.replace('fill="url(#background)"', 'fill="#197d7d"')
    for density in (100, 125, 150, 175, 200, 250, 300):
        target = ROOT / "assets/desktop" / str(density)
        target.mkdir(parents=True, exist_ok=True)
        render(logo, target / "logo.png", 48 / 256 * density / 100)
        for name, fill, stroke in (
            ("primary", "#16796f", "#16796f"),
            ("primary-hover", "#10665e", "#10665e"),
            ("quiet", "#ffffff", "#d9e4e1"),
            ("quiet-hover", "#edf6f3", "#bdd6ce"),
            ("disabled", "#edf1ef", "#e6ece9"),
        ):
            svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24"><rect x="0.5" y="0.5" width="23" height="23" rx="8" fill="{fill}" stroke="{stroke}"/></svg>'
            render(svg, target / (name + ".png"), density / 100)


if __name__ == "__main__":
    main()
