#!/usr/bin/env python3
"""
Step 1: Extract all pages from a PDF file (or image folder) as individual PNG images.

Usage:
    python step1_extract_pages.py <path_to_pdf> [--dpi 200] [--output-dir output/pages]
    python step1_extract_pages.py <path_to_folder> [--book-name "Author - Title"]
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image, ImageDraw, ImageFilter
from tqdm import tqdm

from utils.config import EXTRACTION_DPI, book_dirs

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff"}

# Padding added around the detected content bounding box (pixels at export DPI).
CONTENT_BBOX_PADDING = 20

# Border pixels masked out before content detection to eliminate scan edge shadows.
# Must cover the book spine shadow (typically 20–35 px wide) without touching actual content.
CONTENT_BORDER_MASK = 35

# Row/column projection band size (pixels) and minimum dark-pixel density to count as content.
# Lower density = more sensitive but picks up more noise; higher = misses sparse content.
PROJECTION_BAND = 5
ROW_DENSITY_THRESHOLD = 0.005   # ~12 dark px per row at 2309px width
COL_DENSITY_THRESHOLD = 0.003   # ~11 dark px per column at 3828px height


def _otsu_threshold(hist: list[int], total: int) -> int:
    """Compute Otsu's optimal threshold separating two pixel populations."""
    sum_all = sum(i * hist[i] for i in range(256))
    sum_bg, weight_bg, best_var, best_t = 0, 0, 0.0, 0
    for t in range(256):
        weight_bg += hist[t]
        if weight_bg == 0 or weight_bg == total:
            continue
        weight_fg = total - weight_bg
        sum_bg += t * hist[t]
        mean_bg = sum_bg / weight_bg
        mean_fg = (sum_all - sum_bg) / weight_fg
        var = weight_bg * weight_fg * (mean_bg - mean_fg) ** 2
        if var > best_var:
            best_var, best_t = var, t
    return best_t


def _main_cluster_range(positions: list[int], band: int, min_gap: int = 300) -> tuple[int, int]:
    """Return (start, end) of the largest contiguous group, ignoring isolated outliers."""
    positions = sorted(set(positions))
    groups, cur = [], [positions[0]]
    for p in positions[1:]:
        if p - cur[-1] <= min_gap:
            cur.append(p)
        else:
            groups.append(cur)
            cur = [p]
    groups.append(cur)
    best = max(groups, key=len)
    return best[0], best[-1] + band


