#!/usr/bin/env python3
"""
Post-process the seamless HTML to add footnote/endnote hyperlinks.

Case 1 — symbolic page footnotes (* **):
  Sequential document-order match: N-th <sup>*</sup> → N-th footnote-item starting with *

Case 2 — numeric chapter endnotes:
  Chapter-scoped match: <sup>12</sup> inside chapter N body →
  paragraph "12. ..." under subtitle "N. ..." in Notes section

Usage:
    python postprocess_footnote_links.py '/path/to/book-merged.html'
    python postprocess_footnote_links.py '/path/to/book-merged.html' --out '/path/to/output.html'
"""

import argparse
import re
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag

_UNICODE_SUP_TRANS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
_UNICODE_SUP_RE = re.compile(r"[⁰¹²³⁴⁵⁶⁷⁸⁹]+")


def normalize_unicode_sups(soup: BeautifulSoup, notes_title) -> int:
    """Replace Unicode superscript digits with <sup>N</sup> tags in main-text paragraphs
    before the Notes section. Returns count of replacements made."""
    body_children = list(soup.body.children)
    cutoff = next((i for i, el in enumerate(body_children) if el is notes_title), len(body_children))
    count = 0
    _TEXT_CLASSES = {"main-text", "chapter-title", "subtitle"}
    for el in body_children[:cutoff]:
        if not isinstance(el, Tag) or not (_TEXT_CLASSES & set(el.get("class") or [])):
            continue
        for text_node in list(el.find_all(string=True)):
            text = str(text_node)
            if not _UNICODE_SUP_RE.search(text):
                continue
            parts = _UNICODE_SUP_RE.split(text)
            sups = _UNICODE_SUP_RE.findall(text)
            for i, part in enumerate(parts):
                if part:
                    text_node.insert_before(NavigableString(part))
                if i < len(sups):
                    sup_tag = soup.new_tag("sup")
                    sup_tag.string = sups[i].translate(_UNICODE_SUP_TRANS)
                    text_node.insert_before(sup_tag)
                    count += 1
            text_node.extract()
    return count


LINK_CSS = """
sup a {
    color: inherit;
    text-decoration: none;
    border-bottom: 1px dotted #999;
}
sup a:hover { border-bottom-color: #333; }
.backlink {
    font-size: 0.75em;
    color: #aaa;
    margin-left: 0.3em;
    text-decoration: none;
}
.backlink:hover { color: #555; }
"""


def _chapter_key(text: str):
    """Extract chapter number or string key from a title like '3. The First Civilizations'."""
    m = re.match(r"^(\d+)\.", text.strip())
    if m:
        return int(m.group(1))
    t = text.strip()
    if t:
        return t  # e.g. "Epilogue"
    return None


def _leading_marker(text: str) -> str | None:
    """Return leading symbolic marker (* **) or None."""
    m = re.match(r"^(\*+)", text.strip())
    return m.group(1) if m else None


def _leading_note_num(text: str) -> int | None:
    """Return leading note number from '12. text...' or None."""
    m = re.match(r"^(\d+)\.\s", text.strip())
    return int(m.group(1)) if m else None


def _leading_numeric_marker(item: Tag) -> int | None:
    """Return leading integer from a per-page footnote item, or None.

    Handles:
      ¹ text...            — Unicode superscript
      1 text...            — ASCII digit + space (OCR artefact, no period)
      <sup>1</sup> text... — sup as first child
      ¹ <sup>¹</sup> text  — mixed (Unicode wins)
    Does NOT match '1. text' (that is Case-2 endnote format).
    """
    text = item.get_text().strip()
    # Unicode superscript digits at start
    m = _UNICODE_SUP_RE.match(text)
    if m:
        return int(m.group(0).translate(_UNICODE_SUP_TRANS))
    # "N. text" — digit(s) + period (previously postprocessed by this script)
    m = re.match(r"^(\d+)\.\s", text)
    if m:
        return int(m.group(1))
    # ASCII digit(s) + space, no period — OCR-mangled footnote marker
    m = re.match(r"^(\d+) ", text)
    if m:
        return int(m.group(1))
    # <sup>N</sup> as first non-whitespace child
    for child in item.children:
        if isinstance(child, NavigableString) and child.strip():
            break  # text before any tag — not a sup-prefixed item
        if isinstance(child, Tag):
            if child.name == "sup" and not child.find("a"):
                t = child.get_text().strip()
                if t.isdigit():
                    return int(t)
            break
    return None


