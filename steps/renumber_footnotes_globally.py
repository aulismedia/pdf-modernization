#!/usr/bin/env python3
"""
renumber_footnotes_globally.py

For books where footnote numbering resets at each chapter, this script
assigns document-wide unique sequential integers to all footnote entries,
updating both the leading marker in footnote area text and the matching
<sup>N</sup> / unicode-superscript markers in body text on the same page.

Background: the HTML assembler (step7) merges all footnote areas into a flat
<div class="footnotes"> list.  When chapters restart numbering at 1, footnote
numbers collide and the postprocessor cannot link them.  Pre-numbering in the
JSON (before step7) is the clean fix: the JSON has unambiguous page-level
co-location of marker and item, so renaming both on the same page is safe.

Supported leading-marker formats in footnote area text:
  "5 Р. В. Макарова..."          plain digit(s) + space
  "⁵ Р. В. Макарова..."          unicode superscripts
  "<sup>5</sup> Р. В. ..."       HTML sup tag
  "107a E. Völkl..."             alphanumeric — split correctly, SKIPPED for renumbering

Symbolic markers (* ** ***) are left untouched.

After running this script rebuild with:
    python3 steps/step7_seamless_html.py <book_dir> --book-name "..." --title "..." --lang <lang>
    python3 steps/postprocess_footnote_links.py <path/to/merged.html>

Usage:
    python3 steps/renumber_footnotes_globally.py /path/to/book.json
    python3 steps/renumber_footnotes_globally.py /path/to/book.json --dry-run
    python3 steps/renumber_footnotes_globally.py /path/to/book.json --start 1 --verbose
    python3 steps/renumber_footnotes_globally.py /path/to/book.json --no-backup
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

# ── Unicode superscript helpers ────────────────────────────────────────────────
_USUP_CHARS  = "⁰¹²³⁴⁵⁶⁷⁸⁹"
_USUP_TRANS  = str.maketrans(_USUP_CHARS, "0123456789")
_INT_TO_USUP = str.maketrans("0123456789", _USUP_CHARS)
_USUP_RE     = re.compile(r"[⁰¹²³⁴⁵⁶⁷⁸⁹]+")

_BODY_TYPES = {"main_text", "chapter_title", "subtitle"}

# Detects the start of a new footnote entry (first chars of each line).
# Handles alphanumeric (107a) for splitting even though we skip them in renaming.
_ENTRY_START_RE = re.compile(
    r"^(?:"
    r"[⁰¹²³⁴⁵⁶⁷⁸⁹]+"          # unicode superscripts
    r"|<sup>\d+[a-z]?</sup>"    # HTML sup tag (with optional alpha suffix)
    r"|\d+[a-z]?\s"             # digits + optional alpha + whitespace
    r")"
)


def _usup_to_int(s: str) -> int:
    return int(s.translate(_USUP_TRANS))


def _int_to_usup(n: int) -> str:
    return str(n).translate(_INT_TO_USUP)


def split_entries(text: str) -> list[str]:
    """Split a packed footnote area text into individual entries.

    Entries are separated at lines that begin with a recognised footnote
    marker.  Multi-line citations (a single entry wrapping to the next line)
    are kept intact because those continuation lines don't start with a marker.
    """
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


def parse_leading(entry: str) -> tuple[str, int, str] | None:
    """Extract the leading integer marker from a footnote entry string.

    Returns (original_marker_str, num_int, fmt) or None.
    fmt is one of: 'usup' | 'sup' | 'plain'
    Alphanumeric markers (107a) and symbolic markers (*) return None.
    """
    s = entry.lstrip()

    # Unicode superscripts (⁵, ¹², ...)
    m = _USUP_RE.match(s)
    if m:
        return m.group(0), _usup_to_int(m.group(0)), "usup"

    # <sup>N</sup> (strictly integer, no alpha suffix)
    m = re.match(r"^<sup>(\d+)</sup>", s)
    if m:
        return m.group(0), int(m.group(1)), "sup"

    # Plain digits + whitespace (strictly integer: 107 matches, 107a does not)
    m = re.match(r"^(\d+)(?=\s)", s)
    if m:
        return m.group(1), int(m.group(1)), "plain"

    return None


def _make_marker(new_num: int, fmt: str) -> str:
    if fmt == "usup":
        return _int_to_usup(new_num)
    if fmt == "sup":
        return f"<sup>{new_num}</sup>"
    return str(new_num)


def renumber_entries(text: str, page_map: dict[int, int]) -> tuple[str, list[tuple[int, int]]]:
    """Renumber all integer footnote entries in a footnote area text.

    Returns (new_text, changes) where changes is a list of (orig_num, new_num).
    The marker format (usup / sup / plain) is preserved.
    count=1 in str.replace ensures only the leading marker is replaced even if
    the same digit string appears later in the citation text.
    """
    entries = split_entries(text)
    new_parts: list[str] = []
    changes: list[tuple[int, int]] = []

    for entry in entries:
        result = parse_leading(entry)
        if result is None or result[1] not in page_map:
            new_parts.append(entry)
            continue
        orig_str, orig_num, fmt = result
        new_num = page_map[orig_num]
        new_parts.append(entry.replace(orig_str, _make_marker(new_num, fmt), 1))
        changes.append((orig_num, new_num))

    return "\n".join(new_parts), changes


def renumber_body(text: str, page_map: dict[int, int]) -> tuple[str, list[tuple[int, int]]]:
    """Replace <sup>N</sup> and unicode superscript markers in body text.

    Only pure-integer markers that appear in page_map are replaced.
    Alphanumeric markers (<sup>107a</sup>) are passed through unchanged.
    Returns (new_text, changes).
    """
    changes: list[tuple[int, int]] = []

    def replace_sup(m: re.Match) -> str:
        content = m.group(1)
        if content.isdigit():
            orig = int(content)
            if orig in page_map:
                changes.append((orig, page_map[orig]))
                return f"<sup>{page_map[orig]}</sup>"
        return m.group(0)

    text = re.sub(r"<sup>(\d+[a-z]?)</sup>", replace_sup, text)

    def replace_usup(m: re.Match) -> str:
        orig = _usup_to_int(m.group(0))
        if orig in page_map:
            changes.append((orig, page_map[orig]))
            return _int_to_usup(page_map[orig])
        return m.group(0)

    text = _USUP_RE.sub(replace_usup, text)

    return text, changes


def _body_marker_nums(areas: list) -> set[int]:
    """Collect all integer footnote marker numbers from body-text areas on a page."""
    nums: set[int] = set()
    for area in areas:
        if not area or area.get("type") not in _BODY_TYPES:
            continue
        text = area.get("text") or ""
        for m in re.findall(r"<sup>(\d+)</sup>", text):
            nums.add(int(m))
        for m in _USUP_RE.findall(text):
            nums.add(_usup_to_int(m))
    return nums


def renumber(
    data: dict,
    start: int = 1,
    dry_run: bool = False,
    verbose: bool = False,
) -> tuple[dict, int, int, int]:
    """Walk all pages and renumber footnotes to globally unique integers.

    Only footnote entries whose number also appears as a body marker on the
    SAME page are renumbered.  This correctly handles books that mix per-page
    footnotes (marker and item co-located) with chapter endnotes (items
    collected at the end of a section while markers are on earlier pages):
    endnote items are left with their original numbers because they have no
    co-located marker and the postprocessor already links them via its global
    cursor.

    Returns (data, final_counter, total_fn_changes, total_body_changes).
    When dry_run=True the data dict is NOT mutated (counts are still accurate).
    """
    pages = data["pages"]
    counter    = start - 1
    total_fn   = 0
    total_body = 0

    for page_idx, page in enumerate(pages):
        areas = [a for a in (page.get("areas") or []) if a]

        # Only renumber footnote entries that have a matching body marker on
        # this same page.  Endnote items (no co-located marker) are skipped so
        # the body markers on their earlier pages continue to work unchanged.
        body_nums = _body_marker_nums(areas)

        page_map: dict[int, int] = {}
        for area in areas:
            if area.get("type") != "footnote":
                continue
            for entry in split_entries(area.get("text") or ""):
                result = parse_leading(entry)
                if result and result[1] in body_nums and result[1] not in page_map:
                    counter += 1
                    page_map[result[1]] = counter

        if not page_map:
            continue

        if verbose:
            print(f"  page {page_idx:3d}: {page_map}")

        # Update footnote area texts
        for area in areas:
            if area.get("type") != "footnote":
                continue
            text = area.get("text") or ""
            new_text, changes = renumber_entries(text, page_map)
            if changes:
                total_fn += len(changes)
                if not dry_run:
                    area["text"] = new_text

        # Update body-text <sup> markers
        for area in areas:
            if area.get("type") not in _BODY_TYPES:
                continue
            text = area.get("text") or ""
            new_text, changes = renumber_body(text, page_map)
            if changes:
                total_body += len(changes)
                if not dry_run:
                    area["text"] = new_text

    return data, counter, total_fn, total_body


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("json_path", help="Path to the book JSON file")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would change without writing to disk",
    )
    parser.add_argument(
        "--start", type=int, default=1,
        help="First global footnote number to assign (default: 1)",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print per-page orig→global mapping",
    )
    parser.add_argument(
        "--no-backup", action="store_true",
        help="Skip creating a .json.bak copy before writing",
    )
    args = parser.parse_args()

    json_path = Path(args.json_path)
    if not json_path.exists():
        print(f"Error: {json_path} not found", file=sys.stderr)
        sys.exit(1)

    with json_path.open(encoding="utf-8") as f:
        data = json.load(f)

    data, counter, total_fn, total_body = renumber(
        data,
        start=args.start,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )

    label = "[dry-run] " if args.dry_run else ""
    print(f"{label}Footnote entries renumbered : {total_fn}")
    print(f"{label}Body markers updated        : {total_body}")
    print(f"{label}Highest global number       : {counter}")

    if args.dry_run:
        return

    if not args.no_backup:
        bak = json_path.with_suffix(".json.bak")
        shutil.copy2(json_path, bak)
        print(f"Backup saved                : {bak}")

    with json_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"Saved                       : {json_path}")
    print()
    print("Next steps:")
    print("  python3 steps/step7_seamless_html.py <book_dir> --book-name '...' --title '...' --lang ru")
    print("  python3 steps/postprocess_footnote_links.py <path/to/merged.html>")
    print("Check postprocess output for '0 unmatched'.")


if __name__ == "__main__":
    main()
