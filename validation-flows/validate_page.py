#!/usr/bin/env python3
"""
Run steps 2, 3, and 4 for one or more pages by number.

  Step 2 — detect areas & OCR
  Step 3 — visualize areas (saved next to the source image as page<N>-areas.png)
  Step 4 — extract illustrations into <book-dir>/elements/

Usage:
    python validation-flows/validate_page.py --book-dir "/path/to/book" --page 57
    python validation-flows/validate_page.py --book-dir "/path/to/book" --page 9,10,11,12,13
    python validation-flows/validate_page.py --book-dir "/path/to/book" --page 10-20
    python validation-flows/validate_page.py --book-dir "/path/to/book" --page 9,15-20,25
    python validation-flows/validate_page.py --book-dir "/path/to/book" \
        --book-name "Reay Tannahill - Sex In History" --openrouter --page 57
"""

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent


def run(cmd: list[str]) -> None:
    print(f"\n$ {' '.join(str(c) for c in cmd)}\n")
    result = subprocess.run(cmd, cwd=PROJECT_ROOT)
    if result.returncode != 0:
        sys.exit(result.returncode)


def process_page(page_num: int, args: argparse.Namespace) -> None:
    page_stem = f"page{page_num:04d}"
    page_file = f"{page_stem}.png"
    pages_dir = args.book_dir / "pages"

    python = sys.executable

    step2_cmd = [
        python, "step2_detect_areas.py",
        "--book-dir", str(args.book_dir),
        "--page", page_file,
        "--force",
    ]
    if args.book_name:
        step2_cmd += ["--book-name", args.book_name]
    if args.openrouter:
        step2_cmd.append("--openrouter")

    step3_cmd = [
        python, "step3_visualize_areas.py",
        "--book-dir", str(args.book_dir),
        "--out-dir", str(pages_dir),
        "--page", page_stem,
    ]
    if args.book_name:
        step3_cmd += ["--book-name", args.book_name]

    step4_cmd = [
        python, "step4_extract_elements.py",
        "--book-dir", str(args.book_dir),
        "--page", page_stem,
    ]
    if args.book_name:
        step4_cmd += ["--book-name", args.book_name]

    run(step2_cmd)
    run(step3_cmd)
    run(step4_cmd)


def main():
    parser = argparse.ArgumentParser(
        description="Detect, visualize, and extract elements for one or more pages."
    )
    parser.add_argument("--book-dir", type=Path, required=True,
                        help="Root folder for the book")
    parser.add_argument("--book-name", default=None,
                        help="Book name stem for the JSON file (default: book-dir name)")
    parser.add_argument("--openrouter", action="store_true",
                        help="Use OpenRouter backend for step 2")
    parser.add_argument("--page", required=True,
                        help="Page(s) to process: single (57), list (9,10,11), range (10-20), or mixed (9,15-20,25)")
    args = parser.parse_args()

    page_nums = []
    for token in args.page.split(","):
        token = token.strip()
        if "-" in token:
            start, end = token.split("-", 1)
            page_nums.extend(range(int(start), int(end) + 1))
        else:
            page_nums.append(int(token))

    for page_num in page_nums:
        if len(page_nums) > 1:
            print(f"\n{'='*50}\n  Page {page_num}\n{'='*50}")
        process_page(page_num, args)

    pages_dir = args.book_dir / "pages"
    elements_dir = args.book_dir / "elements"
    print(f"\nDone. Processed pages: {', '.join(str(n) for n in page_nums)}")
    print(f"  Visualizations: {pages_dir}")
    print(f"  Elements:       {elements_dir}")


if __name__ == "__main__":
    main()
