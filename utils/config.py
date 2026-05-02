import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

OPEN_ROUTER_APIKEY = os.getenv("OPEN_ROUTER_APIKEY")

OPENROUTER_MODEL = "google/gemini-3.1-flash-image-preview"
POLISHER_MODEL = "google/gemini-2.5-flash"
POLISHER_UNRESTRICTED_MODEL = "qwen/qwen3-235b-a22b"
POLISHER_FALLBACK_MODEL = "mistralai/mistral-large"

EXTRACTION_DPI = 200

AREA_COLORS = {
    "header":                (255, 100, 100),
    "footer":                (255, 150,  50),
    "page_number":           (255, 220,   0),
    "illustration":          ( 50, 180, 255),
    "main_text":             ( 80, 200,  80),
    "illustration_caption":  (  0, 220, 180),
    "footnote":              (180, 100, 255),
    "decoration":            (180, 180, 180),
    "chapter_title":         (255,  60, 200),
}


def book_dirs(book_dir: Path, stem: str = "") -> dict:
    """Return resolved sub-dir paths for a book, all contained within *book_dir*.

    *stem* is the source-file stem (PDF filename without extension).
    When provided, pages and elements get their own named sub-directories so
    multiple source files in the same folder don't collide.
    """
    pages_name    = f"{stem} - pages"    if stem else "pages"
    elements_name = f"{stem} - elements" if stem else "elements"
    return {
        "pages":      book_dir / pages_name,
        "json":       book_dir,
        "validation": book_dir / "validation",
        "elements":   book_dir / elements_name,
        "html":       book_dir / "html",
        "epub":       book_dir / "epub",
    }
