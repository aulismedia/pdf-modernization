#!/usr/bin/env python3
"""
Step 8: Package the linked HTML into an EPUB file.

Metadata (title, author, year, cover_path) is read from projects.json and
synced into the pipeline JSON's meta field. The cover is converted from PNG
to JPEG if needed. Illustrations are embedded as JPEGs (converted from PNGs
on first run and cached in a sibling <stem> - jpeg folder).

Usage:
    python step8_export_epub.py <path_to_linked_html>
    python step8_export_epub.py <path_to_linked_html> --out /path/to/output.epub
"""

import argparse
import json
import re
import sys
import unicodedata
import uuid
from pathlib import Path

from bs4 import BeautifulSoup
from ebooklib import epub
from PIL import Image

PROJECTS_JSON = Path(__file__).parent.parent / "projects.json"

EPUB_CSS = """
body {
    font-family: Georgia, 'Times New Roman', serif;
    line-height: 1.65;
    margin: 1em 1.5em;
    color: #1a1a1a;
}
h1.book-header { text-align: center; margin-bottom: 2em; }
.chapter-title {
    text-align: center;
    font-size: 1.3em;
    font-weight: bold;
    margin: 2em 0 0.8em;
    letter-spacing: 0.04em;
}
.subtitle {
    font-size: 1.05em;
    font-weight: bold;
    margin: 1.2em 0 0.3em;
}
p.main-text { margin: 0.6em 0; text-align: justify; }
figure { text-align: center; margin: 1.2em auto; }
figure img { max-width: 100%; height: auto; }
figcaption { font-size: 0.85em; font-style: italic; color: #555; margin-top: 0.3em; }
.footnotes {
    margin-top: 2em;
    border-top: 1px solid #ccc;
    padding-top: 0.8em;
    font-size: 0.82em;
    color: #444;
}
.footnote-item { margin: 0.35em 0; }
sup a { color: inherit; text-decoration: none; border-bottom: 1px dotted #999; }
sup a:hover { border-bottom-color: #333; }
.backlink { font-size: 0.75em; color: #aaa; margin-left: 0.3em; text-decoration: none; }
"""


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def _load_projects() -> list[dict]:
    if PROJECTS_JSON.exists():
        return json.loads(PROJECTS_JSON.read_text(encoding="utf-8")).get("projects", [])
    return []


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def find_project(html_path: Path) -> dict | None:
    """Return the projects.json entry whose source_path stem matches the HTML stem (minus -merged)."""
    html_stem = _nfc(html_path.stem)
    if html_stem.endswith("-merged"):
        html_stem = html_stem[: -len("-merged")]

    book_dir = Path(_nfc(str(html_path.parent)))
    fallback: dict | None = None
    for proj in _load_projects():
        src = _nfc(proj.get("source_path", ""))
        if not src:
            continue
        src_path = Path(src)
        if _nfc(src_path.stem) == html_stem:
            return proj
        if fallback is None and (src_path.parent == book_dir or src_path == book_dir):
            fallback = proj
    return fallback


def _find_pipeline_json(book_dir: Path) -> Path | None:
    candidates = [f for f in book_dir.glob("*.json") if f.name != "book.json"]
    return candidates[0] if len(candidates) == 1 else None


def sync_meta_to_book_json(book_dir: Path, meta: dict) -> None:
    """Write meta dict into the pipeline JSON's top-level 'meta' key."""
    json_path = _find_pipeline_json(book_dir)
    if not json_path:
        print("  Warning: pipeline JSON not found — skipping meta sync", file=sys.stderr)
        return
    data = json.loads(json_path.read_text(encoding="utf-8"))
    data["meta"] = meta
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  Meta synced → {json_path.name}")


_MAX_IMAGE_DIM = 1500


def _resize_to_fit(img: Image.Image, max_dim: int = _MAX_IMAGE_DIM) -> Image.Image:
    w, h = img.size
    if max(w, h) <= max_dim:
        return img
    scale = max_dim / max(w, h)
    return img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)


