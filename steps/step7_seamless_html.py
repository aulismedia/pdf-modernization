#!/usr/bin/env python3
"""
Step Y: Assemble a seamless HTML file from the polished book JSON.

Uses the page_join field written by step6 to stitch
main_text across page boundaries into one continuously flowing document.
All footnotes are collected and rendered at the end.

Requires stepX_polish_text.py to have been run first so that page_join is
present on each page object in the JSON.

Output: <book-dir>/<book-name>_seamless.html  (override with --suffix)

Usage:
    python stepY_assemble_seamless_html.py <path_to_pdf>
    python stepY_assemble_seamless_html.py <path_to_pdf> --book-name "Title" --title "Display Title"
"""

import argparse
import json
import sys
from pathlib import Path

from tqdm import tqdm

from utils.config import book_dirs
from utils.shared_layout import _PARA_SPLIT, _sort_areas

CSS = """
body {
    font-family: Georgia, 'Times New Roman', serif;
    max-width: 860px;
    margin: 0 auto;
    padding: 2rem;
    line-height: 1.65;
    color: #1a1a1a;
    background: #fafaf8;
}
h1.book-header { text-align: center; margin-bottom: 2rem; }
.chapter-title {
    text-align: center;
    font-size: 1.4rem;
    font-weight: bold;
    margin: 2.5rem 0 1rem;
    letter-spacing: 0.05em;
}
.subtitle {
    font-size: 1.05rem;
    font-weight: bold;
    margin: 1.5rem 0 0.4rem;
}
.main-text { margin: 0.8rem 0; text-align: justify; }
figure {
    margin: 1.5rem auto;
    text-align: center;
    max-width: 100%;
}
figure img {
    max-width: 100%;
    height: auto;
    border: 1px solid #ccc;
}
figcaption {
    font-size: 0.85rem;
    color: #555;
    margin-top: 0.4rem;
    font-style: italic;
}
.footnotes {
    margin-top: 3rem;
    border-top: 2px solid #ccc;
    padding-top: 1rem;
    font-size: 0.82rem;
    color: #444;
}
.footnote-item { margin: 0.4rem 0; }
.title-page-line {
    text-align: center;
    font-size: 1.1rem;
    margin: 0.25rem 0;
    letter-spacing: 0.04em;
}
.title-page-separator {
    border: none;
    border-top: 1px solid #ccc;
    margin: 1.5rem auto;
    width: 40%;
}
"""


def _flush(pending: str, parts: list) -> str:
    if pending.strip():
        parts.append(f'  <p class="main-text">{pending.strip()}</p>')
    return ""


