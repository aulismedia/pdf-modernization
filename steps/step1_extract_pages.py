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
from utils.rotation_broker import RotationBroker

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".jp2"}

CONTENT_BBOX_PADDING    = 20
CONTENT_BBOX_BOTTOM_SCAN = 150
CONTENT_BORDER_MASK     = 35
PROJECTION_BAND         = 5
ROW_DENSITY_THRESHOLD   = 0.003
COL_DENSITY_THRESHOLD   = 0.002
CONTENT_BBOX_SIDE_SCAN  = 200

DETECT_MAX_DIM = 1500


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
    orig_w, orig_h = img.size
    scale = min(1.0, DETECT_MAX_DIM / max(orig_w, orig_h))
    detect_img = (img.resize((round(orig_w * scale), round(orig_h * scale)), Image.LANCZOS)
                  if scale < 1.0 else img)

    gray = detect_img.convert("L")
    if RotationBroker._is_inverted(gray):
        gray = gray.point(lambda p: 255 - p)

    binary    = RotationBroker._sauvola_binarize(gray)
    hist      = gray.histogram()
    threshold = RotationBroker._otsu_threshold(hist, gray.width * gray.height)

    draw = ImageDraw.Draw(binary)
    w, h = binary.size
    m = max(1, round(CONTENT_BORDER_MASK * scale))
    draw.rectangle([0, 0, w - 1, m],         fill=0)
    draw.rectangle([0, h - m, w - 1, h - 1], fill=0)
    draw.rectangle([0, 0, m, h - 1],         fill=0)
    draw.rectangle([w - m, 0, w - 1, h - 1], fill=0)

    binary = binary.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))

    band     = PROJECTION_BAND
    row_proj = binary.resize((1, h // band), Image.BOX)
    col_proj = binary.resize((w // band, 1), Image.BOX)

    content_rows = [y * band for y, v in enumerate(row_proj.get_flattened_data())
                    if v > ROW_DENSITY_THRESHOLD * 255]
    content_cols = [x * band for x, v in enumerate(col_proj.get_flattened_data())
                    if v > COL_DENSITY_THRESHOLD * 255]

    if not content_rows or not content_cols:
        return None

    top    = content_rows[0]
    bottom = content_rows[-1]
    left   = content_cols[0]
    right  = content_cols[-1]

    raw        = gray.get_flattened_data()
    interior_h = h - 2 * m

    scan_end    = min(h - m, bottom + CONTENT_BBOX_BOTTOM_SCAN)
    true_bottom = bottom
    for y in range(bottom, scan_end):
        row_start = y * w + m
        dark = sum(1 for px in raw[row_start: row_start + w - 2 * m] if px < threshold)
        if dark / max(1, w - 2 * m) > ROW_DENSITY_THRESHOLD:
            true_bottom = y

    true_left = left
    for x in range(max(m, left - CONTENT_BBOX_SIDE_SCAN), left):
        dark = sum(1 for y in range(m, h - m) if raw[y * w + x] < threshold)
        if dark / max(1, interior_h) > COL_DENSITY_THRESHOLD:
            true_left = x
            break

    true_right = right
    for x in range(min(w - m - 1, right + CONTENT_BBOX_SIDE_SCAN), right, -1):
        dark = sum(1 for y in range(m, h - m) if raw[y * w + x] < threshold)
        if dark / max(1, interior_h) > COL_DENSITY_THRESHOLD:
            true_right = x
            break

    detected = (
        max(m, true_left  - CONTENT_BBOX_PADDING),
        max(m, top        - CONTENT_BBOX_PADDING),
        min(w - m, true_right  + CONTENT_BBOX_PADDING),
        min(h - m, true_bottom + CONTENT_BBOX_PADDING),
    )
    if scale < 1.0:
        detected = tuple(round(v / scale) for v in detected)
        detected = (
            max(0, detected[0]),
            max(0, detected[1]),
            min(orig_w, detected[2]),
            min(orig_h, detected[3]),
        )
    return detected


def _upsert_page(
    json_path:    Path,
    source_image: str,
    bbox:         tuple[int, int, int, int],
    rotation:     int   | None = None,
    skew_angle:   float | None = None,
) -> None:
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
    # rotation_applied / skew_angle_applied are metadata only — both transforms are
    # already baked into the saved page image; downstream steps need no correction.
    if rotation:
        page["rotation_applied"] = rotation
    else:
        page.pop("rotation_applied", None)
    if skew_angle is not None:
        page["skew_angle_applied"] = skew_angle
    else:
        page.pop("skew_angle_applied", None)
    sorted_pages = sorted(pages_map.values(), key=lambda p: p["source_image"])
    json_path.write_text(
        json.dumps({**existing, "pages": sorted_pages}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def save_content_preview(img: Image.Image, bbox: tuple[int, int, int, int], out_path: Path) -> None:
    preview = img.copy()
    draw    = ImageDraw.Draw(preview)
    draw.rectangle(bbox, outline=(220, 30, 30), width=4)
    preview.save(str(out_path))


def extract_pages(pdf_path: Path, output_dir: Path, dpi: int, detect_content: bool = False) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)

    doc   = fitz.open(pdf_path)
    total = len(doc)
    print(f"PDF: {pdf_path.name}  |  {total} pages  |  {dpi} DPI")

    zoom      = dpi / 72
    matrix    = fitz.Matrix(zoom, zoom)
    json_path = pdf_path.parent / f"{pdf_path.stem}.json"

    saved = []
    for i, page in enumerate(tqdm(doc, desc="Extracting pages", unit="page")):
        out_path     = output_dir / f"page{i + 1:04d}.png"
        source_image = f"page{i + 1:04d}.png"
        pixmap  = page.get_pixmap(matrix=matrix, alpha=False)
        pil_img = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)

        # PyMuPDF already handles embedded PDF page rotation; only fine skew needed here.
        skew_angle = RotationBroker.detect_skew(pil_img) if detect_content else None
        pil_img    = RotationBroker.apply_to_image(pil_img, rotation=None, skew_angle=skew_angle)

        pil_img.save(str(out_path))
        saved.append(out_path)

        if detect_content:
            bbox = detect_content_bbox(pil_img)
            if bbox:
                save_content_preview(pil_img, bbox, output_dir / f"page{i + 1:04d}-content.png")
            else:
                bbox = (0, 0, pil_img.width, pil_img.height)
            _upsert_page(json_path, source_image, bbox, skew_angle=skew_angle)

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


def process_images_from_folder(
    originals_dir: Path,
    output_dir:    Path,
    json_path:     Path,
    detect_content: bool = False,
) -> list[Path]:
    """Read images from originals_dir, save as page*.png in output_dir, optionally run bbox detection."""
    output_dir.mkdir(parents=True, exist_ok=True)

    images = sorted(
        f for f in originals_dir.iterdir()
        if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS and not f.name.startswith(".")
    )
    print(f"Processing {len(images)} pages from originals/")

    saved = []
    for i, img_path in enumerate(tqdm(images, desc="Processing pages", unit="page")):
        out_path     = output_dir / f"page{i + 1:04d}.png"
        source_image = f"page{i + 1:04d}.png"
        pil_img      = Image.open(str(img_path)).convert("RGB")

        # 1. Orthogonal rotation (Tesseract OSD + projection-profile fallback).
        rotation = RotationBroker.detect_orthogonal_rotation(pil_img)

        # 2. Fine deskew after orientation is corrected.
        pil_img    = RotationBroker.apply_to_image(pil_img, rotation=rotation, skew_angle=None)
        skew_angle = RotationBroker.detect_skew(pil_img)
        pil_img    = RotationBroker.apply_to_image(pil_img, rotation=None, skew_angle=skew_angle)

        pil_img.save(str(out_path))
        saved.append(out_path)

        if detect_content:
            bbox = detect_content_bbox(pil_img)
            if bbox:
                save_content_preview(pil_img, bbox, output_dir / f"page{i + 1:04d}-content.png")
            else:
                bbox = (0, 0, pil_img.width, pil_img.height)
            _upsert_page(json_path, source_image, bbox, rotation, skew_angle)
        elif rotation or skew_angle:
            _upsert_page(json_path, source_image, (0, 0, pil_img.width, pil_img.height), rotation, skew_angle)

    print(f"Saved {len(saved)} images to {output_dir}")
    return saved


def main():
    parser = argparse.ArgumentParser(description="Extract PDF pages or image folder to PNG images.")
    parser.add_argument("pdf", help="Path to the input PDF file or image folder")
    parser.add_argument("--book-dir",  type=Path, default=None)
    parser.add_argument("--book-name", default=None)
    parser.add_argument("--dpi",       type=int, default=EXTRACTION_DPI,
                        help=f"Resolution in DPI (default: {EXTRACTION_DPI})")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--detect-content",   action="store_true", default=False)
    parser.add_argument("--redetect-content", action="store_true", default=False,
                        help="Re-run content detection on already-extracted pages without re-exporting from PDF")
    args = parser.parse_args()

    src = Path(args.pdf)

    if args.redetect_content:
        book_dir  = src if src.is_dir() else src.parent
        stem      = args.book_name or src.stem
        pages_dir = args.output_dir or book_dirs(book_dir, stem)["pages"]
        json_path = book_dir / f"{stem}.json"
        pages = sorted(
            p for p in pages_dir.glob("page*.png")
            if not p.name.endswith("-content.png") and not p.name.endswith("-areas.png")
        )
        if not pages:
            print(f"No page images found in {pages_dir}", file=sys.stderr)
            sys.exit(1)
        print(f"Re-detecting content boundaries for {len(pages)} pages in {pages_dir}")
        for img_path in tqdm(pages, desc="Re-detecting content", unit="page"):
            pil_img = Image.open(str(img_path)).convert("RGB")
            bbox    = detect_content_bbox(pil_img)
            if bbox:
                save_content_preview(pil_img, bbox, pages_dir / f"{img_path.stem}-content.png")
                print(f"  {img_path.name}: left={bbox[0]}, top={bbox[1]}, right={bbox[2]}, bottom={bbox[3]}")
            else:
                bbox = (0, 0, pil_img.width, pil_img.height)
                print(f"  {img_path.name}: no content detected — using full page")
            _upsert_page(json_path, img_path.name, bbox)
        return

    if src.is_dir():
        images       = [f for f in src.iterdir()
                        if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS
                        and not f.name.startswith(".")]
        stem         = args.book_name or src.name
        originals_dir = src / "originals"
        if not images and originals_dir.is_dir():
            originals = originals_dir
        elif images:
            originals = move_to_originals(src)
        else:
            print(f"Error: no images found in {src}", file=sys.stderr)
            sys.exit(1)
        output_dir = args.output_dir or src / f"{stem} - pages"
        json_path  = src / f"{stem}.json"
        process_images_from_folder(originals, output_dir, json_path, detect_content=args.detect_content)
    else:
        if not src.exists():
            print(f"Error: file not found: {src}", file=sys.stderr)
            sys.exit(1)
        book_dir        = args.book_dir or src.parent
        args.output_dir = args.output_dir or book_dirs(book_dir, src.stem)["pages"]
        extract_pages(src, args.output_dir, args.dpi, detect_content=args.detect_content)


if __name__ == "__main__":
    main()