def prepare_cover(cover_raw: str) -> tuple[bytes, str] | None:
    """Return (image_bytes, file_suffix) for the cover, converting PNG to JPEG if needed.

    The converted JPEG is cached next to the source file so subsequent runs are fast.
    """
    if not cover_raw:
        return None
    cover_path = Path(cover_raw.strip("'\""))
    if not cover_path.exists():
        print(f"  Warning: cover not found at {cover_path}", file=sys.stderr)
        return None
    suffix = cover_path.suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        return cover_path.read_bytes(), ".jpg"
    # Convert to JPEG and cache next to the original
    jpeg_path = cover_path.with_suffix(".jpg")
    if not jpeg_path.exists():
        with Image.open(cover_path) as img:
            _resize_to_fit(img.convert("RGB")).save(jpeg_path, "JPEG", quality=85)
        print(f"  Cover converted → {jpeg_path.name}")
    else:
        print(f"  Cover (cached JPEG): {jpeg_path.name}")
    return jpeg_path.read_bytes(), ".jpg"


# ---------------------------------------------------------------------------
# Illustration images
# ---------------------------------------------------------------------------

def _find_elements_dir(html_path: Path, soup: BeautifulSoup) -> Path | None:
    """Locate the elements folder by inspecting img src attributes."""
    book_dir = html_path.parent
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if "/" in src:
            folder_name = src.split("/")[0]
            candidate = book_dir / folder_name
            if candidate.is_dir():
                return candidate
    for d in sorted(book_dir.iterdir()):
        if d.is_dir() and d.name.endswith(" - elements"):
            return d
    legacy = book_dir / "elements"
    return legacy if legacy.is_dir() else None


def prepare_jpeg_folder(elements_dir: Path, quality: int = 70) -> Path:
    """Convert all PNGs in elements_dir to JPEG and return the jpeg folder path.

    The folder is named <stem> - jpeg and sits next to the elements folder.
    Already-converted files are skipped.
    """
    base = elements_dir.name
    jpeg_name = (base[: -len(" - elements")] + " - jpeg") if base.endswith(" - elements") else (base + " - jpeg")
    jpeg_dir = elements_dir.parent / jpeg_name
    jpeg_dir.mkdir(exist_ok=True)

    pngs = list(elements_dir.glob("*.png"))
    converted = 0
    for png_path in pngs:
        jpeg_path = jpeg_dir / (png_path.stem + ".jpg")
        if not jpeg_path.exists():
            with Image.open(png_path) as img:
                _resize_to_fit(img.convert("RGB")).save(jpeg_path, "JPEG", quality=quality)
            converted += 1

    print(f"  JPEG folder: {jpeg_dir.name}  ({converted} converted, {len(pngs) - converted} cached)")
    return jpeg_dir


def prepare_body(soup: BeautifulSoup, html_path: Path) -> tuple[str, list[tuple[str, bytes]]]:
    """Rewrite img srcs to use JPEGs and collect image bytes for embedding."""
    images: list[tuple[str, bytes]] = []
    seen_imgs: set[str] = set()

    for i, el in enumerate(soup.find_all("div", class_="chapter-title")):
        if not el.get("id"):
            el["id"] = f"ch-{i}"

    sub_i = 0
    for el in soup.find_all("div", class_="subtitle"):
        if not el.get("id"):
            el["id"] = f"sub-{sub_i}"
            sub_i += 1

    elements_dir = _find_elements_dir(html_path, soup)
    jpeg_dir: Path | None = None
    elements_prefix: str | None = None

    if elements_dir and elements_dir.is_dir():
        elements_prefix = elements_dir.name + "/"
        jpeg_dir = prepare_jpeg_folder(elements_dir)

    for img in soup.find_all("img"):
        src = img.get("src", "")
        if not elements_prefix or not src.startswith(elements_prefix):
            continue

        png_filename = src[len(elements_prefix):]
        file_stem = Path(png_filename).stem

        if jpeg_dir:
            jpeg_path = jpeg_dir / (file_stem + ".jpg")
            if jpeg_path.exists():
                epub_filename, disk_path = file_stem + ".jpg", jpeg_path
            else:
                epub_filename, disk_path = png_filename, elements_dir / png_filename
        else:
            epub_filename, disk_path = png_filename, elements_dir / png_filename

        if not disk_path.exists():
            continue

        epub_path = f"images/{epub_filename}"
        img["src"] = f"../images/{epub_filename}"
        if epub_filename not in seen_imgs:
            images.append((epub_path, disk_path.read_bytes()))
            seen_imgs.add(epub_filename)

    return str(soup.find("body")), images


