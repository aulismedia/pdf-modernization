#!/usr/bin/env python3
"""
Post-process: consolidate running footnote continuations across page boundaries.

Runs at the end of Cleanup Text (step 6). For each footnote area whose leading
marker is absent from the current page's body-text sup markers, and where there
is an active open footnote group from a preceding page, the area is classified
as a running continuation:

  - Its text is appended to the last active footnote area on the most recent
    page that started a new item (with hyphen-join or space-join).
  - The area is marked  consolidated: true  — kept in areas[] so wrong
    consolidations are recoverable by the user (clear the flag and edit text).
  - The page receives  footnote_consolidated: true.

Also detects the footnote regime (per_page / endnotes / mixed) and writes it
as  footnote_regime  on the book root.

Existing overrides respected:
  is_running_continuation: true  → always consolidate (skip body-marker check)
  is_running_continuation: false → always treat as new item (never consolidate)
  consolidated: true             → already processed; skip on re-run

Usage:
    python3 steps/postprocess_consolidate_footnotes.py <path_to_pdf>
    python3 steps/postprocess_consolidate_footnotes.py <path_to_pdf> --book-name "Title"
    python3 steps/postprocess_consolidate_footnotes.py <path_to_pdf> --dry-run
"""

import argparse
import json
import re
import sys
from pathlib import Path

from utils.config import book_dirs

_USUP_CHARS = "⁰¹²³⁴⁵⁶⁷⁸⁹"
_USUP_TRANS = str.maketrans(_USUP_CHARS, "0123456789")
_USUP_RE = re.compile(rf"[{_USUP_CHARS}]+")
_SUP_TAG_RE = re.compile(r"<sup>(\d+)</sup>", re.IGNORECASE)
_BODY_TYPES = {"main_text", "chapter_title", "subtitle"}

_LEADING_USUP_RE = re.compile(rf"^([{_USUP_CHARS}]+)")
_LEADING_SUP_RE = re.compile(r"^<sup>(\d+)</sup>", re.IGNORECASE)
_LEADING_NUM_RE = re.compile(r"^(\d+)[\.\s]")

_ENTRY_START_RE = re.compile(
    rf"^(?:[{_USUP_CHARS}]+"
    r"|<sup>\d+[a-z]?</sup>"
    r"|\d+[a-z]?[.\s]"
    r")"
)

_NOTES_TITLES = frozenset({
    "notes", "notes on text sources", "notes on sources", "notes to the text",
    "примечания", "примечание", "сноски",
    "endnotes", "end notes", "references", "annotations",
})


def _body_sup_nums(areas: list) -> set[int]:
    """Return set of integer sup markers found in body-text areas."""
    nums: set[int] = set()
    for area in areas:
        if area.get("type") not in _BODY_TYPES:
            continue
        text = area.get("text") or ""
        for m in _SUP_TAG_RE.findall(text):
            nums.add(int(m))
        for m in _USUP_RE.findall(text):
            nums.add(int(m.translate(_USUP_TRANS)))
    return nums


def _leading_marker(text: str) -> int | None:
    """Return the leading integer footnote marker from area text, or None."""
    t = text.strip()
    m = _LEADING_USUP_RE.match(t)
    if m:
        return int(m.group(1).translate(_USUP_TRANS))
    m = _LEADING_SUP_RE.match(t)
    if m:
        return int(m.group(1))
    m = _LEADING_NUM_RE.match(t)
    if m:
        return int(m.group(1))
    return None


def _join_text(base: str, continuation: str) -> str:
    """Append continuation text to base, merging cross-page hyphens."""
    base = base.rstrip()
    cont = continuation.strip()
    if not cont:
        return base
    if base.endswith("-"):
        return base[:-1] + cont
    return base + " " + cont


def split_entries(text: str) -> list[str]:
    """Split a footnote area text block into individual entry strings."""
    lines = text.split("\n")
    entries: list[str] = []
    current: list[str] = []
    for line in lines:
        if _ENTRY_START_RE.match(line.lstrip()) and current:
            entries.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        entries.append("\n".join(current))
    return entries


def detect_regime(pages: list) -> str:
    """Detect whether the book uses per-page footnotes, endnotes, or both."""
    has_footnote_areas = False
    notes_page_indices: list[int] = []
    total_non_ignored = 0
    for i, page in enumerate(pages):
        if page.get("ignored"):
            continue
        total_non_ignored += 1
        for area in page.get("areas") or []:
            if area.get("type") == "footnote" and not area.get("consolidated"):
                has_footnote_areas = True
            if area.get("type") == "chapter_title":
                title = (area.get("text") or "").strip().lower()
                if title in _NOTES_TITLES:
                    notes_page_indices.append(i)
    has_notes_section = bool(notes_page_indices)
    # Inline endnotes: multiple Notes headings distributed throughout the book
    # (more than one, and at least one appears in the first 80% of pages)
    if len(notes_page_indices) > 1 and total_non_ignored > 0:
        cutoff = int(total_non_ignored * 0.8)
        if any(idx < cutoff for idx in notes_page_indices):
            return "inline_endnotes"
    if has_notes_section and has_footnote_areas:
        return "mixed"
    if has_notes_section:
        return "endnotes"
    return "per_page"