def _is_misclassified_chapter_header(text: str) -> bool:
    """True for paragraphs like '5. Rome' or '11. Imperial enterprises' that should be subtitles.
    Must be short, start with a chapter number, and contain only letters/spaces after the number.
    """
    return bool(re.match(r"^\d{1,2}\.\s+[A-Za-z][A-Za-z\s]+$", text.strip()) and len(text.strip()) < 50)


_LONE_NUM = re.compile(r'^(\d+|[IVXLCDM]+)\.?$')


def merge_split_chapter_titles(soup: BeautifulSoup) -> int:
    """Merge consecutive chapter-title divs where the first is a lone number/Roman numeral.
    e.g. <div>VI.</div><div>Приливы…</div> → <div>VI. Приливы…</div>
    Returns count of merges performed.
    """
    count = 0
    titles = list(soup.find_all("div", class_="chapter-title"))
    i = 0
    while i < len(titles) - 1:
        first = titles[i]
        second = titles[i + 1]
        if not (first.parent and second.parent and first.parent is second.parent):
            i += 1
            continue
        t1 = first.get_text().strip()
        t2 = second.get_text().strip()
        if _LONE_NUM.match(t1) and t2:
            sep = " " if t1.endswith(".") else ". "
            first.string = f"{t1}{sep}{t2}"
            second.decompose()
            titles.pop(i + 1)
            count += 1
        else:
            i += 1
    return count


def absorb_all_caps_subtitles(soup: BeautifulSoup) -> int:
    """Merge an all-caps main-text paragraph that immediately follows a chapter-title into that title.
    e.g. <div class="chapter-title">Отдѣленіе второе.</div>
         <p class="main-text">ЗАМѢЧАНІЯ ОБЪ ОСТРОВАХЪ ВЪ ЧАСТНОСТИ.</p>
    → <div class="chapter-title">Отдѣленіе второе. ЗАМѢЧАНІЯ ОБЪ ОСТРОВАХЪ ВЪ ЧАСТНОСТИ.</div>
    Returns count of absorptions.
    """
    count = 0
    for div in soup.find_all("div", class_="chapter-title"):
        nxt = div.find_next_sibling()
        if not (nxt and isinstance(nxt, Tag) and nxt.name == "p"
                and "main-text" in (nxt.get("class") or [])):
            continue
        text = nxt.get_text().strip()
        letters = re.sub(r"[\s\W]", "", text)
        if not letters or letters != letters.upper():
            continue
        t1 = div.get_text().strip()
        sep = " " if t1.endswith(".") else ". "
        div.string = f"{t1}{sep}{text}"
        nxt.decompose()
        count += 1
    return count


def fix_nested_sups(soup: BeautifulSoup) -> int:
    """Unwrap <sup><sup>N</sup></sup> → <sup>N</sup>. Returns count fixed."""
    count = 0
    for outer in list(soup.find_all("sup")):
        children = [c for c in outer.children if not (isinstance(c, NavigableString) and not c.strip())]
        if len(children) == 1 and isinstance(children[0], Tag) and children[0].name == "sup":
            outer.unwrap()
            count += 1
    return count


def reset_existing_links(soup: BeautifulSoup) -> None:
    """Remove all postprocess-added IDs and links so the script can run idempotently."""
    fn_div = soup.find("div", class_="footnotes")
    if fn_div:
        for item in fn_div.find_all("div", class_="footnote-item"):
            if item.get("id"):
                del item["id"]
            for backlink in item.find_all("a", class_="backlink"):
                backlink.decompose()
    for sup in soup.find_all("sup"):
        if sup.get("id"):
            del sup["id"]
        for a in list(sup.find_all("a")):
            a.unwrap()


_UNICODE_SUP_CHARS = "⁰¹²³⁴⁵⁶⁷⁸⁹"

# Matches any leading footnote marker that _set_item_number should strip:
#   ¹²  — Unicode superscript digit(s) + optional whitespace
#   **  — asterisk(s) + optional whitespace
#   12. — digit(s) + period + optional whitespace  (Case 2 endnote format)
#   1   — digit(s) + single space, no period       (OCR-mangled format)
_MARKER_STRIP_RE = re.compile(
    rf"^(?:[{_UNICODE_SUP_CHARS}]+\s*|\*+\s*|\d+\.\s*|\d+ )"
)