def detect_content_bbox(img: Image.Image) -> tuple[int, int, int, int] | None:
    gray = img.convert("L")
    hist = gray.histogram()
    threshold = _otsu_threshold(hist, gray.width * gray.height)
    binary = gray.point(lambda p: 0 if p >= threshold else 255)

    # Blank scan edge shadows so they don't extend the bounding box
    draw = ImageDraw.Draw(binary)
    w, h = binary.size
    m = CONTENT_BORDER_MASK
    draw.rectangle([0, 0, w - 1, m], fill=0)
    draw.rectangle([0, h - m, w - 1, h - 1], fill=0)
    draw.rectangle([0, 0, m, h - 1], fill=0)
    draw.rectangle([w - m, 0, w - 1, h - 1], fill=0)

    # Remove isolated artifact pixels (morphological opening: erode then dilate)
    binary = binary.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))

    # Row/column projection: find bands with enough dark pixels to be real content
    band = PROJECTION_BAND
    row_proj = binary.resize((1, h // band), Image.BOX)
    col_proj = binary.resize((w // band, 1), Image.BOX)

    content_rows = [y * band for y, v in enumerate(row_proj.get_flattened_data()) if v > ROW_DENSITY_THRESHOLD * 255]
    content_cols = [x * band for x, v in enumerate(col_proj.get_flattened_data()) if v > COL_DENSITY_THRESHOLD * 255]

    if not content_rows or not content_cols:
        return None

    top, bottom = _main_cluster_range(content_rows, band)
    left, right  = _main_cluster_range(content_cols, band)

    return (
        max(0, left - CONTENT_BBOX_PADDING),
        max(0, top - CONTENT_BBOX_PADDING),
        min(w, right + CONTENT_BBOX_PADDING),
        min(h, bottom + CONTENT_BBOX_PADDING),
    )


def _upsert_content_bbox(json_path: Path, source_image: str, bbox: tuple[int, int, int, int]) -> None:
    existing: dict = {}
    if json_path.exists():
        try:
            existing = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    pages_map = {p["source_image"]: p for p in existing.get("pages", [])}
    page = pages_map.setdefault(source_image, {"source_image": source_image})
    left, top, right, bottom = bbox
    page["content_bbox"] = {"left": left, "top": top, "right": right, "bottom": bottom}
    sorted_pages = sorted(pages_map.values(), key=lambda p: p["source_image"])
    json_path.write_text(
        json.dumps({**existing, "pages": sorted_pages}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def save_content_preview(img: Image.Image, bbox: tuple[int, int, int, int], out_path: Path) -> None:
    preview = img.copy()
    draw = ImageDraw.Draw(preview)
    draw.rectangle(bbox, outline=(220, 30, 30), width=4)
    preview.save(str(out_path))


def extract_pages(pdf_path: Path, output_dir: Path, dpi: int, detect_content: bool = False) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(pdf_path)
    total = len(doc)
    print(f"PDF: {pdf_path.name}  |  {total} pages  |  {dpi} DPI")

    zoom = dpi / 72  # PyMuPDF default is 72 DPI
    matrix = fitz.Matrix(zoom, zoom)
    json_path = pdf_path.parent / f"{pdf_path.stem}.json"

    saved = []
    for i, page in enumerate(tqdm(doc, desc="Extracting pages", unit="page")):
        out_path = output_dir / f"page{i + 1:04d}.png"
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        pixmap.save(str(out_path))
        saved.append(out_path)

        if detect_content:
            pil_img = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
            bbox = detect_content_bbox(pil_img)
            if bbox:
                source_image = f"page{i + 1:04d}.png"
                preview_path = output_dir / f"page{i + 1:04d}-content.png"
                save_content_preview(pil_img, bbox, preview_path)
                _upsert_content_bbox(json_path, source_image, bbox)

    doc.close()
    print(f"Saved {len(saved)} images to {output_dir}")
    return saved


def move_to_originals(src_dir: Path) -> Path:
    """Move top-level images from src_dir into src_dir/originals/, named page0001.ext etc."""
    originals = src_dir / "originals"
    originals.mkdir(parents=True, exist_ok=True)

    images = sorted(
        f for f in src_dir.iterdir()
        if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS and not f.name.startswith(".")
    )
    if not images:
        print("No images found to move.", file=sys.stderr)
        return originals

    print(f"Folder: {src_dir.name}  |  {len(images)} images found")
    for i, img_path in enumerate(tqdm(images, desc="Moving to originals", unit="file")):
        dest = originals / f"page{i + 1:04d}{img_path.suffix.lower()}"
        if not dest.exists():
            shutil.move(str(img_path), str(dest))
    return originals


def process_images_from_folder(originals_dir: Path, output_dir: Path, json_path: Path, detect_content: bool = False) -> list[Path]:
    """Read images from originals_dir, save as page*.png in output_dir, optionally run bbox detection."""
    output_dir.mkdir(parents=True, exist_ok=True)

    images = sorted(
        f for f in originals_dir.iterdir()
        if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS and not f.name.startswith(".")
    )
    print(f"Processing {len(images)} pages from originals/")

    saved = []
    for i, img_path in enumerate(tqdm(images, desc="Processing pages", unit="page")):
        out_path = output_dir / f"page{i + 1:04d}.png"
        pil_img = Image.open(str(img_path)).convert("RGB")
        pil_img.save(str(out_path))
        saved.append(out_path)

        if detect_content:
            bbox = detect_content_bbox(pil_img)
            if bbox:
                source_image = f"page{i + 1:04d}.png"
                preview_path = output_dir / f"page{i + 1:04d}-content.png"
                save_content_preview(pil_img, bbox, preview_path)
                _upsert_content_bbox(json_path, source_image, bbox)

    print(f"Saved {len(saved)} images to {output_dir}")
    return saved


def main():
    parser = argparse.ArgumentParser(description="Extract PDF pages or image folder to PNG images.")
    parser.add_argument("pdf", help="Path to the input PDF file or image folder")
    parser.add_argument("--book-dir", type=Path, default=None,
                        help="Root folder for this book; output goes to <book-dir>/<stem> - pages/")
    parser.add_argument("--book-name", default=None,
                        help="Stem for output directory and JSON in folder mode (default: folder name)")
    parser.add_argument("--dpi", type=int, default=EXTRACTION_DPI,
                        help=f"Resolution in DPI (default: {EXTRACTION_DPI})")
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="Directory to save page images (overrides --book-dir)")
    parser.add_argument("--detect-content", action="store_true", default=False,
                        help="Run content area detection and save bounding boxes (default: off)")
    args = parser.parse_args()

    src = Path(args.pdf)

    if src.is_dir():
        images = [
            f for f in src.iterdir()
            if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS and not f.name.startswith(".")
        ]
        if not images:
            print(f"Error: no images found in {src}", file=sys.stderr)
            sys.exit(1)
        stem = args.book_name or src.name
        originals = move_to_originals(src)
        output_dir = args.output_dir or src / f"{stem} - pages"
        json_path = src / f"{stem}.json"
        process_images_from_folder(originals, output_dir, json_path, detect_content=args.detect_content)
    else:
        if not src.exists():
            print(f"Error: file not found: {src}", file=sys.stderr)
            sys.exit(1)
        book_dir = args.book_dir or src.parent
        args.output_dir = args.output_dir or book_dirs(book_dir, src.stem)["pages"]
        extract_pages(src, args.output_dir, args.dpi, detect_content=args.detect_content)


if __name__ == "__main__":
    main()
