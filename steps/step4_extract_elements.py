#!/usr/bin/env python3
"""
Step 4: Crop illustration areas from page images and save them as individual files.

For every page in the book JSON, each area of type "illustration" is cropped
from the original page image using its polygon bounding box and saved to
<elements-dir>/<page_stem>_<illustration_id>.png

If PyMuPDF is available and the source PDF is accessible, each crop is checked
against the native resolution of the embedded PDF image. If the embedded image's
effective DPI is lower than the render DPI (200), the crop is downscaled to avoid
storing artificially upscaled blurry images.

These files are later referenced by step 5 (HTML) and step 6 (EPUB).

Usage:
    python step4_extract_elements.py <path_to_pdf> [--book-name "Title"]
                                     [--pages-dir ...]
                                     [--elements-dir ...]
                                     [--page page0001]   # single page stem
                                     [--resume]          # skip already-extracted pages
"""

import argparse
import sys
from pathlib import Path

from PIL import Image
from tqdm import tqdm

from utils.config import book_dirs
from utils.rotation_broker import RotationBroker

try:
    import fitz as _fitz
except ImportError:
    _fitz = None

RENDER_DPI = 200
_PTS_PER_INCH = 72.0


def _get_effective_dpi(fitz_page, box_pixels: tuple[int, int, int, int]) -> float | None:
    """Return the effective DPI of the best-matching embedded image for this crop box.

    box_pixels is (x0, y0, x1, y1) in pixel coords at RENDER_DPI.
    Returns None if no embedded image overlaps the area.
    """
    scale = _PTS_PER_INCH / RENDER_DPI  # pixels → PDF points
    x0, y0, x1, y1 = box_pixels
    area_rect = _fitz.Rect(x0 * scale, y0 * scale, x1 * scale, y1 * scale)

    max_dpi: float | None = None

    for img_info in fitz_page.get_images(full=True):
        xref = img_info[0]
        native_w = img_info[2]
        for rect in fitz_page.get_image_rects(xref):
            intersection = area_rect & rect
            if intersection.is_empty or rect.width == 0:
                continue
            eff_dpi = native_w * _PTS_PER_INCH / rect.width
            if max_dpi is None or eff_dpi > max_dpi:
                max_dpi = eff_dpi

    return max_dpi


def _bounding_box(polygon: list[list[int]]) -> tuple[int, int, int, int]:
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    return (min(xs), min(ys), max(xs), max(ys))


def extract_page_elements(
    page_data: dict,
    pages_dir: Path,
    elements_dir: Path,
    fitz_page=None,
) -> list[Path]:
    stem = Path(page_data["source_image"]).stem
    img_path = pages_dir / f"{stem}.png"
    if not img_path.exists():
        return []

    img      = Image.open(img_path)
    rotation = RotationBroker.effective_rotation(page_data)
    saved: list[Path] = []
    auto_idx = 1

    for area in page_data.get("areas", []):
        if area.get("type") != "illustration":
            continue
        polygon = area.get("polygon", [])
        if len(polygon) < 2:
            continue
        illus_id = area.get("illustration_id") or f"illus_{auto_idx:03d}"
        auto_idx += 1
        area["illustration_id"] = illus_id

        box     = RotationBroker.polygon_bbox(polygon)
        cropped = RotationBroker.extract_illustration_crop(img, polygon, rotation)

        # Downscale if the embedded PDF image has lower native resolution
        if fitz_page is not None:
            eff_dpi = _get_effective_dpi(fitz_page, box)
            if eff_dpi is not None and eff_dpi < RENDER_DPI * 0.95:
                factor = eff_dpi / RENDER_DPI
                new_w = max(1, round(cropped.width * factor))
                new_h = max(1, round(cropped.height * factor))
                cropped = cropped.resize((new_w, new_h), Image.LANCZOS)

        out_path = elements_dir / f"{stem}_{illus_id}.png"
        cropped.save(out_path, format="PNG")
        saved.append(out_path)

    return saved