def _set_item_number(item: Tag, n: int) -> None:
    """Replace the item's original footnote marker with the global sequential number N."""
    # Remove a leading unlinked <sup>digit</sup> child (Pattern-B items after split)
    for child in list(item.children):
        if isinstance(child, NavigableString):
            if not child.strip():
                continue
            break
        if isinstance(child, Tag):
            if child.name == "sup" and child.get_text().strip().isdigit() and not child.find("a"):
                child.decompose()
            break

    # Strip the leading marker from the first real text node
    for child in list(item.children):
        if isinstance(child, NavigableString):
            cleaned = _MARKER_STRIP_RE.sub("", str(child)).lstrip()
            child.replace_with(NavigableString(cleaned))
            break
        if isinstance(child, Tag):
            break

    # Prepend "N. " (non-breaking space keeps number and text together)
    item.insert(0, NavigableString(f"{n}. "))
_PACKED_SPLIT_A = re.compile(rf"\n+(?=[{_UNICODE_SUP_CHARS}])")
_PACKED_BACKLINK = re.compile(r'<a[^>]+class=["\']backlink["\'][^>]*>.*?</a>', re.DOTALL)


def split_packed_footnote_items(soup: BeautifulSoup) -> int:
    """Split footnote-item divs that pack multiple sub-footnotes into one element.

    Pattern A — Unicode superscript separators in text:
        ¹ first\\n² second\\n³ third  →  three separate items

    Pattern B — <sup>N</sup> tags used as sub-footnote markers:
        ¹ first<sup>2</sup>second<sup>3</sup>third  →  three separate items

    Returns number of *extra* items created (0 when nothing was split).
    """
    fn_div = soup.find("div", class_="footnotes")
    if not fn_div:
        return 0

    start_re = re.compile(rf"^[{_UNICODE_SUP_CHARS}]")
    created = 0

    for item in list(fn_div.find_all("div", class_="footnote-item")):
        if not start_re.match(item.get_text().strip()):
            continue

        inner = item.decode_contents()
        core = _PACKED_BACKLINK.sub("", inner).strip()

        # Pattern A: split by newline + Unicode superscript character
        parts = _PACKED_SPLIT_A.split(core)
        if len(parts) > 1:
            for part in parts:
                part = part.strip()
                if not part:
                    continue
                new_item = BeautifulSoup(
                    f'<div class="footnote-item">{part}</div>', "html.parser"
                ).find("div")
                item.insert_before(new_item)
                created += 1
            item.decompose()
            continue

        # Pattern B: unlinked <sup>digit</sup> children act as sub-footnote separators
        children = list(item.children)
        sep_indices = [
            j for j, child in enumerate(children)
            if (j > 0
                and isinstance(child, Tag)
                and child.name == "sup"
                and child.get_text().strip().isdigit()
                and not child.find("a"))
        ]
        if not sep_indices:
            continue

        boundaries = [0] + sep_indices + [len(children)]
        for k in range(len(boundaries) - 1):
            seg_children = children[boundaries[k]:boundaries[k + 1]]
            seg_children = [
                c for c in seg_children
                if not (isinstance(c, Tag) and "backlink" in (c.get("class") or []))
            ]
            if not any(str(c).strip() for c in seg_children):
                continue
            new_div = soup.new_tag("div", **{"class": "footnote-item"})
            for child in seg_children:
                new_div.append(child)
            item.insert_before(new_div)
            created += 1

        item.decompose()

    return created


def fix_misclassified_headers(notes_title) -> int:
    """Promote misclassified chapter headers to <div class="subtitle"> in-place."""
    if not notes_title:
        return 0
    count = 0
    for el in notes_title.find_next_siblings():
        if not isinstance(el, Tag):
            continue
        classes = el.get("class") or []
        if "main-text" in classes and _is_misclassified_chapter_header(el.get_text().strip()):
            el.name = "div"
            el["class"] = ["subtitle"]
            count += 1
    return count


def build_maps(soup: BeautifulSoup):
    """Return (fn_items, endnote_map, numeric_fn_items, notes_title)."""

    # --- Locate 'Notes on text sources' chapter-title ---
    notes_title = None
    for el in soup.find_all("div", class_="chapter-title"):
        if el.get_text().strip() == "Notes on text sources":
            notes_title = el
            break

    fn_div = soup.find("div", class_="footnotes")

    # --- Case 1: symbolic footnote map {marker: [el, ...]} ---
    fn_items: dict[str, list[Tag]] = {}
    if fn_div:
        for item in fn_div.find_all("div", class_="footnote-item"):
            marker = _leading_marker(item.get_text())
            if marker:
                fn_items.setdefault(marker, []).append(item)

    # --- Case 2: chapter-scoped endnote map {(chapter_key, note_num): p_element} ---
    endnote_map: dict[tuple, Tag] = {}
    if notes_title:
        current_ch = None
        for el in notes_title.find_next_siblings():
            if not isinstance(el, Tag):
                continue
            classes = el.get("class") or []
            text = el.get_text().strip()
            if "subtitle" in classes or (
                "main-text" in classes and _is_misclassified_chapter_header(text)
            ):
                current_ch = _chapter_key(text)
            elif "main-text" in classes and current_ch is not None:
                num = _leading_note_num(text)
                if num is not None:
                    endnote_map[(current_ch, num)] = el

    # --- Case 3: per-page sequential footnote map {num: [el, ...]} ---
    numeric_fn_items: dict[int, list[Tag]] = {}
    if fn_div:
        for item in fn_div.find_all("div", class_="footnote-item"):
            if _leading_marker(item.get_text()):
                continue  # Case 1 item
            num = _leading_numeric_marker(item)
            if num is not None:
                numeric_fn_items.setdefault(num, []).append(item)

    return fn_items, endnote_map, numeric_fn_items, notes_title