def consolidate(data: dict, dry_run: bool = False, verbose: bool = False) -> dict:
    """Walk pages in order and consolidate running footnote continuations.

    Returns stats: {consolidated_areas, consolidated_pages}.
    When dry_run=True, the data dict is NOT mutated.
    """
    pages = data.get("pages", [])

    # Reset page-level flags (will be recomputed)
    if not dry_run:
        for page in pages:
            page.pop("footnote_consolidated", None)

    last_fn_area: dict | None = None  # most recent area that started a new fn item
    consolidated_area_count = 0
    consolidated_page_ids: set[str] = set()

    for page in pages:
        if page.get("ignored"):
            last_fn_area = None
            continue

        areas = [a for a in (page.get("areas") or []) if a]
        fn_areas = [a for a in areas if a.get("type") == "footnote"]

        if not fn_areas:
            # No footnote areas on this page — break the continuation chain
            last_fn_area = None
            continue

        body_nums = _body_sup_nums(areas)
        page_id = page.get("source_image", "?")

        for area in fn_areas:
            # Already consolidated in a prior run — skip to avoid double-appending
            if area.get("consolidated"):
                continue

            text = (area.get("text") or "").strip()
            if not text:
                continue

            override = area.get("is_running_continuation")

            if override is False:
                # Manual override: always a new item
                last_fn_area = area
                continue

            if override is True:
                # Manual override: always a continuation
                is_continuation = last_fn_area is not None
            else:
                # Heuristic: check leading marker against this page's body sups
                marker = _leading_marker(text)
                if marker is None:
                    # No leading marker → likely continuation of open footnote
                    is_continuation = last_fn_area is not None
                else:
                    is_continuation = (marker not in body_nums) and (last_fn_area is not None)

            if is_continuation and last_fn_area is not None:
                if verbose:
                    print(f"  [{page_id}] consolidating area {area.get('id', '?')} → "
                          f"{last_fn_area.get('id', '?')} on {page_id}")
                if not dry_run:
                    last_fn_area["text"] = _join_text(last_fn_area.get("text") or "", text)
                    area["consolidated"] = True
                consolidated_area_count += 1
                consolidated_page_ids.add(page_id)
                # last_fn_area stays — the same footnote may continue on the next page
            else:
                last_fn_area = area

    if not dry_run:
        for page in pages:
            if page.get("source_image") in consolidated_page_ids:
                page["footnote_consolidated"] = True

    return {
        "consolidated_areas": consolidated_area_count,
        "consolidated_pages": len(consolidated_page_ids),
    }


def link_orphan_sups(data: dict, dry_run: bool = False, verbose: bool = False) -> dict:
    """Wrap bare footnote index numbers in <sup> tags in body-text areas.

    Step 2 sometimes misses inline footnote markers that appear directly after
    closing punctuation (e.g. '."2' instead of '."<sup>2</sup>'). This pass:
      1. Collects all expected marker numbers from footnote areas on the page
         (including consolidated areas — their marker may have a bare ref).
      2. Subtracts markers already wrapped as <sup>N</sup> in body areas.
      3. Replaces [closing-quot/paren]N (digit not adjacent to other digits)
         with [closing-quot/paren]<sup>N</sup> in body areas.

    Runs BEFORE consolidate() so body_nums is accurate when classifying
    continuation areas. Returns stats: {fixed_refs, fixed_pages}.
    """
    pages = data.get("pages", [])
    fixed_refs = 0
    fixed_page_ids: set[str] = set()

    for page in pages:
        if page.get("ignored"):
            continue
        areas = page.get("areas") or []
        page_id = page.get("source_image", "?")

        # 1. Collect all expected markers from ALL fn areas (incl. consolidated)
        fn_markers: set[int] = set()
        for area in areas:
            if area.get("type") != "footnote":
                continue
            text = (area.get("text") or "").strip()
            if not text:
                continue
            m = _leading_marker(text)
            if m is not None:
                fn_markers.add(m)
            for entry in split_entries(text)[1:]:
                m2 = _leading_marker(entry)
                if m2 is not None:
                    fn_markers.add(m2)

        if not fn_markers:
            continue

        # 2. Already-wrapped markers in body areas
        wrapped: set[int] = set()
        for area in areas:
            if area.get("type") not in _BODY_TYPES:
                continue
            for m_str in _SUP_TAG_RE.findall(area.get("text") or ""):
                try:
                    wrapped.add(int(m_str))
                except ValueError:
                    pass

        candidates = fn_markers - wrapped
        if not candidates:
            continue

        # 3. Replace [closing-quot/paren]N where N is a standalone digit sequence
        # Period excluded — too many false positives in abbreviations and decimals.
        for area in areas:
            if area.get("type") not in _BODY_TYPES:
                continue
            text = area.get("text") or ""
            original = text
            for n in sorted(candidates, reverse=True):
                pat = re.compile(
                    rf'(?<=["“”‘’»)\]])({re.escape(str(n))})(?!\d)'
                )
                new_text = pat.sub(r'<sup>\1</sup>', text)
                if new_text != text:
                    if verbose:
                        print(f"  [{page_id}] linked bare sup {n} in {area.get('id', '?')}")
                    text = new_text
                    fixed_refs += 1
                    fixed_page_ids.add(page_id)
            if not dry_run and text != original:
                area["text"] = text

    return {"fixed_refs": fixed_refs, "fixed_pages": len(fixed_page_ids)}


