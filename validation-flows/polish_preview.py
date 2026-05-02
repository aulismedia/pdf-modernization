#!/usr/bin/env python3
"""
Dry-run preview of step6_polish_text.py suggestions for a single page.

Calls the polisher API exactly as step6 would, but prints the suggested
fixes and page_join to the console without modifying the JSON.

Usage:
    python validation-flows/polish_preview.py "/path/to/book.pdf" --page 42
    python validation-flows/polish_preview.py "/path/to/book.pdf" --page 42 --book-name "Title"
"""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from utils.config import OPEN_ROUTER_APIKEY, POLISHER_MODEL, POLISHER_UNRESTRICTED_MODEL, book_dirs
from step6_polish_text import _sort_areas, _first_n_words, _call_polisher_batch


def _preview(text: str, max_len: int = 120) -> str:
    flat = text.replace("\n", "↵ ")
    return flat if len(flat) <= max_len else flat[:max_len] + "…"


def _print_results(batch_items: list, results: list) -> None:
    results_by_id = {r["id"]: r for r in results if isinstance(r, dict) and "id" in r}

    for item in batch_items:
        idx = item["id"]
        result = results_by_id.get(idx)
        label = f"Area {idx} [{item['type']}]"
        if item.get("is_last_main"):
            label += "  ← last main_text"
        print(f"\n{label}")
        print(f"  Text: {_preview(item['text'])!r}")

        if result is None:
            print("  (no fixes)")
            continue

        fixes = result.get("fixes") or []
        if fixes:
            print(f"  Fixes ({len(fixes)}):")
            for f in fixes:
                ftype = f.get("type", "?")
                if ftype == "ocr_hyphen":
                    print(f"    [ocr_hyphen]  {f.get('part1')!r} + {f.get('part2')!r}  →  {f.get('part1', '') + f.get('part2', '')!r}")
                else:
                    print(f"    [{ftype:>8}]  {f.get('find')!r}  →  {f.get('replace')!r}")
        else:
            print("  Fixes: (none)")

        if "page_join" in result:
            print(f"  Page join: {result['page_join']!r}")
            next_prefix = item.get("next_page_prefix", "")
            if next_prefix:
                print(f"    (next page starts: {next_prefix!r})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview polisher suggestions for one page.")
    parser.add_argument("pdf", type=Path, help="Path to the source PDF file")
    parser.add_argument("--page", required=True, type=int, help="1-based page number (e.g. 42 → page0042.png)")
    parser.add_argument("--book-name", default=None)
    args = parser.parse_args()

    if not OPEN_ROUTER_APIKEY:
        print("OPEN_ROUTER_APIKEY not set in .env", file=sys.stderr)
        sys.exit(1)

    dirs = book_dirs(args.pdf.parent)
    json_dir = dirs["json"]

    if args.book_name:
        json_path = json_dir / f"{args.book_name}.json"
        if not json_path.exists():
            print(f"JSON not found: {json_path}", file=sys.stderr)
            sys.exit(1)
    else:
        candidates = [f for f in json_dir.glob("*.json") if f.name != "book.json"]
        if not candidates:
            print(f"No JSON file found in {json_dir}", file=sys.stderr)
            sys.exit(1)
        if len(candidates) > 1:
            print("Multiple JSON files found; use --book-name to select one:")
            for c in candidates:
                print(f"  {c.name}")
            sys.exit(1)
        json_path = candidates[0]

    book_data = json.loads(json_path.read_text(encoding="utf-8"))
    all_pages = book_data.get("pages", [])
    active_pages = [p for p in all_pages if not p.get("ignored")]

    target_stem = f"page{args.page:04d}"
    page_idx = next(
        (i for i, p in enumerate(active_pages)
         if Path(p.get("source_image", "")).stem == target_stem),
        None,
    )
    if page_idx is None:
        print(f"Page {args.page} ({target_stem}) not found in active pages.", file=sys.stderr)
        sys.exit(1)

    page = active_pages[page_idx]
    page_w = page.get("page_dimensions", {}).get("width", 1000)
    sorted_areas = _sort_areas(page.get("areas", []), page_w)

    next_prefix: str | None = None
    if page_idx + 1 < len(active_pages):
        next_page = active_pages[page_idx + 1]
        next_w = next_page.get("page_dimensions", {}).get("width", 1000)
        for a in _sort_areas(next_page.get("areas", []), next_w):
            if a.get("type") == "main_text" and (a.get("text") or "").strip():
                next_prefix = _first_n_words(a["text"])
                break

    last_main: dict | None = None
    for a in reversed(sorted_areas):
        if a.get("type") == "main_text" and (a.get("text") or "").strip():
            last_main = a
            break

    batch_items: list[dict] = []
    for area in sorted_areas:
        atype = area.get("type")
        if atype not in ("main_text", "footnote"):
            continue
        text = (area.get("text") or "").strip()
        if not text:
            continue
        item: dict = {"id": len(batch_items), "type": atype, "text": text}
        if atype == "main_text" and area is last_main:
            item["is_last_main"] = True
            if next_prefix:
                item["next_page_prefix"] = next_prefix
        batch_items.append(item)

    model = POLISHER_UNRESTRICTED_MODEL if page.get("prohibited") else POLISHER_MODEL
    prohibited_flag = "  [PROHIBITED — using unrestricted model]" if page.get("prohibited") else ""

    print(f"\n{'━' * 60}")
    print(f"  Page {args.page}  ({target_stem})  —  {model}{prohibited_flag}")
    print(f"  {len(batch_items)} area(s) to polish")
    print(f"{'━' * 60}")

    if not batch_items:
        print("  No polishable text areas on this page.")
        return

    print("\nCalling API…")
    results = _call_polisher_batch(batch_items, model)

    if results is None:
        print("API call failed — no results.")
        return

    _print_results(batch_items, results)
    print()


if __name__ == "__main__":
    main()