def main():
    parser = argparse.ArgumentParser(description="Extract illustration elements from page images.")
    parser.add_argument("pdf", help="Path to the source PDF file")
    parser.add_argument("--book-name", default=None,
                        help="Book JSON filename stem (default: PDF filename stem)")
    parser.add_argument("--pages-dir", type=Path, default=None,
                        help="Page images directory (default: <pdf-dir>/pages/)")
    parser.add_argument("--elements-dir", type=Path, default=None,
                        help="Output directory for extracted elements (default: <pdf-dir>/elements/)")
    parser.add_argument("--page", default=None,
                        help="Process a single page by stem (e.g. page0009)")
    parser.add_argument("--resume", action="store_true",
                        help="Skip pages whose illustrations are already extracted")
    args = parser.parse_args()

    src = Path(args.pdf)
    book_dir  = src if src.is_dir() else src.parent
    book_name = args.book_name or src.stem
    dirs = book_dirs(book_dir, book_name)
    args.pages_dir    = args.pages_dir    or dirs["pages"]
    args.elements_dir = args.elements_dir or dirs["elements"]

    import json
    json_dir = dirs["json"]
    if args.book_name:
        json_path = json_dir / f"{args.book_name}.json"
        if not json_path.exists():
            print(f"Book JSON not found: {json_path}", file=sys.stderr)
            sys.exit(1)
    else:
        candidates = [f for f in json_dir.glob("*.json") if f.name != "book.json"]
        if not candidates:
            print(f"No JSON file found in {json_dir}", file=sys.stderr)
            sys.exit(1)
        if len(candidates) > 1:
            print("Multiple JSON files found, use --book-name to select one:")
            for c in candidates:
                print(f"  {c.name}")
            sys.exit(1)
        json_path = candidates[0]

    book_data = json.loads(json_path.read_text())
    all_pages = book_data.get("pages", [])

    if args.page:
        stem = Path(args.page).stem
        pages = [p for p in all_pages if Path(p["source_image"]).stem == stem]
    else:
        pages = all_pages

    ignored = [p for p in pages if p.get("ignored")]
    if ignored:
        print(f"Skipping {len(ignored)} ignored page(s).")
    pages = [p for p in pages if not p.get("ignored")]

    if not pages:
        print("No matching pages found.")
        return

    if args.resume:
        def _has_all_elements(page_data: dict) -> bool:
            stem = Path(page_data["source_image"]).stem
            for area in page_data.get("areas", []):
                if area.get("type") == "illustration" and area.get("illustration_id"):
                    if not (args.elements_dir / f"{stem}_{area['illustration_id']}.png").exists():
                        return False
            return True
        pending = [p for p in pages if not _has_all_elements(p)]
        skipped = len(pages) - len(pending)
        if skipped:
            print(f"Skipping {skipped} already-extracted pages.")
    else:
        pending = pages

    if not pending:
        print("Nothing to do.")
        return

    args.elements_dir.mkdir(parents=True, exist_ok=True)

    # Open PDF for native resolution lookup (optional)
    fitz_doc = None
    if _fitz is not None:
        pdf_path = src if src.suffix.lower() == ".pdf" else None
        if pdf_path is None:
            pdf_candidates = list(book_dir.glob("*.pdf"))
            pdf_path = pdf_candidates[0] if len(pdf_candidates) == 1 else None
        if pdf_path and pdf_path.exists():
            fitz_doc = _fitz.open(str(pdf_path))
            print(f"  Resolution check enabled via {pdf_path.name}")
        else:
            print("  Resolution check skipped (PDF not found)")
    else:
        print("  Resolution check skipped (PyMuPDF not installed)")

    total_saved = 0
    for page_data in tqdm(pending, desc="Extracting elements", unit="page"):
        fitz_page = None
        if fitz_doc is not None:
            stem_str = Path(page_data["source_image"]).stem  # e.g. "page0001"
            try:
                page_idx = int(stem_str.lstrip("page").lstrip("0") or "0") - 1
                if 0 <= page_idx < len(fitz_doc):
                    fitz_page = fitz_doc[page_idx]
            except ValueError:
                pass

        saved = extract_page_elements(page_data, args.pages_dir, args.elements_dir, fitz_page)
        total_saved += len(saved)
        if saved:
            tqdm.write(f"  {page_data['source_image']} → {len(saved)} illustration(s)")

    if fitz_doc is not None:
        fitz_doc.close()

    json_path.write_text(json.dumps(book_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nDone. {total_saved} illustration(s) saved to {args.elements_dir}")


if __name__ == "__main__":
    main()
