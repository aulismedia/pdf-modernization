#!/usr/bin/env python3
"""
Step 9: Export the linked HTML to a Markdown-formatted .txt file.

Element mapping:
  h1.book-header    →  # Title
  div.chapter-title →  ## Chapter
  div.subtitle      →  ### Subtitle
  p.main-text       →  paragraph  (sup refs become [N] or [*])
  figure            →  [Illustration: caption]
  div.footnotes     →  ## Footnotes section at the end

Output filename stem matches source_pdf in book.json.

Usage:
    python step9_export_txt.py <path_to_linked_html>
    python step9_export_txt.py <path_to_linked_html> --out /path/to/output.txt
    python step9_export_txt.py <path_to_linked_html> --book-json /path/to/book.json
"""

import argparse
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag


def load_pdf_stem(html_path: Path, book_json_override: str | None) -> str:
    if book_json_override:
        return Path(book_json_override).stem
    candidates = sorted(html_path.parent.glob("*.json"))
    if candidates:
        return candidates[0].stem
    return html_path.stem


def _collect(node) -> str:
    """Recursively collect text, rendering <sup> as [marker], skipping backlinks."""
    if isinstance(node, NavigableString):
        return str(node)
    if not isinstance(node, Tag):
        return ""
    if node.name == "a" and "backlink" in (node.get("class") or []):
        return ""
    if node.name == "sup":
        marker = node.get_text().strip()
        return f"[{marker}]" if marker else ""
    return "".join(_collect(child) for child in node.children)


def _figure_text(el: Tag) -> str:
    cap = el.find("figcaption")
    cap_text = cap.get_text().strip() if cap else ""
    return f"[Illustration: {cap_text}]" if cap_text else "[Illustration]"


def convert(soup: BeautifulSoup) -> str:
    lines: list[str] = []
    footnote_lines: list[str] = []

    for el in soup.body.children:
        if not isinstance(el, Tag):
            continue
        classes = el.get("class") or []

        if el.name == "h1" and "book-header" in classes:
            lines += [f"# {el.get_text().strip()}", ""]

        elif el.name == "div" and "chapter-title" in classes:
            lines += ["", f"## {el.get_text().strip()}", ""]

        elif el.name == "div" and "subtitle" in classes:
            lines += ["", f"### {el.get_text().strip()}", ""]

        elif el.name == "p" and "main-text" in classes:
            text = re.sub(r"[ \t]*\n[ \t]*", " ", _collect(el)).strip()
            text = re.sub(r" {2,}", " ", text)
            if text:
                lines += [text, ""]

        elif el.name == "figure":
            lines += [_figure_text(el), ""]

        elif el.name == "div" and "footnotes" in classes:
            for item in el.find_all("div", class_="footnote-item"):
                text = re.sub(r"\s+", " ", _collect(item)).strip()
                if text:
                    footnote_lines.append(text)

    if footnote_lines:
        lines += ["", "---", "", "## Footnotes", ""]
        for item in footnote_lines:
            lines += [item, ""]

    result = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return result.strip() + "\n"


def main():
    parser = argparse.ArgumentParser(description="Export linked HTML to Markdown .txt")
    parser.add_argument("html", help="Path to the linked HTML file")
    parser.add_argument("--out", default=None,
                        help="Output path (default: same dir as HTML, pdf-stem.txt)")
    parser.add_argument("--book-json", default=None)
    args = parser.parse_args()

    html_path = Path(args.html)
    if not html_path.exists():
        print(f"HTML file not found: {html_path}", file=sys.stderr)
        sys.exit(1)

    pdf_stem = load_pdf_stem(html_path, args.book_json)
    out_path = Path(args.out) if args.out else html_path.parent / f"{pdf_stem}.txt"

    print(f"Parsing {html_path.name}…")
    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")

    text = convert(soup)

    out_path.write_text(text, encoding="utf-8")
    print(f"Saved: {out_path}  ({len(text):,} chars, {text.count(chr(10)):,} lines)")


if __name__ == "__main__":
    main()