def split_packed_areas(data: dict, dry_run: bool = False, verbose: bool = False) -> dict:
    """Split footnote areas containing multiple packed entries into individual areas.

    Runs after consolidate(). Each area whose text yields more than one entry
    via split_entries() is replaced in-place by N separate area dicts — one per
    entry. The original area's id gets a _s1/_s2/… suffix on each child.

    Skips: consolidated: true, is_running_continuation: true.
    Returns stats: {split_areas, split_pages}.
    """
    pages = data.get("pages", [])
    split_area_count = 0
    split_page_ids: set[str] = set()

    for page in pages:
        if page.get("ignored"):
            continue

        areas = page.get("areas") or []
        new_areas: list = []
        page_id = page.get("source_image", "?")
        page_changed = False

        for area in areas:
            if (area.get("type") != "footnote"
                    or area.get("consolidated")
                    or area.get("is_running_continuation") is True):
                new_areas.append(area)
                continue

            text = (area.get("text") or "").strip()
            entries = split_entries(text)

            if len(entries) <= 1:
                new_areas.append(area)
                continue

            original_id = area.get("id") or ""
            for n, entry in enumerate(entries):
                child = dict(area)
                child["text"] = entry.strip()
                child["id"] = f"{original_id}_s{n + 1}" if original_id else ""
                new_areas.append(child)

            if verbose:
                print(f"  [{page_id}] split area {original_id!r} → {len(entries)} entries")

            split_area_count += len(entries) - 1
            split_page_ids.add(page_id)
            page_changed = True

        if page_changed and not dry_run:
            page["areas"] = new_areas

    return {
        "split_areas": split_area_count,
        "split_pages": len(split_page_ids),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Consolidate running footnote continuations across page boundaries."
    )
    parser.add_argument("pdf", help="Path to the source PDF file or book directory")
    parser.add_argument("--book-name", default=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would change without writing to disk")
    parser.add_argument("--verbose", action="store_true",
                        help="Print per-area consolidation decisions")
    args = parser.parse_args()

    src = Path(args.pdf)
    book_dir = src if src.is_dir() else src.parent
    dirs = book_dirs(book_dir, args.book_name or "")

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

    data = json.loads(json_path.read_text(encoding="utf-8"))
    pages = data.get("pages", [])

    regime = detect_regime(pages)
    label = "[dry-run] " if args.dry_run else ""
    print(f"{label}Footnote regime detected: {regime}")

    orphan_stats = link_orphan_sups(data, dry_run=args.dry_run, verbose=args.verbose)
    print(f"{label}Bare sups linked   : {orphan_stats['fixed_refs']} ref(s) "
          f"across {orphan_stats['fixed_pages']} page(s)")

    stats = consolidate(data, dry_run=args.dry_run, verbose=args.verbose)
    print(f"{label}Areas consolidated : {stats['consolidated_areas']}")
    print(f"{label}Pages affected     : {stats['consolidated_pages']}")

    split_stats = split_packed_areas(data, dry_run=args.dry_run, verbose=args.verbose)
    print(f"{label}Packed areas split : {split_stats['split_areas']} extra entries "
          f"across {split_stats['split_pages']} page(s)")

    if args.dry_run:
        return

    # Preserve manually-set regimes that auto-detection can never produce.
    if data.get("footnote_regime") not in ("per_chapter_endnotes", "inline_endnotes"):
        data["footnote_regime"] = regime
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved: {json_path}")


if __name__ == "__main__":
    main()