def build_seamless_html(active_pages: list, elements_rel: str) -> list[str]:
    parts: list[str] = []
    all_footnotes: list[str] = []
    pending = ""           # open paragraph being built, may span page boundaries
    prev_page_join: str | None = None

    for page in tqdm(active_pages, desc="Assembling", unit="page"):
        page_w = page.get("page_dimensions", {}).get("width", 1000)
        sorted_areas = _sort_areas(page.get("areas", []), page_w)
        stem = Path(page.get("source_image", "")).stem or "unknown"

        captions: dict[str, list] = {}
        for area in sorted_areas:
            if area["type"] == "illustration_caption":
                lid = area.get("linked_illustration_id") or "__unlinked__"
                captions.setdefault(lid, []).append(area)

        emitted: set[str] = set()
        first_main_on_page = True

        for area in sorted_areas:
            atype = area.get("type")
            text = (area.get("text") or "").strip()

            if atype == "main_text" and text:
                paras = _PARA_SPLIT.split(text)
                paras = [p.replace("\n", " ").strip() for p in paras if p.strip()]

                for para_idx, para in enumerate(paras):
                    if para_idx == 0 and first_main_on_page and prev_page_join:
                        # Page boundary — apply the join rule from the previous page
                        if prev_page_join == "merge_hyphen" and pending:
                            stripped = pending.rstrip()
                            base = stripped[:-1] if stripped.endswith("-") else stripped
                            pending = base + para
                        elif prev_page_join == "merge_sentence" and pending:
                            stripped = pending.rstrip()
                            if stripped.endswith("-"):
                                pending = stripped[:-1] + para
                            else:
                                pending = stripped + " " + para
                        else:  # new_paragraph or unrecognised value
                            pending = _flush(pending, parts)
                            pending = para
                    else:
                        # Internal paragraph break (\n\n) or second+ area on same page
                        pending = _flush(pending, parts)
                        pending = para

                first_main_on_page = False

            elif atype == "chapter_title" and text:
                pending = _flush(pending, parts)
                parts.append(f'  <div class="chapter-title">{" ".join(text.split())}</div>')

            elif atype == "subtitle" and text:
                pending = _flush(pending, parts)
                parts.append(f'  <div class="subtitle">{" ".join(text.split())}</div>')

            elif atype == "title_page" and text:
                pending = _flush(pending, parts)
                parts.append(f'  <p class="title-page-line">{" ".join(text.split())}</p>')

            elif atype == "illustration":
                illus_id = area.get("illustration_id", "")
                if not illus_id or illus_id in emitted:
                    continue
                emitted.add(illus_id)
                pending = _flush(pending, parts)

                img_src = f"{elements_rel}/{stem}_{illus_id}.png"
                cap_areas = captions.get(illus_id, [])
                cap_text = " ".join(
                    (c.get("text") or "").strip() for c in cap_areas if c.get("text")
                ).strip()
                figcap = f"<figcaption>{cap_text}</figcaption>" if cap_text else ""
                fig_id = f"{stem}_{illus_id}"
                parts.append(
                    f'  <figure id="{fig_id}">\n'
                    f'    <img src="{img_src}" alt="Illustration {fig_id}">\n'
                    f'    {figcap}\n'
                    f'  </figure>'
                )

            elif atype == "footnote" and text:
                all_footnotes.append(f'    <div class="footnote-item">{text}</div>')

        prev_page_join = page.get("page_join")

    pending = _flush(pending, parts)

    if all_footnotes:
        parts.append(
            '  <div class="footnotes">\n' + "\n".join(all_footnotes) + "\n  </div>"
        )

    return parts


def main():
    parser = argparse.ArgumentParser(
        description="Assemble seamless HTML from polished book JSON."
    )
    parser.add_argument("pdf", help="Path to the source PDF file")
    parser.add_argument("--book-name", default=None)
    parser.add_argument("--title", default=None)
    parser.add_argument(
        "--suffix", default="-merged",
        help="Output filename suffix appended before .html (default: -merged)",
    )
    parser.add_argument(
        "--lang", default="en",
        help="HTML lang attribute (default: en)",
    )
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    book_dir = pdf_path.parent
    dirs = book_dirs(book_dir, pdf_path.stem)
    json_dir = dirs["json"]

    if args.book_name:
        json_path = json_dir / f"{args.book_name}.json"
        if not json_path.exists():
            print(f"JSON not found: {json_path}", file=sys.stderr)
            sys.exit(1)
    else:
        candidates = [f for f in json_dir.glob("*.json") if f.name != "book.json"]
        if not candidates:
            print(f"No JSON file found in {json_dir}", file=sys.stderr)
            sys.exit(1)
        if len(candidates) > 1:
            print("Multiple JSON files found; use --book-name to select one:")
            for c in candidates:
                print(f"  {c.name}")
            sys.exit(1)
        json_path = candidates[0]

    book_name = json_path.stem
    title = args.title or book_name
    out_path = book_dir / f"{book_name}{args.suffix}.html"

    book_data = json.loads(json_path.read_text(encoding="utf-8"))
    all_pages = book_data.get("pages", [])
    active_pages = [p for p in all_pages if not p.get("ignored")]

    ignored_count = len(all_pages) - len(active_pages)
    if ignored_count:
        print(f"Skipping {ignored_count} ignored page(s).")

    if not active_pages:
        print("No pages to assemble.")
        return

    body_parts = build_seamless_html(active_pages, dirs["elements"].name)
    body = "\n\n".join(body_parts)

    html = f"""<!DOCTYPE html>
<html lang="{args.lang}">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <style>
{CSS}
  </style>
</head>
<body>
  <h1 class="book-header">{title}</h1>

{body}

</body>
</html>
"""
    out_path.write_text(html, encoding="utf-8")
    print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()
