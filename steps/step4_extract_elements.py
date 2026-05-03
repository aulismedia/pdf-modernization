#!/usr/bin/env python3
"""
Step 4: Crop illustration areas from page images and save them as individual files.

For every page in the book JSON, each area of type "illustration" is cropped
from the original page image using its polygon bounding box and saved to
<elements-dir>/<page_stem>_<illustration_id>.png

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


def _bounding_box(polygon: list[list[int]]) -> tuple[int, int, int, int]:
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    return (min(xs), min(ys), max(xs), max(ys))


def extract_page_elements(
    page_data: dict,
    pages_dir: Path,
    elements_dir: Path,
) -> list[Path]:
    stem = Path(page_data["source_image"]).stem
    img_path = pages_dir / f"{stem}.png"
    if not img_path.exists():
        return []

    img = Image.open(img_path)
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
        # Persist the id back so step7 can reference it
        area["illustration_id"] = illus_id

        box = _bounding_box(polygon)
        cropped = img.crop(box)
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

    total_saved = 0
    for page_data in tqdm(pending, desc="Extracting elements", unit="page"):
        saved = extract_page_elements(page_data, args.pages_dir, args.elements_dir)
        total_saved += len(saved)
        if saved:
            tqdm.write(f"  {page_data['source_image']} → {len(saved)} illustration(s)")

    # Persist any auto-assigned illustration_ids back to JSON
    json_path.write_text(json.dumps(book_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nDone. {total_saved} illustration(s) saved to {args.elements_dir}")


if __name__ == "__main__":
    main()