def link_superscripts(
    soup: BeautifulSoup,
    fn_items: dict,
    endnote_map: dict,
    notes_title,
    numeric_fn_items: dict,
):
    """Walk body children up to Notes section, link every <sup> with globally unique numbers."""

    body_children = list(soup.body.children)
    cutoff = len(body_children)
    if notes_title:
        try:
            cutoff = next(i for i, el in enumerate(body_children) if el is notes_title)
        except StopIteration:
            pass

    sym_cursor: dict[str, int] = {}
    numeric_fn_cursors: dict[int, int] = {}
    current_chapter = None
    global_counter = 0
    linked_sym = 0
    linked_num = 0
    linked_paged = 0
    unmatched_sym = []
    unmatched_num = []
    unmatched_paged = []

    for el in body_children[:cutoff]:
        if not isinstance(el, Tag):
            continue

        classes = el.get("class") or []

        if "chapter-title" in classes:
            title_text = el.get_text().strip()
            m = re.match(r"^(\d+)\.", title_text)
            if m:
                current_chapter = int(m.group(1))

        if any(c in classes for c in ("main-text", "chapter-title", "subtitle")):
            for sup in el.find_all("sup"):
                if sup.find("a"):
                    continue  # already linked
                marker = sup.get_text().strip()

                if re.fullmatch(r"\*+", marker):
                    # Case 1: symbolic footnote
                    idx = sym_cursor.get(marker, 0)
                    targets = fn_items.get(marker, [])
                    if idx < len(targets):
                        target = targets[idx]
                        sym_cursor[marker] = idx + 1
                        global_counter += 1
                        sup_id = f"sup-fn-{global_counter}"
                        link_id = f"fn-{global_counter}"
                        sup["id"] = sup_id
                        target["id"] = link_id
                        a = soup.new_tag("a", href=f"#{link_id}")
                        a.string = str(global_counter)
                        sup.clear()
                        sup.append(a)
                        _set_item_number(target, global_counter)
                        back = soup.new_tag("a", href=f"#{sup_id}", **{"class": "backlink"})
                        back.string = "↩"
                        target.append(back)
                        linked_sym += 1
                    else:
                        unmatched_sym.append(marker)

                elif re.fullmatch(r"\d+", marker):
                    note_num = int(marker)
                    case2_matched = False

                    # Case 2: chapter-scoped endnote
                    if current_chapter is not None and endnote_map:
                        key = (current_chapter, note_num)
                        if key in endnote_map:
                            target = endnote_map[key]
                            global_counter += 1
                            sup_id = f"sup-fn-{global_counter}"
                            link_id = f"fn-{global_counter}"
                            target["id"] = link_id
                            sup["id"] = sup_id
                            a = soup.new_tag("a", href=f"#{link_id}")
                            a.string = str(global_counter)
                            sup.clear()
                            sup.append(a)
                            _set_item_number(target, global_counter)
                            back = soup.new_tag("a", href=f"#{sup_id}", **{"class": "backlink"})
                            back.string = "↩"
                            target.append(back)
                            linked_num += 1
                            case2_matched = True
                        else:
                            unmatched_num.append((current_chapter, note_num))

                    # Case 3: per-page sequential footnote
                    if not case2_matched and numeric_fn_items:
                        idx = numeric_fn_cursors.get(note_num, 0)
                        targets = numeric_fn_items.get(note_num, [])
                        if idx < len(targets):
                            target = targets[idx]
                            numeric_fn_cursors[note_num] = idx + 1
                            global_counter += 1
                            sup_id = f"sup-fn-{global_counter}"
                            link_id = f"fn-{global_counter}"
                            sup["id"] = sup_id
                            target["id"] = link_id
                            a = soup.new_tag("a", href=f"#{link_id}")
                            a.string = str(global_counter)
                            sup.clear()
                            sup.append(a)
                            _set_item_number(target, global_counter)
                            back = soup.new_tag("a", href=f"#{sup_id}", **{"class": "backlink"})
                            back.string = "↩"
                            target.append(back)
                            linked_paged += 1
                        else:
                            unmatched_paged.append(note_num)

    return linked_sym, linked_num, linked_paged, unmatched_sym, unmatched_num, unmatched_paged


