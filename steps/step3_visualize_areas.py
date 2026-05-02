#!/usr/bin/env python3
"""
Step 3 (optional): Draw detected areas on page images for visual validation.

Reads from the single book JSON produced by step 2 and draws colored polygon
borders over the original page images.

Usage:
    python step3_visualize_areas.py <path_to_pdf> [--book-name "Title"]
                                    [--pages-dir output/pages]
                                    [--json-dir output/json]
                                    [--out-dir output/validation]
                                    [--page page0001]   # single page stem
                                    [--alpha 80]        # fill opacity 0-255
"""

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from tqdm import tqdm

from utils.config import AREA_COLORS, book_dirs

LABEL_FONT_SIZE = 18
BORDER_WIDTH = 3


def _get_font(size: int) -> ImageFont.ImageFont:
    for name in ("DejaVuSans.ttf", "Arial.ttf", "Helvetica.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def visualize_page(img_path: Path, page_data: dict, out_path: Path, alpha: int):
    areas = page_data.get("areas", [])

    base = Image.open(img_path).convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw_overlay = ImageDraw.Draw(overlay)
    draw_base = ImageDraw.Draw(base)

    font = _get_font(LABEL_FONT_SIZE)

    for area in areas:
        area_type = area.get("type", "decoration")
        color_rgb = AREA_COLORS.get(area_type, (200, 200, 200))
        color_fill = (*color_rgb, alpha)
        color_border = (*color_rgb, 255)

        polygon = area.get("polygon", [])
        if len(polygon) < 2:
            continue
        flat = [coord for point in polygon for coord in point]

        draw_overlay.polygon(flat, fill=color_fill)

        for i in range(len(polygon)):
            p1 = tuple(polygon[i])
            p2 = tuple(polygon[(i + 1) % len(polygon)])
            draw_base.line([p1, p2], fill=color_border, width=BORDER_WIDTH)

        label = f"{area.get('id', '')} {area_type}"
        lx, ly = polygon[0]
        draw_base.rectangle(
            [lx, ly - LABEL_FONT_SIZE - 2, lx + len(label) * 9, ly],
            fill=(*color_rgb, 220),
        )
        draw_base.text((lx + 2, ly - LABEL_FONT_SIZE - 1), label,
                       fill=(0, 0, 0, 255), font=font)

    composite = Image.alpha_composite(base, overlay).convert("RGB")
    composite.save(out_path)


def main():
    parser = argparse.ArgumentParser(description="Visualize detected page areas.")
    parser.add_argument("pdf", help="Path to the source PDF file")
    parser.add_argument("--book-name", default=None,
                        help="Book JSON filename stem (default: PDF filename stem)")
    parser.add_argument("--pages-dir", type=Path, default=None,
                        help="Page images directory (default: <pdf-dir>/pages/)")
    parser.add_argument("--json-dir", type=Path, default=None,
                        help="JSON input directory (default: <pdf-dir>/)")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="Visualization output directory (default: <pdf-dir>/validation/)")
    parser.add_argument("--page", help="Process a single page by stem (e.g. page0001)")
    parser.add_argument("--alpha", type=int, default=60,
                        help="Fill opacity 0-255 (default: 60)")
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    book_dir = pdf_path.parent
    dirs = book_dirs(book_dir, pdf_path.stem)
    args.pages_dir = args.pages_dir or dirs["pages"]
    args.json_dir  = args.json_dir  or dirs["json"]
    args.out_dir   = args.out_dir   or args.pages_dir

    book_name = args.book_name or pdf_path.stem
    book_json_path = args.json_dir / f"{book_name}.json"

    if not book_json_path.exists():
        print(f"Book JSON not found: {book_json_path}")
        return

    book_data = json.loads(book_json_path.read_text())
    pages_by_stem = {
        Path(p["source_image"]).stem: p
        for p in book_data.get("pages", [])
    }

    if args.page:
        stems = [Path(args.page).stem]
    else:
        stems = sorted(pages_by_stem.keys())

    args.out_dir.mkdir(parents=True, exist_ok=True)

    for stem in tqdm(stems, desc="Visualizing", unit="page"):
        page_data = pages_by_stem.get(stem)
        if page_data is None:
            tqdm.write(f"  No data for {stem}, skipping")
            continue
        img_path = args.pages_dir / f"{stem}.png"
        if not img_path.exists():
            tqdm.write(f"  Missing image: {img_path.name}, skipping")
            continue
        out_path = args.out_dir / f"{stem}-areas.png"
        visualize_page(img_path, page_data, out_path, args.alpha)

    print(f"Saved visualizations to {args.out_dir}")


if __name__ == "__main__":
    main()
