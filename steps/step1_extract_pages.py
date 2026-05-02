#!/usr/bin/env python3
"""
Step 1: Extract all pages from a PDF file as individual PNG images.

Usage:
    python step1_extract_pages.py <path_to_pdf> [--dpi 200] [--output-dir output/pages]
"""

import argparse
import sys
from pathlib import Path

import fitz  # PyMuPDF
from tqdm import tqdm

from utils.config import EXTRACTION_DPI, book_dirs


def extract_pages(pdf_path: Path, output_dir: Path, dpi: int) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(pdf_path)
    total = len(doc)
    print(f"PDF: {pdf_path.name}  |  {total} pages  |  {dpi} DPI")

    zoom = dpi / 72  # PyMuPDF default is 72 DPI
    matrix = fitz.Matrix(zoom, zoom)

    saved = []
    for i, page in enumerate(tqdm(doc, desc="Extracting pages", unit="page")):
        out_path = output_dir / f"page{i + 1:04d}.png"
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        pixmap.save(str(out_path))
        saved.append(out_path)

    doc.close()
    print(f"Saved {len(saved)} images to {output_dir}")
    return saved


def main():
    parser = argparse.ArgumentParser(description="Extract PDF pages to PNG images.")
    parser.add_argument("pdf", help="Path to the input PDF file")
    parser.add_argument("--book-dir", type=Path, default=None,
                        help="Root folder for this book; output goes to <book-dir>/pages/")
    parser.add_argument("--dpi", type=int, default=EXTRACTION_DPI,
                        help=f"Resolution in DPI (default: {EXTRACTION_DPI})")
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="Directory to save page images (overrides --book-dir)")
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"Error: file not found: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    book_dir = args.book_dir or pdf_path.parent
    args.output_dir = args.output_dir or book_dirs(book_dir, pdf_path.stem)["pages"]

    extract_pages(pdf_path, args.output_dir, args.dpi)


if __name__ == "__main__":
    main()