def build_toc(soup: BeautifulSoup) -> list:
    toc = []
    current_chapter: epub.Section | None = None
    current_chapter_anchor: str = ""
    current_children: list[epub.Link] = []

    def _flush():
        nonlocal current_chapter, current_chapter_anchor, current_children
        if current_chapter is None:
            return
        if current_children:
            toc.append((current_chapter, current_children))
        else:
            toc.append(epub.Link(current_chapter.href, current_chapter.title, current_chapter_anchor))
        current_chapter = None
        current_chapter_anchor = ""
        current_children = []

    for el in soup.find_all("div", class_=["chapter-title", "subtitle"]):
        anchor = el.get("id", "")
        title = el.get_text().strip()
        if not anchor or not title:
            continue
        href = f"content.xhtml#{anchor}"
        if "chapter-title" in el.get("class", []):
            _flush()
            current_chapter = epub.Section(title, href)
            current_chapter_anchor = anchor
        else:
            current_children.append(epub.Link(href, title, anchor))

    _flush()
    return toc


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Export linked HTML to EPUB.")
    parser.add_argument("html", help="Path to the linked HTML file")
    parser.add_argument("--out", default=None, help="Output .epub path")
    parser.add_argument("--language", default="ru")
    args = parser.parse_args()

    html_path = Path(args.html)
    if not html_path.exists():
        print(f"HTML file not found: {html_path}", file=sys.stderr)
        sys.exit(1)

    book_dir = html_path.parent

    # --- Metadata from projects.json ---
    project = find_project(html_path)
    if project:
        src_path = Path(project.get("source_path", ""))
        source_pdf = src_path.stem if src_path.suffix else ""
        meta = {
            "title":      project.get("title", ""),
            "author":     project.get("author", ""),
            "year":       project.get("year", ""),
            "cover_path": project.get("cover_path", ""),
            "source_pdf": source_pdf,
        }
        print(f"Metadata from projects.json: {meta['title']}")
        sync_meta_to_book_json(book_dir, meta)
    else:
        print("Warning: project not found in projects.json — EPUB metadata will be empty", file=sys.stderr)
        meta = {}

    title    = meta.get("title") or html_path.stem
    author   = meta.get("author") or "Unknown Author"
    year     = meta.get("year") or ""
    if meta.get("source_pdf"):
        file_stem = meta["source_pdf"]
    elif author != "Unknown Author":
        file_stem = f"{author} - {title}"
    else:
        file_stem = re.sub(r'[^\w\-]', '_', title)

    out_path = Path(args.out) if args.out else book_dir / f"{file_stem}.epub"

    print(f"Parsing {html_path.name}…")
    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")

    body_html, images = prepare_body(soup, html_path)
    toc_links = build_toc(soup)

    print(f"  {len(images)} illustration(s) embedded")
    print(f"  {len(toc_links)} TOC entries")

    # --- Build EPUB ---
    book = epub.EpubBook()
    book.set_identifier(str(uuid.uuid4()))
    book.set_title(title)
    book.set_language(args.language)
    book.add_author(author)
    if year:
        book.add_metadata("DC", "date", year)

    # Cover
    cover_result = prepare_cover(meta.get("cover_path", ""))
    if cover_result:
        cover_bytes, cover_suffix = cover_result
        book.set_cover(f"cover{cover_suffix}", cover_bytes, create_page=True)

    # CSS
    css_item = epub.EpubItem(
        uid="style_main",
        file_name="style/main.css",
        media_type="text/css",
        content=EPUB_CSS.encode(),
    )
    book.add_item(css_item)

    # Content XHTML
    xhtml = f"""<?xml version='1.0' encoding='utf-8'?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{args.language}">
<head>
  <meta charset="utf-8"/>
  <title>{title}</title>
  <link rel="stylesheet" type="text/css" href="../style/main.css"/>
</head>
{body_html}
</html>"""

    content = epub.EpubHtml(title=title, file_name="content.xhtml", lang=args.language)
    content.content = xhtml.encode("utf-8")
    content.add_item(css_item)
    book.add_item(content)

    # Images
    for epub_path, img_bytes in images:
        suffix = Path(epub_path).suffix.lower()
        mime = "image/jpeg" if suffix in (".jpg", ".jpeg") else "image/png"
        item = epub.EpubItem(
            uid=epub_path.replace("/", "_").replace(".", "_"),
            file_name=epub_path,
            media_type=mime,
            content=img_bytes,
        )
        book.add_item(item)

    book.toc = toc_links
    book.spine = ["nav", content]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    epub.write_epub(str(out_path), book)
    print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()