def sort_footnote_items(soup: BeautifulSoup) -> None:
    """Re-order footnote-item divs inside <div class="footnotes"> by their fn-N id.

    No-id continuation items (multi-paragraph footnotes) are kept attached to
    the preceding id-tagged item so they follow it after sorting.
    """
    fn_div = soup.find("div", class_="footnotes")
    if not fn_div:
        return

    all_items = list(fn_div.find_all("div", class_="footnote-item"))

    # Build groups: (sort_key, [tagged_item, *continuations])
    groups: list[tuple[float, list[Tag]]] = []
    current_key: float = float("inf")
    current_group: list[Tag] = []

    for item in all_items:
        fid = item.get("id", "")
        m = re.match(r"fn-(\d+)$", fid)
        if m:
            if current_group:
                groups.append((current_key, current_group))
            current_key = int(m.group(1))
            current_group = [item]
        else:
            current_group.append(item)  # continuation — stays with preceding item

    if current_group:
        groups.append((current_key, current_group))

    groups.sort(key=lambda g: g[0])

    for _, items in groups:
        for item in items:
            item.extract()

    for _, items in groups:
        for item in items:
            fn_div.append(item)


def inject_css(soup: BeautifulSoup, extra_css: str) -> None:
    style = soup.find("style")
    if style:
        style.string = (style.string or "") + extra_css


def main():
    parser = argparse.ArgumentParser(description="Add footnote/endnote hyperlinks to seamless HTML.")
    parser.add_argument("html", help="Path to the merged HTML file")
    parser.add_argument("--out", default=None, help="Output path (default: overwrite input)")
    args = parser.parse_args()

    html_path = Path(args.html)
    out_path = Path(args.out) if args.out else html_path

    print(f"Parsing {html_path.name}…")
    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")

    reset_existing_links(soup)

    merged = merge_split_chapter_titles(soup)
    if merged:
        print(f"  Merged {merged} split chapter title(s)")

    absorbed = absorb_all_caps_subtitles(soup)
    if absorbed:
        print(f"  Absorbed {absorbed} all-caps subtitle(s) into chapter title(s)")

    fixed_sups = fix_nested_sups(soup)
    if fixed_sups:
        print(f"  Fixed {fixed_sups} double-nested <sup> tag(s)")

    split_count = split_packed_footnote_items(soup)
    if split_count:
        print(f"  Split {split_count} packed footnote item(s) into separate entries")

    fn_items, endnote_map, numeric_fn_items, notes_title = build_maps(soup)
    print(f"  Footnote items (symbolic): {sum(len(v) for v in fn_items.values())} across markers {list(fn_items.keys())}")
    print(f"  Endnote entries (chapter-scoped): {len(endnote_map)} across {len(set(k[0] for k in endnote_map))} chapters")
    total_paged = sum(len(v) for v in numeric_fn_items.values())
    print(f"  Footnote items (per-page numeric): {total_paged} across numbers {sorted(numeric_fn_items.keys())}")

    fixed = fix_misclassified_headers(notes_title)
    if fixed:
        print(f"  Promoted {fixed} misclassified chapter header(s) to subtitle")

    normalized = normalize_unicode_sups(soup, notes_title)
    if normalized:
        print(f"  Normalized {normalized} Unicode superscript(s) to <sup> tags")

    linked_sym, linked_num, linked_paged, unmatched_sym, unmatched_num, unmatched_paged = link_superscripts(
        soup, fn_items, endnote_map, notes_title, numeric_fn_items
    )

    sort_footnote_items(soup)

    inject_css(soup, LINK_CSS)

    out_path.write_text(str(soup), encoding="utf-8")

    print(f"\nLinked {linked_sym} symbolic + {linked_num} chapter-endnotes + {linked_paged} per-page footnotes.")
    if unmatched_sym:
        print(f"  Unmatched symbolic: {len(unmatched_sym)} — {unmatched_sym[:10]}")
    if unmatched_num:
        print(f"  Unmatched chapter-endnotes: {len(unmatched_num)} — {unmatched_num[:10]}")
    if unmatched_paged:
        print(f"  Unmatched per-page: {len(unmatched_paged)} refs had no footnote item — {unmatched_paged[:10]}")
    print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()
