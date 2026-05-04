#!/usr/bin/env python3
"""
Post-process: fix cross-page illustration captions.

Captions containing '<<<' belong to an illustration on the previous page.
Captions containing '>>>' belong to an illustration on the next page.

For each such caption:
  1. Find an unlinked illustration on the target page.
  2. Set linked_illustration_id to that illustration's illustration_id.
  3. Strip '<<<' / '>>>' from the caption text.
  4. Physically move the caption area to the target page.

If no unlinked illustration is found on the target page, the caption is left
unchanged (marker preserved).
"""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _linked_illus_ids_on_page(page: dict) -> set[str]:
    """Return illustration_ids already claimed by captions on this page."""
    return {
        a["linked_illustration_id"]
        for a in page.get("areas", [])
        if a.get("type") == "illustration_caption" and a.get("linked_illustration_id")
    }


def fix_cross_page_captions(pages: list[dict]) -> tuple[int, int]:
    """Fix cross-page captions in-place. Returns (fixed, skipped)."""
    fixed = skipped = 0

    # We may add captions to target pages while iterating, so collect moves first.
    moves: list[tuple[dict, dict, dict, str]] = []  # (caption_area, src_page, dst_page, marker)

    for idx, page in enumerate(pages):
        if page.get("ignored"):
            continue
        for area in page.get("areas", []):
            if area.get("type") != "illustration_caption":
                continue
            text = area.get("text") or ""
            if "<<<" in text:
                marker, target_idx = "<<<", idx - 1
            elif ">>>" in text:
                marker, target_idx = ">>>", idx + 1
            else:
                continue

            if target_idx < 0 or target_idx >= len(pages):
                print(f"  [skip] {page['source_image']} / {area.get('id')}: "
                      f"target page {target_idx} out of range")
                skipped += 1
                continue

            target_page = pages[target_idx]
            if target_page.get("ignored"):
                print(f"  [skip] {page['source_image']} / {area.get('id')}: "
                      f"target page {target_page['source_image']} is ignored")
                skipped += 1
                continue

            moves.append((area, page, target_page, marker))

    for area, src_page, dst_page, marker in moves:
        # Find an unlinked illustration on the destination page.
        already_linked = _linked_illus_ids_on_page(dst_page)
        illustrations = [
            a for a in dst_page.get("areas", [])
            if a.get("type") == "illustration"
            and a.get("illustration_id")
            and a["illustration_id"] not in already_linked
        ]

        if not illustrations:
            print(f"  [skip] {src_page['source_image']} / {area.get('id')}: "
                  f"no unlinked illustration on {dst_page['source_image']}")
            skipped += 1
            continue

        if len(illustrations) > 1:
            print(f"  [warn] {dst_page['source_image']} has {len(illustrations)} unlinked illustrations; "
                  f"linking to {illustrations[0]['illustration_id']}")

        target_illus = illustrations[0]

        # Strip the marker from caption text.
        new_text = (area.get("text") or "").replace(marker, "").strip()
        area["text"] = new_text if new_text else None
        area["linked_illustration_id"] = target_illus["illustration_id"]

        # Move the caption area to the destination page.
        src_page["areas"].remove(area)
        dst_page.setdefault("areas", []).append(area)

        print(f"  [fixed] {src_page['source_image']} / {area.get('id')} "
              f"({marker}) → {dst_page['source_image']} / {target_illus['illustration_id']}")
        fixed += 1

    return fixed, skipped


def main():
    parser = argparse.ArgumentParser(description="Fix cross-page illustration captions.")
    parser.add_argument("json_path", help="Path to the book areas JSON file")
    args = parser.parse_args()

    json_path = Path(args.json_path)
    if not json_path.exists():
        print(f"File not found: {json_path}", file=sys.stderr)
        sys.exit(1)

    data = json.loads(json_path.read_text(encoding="utf-8"))
    pages = data.get("pages", [])

    # Count cross-page captions before fixing
    pending = sum(
        1 for p in pages if not p.get("ignored")
        for a in p.get("areas", [])
        if a.get("type") == "illustration_caption"
        and ("<<<" in (a.get("text") or "") or ">>>" in (a.get("text") or ""))
    )
    if not pending:
        print("No cross-page captions found.")
        return

    print(f"Found {pending} cross-page caption(s) to fix...")
    fixed, skipped = fix_cross_page_captions(pages)

    _write_atomic(json_path, json.dumps(data, ensure_ascii=False, indent=2))
    print(f"\nDone. Fixed: {fixed}  Skipped (no target illustration): {skipped}")
    print(f"Output: {json_path}")


if __name__ == "__main__":
    main()
