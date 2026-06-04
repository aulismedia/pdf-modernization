#!/usr/bin/env python3
"""Unified Flask app for the PDF modernisation pipeline."""

import base64
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path

import requests

from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, send_file, stream_with_context, url_for)

from utils import json_broker
from utils.config import DETECT_WORKERS, POLISH_WORKERS

PROJECT_ROOT = Path(__file__).parent
PROJECTS_FILE = PROJECT_ROOT / "projects.json"
app = Flask(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

_SMART_QUOTE_CHARS = "‘’""′″ʼ"

# ── Footnote review helpers ───────────────────────────────────────────────────

_FN_USUP_CHARS = "⁰¹²³⁴⁵⁶⁷⁸⁹"
_FN_BODY_TYPES = {"main_text", "quote", "chapter_title", "subtitle", "table"}
_FN_SUP_TAG_RE       = re.compile(r"<sup>(\d+)</sup>", re.IGNORECASE)


def _area_sup_text(area: dict) -> str:
    """Return the text to scan for <sup> markers. For tables, use table_html."""
    if area.get("type") == "table":
        return area.get("table_html") or area.get("text") or ""
    return area.get("text") or ""
_FN_SYMBOL_CHARS     = r"*†‡§¶|"
_FN_SYM_SUP_TAG_RE   = re.compile(rf"<sup>([{re.escape(_FN_SYMBOL_CHARS)}]+)</sup>", re.IGNORECASE)
_FN_SYM_LEADER_RE    = re.compile(rf"^([{re.escape(_FN_SYMBOL_CHARS)}]+)\s*")
_FN_USUP_RE    = re.compile(rf"[{_FN_USUP_CHARS}]+")
_FN_ENTRY_START_RE = re.compile(
    rf"^(?:[{_FN_USUP_CHARS}]+"
    r"|<sup>\d+[a-z]?</sup>"
    r"|\d+[a-z]?[.\s]"
    rf"|[{re.escape(_FN_SYMBOL_CHARS)}]+"
    r")"
)
_FN_USUP_MAP = dict(zip("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789"))


def _fn_leading_marker_int(text: str):
    """Return the integer leading marker of a footnote area text, or None."""
    t = (text or "").strip()
    m = re.match(r"^(\d+)[.\s]", t)
    if m:
        return int(m.group(1))
    m = re.match(r"^<sup>(\d+)</sup>", t, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.match(rf"^([{_FN_USUP_CHARS}]+)", t)
    if m:
        return int("".join(_FN_USUP_MAP.get(c, c) for c in m.group(1)))
    return None


def _fn_sequence_gaps(pages: list) -> list:
    """Return flag records for footnote index anomalies within chapters.

    Two kinds of anomaly are detected:

    * "gap"   — a number in 1…max is absent entirely (e.g. fn 5 never appears).
                flag placed after the last page that has fn (gap-1).
    * "order" — a marker appears on a page that comes after a page with a
                higher marker, and it is not a duplicate (e.g. fn 23 appears
                after pages with fn 24-27).
                flag placed after the page where the out-of-order marker appears.

    Each record:
        {after: page_name, before: page_name, missing: [n,…], out_of_order: [n,…]}
    Fields missing/out_of_order may each be empty lists.
    Multiple issues sharing the same (after, before) pair are merged.
    """
    # Build per-page non-consolidated fn marker list
    page_markers: dict[str, list[int]] = {}
    for page in pages:
        if page.get("ignored"):
            continue
        markers = []
        for area in (page.get("areas") or []):
            if area.get("type") == "footnote" and not area.get("consolidated"):
                mk = _fn_leading_marker_int(area.get("text", ""))
                if mk is not None:
                    markers.append(mk)
        page_markers[page["source_image"]] = markers

    all_names = [p["source_image"] for p in pages if not p.get("ignored")]

    # Locate chapter boundaries (pages that contain fn marker 1)
    ch_starts = [i for i, n in enumerate(all_names) if 1 in page_markers.get(n, [])]
    if not ch_starts:
        ch_starts = [0]
    ch_starts.append(len(all_names))

    flags: list[dict] = []

    def _merge(after: str, before: str, *, missing=(), out_of_order=()):
        existing = next(
            (f for f in flags if f["after"] == after and f["before"] == before), None
        )
        if existing:
            existing["missing"].extend(missing)
            existing["out_of_order"].extend(out_of_order)
        else:
            flags.append({
                "after": after, "before": before,
                "missing": list(missing), "out_of_order": list(out_of_order),
            })

    for ci in range(len(ch_starts) - 1):
        ch_page_names = all_names[ch_starts[ci]: ch_starts[ci + 1]]

        # Ordered (marker, page_name) pairs
        seq: list[tuple[int, str]] = []
        for pname in ch_page_names:
            for mk in page_markers.get(pname, []):
                seq.append((mk, pname))

        if not seq:
            continue

        present     = sorted(set(mk for mk, _ in seq))
        max_mk      = present[-1]

        # ── Gap detection (missing numbers in 1…max) ──────────────────────────
        last_page_of: dict[int, str] = {}
        first_page_of: dict[int, str] = {}
        for mk, pname in seq:
            last_page_of[mk] = pname
            if mk not in first_page_of:
                first_page_of[mk] = pname

        for m in sorted(set(range(1, max_mk + 1)) - set(present)):
            prev = max((n for n in present if n < m), default=None)
            nxt  = min((n for n in present if n > m), default=None)
            after  = last_page_of[prev]    if prev is not None else ch_page_names[0]
            before = first_page_of[nxt]    if nxt  is not None else ch_page_names[-1]
            _merge(after, before, missing=[m])

        # ── Out-of-order detection ─────────────────────────────────────────────
        # Scan pages in order; a marker is out-of-order if it is less than the
        # running maximum AND it has not been seen before on an earlier page
        # (that would make it a duplicate, already surfaced in per-page review).
        running_max = 0
        first_seen: set[int] = set()
        for pi, pname in enumerate(ch_page_names):
            markers = sorted(page_markers.get(pname, []))
            oop = [
                mk for mk in markers
                if mk < running_max and mk not in first_seen
            ]
            if oop:
                before = ch_page_names[pi + 1] if pi + 1 < len(ch_page_names) else pname
                _merge(pname, before, out_of_order=oop)
            first_seen.update(markers)
            if markers:
                running_max = max(running_max, max(markers))

    return flags


def _fn_count_sups(areas: list) -> int:
    """Count <sup> footnote references (numeric or symbolic) in body-text areas."""
    count = 0
    for area in areas:
        if area.get("type") not in _FN_BODY_TYPES:
            continue
        text = _area_sup_text(area)
        count += len(_FN_SUP_TAG_RE.findall(text))
        count += len(_FN_USUP_RE.findall(text))
        count += len(_FN_SYM_SUP_TAG_RE.findall(text))
    return count


def _fn_count_sym_sups(areas: list) -> int:
    """Count only symbolic <sup> references (*, †, …) in body-text areas."""
    count = 0
    for area in areas:
        if area.get("type") not in _FN_BODY_TYPES:
            continue
        count += len(_FN_SYM_SUP_TAG_RE.findall(area.get("text") or ""))
    return count


def _fn_count_sym_items(areas: list) -> int:
    """Count footnote entries whose leading marker is a symbol (*, †, …)."""
    count = 0
    for area in areas:
        if area.get("type") != "footnote":
            continue
        if area.get("consolidated") or area.get("is_running_continuation"):
            continue
        text = (area.get("text") or "").lstrip()
        if _FN_SYM_LEADER_RE.match(text):
            count += 1
    return count


_FN_DIGIT_SPACE_RE = re.compile(r"^\d+[a-z]?\s")

def _fn_split_entries(text: str) -> list[str]:
    """Split a footnote area text into individual entry strings.

    Within a symbolic-marker entry (*, †, …) a line that starts with a bare
    digit+space is NOT treated as a new entry — it is a continuation (e.g. a
    wrapped page-number citation like "стр.\\n19 и 325.").
    """
    lines = text.split("\n")
    entries: list[str] = []
    current: list[str] = []
    current_is_symbolic = False
    for line in lines:
        stripped = line.lstrip()
        if _FN_ENTRY_START_RE.match(stripped) and current:
            # Suppress digit+space split while inside a symbolic entry
            if current_is_symbolic and _FN_DIGIT_SPACE_RE.match(stripped):
                current.append(line)
                continue
            entries.append("\n".join(current))
            current = [line]
            current_is_symbolic = bool(_FN_SYM_LEADER_RE.match(stripped))
        else:
            current.append(line)
    if current:
        entries.append("\n".join(current))
    return entries


# ── Page-level normalization (mirrors normalizeAll in review_footnote_page.html) ─

_NORM_USUP_CHARS = "⁰¹²³⁴⁵⁶⁷⁸⁹"
_NORM_USUP_MAP   = str.maketrans(_NORM_USUP_CHARS, "0123456789")
_NORM_USUP_RE    = re.compile(rf"[{_NORM_USUP_CHARS}]+")
_NORM_SYM_LEAD   = re.compile(rf"^[{re.escape(_FN_SYMBOL_CHARS)}]+\s*")
_NORM_BODY_TYPES = {"main_text", "chapter_title", "subtitle"}


def _norm_text_body(text: str) -> str:
    text = _NORM_USUP_RE.sub(
        lambda m: f'<sup>{m.group().translate(_NORM_USUP_MAP)}</sup>', text)
    text = re.sub(r'<sup>(<sup>\d+</sup>)</sup>', r'\1', text, flags=re.I)
    def _expand_range(m):
        f, t = int(m.group(1)), int(m.group(2))
        if f >= t or t - f > 20:
            return m.group(0)
        return ''.join(f'<sup>{i}</sup>' for i in range(f, t + 1))
    text = re.sub(r'<sup>(\d+)[-–](\d+)</sup>', _expand_range, text, flags=re.I)
    return text


def _norm_text_footnote(text: str) -> str:
    text = re.sub(r'^\s*<sup>(\d+)</sup>\s*', r'\1 ', text, flags=re.I)
    text = re.sub(rf'^\s*<sup>([{re.escape(_FN_SYMBOL_CHARS)}]+)</sup>\s*', r'\1 ', text, flags=re.I)
    text = re.sub(rf'^\s*([{_NORM_USUP_CHARS}]+)\s*',
                  lambda m: m.group(1).translate(_NORM_USUP_MAP) + ' ', text)
    text = re.sub(rf'<sup>([{_NORM_USUP_CHARS}]+)</sup>',
                  lambda m: m.group(1).translate(_NORM_USUP_MAP), text, flags=re.I)
    return text


def _normalize_page_areas(areas: list) -> tuple[list, bool]:
    """Apply all safe normalization transforms. Returns (new_areas, changed)."""
    changed = False
    ids_used: set[str] = {a.get('id', '') for a in areas}

    def unique_id(base: str, suffix: str) -> str:
        cand = f'{base}{suffix}'
        n = 2
        while cand in ids_used:
            cand = f'{base}{suffix}_{n}'
            n += 1
        ids_used.add(cand)
        return cand

    # Phase 1: text transforms
    result = []
    for area in areas:
        atype = area.get('type', '')
        text  = area.get('text') or ''
        cons  = area.get('consolidated') or area.get('is_running_continuation')
        if atype in _NORM_BODY_TYPES:
            new_text = _norm_text_body(text)
        elif atype == 'footnote' and not cons:
            new_text = _norm_text_footnote(text)
        else:
            new_text = text
        if new_text != text:
            area = {**area, 'text': new_text}
            changed = True
        result.append(area)
    areas = result

    # Phases 2-6: area splits
    def _split_areas(areas, split_fn, id_suffix):
        nonlocal changed
        i = 0
        while i < len(areas):
            area = areas[i]
            if area.get('type') != 'footnote' or area.get('consolidated') \
                    or area.get('is_running_continuation'):
                i += 1
                continue
            segments = split_fn(area.get('text') or '')
            if len(segments) <= 1:
                i += 1
                continue
            new_areas = [{**area, 'text': segments[0]}]
            for j, seg in enumerate(segments[1:], 2):
                new_areas.append({**area, 'id': unique_id(area['id'], f'{id_suffix}{j}'), 'text': seg})
            areas[i:i + 1] = new_areas
            changed = True
            i += len(new_areas)
        return areas

    # Phase 2: inline <sup>N</sup> separators
    def _split_inline_sup(text):
        t = text.strip()
        m = re.search(r'<sup>\d+</sup>', t, re.I)
        if not m or m.start() == 0:
            return [text]
        parts = re.split(r'(<sup>\d+</sup>)', t, flags=re.I)
        segments, cur = [], ''
        for part in parts:
            if re.match(r'<sup>\d+</sup>', part, re.I):
                if cur.strip():
                    segments.append(cur.strip())
                cur = re.sub(r'<sup>(\d+)</sup>', r'\1 ', part, flags=re.I)
            else:
                cur += part
        if cur.strip():
            segments.append(cur.strip())
        return segments if len(segments) > 1 else [text]

    areas = _split_areas(areas, _split_inline_sup, '_s')

    # Phase 3: unicode sup on new lines
    def _split_usup_lines(text):
        lines = text.split('\n')
        if not any(_NORM_USUP_RE.match(l.lstrip()) for l in lines[1:]):
            return [text]
        segments, cur = [], []
        for line in lines:
            if _NORM_USUP_RE.match(line.lstrip()) and cur:
                segments.append('\n'.join(cur).strip())
                cur = [_NORM_USUP_RE.sub(lambda m: m.group().translate(_NORM_USUP_MAP) + ' ', line, count=1)]
            else:
                cur.append(line)
        if cur:
            segments.append('\n'.join(cur).strip())
        return segments if len(segments) > 1 else [text]

    areas = _split_areas(areas, _split_usup_lines, '_us')

    # Phase 5: plain numeric leaders on subsequent lines
    def _split_num_lines(text):
        lines = text.split('\n')
        if len(lines) <= 1:
            return [text]
        first_num = _fn_leading_marker_int(lines[0])
        if first_num is None:
            return [text]
        if not any((lm := _fn_leading_marker_int(l)) is not None and lm > first_num for l in lines[1:]):
            return [text]
        segments, cur, cur_lead = [], [], first_num
        for line in lines:
            if cur:
                lm = _fn_leading_marker_int(line)
                if lm is not None and lm > cur_lead:
                    segments.append('\n'.join(cur).strip())
                    cur, cur_lead = [line], lm
                    continue
            cur.append(line)
        if cur:
            segments.append('\n'.join(cur).strip())
        return segments if len(segments) > 1 else [text]

    areas = _split_areas(areas, _split_num_lines, '_p')

    # Phase 6: symbolic marker on new lines
    def _split_sym_lines(text):
        lines = text.split('\n')
        if not any(_NORM_SYM_LEAD.match(l.lstrip()) for l in lines[1:]):
            return [text]
        segments, cur = [], []
        for line in lines:
            if cur and _NORM_SYM_LEAD.match(line.lstrip()):
                segments.append('\n'.join(cur).strip())
                cur = [line]
            else:
                cur.append(line)
        if cur:
            segments.append('\n'.join(cur).strip())
        return segments if len(segments) > 1 else [text]

    areas = _split_areas(areas, _split_sym_lines, '_sy')

    return areas, changed


def _fn_count_items(areas: list) -> int:
    """Count new footnote entries starting on a page.

    Skips consolidated and is_running_continuation areas — those belong to a
    previous page's footnote and must not inflate this page's fn_count.
    """
    count = 0
    for area in areas:
        if area.get("type") != "footnote":
            continue
        if area.get("is_running_continuation") is True:
            continue
        if area.get("consolidated"):
            continue
        text = (area.get("text") or "").strip()
        if not text:
            continue
        entries = _fn_split_entries(text)
        count += max(len(entries), 1)
    return count

def _clean_path(value: str) -> str:
    """Strip macOS smart-quote decorations that Terminal pastes around paths."""
    return value.strip(_SMART_QUOTE_CHARS).strip()


# ── Atomic I/O ────────────────────────────────────────────────────────────────

def _write_atomic(path: Path, text: str) -> None:
    """Write text to a temp file then rename — prevents truncation on crash."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# ── Projects DB ───────────────────────────────────────────────────────────────

def _load_projects() -> dict:
    if PROJECTS_FILE.exists():
        data = json.loads(PROJECTS_FILE.read_text(encoding="utf-8"))
        # Migrate legacy pdf_path → source_path
        for p in data["projects"]:
            if "pdf_path" in p and "source_path" not in p:
                p["source_path"] = p.pop("pdf_path")
        return data
    return {"projects": []}


def _save_projects(data: dict) -> None:
    _write_atomic(PROJECTS_FILE, json.dumps(data, ensure_ascii=False, indent=2))


def _get_project(pid: str) -> dict | None:
    return next(
        (p for p in _load_projects()["projects"] if p["id"] == pid), None
    )


# ── Path helpers ──────────────────────────────────────────────────────────────

def _source_path(project: dict) -> Path:
    return Path(project.get("source_path", ""))


def _source_type(project: dict) -> str:
    """Returns 'pdf', 'djvu', or 'folder'."""
    p = _source_path(project)
    if p.is_dir():
        return "folder"
    ext = p.suffix.lower()
    if ext == ".djvu":
        return "djvu"
    return "pdf"


def _book_dir(project: dict) -> Path:
    """Working directory: the folder itself for image-folder projects, parent dir otherwise."""
    p = _source_path(project)
    return p if p.is_dir() else p.parent


def _json_stem(project: dict) -> str:
    """Canonical stem for the areas JSON: source file stem, or folder name for folders."""
    if _source_type(project) == "folder":
        return _source_path(project).name
    return _source_path(project).stem


def _elements_dir(project: dict) -> Path:
    """Return the elements directory, preferring <stem> - elements, falling back to elements/."""
    folder = _book_dir(project)
    stem   = _json_stem(project)
    canonical = folder / f"{stem} - elements"
    if canonical.exists():
        return canonical
    legacy = folder / "elements"
    if legacy.exists():
        return legacy
    return canonical  # not yet created — step4 will make it here


def _pages_dir(project: dict) -> Path:
    """Return the pages directory for a project.

    Layout: ``<book_dir>/<stem> - pages/``
    Legacy fallback: ``<book_dir>/pages/`` (existing data before this convention).
    """
    folder    = _book_dir(project)
    stem      = _json_stem(project)
    canonical = folder / f"{stem} - pages"
    if canonical.exists():
        return canonical
    legacy = folder / "pages"
    if legacy.exists():
        return legacy
    return canonical  # not yet created — step1 will make it here


# Sidecar recovery disabled — sidecar files don't carry user fields (rotation,
# content_bbox, skew_angle, …) so recovery would silently wipe user edits.
# Re-enable only after sidecars are made to mirror the full page dict.
#
# def _try_recover_from_sidecars(project, json_path): ...


def _book_json(project: dict) -> Path | None:
    p = _book_dir(project) / f"{_json_stem(project)}.json"
    if p.exists() and p.stat().st_size > 2:
        return p
    return None


def _write_meta_to_json(project: dict) -> None:
    """Sync project metadata into the areas JSON top-level meta field."""
    book_dir = _book_dir(project)
    if not book_dir.exists():
        return
    json_path = book_dir / f"{_json_stem(project)}.json"
    if not json_path.exists():
        return
    meta = {
        "title":      project.get("title", ""),
        "author":     project.get("author", ""),
        "year":       project.get("year", ""),
        "cover_path": project.get("cover_path", ""),
    }
    def _apply(data: dict) -> None:
        data.setdefault("book", project.get("title", ""))
        data["meta"] = meta
    json_broker.mutate(json_path, _apply)


# ── Pipeline state ────────────────────────────────────────────────────────────

def pipeline_state(project: dict) -> dict:
    src_type     = _source_type(project)
    pages_dir    = _pages_dir(project)
    elements_dir = _elements_dir(project)

    pages    = sorted(
        p for p in (pages_dir.glob("page*.png") if pages_dir.exists() else [])
        if not p.name.endswith("-areas.png")
        and not p.name.endswith("-content.png")
        and not p.name.startswith(".")
    )
    overlays = list(pages_dir.glob("*-areas.png")) if pages_dir.exists() else []

    bj    = _book_json(project)

    area_pages = total_areas = ignored_pages = 0
    processed_pages = 0
    pending_area_pages = 0
    process_later_pending = 0
    table_pages = table_areas_total = table_areas_done = 0
    cross_page_captions = 0
    step2_done = False
    ps = []
    footnote_regime = None
    fn_consolidation_done = False
    fn_review_relevant = 0
    fn_review_mismatched = 0
    fn_review_consolidated = 0
    if bj:
        try:
            bd = json.loads(bj.read_text(encoding="utf-8"))
            ps = bd.get("pages", [])
            area_pages      = sum(1 for p in ps if not p.get("ignored") and "areas" in p)
            total_areas     = sum(len(p.get("areas", [])) for p in ps if not p.get("ignored"))
            ignored_pages   = sum(1 for p in ps if p.get("ignored"))
            processed_pages = area_pages
            step2_done      = processed_pages > 0
            # pending = non-ignored pages in JSON that haven't been processed yet
            pending_area_pages = sum(
                1 for p in ps
                if not p.get("ignored") and "areas" not in p
            )
            process_later_pending = sum(
                1 for p in ps
                if not p.get("ignored")
                and p.get("process_later")
                and not (p.get("detected_by") or "").startswith("opus:later:")
            )
            table_pages = sum(
                1 for p in ps
                if not p.get("ignored")
                and any(a.get("type") == "table" for a in p.get("areas", []))
            )
            table_areas_total = sum(
                1 for p in ps if not p.get("ignored")
                for a in p.get("areas", []) if a.get("type") == "table"
            )
            table_areas_done = sum(
                1 for p in ps if not p.get("ignored")
                for a in p.get("areas", []) if a.get("type") == "table" and a.get("table_html")
            )
            cross_page_captions = sum(
                1 for p in ps if not p.get("ignored")
                for a in p.get("areas", [])
                if a.get("type") == "illustration_caption"
                and ("<<<" in (a.get("text") or "") or ">>>" in (a.get("text") or ""))
            )

            # Footnote review stats
            footnote_regime = bd.get("footnote_regime")
            fn_consolidation_done = footnote_regime is not None
            fn_review_relevant = 0
            fn_review_mismatched = 0
            fn_review_consolidated = 0
            if footnote_regime in ("per_chapter_endnotes", "inline_endnotes"):
                builder = (_build_inline_endnote_data if footnote_regime == "inline_endnotes"
                           else _build_chapter_endnote_data)
                ch_data = builder(bd)
                chapters = ch_data.get("chapters", [])
                fn_review_relevant = len(chapters)
                fn_review_mismatched = sum(1 for c in chapters if c.get("state") != "green")
            else:
                for _p in ps:
                    if _p.get("ignored"):
                        continue
                    _areas = [_a for _a in (_p.get("areas") or []) if _a]
                    _sup = _fn_count_sups(_areas)
                    _fn  = _fn_count_items(_areas)
                    _is_cons = bool(_p.get("footnote_consolidated"))
                    _has_fn  = any(
                        _a.get("type") == "footnote" and not _a.get("consolidated")
                        for _a in _areas
                    )
                    if not (_has_fn or _sup > 0 or _is_cons):
                        continue
                    fn_review_relevant += 1
                    if _is_cons:
                        fn_review_consolidated += 1
                    if footnote_regime == "mixed":
                        if _fn_count_sym_sups(_areas) != _fn_count_sym_items(_areas):
                            fn_review_mismatched += 1
                    elif _sup != _fn:
                        fn_review_mismatched += 1
        except Exception:
            pass

    has_elements = elements_dir.exists() and any(elements_dir.glob("*.png"))

    step6_done = False
    polished_pages = 0
    polished_count = 0
    json_active_pages = 0
    if ps:
        try:
            polished_pages    = sum(1 for p in ps if "page_join" in p)
            polished_count    = sum(1 for p in ps if p.get("polished"))
            json_active_pages = sum(1 for p in ps if not p.get("ignored"))
            polished_count    = max(polished_count, polished_pages)
            step6_done        = polished_count > 0
        except Exception:
            pass

    merged_html = _book_dir(project) / f"{_json_stem(project)}-merged.html"
    step7_done = merged_html.exists()

    return {
        "step1_done":    len(pages) > 0,
        "step2_done":    step2_done,
        "step3_done":    len(overlays) > 0,
        "step4_done":    has_elements,
        "step6_done":    step6_done,
        "step7_done":    step7_done,
        "page_count":    len(pages),
        "overlay_count": len(overlays),
        "area_pages":    area_pages,
        "processed_pages": processed_pages,
        "total_areas":   total_areas,
        "ignored_pages":      ignored_pages,
        "pending_area_pages":    pending_area_pages,
        "process_later_pending": process_later_pending,
        "table_pages":           table_pages,
        "table_areas_total":     table_areas_total,
        "table_areas_done":      table_areas_done,
        "cross_page_captions":   cross_page_captions,
        "seamless_html_file": merged_html.name if step7_done else None,
        "polished_pages":    polished_pages,
        "polished_count":    polished_count,
        "json_active_pages": json_active_pages,
        "source_type":       src_type,
        "footnote_regime":         footnote_regime,
        "fn_consolidation_done":   fn_consolidation_done,
        "fn_review_relevant":      fn_review_relevant,
        "fn_review_mismatched":    fn_review_mismatched,
        "fn_review_consolidated":  fn_review_consolidated,
    }




# ── Background jobs ───────────────────────────────────────────────────────────

_jobs: dict[str, dict] = {}


def _job_append(job: dict, line: str) -> None:
    job["lines"].append(line)


def _run_sequence(pid: str, commands: list[tuple[list[str], str]]) -> None:
    job = _jobs[pid]
    for cmd, label in commands:
        if job.get("cancelled"):
            _job_append(job, "⛔ Stopped by user")
            job["status"] = "error"
            return
        _job_append(job, f"=== {label} ===")
        try:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, cwd=str(PROJECT_ROOT), env=env,
            )
            job["proc"] = proc
            for raw in proc.stdout:
                _job_append(job, raw.rstrip())
            proc.wait()
            job["proc"] = None
            if job.get("cancelled"):
                _job_append(job, "⛔ Stopped by user")
                job["status"] = "error"
                return
            if proc.returncode != 0:
                job["status"]     = "error"
                job["returncode"] = proc.returncode
                return
        except Exception as exc:
            _job_append(job, f"ERROR: {exc}")
            job["status"] = "error"
            return
    job["status"]     = "done"
    job["returncode"] = 0


def _run_step1(pid: str, src: Path, book_name: str | None = None, detect_content: bool = False) -> None:
    cmd = [sys.executable, "steps/step1_extract_pages.py", str(src)]
    if book_name:
        cmd += ["--book-name", book_name]
    if detect_content:
        cmd += ["--detect-content"]
    _run_sequence(pid, [(cmd, "Step 1: Extract Pages")])


def _run_redetect_content(pid: str, src: Path, book_name: str | None = None) -> None:
    cmd = [sys.executable, "-u", "steps/step1_extract_pages.py", str(src), "--redetect-content"]
    if book_name:
        cmd += ["--book-name", book_name]
    _run_sequence(pid, [(cmd, "Re-detect Content Boundaries")])


def _run_step2_with_step3(pid: str, src: Path, book_name: str | None = None, styles: bool = False) -> None:
    """Run step 2; visualization is done inline per page inside step2."""
    cmd = [sys.executable, "-u", "steps/step2_detect_areas.py", str(src)]
    if book_name:
        cmd += ["--book-name", book_name]
    if styles:
        cmd += ["--styles"]
    if DETECT_WORKERS > 1:
        cmd += ["--workers", str(DETECT_WORKERS)]
    _run_sequence(pid, [(cmd, "Step 2: Detect Areas & Visualise")])


def _run_redetect_opus(pid: str, src: Path, page_name: str, book_name: str | None = None, styles: bool = False) -> None:
    cmd = [sys.executable, "-u", "steps/step2_detect_areas.py", str(src),
           "--page", page_name, "--model", "anthropic/claude-opus-4-7", "--force"]
    if book_name:
        cmd += ["--book-name", book_name]
    if styles:
        cmd += ["--styles"]
    _run_sequence(pid, [(cmd, f"Re-detect {page_name} with Opus")])


def _run_redetect_sonnet(pid: str, src: Path, page_name: str, book_name: str | None = None, styles: bool = False) -> None:
    cmd = [sys.executable, "-u", "steps/step2_detect_areas.py", str(src),
           "--page", page_name, "--model", "anthropic/claude-sonnet-4-6", "--force"]
    if book_name:
        cmd += ["--book-name", book_name]
    if styles:
        cmd += ["--styles"]
    _run_sequence(pid, [(cmd, f"Re-detect {page_name} with Sonnet")])


def _run_redetect_default(pid: str, src: Path, page_name: str, book_name: str | None = None, styles: bool = False) -> None:
    cmd = [sys.executable, "-u", "steps/step2_detect_areas.py", str(src),
           "--page", page_name, "--force"]
    if book_name:
        cmd += ["--book-name", book_name]
    if styles:
        cmd += ["--styles"]
    _run_sequence(pid, [(cmd, f"Re-detect {page_name}")])


def _run_process_later_opus(pid: str, src: Path, book_name: str | None = None, styles: bool = False) -> None:
    cmd = [sys.executable, "-u", "steps/step2_detect_areas.py", str(src),
           "--process-later-only"]
    if book_name:
        cmd += ["--book-name", book_name]
    if styles:
        cmd += ["--styles"]
    if DETECT_WORKERS > 1:
        cmd += ["--workers", str(DETECT_WORKERS)]
    _run_sequence(pid, [(cmd, "Process Later pages with Opus")])


def _run_process_tables(pid: str, src: Path, book_name: str | None = None) -> None:
    cmd = [sys.executable, "-u", "steps/step5_process_tables.py", str(src)]
    if book_name:
        cmd += ["--book-name", book_name]
    _run_sequence(pid, [(cmd, "Step 5: Process Tables")])


def _run_fix_captions(pid: str, json_path: Path) -> None:
    cmd = [sys.executable, "-u", "steps/postprocess_cross_page_captions.py", str(json_path)]
    _run_sequence(pid, [(cmd, "Fix cross-page captions")])



def _consolidate_cmd(src: Path, book_name: str | None) -> list[str]:
    cmd = [sys.executable, "-u", "steps/postprocess_consolidate_footnotes.py", str(src)]
    if book_name:
        cmd += ["--book-name", book_name]
    return cmd


def _run_step6(pid: str, src: Path, book_name: str | None = None) -> None:
    step6_cmd = [sys.executable, "-u", "steps/step6_polish_text.py", str(src)]
    if POLISH_WORKERS > 1:
        step6_cmd += ["--workers", str(POLISH_WORKERS)]
    if book_name:
        step6_cmd += ["--book-name", book_name]
    _run_sequence(pid, [
        (step6_cmd, "Step 6: Cleanup Text"),
        (_consolidate_cmd(src, book_name), "Consolidate Footnotes"),
    ])


def _run_step6_resume(pid: str, src: Path, book_name: str | None = None) -> None:
    step6_cmd = [sys.executable, "-u", "steps/step6_polish_text.py", str(src), "--resume"]
    if POLISH_WORKERS > 1:
        step6_cmd += ["--workers", str(POLISH_WORKERS)]
    if book_name:
        step6_cmd += ["--book-name", book_name]
    _run_sequence(pid, [
        (step6_cmd, "Step 6: Cleanup Text (resume)"),
        (_consolidate_cmd(src, book_name), "Consolidate Footnotes"),
    ])


def _run_consolidate_footnotes(pid: str, src: Path, book_name: str | None = None) -> None:
    _run_sequence(pid, [(_consolidate_cmd(src, book_name), "Consolidate Footnotes")])


def _merged_html(project: dict) -> Path:
    return _book_dir(project) / f"{_json_stem(project)}-merged.html"


def _footnote_numbering_resets(project: dict) -> bool:
    """Returns True if more than one page starts a footnote at '1.' — i.e. per-chapter reset."""
    json_path = _book_json(project)
    if not json_path or not json_path.exists():
        return False
    try:
        import re as _re
        data = json.load(json_path.open())
        count = sum(
            1 for p in data.get("pages", [])
            if not p.get("ignored")
            for a in p.get("areas", [])
            if a.get("type") == "footnote"
            and _re.match(r"^1[\. ]", (a.get("text") or "").strip())
        )
        return count > 1
    except Exception:
        return False


def _run_step7(pid: str, project: dict) -> None:
    src = _source_path(project)
    merged_html = _merged_html(project)
    book_name = _json_stem(project)
    step4_cmd = [sys.executable, "steps/step4_extract_elements.py", str(src)]
    step7_cmd = [sys.executable, "-u", "steps/step7_seamless_html.py", str(src)]
    if book_name:
        step4_cmd += ["--book-name", book_name]
        step7_cmd += ["--book-name", book_name]
    commands = [(step4_cmd, "Step 4: Extract Elements")]
    if _footnote_numbering_resets(project):
        json_path = _book_json(project)
        commands.append((
            [sys.executable, "-u", "steps/renumber_footnotes_globally.py", str(json_path)],
            "Pre-process: Renumber Footnotes Globally",
        ))
    commands += [
        (step7_cmd, "Step 7: Build Seamless HTML"),
        ([sys.executable, "-u", "steps/postprocess_footnote_links.py", str(merged_html)],
         "Post-process: Link Footnotes"),
    ]
    _run_sequence(pid, commands)


def _run_step8(pid: str, project: dict) -> None:
    _run_sequence(pid, [
        ([sys.executable, "-u", "steps/step8_export_epub.py", str(_merged_html(project))],
         "Step 8: Export to EPUB"),
    ])


def _run_step9(pid: str, project: dict) -> None:
    _run_sequence(pid, [
        ([sys.executable, "-u", "steps/step9_export_txt.py", str(_merged_html(project))],
         "Step 9: Export to TXT"),
    ])



def _start_job(pid: str, step: str, target, *args) -> None:
    _jobs[pid] = {"status": "running", "step": step, "lines": [], "returncode": None,
                  "cancelled": False, "proc": None}
    threading.Thread(target=target, args=(pid, *args), daemon=True).start()


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    data = _load_projects()
    projects_with_state = []
    for p in data["projects"]:
        try:
            state = pipeline_state(p)
        except Exception:
            state = {}
        projects_with_state.append({**p, "state": state})
    return render_template("dashboard.html", projects=projects_with_state)


@app.route("/projects/new", methods=["POST"])
def create_project():
    title       = request.form.get("title",       "").strip()
    author      = request.form.get("author",      "").strip()
    year        = request.form.get("year",        "").strip()
    source_path = _clean_path(request.form.get("source_path", ""))
    cover_path  = _clean_path(request.form.get("cover_path",  ""))
    if not title or not source_path:
        return redirect(url_for("dashboard"))
    src = Path(source_path)
    if src.is_dir():
        _image_exts = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".jp2"}
        has_images = any(
            f.is_file() and f.suffix.lower() in _image_exts
            for f in src.iterdir()
            if not f.name.startswith(".")
        )
        if not has_images:
            return redirect(url_for("dashboard"))
    detect_content = request.form.get("detect_content") == "1"
    styles        = request.form.get("styles") == "1"
    pid = str(uuid.uuid4())[:8]
    data = _load_projects()
    project = {
        "id":             pid,
        "title":          title,
        "author":         author,
        "year":           year,
        "source_path":    source_path,
        "cover_path":     cover_path,
        "detect_content": detect_content,
        "styles":         styles,
        "created_at":     datetime.utcnow().isoformat(),
    }
    data["projects"].append(project)
    _save_projects(data)
    _write_meta_to_json(project)
    return redirect(url_for("project_view", pid=pid))


@app.route("/projects/open", methods=["POST"])
def open_project():
    json_path = _clean_path(request.form.get("json_path", ""))
    if not json_path:
        return redirect(url_for("dashboard"))
    jp = Path(json_path)
    if not jp.exists() or jp.suffix.lower() != ".json":
        return redirect(url_for("dashboard"))
    try:
        file_data = json.loads(jp.read_text(encoding="utf-8"))
    except Exception:
        return redirect(url_for("dashboard"))
    meta = file_data.get("meta", {})
    title = (meta.get("title") or file_data.get("book") or "").strip()
    if not title:
        return redirect(url_for("dashboard"))
    author     = (meta.get("author") or "").strip()
    year       = (meta.get("year") or "").strip()
    cover_path = (meta.get("cover_path") or "").strip()
    book_dir   = jp.parent
    stem       = jp.stem
    # Derive source_path: PDF/DJVU alongside the JSON, or the folder itself
    source_path = str(book_dir)
    for ext in (".pdf", ".djvu"):
        candidate = book_dir / (stem + ext)
        if candidate.exists():
            source_path = str(candidate)
            break
    # Return existing project if already registered
    existing = _load_projects()
    for p in existing["projects"]:
        if p.get("source_path") == source_path:
            return redirect(url_for("project_view", pid=p["id"]))
    pid = str(uuid.uuid4())[:8]
    project = {
        "id":             pid,
        "title":          title,
        "author":         author,
        "year":           year,
        "source_path":    source_path,
        "cover_path":     cover_path,
        "detect_content": False,
        "styles":         False,
        "created_at":     datetime.utcnow().isoformat(),
    }
    existing["projects"].append(project)
    _save_projects(existing)
    return redirect(url_for("project_view", pid=pid))


@app.route("/projects/<pid>/edit", methods=["POST"])
def edit_project(pid: str):
    project = _get_project(pid)
    if not project:
        return "Project not found", 404
    data = _load_projects()
    for p in data["projects"]:
        if p["id"] == pid:
            p["title"]          = request.form.get("title",       "").strip() or p["title"]
            p["author"]         = request.form.get("author",      "").strip()
            p["year"]           = request.form.get("year",        "").strip()
            p["source_path"]    = _clean_path(request.form.get("source_path", "")) or p.get("source_path", "")
            p["cover_path"]     = _clean_path(request.form.get("cover_path",  ""))
            p["detect_content"] = request.form.get("detect_content") == "1"
            p["styles"]         = request.form.get("styles") == "1"
            break
    _save_projects(data)
    _write_meta_to_json(_get_project(pid))

    regime = request.form.get("footnote_regime", "")
    allowed = {"", "per_page", "endnotes", "per_chapter_endnotes", "inline_endnotes", "mixed"}
    if regime in allowed:
        bj = _book_json(_get_project(pid))
        if bj and bj.exists():
            def _set_regime(data: dict) -> None:
                if regime:
                    data["footnote_regime"] = regime
                else:
                    data.pop("footnote_regime", None)
            json_broker.mutate(bj, _set_regime)

    redirect_to = request.form.get("redirect", "dashboard")
    if redirect_to == "project":
        return redirect(url_for("project_view", pid=pid))
    return redirect(url_for("dashboard"))


@app.route("/projects/<pid>")
def project_view(pid: str):
    project = _get_project(pid)
    if not project:
        return "Project not found", 404
    state = pipeline_state(project)
    job   = _jobs.get(pid, {})
    return render_template("project.html", project=project, state=state, job=job)


@app.route("/projects/<pid>/delete", methods=["POST"])
def delete_project(pid: str):
    data = _load_projects()
    data["projects"] = [p for p in data["projects"] if p["id"] != pid]
    _save_projects(data)
    return redirect(url_for("dashboard"))


@app.route("/projects/<pid>/reset", methods=["POST"])
def reset_project(pid: str):
    import shutil
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404

    pages_dir    = _pages_dir(project)
    elements_dir = _elements_dir(project)
    book_json    = _book_dir(project) / f"{_json_stem(project)}.json"

    if pages_dir.exists():
        shutil.rmtree(pages_dir)
    if elements_dir.exists():
        shutil.rmtree(elements_dir)
    if book_json.exists():
        book_json.unlink()

    # Remove merged/seamless HTML outputs
    book_dir = _book_dir(project)
    stem     = _json_stem(project)
    for pattern in (f"{stem}-merged.html", f"{stem}_seamless.html", f"{stem}_*.html"):
        for f in book_dir.glob(pattern):
            f.unlink()

    return redirect(url_for("project_view", pid=pid))


# ── Step runners ──────────────────────────────────────────────────────────────

@app.route("/projects/<pid>/run/step1", methods=["POST"])
def run_step1(pid: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    src = _source_path(project)
    book_name = _json_stem(project)
    detect_content = bool(project.get("detect_content", False))
    _start_job(pid, "step1", _run_step1, src, book_name, detect_content)
    return jsonify({"ok": True})


@app.route("/projects/<pid>/run/redetect-content", methods=["POST"])
def run_redetect_content(pid: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    src = _source_path(project)
    book_name = _json_stem(project)
    _start_job(pid, "redetect-content", _run_redetect_content, src, book_name)
    return jsonify({"ok": True})


@app.route("/projects/<pid>/run/step2", methods=["POST"])
def run_step2(pid: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    src = _source_path(project)
    book_name = _json_stem(project)
    styles = bool(project.get("styles", False))
    _start_job(pid, "step2+3", _run_step2_with_step3, src, book_name, styles)
    return jsonify({"ok": True})


@app.route("/projects/<pid>/run/redetect-opus/<page_name>", methods=["POST"])
def run_redetect_opus(pid: str, page_name: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    src = _source_path(project)
    book_name = _json_stem(project)
    styles = bool(project.get("styles", False))
    _start_job(pid, "redetect-opus", _run_redetect_opus, src, page_name, book_name, styles)
    return jsonify({"ok": True})


@app.route("/projects/<pid>/run/redetect-sonnet/<page_name>", methods=["POST"])
def run_redetect_sonnet(pid: str, page_name: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    src = _source_path(project)
    book_name = _json_stem(project)
    styles = bool(project.get("styles", False))
    _start_job(pid, "redetect-sonnet", _run_redetect_sonnet, src, page_name, book_name, styles)
    return jsonify({"ok": True})


@app.route("/projects/<pid>/run/redetect/<page_name>", methods=["POST"])
def run_redetect_default(pid: str, page_name: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    src = _source_path(project)
    book_name = _json_stem(project)
    styles = bool(project.get("styles", False))
    _start_job(pid, "redetect", _run_redetect_default, src, page_name, book_name, styles)
    return jsonify({"ok": True})


@app.route("/projects/<pid>/run/process-later-opus", methods=["POST"])
def run_process_later_opus(pid: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    src = _source_path(project)
    book_name = _json_stem(project)
    styles = bool(project.get("styles", False))
    _start_job(pid, "process-later-opus", _run_process_later_opus, src, book_name, styles)
    return jsonify({"ok": True})


@app.route("/projects/<pid>/run/fix-captions", methods=["POST"])
def run_fix_captions(pid: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    json_path = _book_dir(project) / f"{_json_stem(project)}.json"
    if not json_path.exists():
        return jsonify({"error": "no areas JSON"}), 400
    _start_job(pid, "fix-captions", _run_fix_captions, json_path)
    return jsonify({"ok": True})


@app.route("/projects/<pid>/run/process-tables", methods=["POST"])
def run_process_tables(pid: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    src = _source_path(project)
    book_name = _json_stem(project)
    _start_job(pid, "process-tables", _run_process_tables, src, book_name)
    return jsonify({"ok": True})


@app.route("/projects/<pid>/run/step6", methods=["POST"])
def run_step6(pid: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    src = _source_path(project)
    book_name = _json_stem(project)
    _start_job(pid, "step6", _run_step6, src, book_name)
    return jsonify({"ok": True})


@app.route("/projects/<pid>/run/step6-resume", methods=["POST"])
def run_step6_resume(pid: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    src = _source_path(project)
    book_name = _json_stem(project)
    _start_job(pid, "step6", _run_step6_resume, src, book_name)
    return jsonify({"ok": True})


@app.route("/projects/<pid>/run/consolidate-footnotes", methods=["POST"])
def run_consolidate_footnotes(pid: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    src = _source_path(project)
    book_name = _json_stem(project)
    _start_job(pid, "consolidate-footnotes", _run_consolidate_footnotes, src, book_name)
    return jsonify({"ok": True})


@app.route("/projects/<pid>/run/step7", methods=["POST"])
def run_step7(pid: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    _start_job(pid, "step7", _run_step7, project)
    return jsonify({"ok": True})


@app.route("/projects/<pid>/run/step8", methods=["POST"])
def run_step8(pid: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    _start_job(pid, "step8", _run_step8, project)
    return jsonify({"ok": True})


@app.route("/projects/<pid>/run/step9", methods=["POST"])
def run_step9(pid: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    _start_job(pid, "step9", _run_step9, project)
    return jsonify({"ok": True})



# ── SSE job stream ────────────────────────────────────────────────────────────

@app.route("/projects/<pid>/stream")
def stream_job(pid: str):
    job = _jobs.get(pid)
    if not job:
        return Response(
            "data: [DONE status=idle]\n\n",
            mimetype="text/event-stream",
        )

    def generate():
        sent = 0
        while True:
            lines = job["lines"]
            while sent < len(lines):
                yield f"data: {lines[sent]}\n\n"
                sent += 1
            status = job.get("status")
            if status in ("done", "error") and sent >= len(job["lines"]):
                yield f"data: [DONE status={status}]\n\n"
                break
            time.sleep(0.15)
            yield ": keepalive\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/projects/<pid>/state")
def project_state_api(pid: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    return jsonify(pipeline_state(project))


@app.route("/projects/<pid>/status")
def job_status(pid: str):
    job = _jobs.get(pid, {})
    return jsonify({
        "status":     job.get("status", "idle"),
        "step":       job.get("step", ""),
        "line_count": len(job.get("lines", [])),
    })


@app.route("/projects/<pid>/stop", methods=["POST"])
def stop_job(pid: str):
    job = _jobs.get(pid)
    if not job or job.get("status") != "running":
        return jsonify({"ok": False, "error": "no running job"})
    job["cancelled"] = True
    proc = job.get("proc")
    if proc:
        proc.kill()
    return jsonify({"ok": True})


# ── Area editor API ───────────────────────────────────────────────────────────

@app.route("/projects/<pid>/review")
def review(pid: str):
    project = _get_project(pid)
    if not project:
        return "Project not found", 404
    state = pipeline_state(project)
    return render_template("review.html", project=project, state=state)


@app.route("/projects/<pid>/review-footnotes")
def review_footnotes(pid: str):
    project = _get_project(pid)
    if not project:
        return "Project not found", 404
    state = pipeline_state(project)
    return render_template("review_footnotes.html", project=project, state=state)


@app.route("/projects/<pid>/review-footnotes/page")
def review_footnote_page(pid: str):
    project = _get_project(pid)
    if not project:
        return "Project not found", 404
    return render_template("review_footnote_page.html", project=project)


@app.route("/projects/<pid>/api/footnote-review")
def api_footnote_review(pid: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    bj = _book_json(project)
    if not bj or not bj.exists():
        return jsonify({"error": "no JSON"}), 404

    book_data = json.loads(bj.read_text(encoding="utf-8"))
    regime = book_data.get("footnote_regime")
    pages = book_data.get("pages", [])

    page_rows = []
    mismatched = 0
    consolidated_count = 0

    for page in pages:
        if page.get("ignored"):
            continue
        areas = [a for a in (page.get("areas") or []) if a]
        sup_count = _fn_count_sups(areas)
        fn_count  = _fn_count_items(areas)
        is_cons   = bool(page.get("footnote_consolidated"))
        has_fn    = any(
            a.get("type") == "footnote" and not a.get("consolidated")
            for a in areas
        )

        if regime == "mixed":
            sym_sup = _fn_count_sym_sups(areas)
            sym_fn  = _fn_count_sym_items(areas)
            matched = (sym_sup == sym_fn)
            active  = has_fn or sup_count > 0 or is_cons
        elif regime in ("per_chapter_endnotes", "inline_endnotes"):
            sym_sup = sym_fn = None
            matched = True   # page-level matching is meaningless; chapter review handles it
            active  = sup_count > 0 or is_cons
        else:
            sym_sup = sym_fn = None
            matched = (sup_count == fn_count)
            active  = has_fn or sup_count > 0 or is_cons

        if active and not matched:
            mismatched += 1
        if is_cons:
            consolidated_count += 1

        # Determine display state
        if not active:
            state = "none"
        elif regime == "mixed":
            # Only numeric sups present, no local fn → treat as endnote refs
            if sym_sup == 0 and sym_fn == 0 and not is_cons:
                state = "endnote"
            elif not matched:
                state = "red"
            else:
                state = "green"
        elif (regime in ("endnotes", "per_chapter_endnotes", "inline_endnotes")
                and sup_count > 0 and not has_fn and not is_cons):
            state = "endnote"
        elif not matched:
            state = "red"
        else:
            state = "green"

        if is_cons and not has_fn and sup_count == 0:
            badge = "continuation"
        elif not active:
            badge = ""
        elif regime == "mixed" and sym_sup is not None:
            badge = f"{sym_sup}↑·{sym_fn}fn" if (sym_sup or sym_fn) else f"{sup_count}↑·{fn_count}fn"
        elif regime in ("per_chapter_endnotes", "inline_endnotes"):
            badge = f"{sup_count}↑" if sup_count else ""
        else:
            badge = f"{sup_count}↑\xb7{fn_count}fn"

        page_rows.append({
            "name":                 page.get("source_image", ""),
            "sup_count":            sup_count,
            "fn_count":             fn_count,
            "footnote_consolidated": is_cons,
            "matched":              matched,
            "state":                state,
            "badge":                badge,
        })

    # Gap detection only makes sense when footnote numbering is sequential across
    # pages. For per_page regime numbers reset on every page, so cross-page gap
    # analysis produces meaningless results (e.g. "1–223 are lost" on one flag).
    seq_gaps = (
        _fn_sequence_gaps(pages)
        if regime in ("endnotes", "mixed", None)
        else []
    )

    return jsonify({
        "footnote_regime": regime,
        "pages":           page_rows,
        "gaps":            seq_gaps,
        "summary": {
            "total_relevant": len(page_rows),
            "mismatched":     mismatched,
            "consolidated":   consolidated_count,
        },
    })


@app.route("/projects/<pid>/api/set-footnote-regime", methods=["POST"])
def api_set_footnote_regime(pid: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    bj = _book_json(project)
    if not bj or not bj.exists():
        return jsonify({"error": "no JSON"}), 404
    body = request.get_json(silent=True) or {}
    regime = body.get("regime")
    allowed = {None, "", "per_page", "endnotes", "per_chapter_endnotes", "inline_endnotes", "mixed"}
    if regime not in allowed:
        return jsonify({"error": f"invalid regime: {regime!r}"}), 400
    def _set_regime(data: dict) -> None:
        if regime:
            data["footnote_regime"] = regime
        else:
            data.pop("footnote_regime", None)
    json_broker.mutate(bj, _set_regime)
    return jsonify({"ok": True, "footnote_regime": regime or None})


@app.route("/projects/<pid>/api/normalize-all", methods=["POST"])
def api_normalize_all(pid: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    bj = _book_json(project)
    if not bj or not bj.exists():
        return jsonify({"error": "no JSON"}), 404

    pages_changed = 0

    def _normalize(data: dict) -> None:
        nonlocal pages_changed
        changed_page_indices = set()

        # Phase 1: Existing page-level normalization transforms
        for page_idx, page in enumerate(data.get("pages", [])):
            if page.get("ignored"):
                continue
            areas = [a for a in (page.get("areas") or []) if a]
            if not any(a.get("type") in ("main_text", "footnote") for a in areas):
                continue
            new_areas, changed = _normalize_page_areas(areas)
            if changed:
                page["areas"] = new_areas
                changed_page_indices.add(page_idx)

        # Phase 2: Book-level sequential index gap-filling normalization
        # A. Collect all existing index numbers in <sup>...</sup> in sequence
        all_sups = []
        for p_idx, page in enumerate(data.get("pages", [])):
            if page.get("ignored"):
                continue
            for a_idx, area in enumerate(page.get("areas", [])):
                if area.get("type") not in ("main_text", "chapter_title", "subtitle"):
                    continue
                text = area.get("text") or ""
                for m in re.findall(r"<sup>(\d+)</sup>", text, re.I):
                    all_sups.append({
                        "val": int(m),
                        "page_idx": p_idx
                    })

        # B. Identify all sequence gaps (with safe gap size threshold)
        gaps = []
        for i in range(len(all_sups) - 1):
            curr = all_sups[i]
            nxt = all_sups[i+1]
            c_val = curr["val"]
            n_val = nxt["val"]
            if c_val < n_val and n_val - c_val > 1:
                gap_size = n_val - c_val - 1
                if gap_size <= 15:  # Safe threshold to filter out chapter resets
                    gaps.append({
                        "missing": list(range(c_val + 1, n_val)),
                        "p_start": curr["page_idx"],
                        "p_end": nxt["page_idx"]
                    })

        # C. Resolve gaps by wrapping missing integers in candidate page ranges
        for gap in gaps:
            missing = gap["missing"]
            p_start = gap["p_start"]
            p_end = gap["p_end"]
            
            for page_idx in range(p_start, p_end + 1):
                page = data["pages"][page_idx]
                if page.get("ignored"):
                    continue
                
                page_changed_in_gap = False
                for area in page.get("areas", []):
                    if area.get("type") not in ("main_text", "chapter_title", "subtitle"):
                        continue
                    text = area.get("text") or ""
                    new_text = text
                    
                    for num in missing:
                        # Match when preceded immediately by letter/punctuation OR by letter/punctuation and a single space
                        pattern = rf"(?:(?<=[a-zA-Z.,?!;:\(\)\[\]\'\"”’“/\\—])|(?<=[a-zA-Z.,?!;:\(\)\[\]\'\"”’“/\\—]\s))({num})\b"
                        new_text = re.sub(pattern, rf"<sup>\1</sup>", new_text)
                    
                    if new_text != text:
                        area["text"] = new_text
                        page_changed_in_gap = True
                
                if page_changed_in_gap:
                    changed_page_indices.add(page_idx)

        pages_changed = len(changed_page_indices)

    json_broker.mutate(bj, _normalize)
    return jsonify({"ok": True, "pages_changed": pages_changed})


@app.route("/projects/<pid>/api/deconsolidate", methods=["POST"])
def api_deconsolidate(pid: str):
    """Remove consolidated:true from one footnote area and strip its text
    from the target area it was appended to."""
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    bj = _book_json(project)
    if not bj or not bj.exists():
        return jsonify({"error": "no JSON"}), 404

    body = request.get_json(force=True)
    page_name = body.get("page_name")
    area_id   = body.get("area_id")
    if not page_name or not area_id:
        return jsonify({"error": "page_name and area_id required"}), 400

    result: dict = {}
    error:  tuple | None = None

    def _deconsolidate(data: dict) -> None:
        nonlocal result, error
        pages = data.get("pages", [])
        target_page = next((p for p in pages if p.get("source_image") == page_name), None)
        if not target_page:
            error = ("page not found", 404); return
        cons_area = next(
            (a for a in (target_page.get("areas") or []) if a.get("id") == area_id), None)
        if not cons_area:
            error = ("area not found", 404); return
        if not cons_area.get("consolidated"):
            error = ("area is not consolidated", 400); return

        cont_text = (cons_area.get("text") or "").strip()
        last_fn_area = None
        found = False
        for page in pages:
            if page.get("ignored"):
                last_fn_area = None; continue
            fn_areas = [a for a in (page.get("areas") or []) if a and a.get("type") == "footnote"]
            if not fn_areas:
                last_fn_area = None; continue
            for area in fn_areas:
                if area.get("id") == area_id and page.get("source_image") == page_name:
                    found = True; break
                if not area.get("consolidated"):
                    last_fn_area = area
            if found:
                break

        if not last_fn_area:
            error = ("could not locate recipient area", 500); return

        base = last_fn_area.get("text") or ""
        if base.endswith(" " + cont_text):
            last_fn_area["text"] = base[: -(len(cont_text) + 1)]
        elif base.endswith(cont_text):
            last_fn_area["text"] = base[: -len(cont_text)] + "-"

        cons_area.pop("consolidated", None)
        cons_area["is_running_continuation"] = False
        has_any_cons = any(a.get("consolidated") for a in (target_page.get("areas") or []))
        if has_any_cons:
            target_page["footnote_consolidated"] = True
        else:
            target_page.pop("footnote_consolidated", None)
        result = {"recipient_area_id": last_fn_area.get("id")}

    json_broker.mutate(bj, _deconsolidate)
    if error:
        return jsonify({"error": error[0]}), error[1]
    return jsonify({"ok": True, **result})


@app.route("/projects/<pid>/api/consolidate-area", methods=["POST"])
def api_consolidate_area(pid: str):
    """Manually consolidate one footnote area into the previous footnote area."""
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    bj = _book_json(project)
    if not bj or not bj.exists():
        return jsonify({"error": "no JSON"}), 404

    body      = request.get_json(force=True)
    page_name = body.get("page_name")
    area_id   = body.get("area_id")
    if not page_name or not area_id:
        return jsonify({"error": "page_name and area_id required"}), 400

    result: dict = {}
    error:  tuple | None = None

    def _consolidate(data: dict) -> None:
        nonlocal result, error
        pages = data.get("pages", [])
        target_page = next((p for p in pages if p.get("source_image") == page_name), None)
        if not target_page:
            error = ("page not found", 404); return
        area = next(
            (a for a in (target_page.get("areas") or []) if a.get("id") == area_id), None)
        if not area:
            error = ("area not found", 404); return
        if area.get("consolidated"):
            error = ("area is already consolidated", 400); return

        cont_text = (area.get("text") or "").strip()
        last_fn_area = None
        found = False
        for page in pages:
            if page.get("ignored"):
                last_fn_area = None; continue
            fn_areas = [a for a in (page.get("areas") or []) if a and a.get("type") == "footnote"]
            if not fn_areas:
                last_fn_area = None; continue
            for a in fn_areas:
                if a.get("id") == area_id and page.get("source_image") == page_name:
                    found = True; break
                if not a.get("consolidated"):
                    last_fn_area = a
            if found:
                break

        if not last_fn_area:
            error = ("no previous footnote area to consolidate into", 400); return

        def _join(base: str, cont: str) -> str:
            base = base.rstrip(); cont = cont.strip()
            if not cont: return base
            return (base[:-1] + cont) if base.endswith("-") else (base + " " + cont)

        last_fn_area["text"] = _join(last_fn_area.get("text") or "", cont_text)
        area["consolidated"] = True
        area.pop("is_running_continuation", None)
        target_page["footnote_consolidated"] = True
        result = {"recipient_area_id": last_fn_area.get("id")}

    json_broker.mutate(bj, _consolidate)
    if error:
        return jsonify({"error": error[0]}), error[1]
    return jsonify({"ok": True, **result})


@app.route("/projects/<pid>/api/book")
def api_book(pid: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    folder = _book_dir(project)
    bj     = _book_json(project)

    # Build a lookup from the JSON so we can overlay metadata on all page images.
    bj_data: dict | None = None
    json_pages: dict[str, dict] = {}
    if bj:
        bj_data = json.loads(bj.read_text(encoding="utf-8"))
        json_pages = {p["source_image"]: p for p in bj_data.get("pages", [])}

    has_areas = any("areas" in p for p in json_pages.values())
    book_title = (bj_data.get("book", project["title"]) if bj_data else project["title"])

    def page_entry(name: str) -> dict:
        p = json_pages.get(name, {})
        return {
            "name":          name,
            "prohibited":    p.get("prohibited", False),
            "ignored":       p.get("ignored", False),
            "process_later": p.get("process_later", False),
            "detected_by":   p.get("detected_by", None),
        }

    pages_dir = _pages_dir(project)
    src_type  = _source_type(project)
    if not pages_dir.exists():
        if src_type == "folder":
            exts = ("*.png", "*.jpg", "*.jpeg", "*.tif", "*.tiff")
            pgs  = [f for ext in exts for f in sorted(folder.glob(ext))]
            if pgs:
                    res = jsonify({
                        "book":      book_title,
                        "has_areas": has_areas,
                        "pages":     [page_entry(f.name) for f in pgs],
                    })
                    res.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
                    return res
        return jsonify({"error": "no pages"}), 404

    pgs = [f for f in sorted(pages_dir.glob("page*.png")) if "-content" not in f.name and "-areas" not in f.name]
    res = jsonify({
        "book":      book_title,
        "has_areas": has_areas,
        "pages":     [page_entry(f.name) for f in pgs],
    })
    res.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return res


@app.route("/projects/<pid>/api/page/<page_name>")
def api_get_page(pid: str, page_name: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    folder  = _book_dir(project)
    bj      = _book_json(project)
    bj_data = None

    if bj:
        bj_data = json.loads(bj.read_text(encoding="utf-8"))
        for p in bj_data.get("pages", []):
            if p["source_image"] == page_name and p.get("page_dimensions"):
                return jsonify(p)

    # Fallback: derive dimensions from the PNG itself
    img = _pages_dir(project) / page_name
    if not img.exists():
        img = folder / page_name  # image-folder projects
    if not img.exists():
        return jsonify({"error": "image not found"}), 404

    from PIL import Image as PilImage
    with PilImage.open(img) as im:
        w, h = im.size

    # Read ignored flag and content_bbox from any existing stub
    ignored = False
    process_later = False
    content_bbox = None
    rotation = None
    areas = []
    if bj_data:
        for p in bj_data.get("pages", []):
            if p["source_image"] == page_name:
                ignored       = p.get("ignored", False)
                process_later = p.get("process_later", False)
                content_bbox  = p.get("content_bbox")
                rotation      = p.get("rotation")
                areas         = p.get("areas", [])
                break

    page_resp = {
        "source_image":    page_name,
        "ignored":         ignored,
        "process_later":   process_later,
        "page_dimensions": {"width": w, "height": h},
        "areas":           areas,
    }
    skew_angle = None
    if bj_data:
        for p in bj_data.get("pages", []):
            if p["source_image"] == page_name:
                skew_angle = p.get("skew_angle")
                break
    if content_bbox:
        page_resp["content_bbox"] = content_bbox
    if rotation:
        page_resp["rotation"] = rotation
    if skew_angle is not None:
        page_resp["skew_angle"] = skew_angle
    res = jsonify(page_resp)
    res.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return res


@app.route("/projects/<pid>/api/page/<page_name>", methods=["POST"])
def api_save_page(pid: str, page_name: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    areas = (request.get_json() or {}).get("areas")
    json_path = _book_dir(project) / f"{_json_stem(project)}.json"

    json_broker.mutate_page(json_path, page_name, lambda page: page.update({"areas": areas}))
    return jsonify({"ok": True})


@app.route("/projects/<pid>/api/page/<page_name>/area/<area_id>/ocr", methods=["POST"])
def api_area_ocr(pid: str, page_name: str, area_id: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "Project not found"}), 404

    body = request.get_json(silent=True) or {}
    engine = body.get("engine")
    if engine not in ("tesseract", "model"):
        return jsonify({"error": "Invalid engine specified"}), 400

    book_dir = _book_dir(project)
    pages_dir = _pages_dir(project)
    json_path = book_dir / f"{_json_stem(project)}.json"

    if not json_path.exists():
        return jsonify({"error": "Book JSON not found"}), 404

    book_data = json.loads(json_path.read_text(encoding="utf-8"))

    # Find page
    page = None
    for p in book_data.get("pages", []):
        if p["source_image"] == page_name:
            page = p
            break

    if not page:
        return jsonify({"error": "Page not found"}), 404

    # Find area
    areas = page.get("areas", [])
    area = None
    for a in areas:
        if a.get("id") == area_id:
            area = a
            break

    if not area:
        return jsonify({"error": "Area not found"}), 404

    polygon = area.get("polygon", [])
    if len(polygon) < 2:
        return jsonify({"error": "Area has no valid polygon coordinates"}), 400

    img_path = pages_dir / page_name
    if not img_path.exists():
        img_path = book_dir / page_name
    if not img_path.exists():
        return jsonify({"error": "Page image file not found"}), 404

    try:
        from PIL import Image as PilImage
        from utils.rotation_broker import RotationBroker
        import io

        page_img = PilImage.open(img_path).convert("RGB")
        rotation = RotationBroker.effective_rotation(page)
        skew_angle = float(page.get("skew_angle") or 0)

        # Crop block directly from full page image
        cropped = RotationBroker.extract_illustration_crop(page_img, polygon, rotation, skew_angle)

        buf = io.BytesIO()
        cropped.save(buf, format="JPEG", quality=95)
        crop_bytes = buf.getvalue()
    except Exception as e:
        return jsonify({"error": f"Error cropping area: {str(e)}"}), 500

    text = ""
    if engine == "tesseract":
        import tempfile
        tmp = Path(tempfile.mktemp(suffix=".png"))
        try:
            tmp.write_bytes(crop_bytes)
            # Build Tesseract command with all dictionary lookups (DAWGs) completely disabled
            cmd_base = [
                "tesseract", str(tmp), "stdout",
                "-c", "load_system_dawg=0",
                "-c", "load_freq_dawg=0",
                "-c", "load_punc_dawg=0",
                "-c", "load_number_dawg=0",
                "-c", "load_unambig_dawg=0",
                "-c", "load_bigram_dawg=0",
            ]
            # Try running tesseract with eng+rus first
            result = subprocess.run(
                cmd_base + ["-l", "eng+rus"],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                text = result.stdout.strip()
            else:
                # Fallback to eng if eng+rus fails
                result = subprocess.run(
                    cmd_base + ["-l", "eng"],
                    capture_output=True, text=True, timeout=30
                )
                if result.returncode == 0:
                    text = result.stdout.strip()
                else:
                    return jsonify({"error": f"Tesseract error: {result.stderr}"}), 500
        except Exception as e:
            return jsonify({"error": f"Tesseract failed: {str(e)}"}), 500
        finally:
            tmp.unlink(missing_ok=True)
    else:  # engine == "model"
        from utils.config import OPEN_ROUTER_APIKEY, OPENROUTER_MODEL
        from utils.ocr import OCR_PROMPT, OCR_PROMPT_STYLES_ADDON, ocr_area_api, clean_ocr_text

        if not OPEN_ROUTER_APIKEY:
            return jsonify({"error": "No OpenRouter API key configured"}), 500

        model = project.get("ocr_model") or OPENROUTER_MODEL or "google/gemini-3.1-flash-image-preview"
        prompt = OCR_PROMPT
        if project.get("styles") or project.get("styles") is None: # default to styles if requested
            prompt += OCR_PROMPT_STYLES_ADDON

        fallback_used = False
        try:
            raw = ocr_area_api(crop_bytes, model, prompt)
            text = clean_ocr_text(raw)
            if not text:
                raise ValueError("Model returned empty text (possible recitation or safety block)")
        except Exception as e:
            from utils.models import load_detect_models
            _, prohibited = load_detect_models()
            if prohibited:
                fallback_backend, fallback_model = prohibited
                print(f"    [ocr fallback] Primary model '{model}' failed or blocked ({e}). Retrying with restricted/fallback model '{fallback_model}' via {fallback_backend}…")
                try:
                    raw = ocr_area_api(crop_bytes, fallback_model, prompt)
                    text = clean_ocr_text(raw)
                    fallback_used = True
                except Exception as fallback_err:
                    return jsonify({"error": f"Model OCR failed: {str(e)} (Fallback also failed: {fallback_err})"}), 500
            else:
                return jsonify({"error": f"Model OCR failed: {str(e)}"}), 500

    # Save to database
    def _update_area_text(page_dict):
        updated = False
        for a in page_dict.get("areas", []):
            if a.get("id") == area_id:
                a["text"] = text
                updated = True
                break
        if not updated:
            raise KeyError("Area not found in page data")

        if fallback_used:
            page_dict["prohibited"] = True

        return True

    try:
        json_broker.mutate_page(json_path, page_name, _update_area_text)
    except Exception as e:
        return jsonify({"error": f"Error saving OCR result to database: {str(e)}"}), 500

    return jsonify({"ok": True, "text": text})


@app.route("/projects/<pid>/api/page/<page_name>/add-area", methods=["POST"])
def api_add_area(pid: str, page_name: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    area = (request.get_json() or {}).get("area")
    if not area:
        return jsonify({"error": "missing area"}), 400
    json_path = _book_dir(project) / f"{_json_stem(project)}.json"
    json_broker.mutate_page(json_path, page_name, lambda page: page.update(
        {"areas": (page.get("areas") or []) + [area]}
    ))
    return jsonify({"ok": True})


@app.route("/projects/<pid>/api/page/<page_name>/content-bbox", methods=["POST"])
def api_save_content_bbox(pid: str, page_name: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    cb = (request.get_json() or {}).get("content_bbox")
    if not cb or not all(k in cb for k in ("left", "top", "right", "bottom")):
        return jsonify({"error": "invalid content_bbox"}), 400
    json_path = _book_dir(project) / f"{_json_stem(project)}.json"
    json_broker.mutate_page(json_path, page_name, lambda page: page.update({"content_bbox": cb}))
    return jsonify({"ok": True})


@app.route("/projects/<pid>/api/page/<page_name>/process-later", methods=["POST"])
def api_toggle_process_later(pid: str, page_name: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    json_path = _book_dir(project) / f"{_json_stem(project)}.json"
    new_state = json_broker.mutate_page(json_path, page_name, lambda page: (
        page.update({"process_later": not page.get("process_later", False)}) or
        page["process_later"]
    ))
    return jsonify({"ok": True, "process_later": new_state})


@app.route("/projects/<pid>/api/page/<page_name>/rotation", methods=["POST"])
def api_save_rotation(pid: str, page_name: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    rotation = int((request.get_json() or {}).get("rotation", 0)) % 360
    json_path = _book_dir(project) / f"{_json_stem(project)}.json"
    def _apply(page: dict) -> None:
        if rotation:
            page["rotation"] = rotation
        else:
            page.pop("rotation", None)
    json_broker.mutate_page(json_path, page_name, _apply)
    return jsonify({"ok": True})


@app.route("/projects/<pid>/api/page/<page_name>/detect-skew", methods=["GET"])
def api_detect_skew(pid: str, page_name: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    from PIL import Image as PilImage
    from utils.rotation_broker import RotationBroker
    json_path = _book_dir(project) / f"{_json_stem(project)}.json"
    content_bbox = None
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            for p in data.get("pages", []):
                if p["source_image"] == page_name:
                    content_bbox = p.get("content_bbox")
                    break
        except Exception:
            pass
    img_path = _pages_dir(project) / page_name
    if not img_path.exists():
        img_path = _book_dir(project) / page_name
    if not img_path.exists():
        return jsonify({"error": "image not found"}), 404
    pil_img = PilImage.open(img_path).convert("RGB")
    if content_bbox:
        pil_img = pil_img.crop((content_bbox["left"], content_bbox["top"],
                                 content_bbox["right"], content_bbox["bottom"]))
    angle = RotationBroker.detect_skew(pil_img)
    return jsonify({"skew_angle": angle})


@app.route("/projects/<pid>/api/page/<page_name>/skew", methods=["POST"])
def api_save_skew(pid: str, page_name: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    skew_angle = (request.get_json() or {}).get("skew_angle")
    json_path = _book_dir(project) / f"{_json_stem(project)}.json"
    def _apply(page: dict) -> None:
        if skew_angle is not None:
            page["skew_angle"] = skew_angle
        else:
            page.pop("skew_angle", None)
    json_broker.mutate_page(json_path, page_name, _apply)
    return jsonify({"ok": True, "skew_angle": skew_angle})


@app.route("/projects/<pid>/api/page/<page_name>/ignore", methods=["POST"])
def api_toggle_ignore(pid: str, page_name: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    json_path = _book_dir(project) / f"{_json_stem(project)}.json"
    new_state = json_broker.mutate_page(json_path, page_name, lambda page: (
        page.update({"ignored": not page.get("ignored", False)}) or page["ignored"]
    ))
    return jsonify({"ok": True, "ignored": new_state})


@app.route("/projects/<pid>/api/page/<page_name>/detect-content", methods=["POST"])
def api_detect_content_bbox(pid: str, page_name: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404

    img_path = _pages_dir(project) / page_name
    if not img_path.exists():
        img_path = _book_dir(project) / page_name
    if not img_path.exists():
        return jsonify({"error": "image not found"}), 404

    from PIL import Image as PilImage
    from steps.step1_extract_pages import detect_content_bbox

    with PilImage.open(img_path) as pil_img:
        pil_img = pil_img.convert("RGB")
        w, h = pil_img.size
        bbox = detect_content_bbox(pil_img)

    if not bbox:
        bbox = (0, 0, w, h)
    left, top, right, bottom = bbox
    cb = {"left": left, "top": top, "right": right, "bottom": bottom}

    json_path = _book_dir(project) / f"{_json_stem(project)}.json"
    json_broker.mutate_page(json_path, page_name, lambda page: page.update({"content_bbox": cb}))

    return jsonify({"ok": True, "content_bbox": cb})


@app.route("/projects/<pid>/api/page/<page_name>/table-to-html", methods=["POST"])
def api_table_to_html(pid: str, page_name: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404

    payload = request.get_json() or {}
    area_id = payload.get("area_id")
    if not area_id:
        return jsonify({"error": "area_id required"}), 400

    bj = _book_json(project)
    if not bj or not bj.exists():
        return jsonify({"error": "no areas JSON"}), 400

    data = json.loads(bj.read_text(encoding="utf-8"))
    area = None
    for p in data.get("pages", []):
        if p["source_image"] == page_name:
            for a in p.get("areas", []):
                if a.get("id") == area_id:
                    area = a
                    break
            break

    if not area:
        return jsonify({"error": "area not found"}), 404
    if area.get("type") != "table":
        return jsonify({"error": "area is not a table"}), 400

    img_path = _pages_dir(project) / page_name
    if not img_path.exists():
        img_path = _book_dir(project) / page_name
    if not img_path.exists():
        return jsonify({"error": "page image not found"}), 404

    from PIL import Image as PilImage
    from utils.config import OPEN_ROUTER_APIKEY, OPENROUTER_MODEL
    from prompts.tables import TABLE_PROMPT

    polygon = area["polygon"]
    xs = [pt[0] for pt in polygon]
    ys = [pt[1] for pt in polygon]
    x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
    with PilImage.open(img_path) as img:
        img = img.convert("RGB")
        cropped = img.crop((x1, y1, x2, y2))
    buf = io.BytesIO()
    cropped.save(buf, format="JPEG", quality=90)
    image_bytes = buf.getvalue()

    from utils.config import GEMINI_API_KEY
    model = OPENROUTER_MODEL or "google/gemini-3.1-flash-image-preview"
    model_lower = model.lower()

    if GEMINI_API_KEY and ("gemini" in model_lower or model_lower.startswith("gemini")):
        from utils.gemini import gemini_generate_content
        real_model = model.split(":", 1)[1] if ":" in model else model
        if "/" in real_model:
            real_model = real_model.split("/")[-1]
        if real_model in ("gemini-3.1-flash-image-preview", "gemini-3.1-flash", "gemini"):
            real_model = "gemini-2.5-flash"

        try:
            html = gemini_generate_content(
                prompt=TABLE_PROMPT,
                image_bytes=image_bytes,
                model=real_model,
                temperature=0,
                max_tokens=4000
            )
        except Exception as e:
            return jsonify({"error": f"Gemini direct table OCR failed: {str(e)}"}), 500
    else:
        if not OPEN_ROUTER_APIKEY:
            return jsonify({"error": "No OpenRouter API key configured"}), 500

        b64 = base64.b64encode(image_bytes).decode()
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": TABLE_PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ]}],
                "temperature": 0,
                "max_tokens": 4000,
            },
            headers={
                "Authorization": f"Bearer {OPEN_ROUTER_APIKEY}",
                "Content-Type": "application/json",
            },
            timeout=120,
        )
        resp.raise_for_status()
        html = resp.json()["choices"][0]["message"]["content"].strip()
    if html.startswith("```"):
        lines = html.splitlines()
        end = -1 if lines[-1].strip() == "```" else len(lines)
        html = "\n".join(lines[1:end]).strip()

    captured_html = html

    def _save_table_html(page: dict) -> None:
        for a in page.get("areas") or []:
            if a.get("id") == area_id:
                a["table_html"] = captured_html
                break

    json_broker.mutate_page(bj, page_name, _save_table_html)
    return jsonify({"ok": True, "table_html": html})


@app.route("/projects/<pid>/cover")
def serve_cover(pid: str):
    project = _get_project(pid)
    if not project or not project.get("cover_path"):
        return "not found", 404
    p = Path(project["cover_path"])
    if not p.exists():
        return "not found", 404
    return send_file(p)


@app.route("/projects/<pid>/images/<path:filename>")
def serve_image(pid: str, filename: str):
    project = _get_project(pid)
    if not project:
        return "not found", 404
    p = _pages_dir(project) / filename
    if not p.exists():
        p = _book_dir(project) / filename  # image-folder projects
    if not p.exists():
        return "not found", 404
    return send_file(p)


@app.route("/projects/<pid>/preview-page-assembly")
def preview_page_assembly(pid: str):
    project = _get_project(pid)
    if not project:
        return "Project not found", 404
    return render_template("preview_page_assembly.html", project=project)


@app.route("/projects/<pid>/api/preview-assembly/<path:page_name>")
def api_preview_assembly(pid: str, page_name: str):
    from utils.rotation_broker import RotationBroker
    from utils.shared_layout import _PARA_SPLIT
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    bj = _book_json(project)
    if not bj or not bj.exists():
        return jsonify({"error": "no JSON"}), 404

    book_data = json.loads(bj.read_text(encoding="utf-8"))
    page = next((p for p in book_data.get("pages", []) if p.get("source_image") == page_name), None)
    if not page:
        return jsonify({"error": "page not found"}), 404

    page_w    = page.get("page_dimensions", {}).get("width", 1000)
    page_h    = page.get("page_dimensions", {}).get("height", 1000)
    rotation  = RotationBroker.effective_rotation(page)
    stem      = Path(page_name).stem
    sorted_areas = RotationBroker.sort_areas(page.get("areas", []) or [], page_w, page_h, rotation)

    # Pre-collect captions keyed by linked_illustration_id
    captions: dict[str, list] = {}
    for area in sorted_areas:
        if area.get("type") == "illustration_caption":
            lid = area.get("linked_illustration_id") or "__unlinked__"
            captions.setdefault(lid, []).append(area)

    segments = []
    emitted_illus: set[str] = set()

    def push(area_id, area_type, html, extra_area_ids=None):
        segments.append({
            "area_id":   area_id,
            "area_type": area_type,
            "html":      html,
            "also":      extra_area_ids or [],
        })

    for area in sorted_areas:
        atype = area.get("type")
        aid   = area.get("id", "?")
        text  = (area.get("text") or "").strip()

        if atype == "illustration_caption":
            continue  # emitted inline with illustration

        if atype == "main_text" and text:
            paras = [p.replace("\n", " ").strip() for p in _PARA_SPLIT.split(text) if p.strip()]
            html  = "\n".join(f"<p>{p}</p>" for p in paras)
            push(aid, atype, html)

        elif atype in ("chapter_title", "subtitle", "title_page") and text:
            cls  = {"chapter_title": "chapter-title", "subtitle": "subtitle",
                    "title_page": "title-page-line"}[atype]
            tag  = "p" if atype == "title_page" else "div"
            push(aid, atype, f'<{tag} class="{cls}">{" ".join(text.split())}</{tag}>')

        elif atype == "illustration":
            illus_id = area.get("illustration_id", "")
            if not illus_id or illus_id in emitted_illus:
                continue
            emitted_illus.add(illus_id)
            img_src   = f"/projects/{pid}/elements/{stem}_{illus_id}.png"
            cap_areas = captions.get(illus_id, [])
            cap_text  = " ".join((c.get("text") or "").strip() for c in cap_areas if c.get("text")).strip()
            figcap    = f"<figcaption>{cap_text}</figcaption>" if cap_text else ""
            html      = (f'<figure>\n  <img src="{img_src}" alt="{illus_id}">\n'
                         f'  {figcap}\n</figure>')
            push(aid, atype, html, extra_area_ids=[c.get("id") for c in cap_areas])

        elif atype == "footnote" and text and not area.get("consolidated"):
            push(aid, atype, f'<div class="footnote">{text}</div>')

        elif atype == "table" and (area.get("table_html") or text):
            content = area.get("table_html") or f"<p>{text}</p>"
            push(aid, atype, content)

    return jsonify({"page_name": page_name, "segments": segments})


@app.route("/projects/<pid>/elements/<path:filename>")
def serve_element(pid: str, filename: str):
    project = _get_project(pid)
    if not project:
        return "not found", 404
    p = _elements_dir(project) / filename
    if not p.exists():
        return "not found", 404
    return send_file(p)


def _serve_html_with_rewritten_elements(html_path: Path, pid: str, elements_name: str) -> Response:
    content = html_path.read_text(encoding="utf-8")
    nfc_name = unicodedata.normalize("NFC", elements_name)
    content = content.replace(
        f'src="{nfc_name}/', f'src="/projects/{pid}/elements/'
    )
    return Response(content, mimetype="text/html")



@app.route("/projects/<pid>/preview-seamless")
def preview_seamless_html(pid: str):
    project = _get_project(pid)
    if not project:
        return "Project not found", 404
    html_path = _merged_html(project)
    if not html_path.exists():
        return "No seamless HTML generated yet", 404
    return _serve_html_with_rewritten_elements(
        html_path, pid, _elements_dir(project).name
    )


# ── Inline endnotes (per-article Notes sections scattered through the body) ───

def _build_inline_endnote_data(book_data: dict) -> dict:
    """Build review data for inline_endnotes regime.

    Each article/chapter is followed immediately by its own 'Notes' section.
    Sections are detected by proximity: a Notes chapter-title closes the current
    body section and opens a notes zone; the next non-Notes chapter-title starts
    a new body section.
    """
    pages = book_data.get("pages", [])

    # ── Segment pages into sections ───────────────────────────────────────────
    sections: list[dict] = []
    cur: dict | None = None
    zone = "body"

    for page in pages:
        if page.get("ignored"):
            continue
        pname = page.get("source_image", "")
        areas = page.get("areas") or []

        # Collect chapter_title events in spatial (area) order
        ch_events: list[tuple[str, str]] = []
        for a in areas:
            if a.get("type") != "chapter_title":
                continue
            text = (a.get("text") or "").strip()
            ch_events.append(("notes" if _is_inline_notes_title(text) else "content", text))

        has_footnote = any(a.get("type") == "footnote" for a in areas)

        for ctype, ctext in ch_events:
            if ctype == "notes":
                zone = "notes"
                if cur is not None and pname not in cur["notes_pages"]:
                    cur["notes_pages"].append(pname)
            else:
                if cur is not None:
                    sections.append(cur)
                cur = {"title": ctext, "body_pages": [], "notes_pages": []}
                zone = "body"

        # Assign page to current section
        if cur is not None:
            if zone == "body":
                if pname not in cur["body_pages"]:
                    cur["body_pages"].append(pname)
            else:
                if pname not in cur["notes_pages"]:
                    cur["notes_pages"].append(pname)

        # Pages with footnote areas but no chapter_title → force into notes zone
        if not ch_events and has_footnote and cur is not None:
            zone = "notes"
            if pname not in cur["notes_pages"]:
                cur["notes_pages"].append(pname)
            if pname in cur["body_pages"]:
                cur["body_pages"].remove(pname)

    if cur is not None:
        sections.append(cur)

    # ── Build lookup and process sections ─────────────────────────────────────
    page_lookup = {p.get("source_image", ""): p for p in pages}
    chapters_out: list[dict] = []
    pages_map: dict[str, dict] = {}

    # ── Satellite grouping ─────────────────────────────────────────────────────
    # When several sub-chapters share one Notes block (e.g. chs 4–8 all point to
    # the same notes pages), only the last sub-chapter before the Notes heading
    # gets notes_pages; the earlier ones are "satellites".
    # Map each section index → owner index (next section that has notes_pages,
    # or itself if it already owns notes).
    _n = len(sections)
    notes_owner_idx: list[int] = list(range(_n))
    _next_owner: int | None = None
    for _i in range(_n - 1, -1, -1):
        if sections[_i]["notes_pages"]:
            _next_owner = _i
        notes_owner_idx[_i] = _i if sections[_i]["notes_pages"] else (
            _next_owner if _next_owner is not None else _i
        )

    # Combined body sups per owner: union across owner + all its satellites.
    # Also includes sups found in notes pages (reverse-mixed pages where the notes
    # page also carries body text with sup markers).
    group_body_sups: dict[int, set[int]] = {}
    for _i, _sect in enumerate(sections):
        _owner = notes_owner_idx[_i]
        _gs = group_body_sups.setdefault(_owner, set())
        for _pname in _sect["body_pages"] + _sect["notes_pages"]:
            _pg = page_lookup.get(_pname)
            if not _pg:
                continue
            for _area in (_pg.get("areas") or []):
                if _area.get("type") not in _FN_BODY_TYPES:
                    continue
                for _m in _FN_SUP_TAG_RE.finditer(_area_sup_text(_area)):
                    _gs.add(int(_m.group(1)))

    for idx, sect in enumerate(sections):
        title      = sect["title"]
        body_pgs   = sect["body_pages"]
        notes_pgs  = sect["notes_pages"]

        is_satellite = notes_owner_idx[idx] != idx
        owner_idx    = notes_owner_idx[idx]
        owner_sect   = sections[owner_idx]

        # Body sups: own body pages + any notes pages that carry body-zone sups
        body_sups: set[int] = set()
        sup_to_page: dict[str, str] = {}
        _reverse_mixed: set[str] = set()  # notes pages that also have body sups
        sup_to_snippet: dict[str, str] = {}
        for pname in body_pgs + notes_pgs:
            pg = page_lookup.get(pname)
            if not pg:
                continue
            _pg_new_sups: set[int] = set()
            for area in (pg.get("areas") or []):
                if area.get("type") not in _FN_BODY_TYPES:
                    continue
                atext = _area_sup_text(area)
                atype = area.get("type", "")
                for m in _FN_SUP_TAG_RE.finditer(atext):
                    n = int(m.group(1))
                    _pg_new_sups.add(n)
                    body_sups.add(n)
                    sup_to_page.setdefault(str(n), pname)
                    if str(n) not in sup_to_snippet:
                        s, e = max(0, m.start() - 35), min(len(atext), m.end() + 35)
                        raw = atext[s:e].replace("\n", " ")
                        sup_to_snippet[str(n)] = (
                            f"[{atype}] " +
                            ("…" if s > 0 else "") + raw + ("…" if e < len(atext) else "")
                        )
            if _pg_new_sups and pname in notes_pgs:
                _reverse_mixed.add(pname)

        # Note entries: satellites borrow from their owner's notes pages
        _notes_pgs_for_entries = owner_sect["notes_pages"] if is_satellite else notes_pgs
        entries: set[int] = set()
        entry_to_page: dict[str, str] = {}
        entry_to_area: dict[str, str] = {}
        _mixed_body_pgs: set[str] = set()  # body pages that also contain footnote areas
        for pname in _notes_pgs_for_entries:
            pg = page_lookup.get(pname)
            if not pg:
                continue
            for area in (pg.get("areas") or []):
                if area.get("type") != "footnote":
                    continue
                aid = area.get("id", "")
                for m in _ENTRY_LINE_RE.finditer(area.get("text") or ""):
                    n = int(m.group(1))
                    entries.add(n)
                    entry_to_page.setdefault(str(n), pname)
                    entry_to_area.setdefault(str(n), aid)
        # Mixed pages: body pages that ALSO carry footnote areas (e.g. a page whose
        # bottom half is the start of the Notes section while the top half is body text)
        for pname in body_pgs:
            pg = page_lookup.get(pname)
            if not pg:
                continue
            _has_fn = False
            for area in (pg.get("areas") or []):
                if area.get("type") != "footnote":
                    continue
                _has_fn = True
                aid = area.get("id", "")
                for m in _ENTRY_LINE_RE.finditer(area.get("text") or ""):
                    n = int(m.group(1))
                    entries.add(n)
                    entry_to_page.setdefault(str(n), pname)
                    entry_to_area.setdefault(str(n), aid)
            if _has_fn:
                _mixed_body_pgs.add(pname)

        if is_satellite:
            # Own sups vs shared entries. Suppress missing_sups: we can't isolate
            # which entries from the shared Notes block belong to this sub-chapter.
            missing_entries = sorted(body_sups - entries)
            missing_sups    = []
        else:
            # Owner uses full group sups so satellite sups don't appear orphaned.
            eff_sups        = group_body_sups.get(idx, body_sups)
            missing_entries = sorted(eff_sups - entries)
            missing_sups    = sorted(entries - eff_sups)
        matched = not missing_entries and not missing_sups

        ch_rec = {
            "index":            idx,
            "body_title_raw":   title,
            "notes_title_raw":  "Notes",
            "body_title_norm":  _normalize_ch_title(title),
            "notes_title_norm": "notes",
            "name_match":       True,
            "name_match_type":  "proximity",
            "body_pages":       body_pgs,
            "endnote_pages":    owner_sect["notes_pages"] if is_satellite else notes_pgs,
            "body_sups":        sorted(body_sups),
            "endnote_entries":  sorted(entries),
            "sup_to_page":      sup_to_page,
            "sup_to_snippet":   sup_to_snippet,
            "entry_to_page":    entry_to_page,
            "entry_to_area":    entry_to_area,
            "missing_entries":  missing_entries,
            "missing_sups":     missing_sups,
            "state":            "green" if matched else "red",
            "badge":            f"{len(body_sups)}↑·{len(entries)}fn",
        }
        chapters_out.append(ch_rec)

        # Body page tiles
        for pname in body_pgs:
            pg = page_lookup.get(pname)
            if not pg or pg.get("ignored"):
                continue
            sups: set[int] = set()
            section_starts: list[str] = []
            is_chapter_start = False
            for area in (pg.get("areas") or []):
                if area.get("type") in _FN_BODY_TYPES:
                    for m in _FN_SUP_TAG_RE.finditer(_area_sup_text(area)):
                        sups.add(int(m.group(1)))
                if area.get("type") == "chapter_title":
                    t = (area.get("text") or "").strip()
                    if not _is_inline_notes_title(t):
                        section_starts.append(t.replace("\n", " "))
                        if pname == body_pgs[0]:
                            is_chapter_start = True
            sups_list = sorted(sups)
            unlinked  = sorted(sups - entries)
            gap: list[int] = []
            for i2 in range(len(sups_list) - 1):
                if sups_list[i2 + 1] > sups_list[i2] + 1:
                    gap.extend(range(sups_list[i2] + 1, sups_list[i2 + 1]))
            pages_map[pname] = {
                "name":                 pname,
                "chapter_index":        idx,
                "zone":                 "body",
                "state":                "none" if not sups else ("red" if gap else "green"),
                "badge":                (f"{len(sups)}↑ [{min(sups)}–{max(sups)}]" if len(sups) > 1
                                         else f"1↑ [{min(sups)}]" if sups else ""),
                "sups":                 sups_list,
                "unlinked_sups":        unlinked,
                "internal_gap_missing": gap,
                "chapter_start":        is_chapter_start,
                "chapter_title":        title if is_chapter_start else None,
                "section_starts":       section_starts,
            }
            # Mixed page: also has footnote entries → emit an endnote tile right after
            if pname in _mixed_body_pgs:
                _pg_entries: set[int] = set()
                for _area in (pg.get("areas") or []):
                    if _area.get("type") != "footnote":
                        continue
                    for _m in _ENTRY_LINE_RE.finditer(_area.get("text") or ""):
                        _pg_entries.add(int(_m.group(1)))
                if _pg_entries:
                    _eff_gs2 = group_body_sups.get(idx, body_sups)
                    _uln_e   = sorted(_pg_entries - _eff_gs2)
                    _mn2, _mx2 = min(_pg_entries), max(_pg_entries)
                    pages_map[pname + ":en"] = {
                        "name":                    pname,
                        "chapter_index":           idx,
                        "zone":                    "endnote",
                        "state":                   ("green" if not _uln_e else "red"),
                        "badge":                   (f"{_mn2}–{_mx2}" if _mn2 != _mx2 else str(_mn2)),
                        "entries":                 sorted(_pg_entries),
                        "unlinked_entries":        _uln_e,
                        "endnotes_section_start":  False,
                        "chapter_subtitle_starts": [],
                    }

        # Notes page tiles (owner renders them; satellites have notes_pgs=[] so skip)
        eff_group_sups = group_body_sups.get(idx, body_sups)
        for pname in notes_pgs:
            # If a page already got a body tile (rare mixed-zone case), store the
            # endnote tile under a ":en" key so both tiles survive in pages_out.
            if pname in pages_map:
                if pages_map[pname]["zone"] == "body":
                    tile_key = pname + ":en"
                    if tile_key in pages_map:
                        continue  # already handled in the body loop above
                else:
                    continue
            else:
                tile_key = pname
            pg = page_lookup.get(pname)
            if not pg or pg.get("ignored"):
                continue
            page_entries: set[int] = set()
            for area in (pg.get("areas") or []):
                if area.get("type") != "footnote":
                    continue
                for m in _ENTRY_LINE_RE.finditer(area.get("text") or ""):
                    page_entries.add(int(m.group(1)))
            unlinked_e = sorted(page_entries - eff_group_sups)
            mn, mx = (min(page_entries), max(page_entries)) if page_entries else (0, 0)
            pages_map[tile_key] = {
                "name":                    pname,
                "chapter_index":           idx,
                "zone":                    "endnote",
                "state":                   ("green" if page_entries and not unlinked_e
                                            else "red" if unlinked_e else "blue"),
                "badge":                   (f"{mn}–{mx}" if page_entries and mn != mx
                                            else str(mn) if page_entries else ""),
                "entries":                 sorted(page_entries),
                "unlinked_entries":        unlinked_e,
                "endnotes_section_start":  False,
                "chapter_subtitle_starts": [],
            }
            # Reverse-mixed page: also has body sups → create a body tile
            if pname in _reverse_mixed:
                _pg_sups2: set[int] = set()
                for _area in (pg.get("areas") or []):
                    if _area.get("type") not in _FN_BODY_TYPES:
                        continue
                    for _m in _FN_SUP_TAG_RE.finditer(_area_sup_text(_area)):
                        _pg_sups2.add(int(_m.group(1)))
                _sups_list2 = sorted(_pg_sups2)
                _uln2 = sorted(_pg_sups2 - entries)
                _gap2: list[int] = []
                for _i2 in range(len(_sups_list2) - 1):
                    if _sups_list2[_i2 + 1] > _sups_list2[_i2] + 1:
                        _gap2.extend(range(_sups_list2[_i2] + 1, _sups_list2[_i2 + 1]))
                pages_map[pname + ":body"] = {
                    "name":                 pname,
                    "chapter_index":        idx,
                    "zone":                 "body",
                    "state":                "none" if not _pg_sups2 else ("red" if _gap2 else "green"),
                    "badge":                (f"{len(_pg_sups2)}↑ [{min(_pg_sups2)}–{max(_pg_sups2)}]" if len(_pg_sups2) > 1
                                             else f"1↑ [{min(_pg_sups2)}]" if _pg_sups2 else ""),
                    "sups":                 _sups_list2,
                    "unlinked_sups":        _uln2,
                    "internal_gap_missing": _gap2,
                    "chapter_start":        True,
                    "chapter_title":        title,
                    "section_starts":       [],
                }

    # Pages in document order:
    #   ":body" twin (reverse-mixed) emitted before the endnote tile for that page
    #   ":en"   twin (mixed body page) emitted after the body tile for that page
    pages_out = []
    for p in pages:
        if p.get("ignored"):
            continue
        src = p.get("source_image", "")
        if (src + ":body") in pages_map:
            pages_out.append(pages_map[src + ":body"])
        if src in pages_map:
            pages_out.append(pages_map[src])
        if (src + ":en") in pages_map:
            pages_out.append(pages_map[src + ":en"])

    # Gap detection
    gaps: list[dict] = []
    # Global pass: sups are numbered continuously across all chapters in this regime
    _prev_num: int | None = None
    _prev_pg: str | None = None
    for p in pages_out:
        if p.get("zone") != "body" or not p.get("sups"):
            continue
        sups   = p["sups"]
        ch_idx = p["chapter_index"]
        if _prev_num is None:
            if sups[0] > 1:
                gaps.append({"before": p["name"], "missing": list(range(1, sups[0])), "chapter_index": ch_idx})
        elif sups[0] > _prev_num + 1:
            gaps.append({"after": _prev_pg, "missing": list(range(_prev_num + 1, sups[0])), "chapter_index": ch_idx})
        _prev_num = sups[-1]
        _prev_pg  = p["name"]

    return {
        "endnotes_start_page":     None,
        "endnotes_start_detected": False,
        "chapters":                chapters_out,
        "pages":                   pages_out,
        "gaps":                    gaps,
    }


# ── Per-chapter endnotes ──────────────────────────────────────────────────────

_CH_NUM_TITLE_RE  = re.compile(r'^(\d+)\.\s+(.*)', re.DOTALL)
_CH_ROMAN_PFX_RE  = re.compile(
    r'^Chapter\s+[IVXLCDM]+\s*[—–\-:]\s*(.*)', re.IGNORECASE | re.DOTALL
)
_CH_WORD_TITLE_RE = re.compile(r'^(?:<[^>]+>)*\s*Chapter\s+(\d+)', re.IGNORECASE)
_ENTRY_LINE_RE    = re.compile(r'^\s*(?:<[^>]+>)*\s*(\d+)\.?\s*(?:</[^>]+>)*\.?\s', re.MULTILINE)
_ENDNOTE_HINTS    = frozenset({
    "notes", "note", "endnotes", "end notes", "references", "annotations",
    "notes on sources", "notes on text sources", "source notes",
    "примечания", "сноски",
})


def _is_inline_notes_title(text: str) -> bool:
    low = re.sub(r"[^a-zA-Zа-яёА-ЯЁ\s]", "", text or "").strip().lower()
    return low in _ENDNOTE_HINTS


def _normalize_ch_title(raw: str) -> str:
    t = (raw or "").strip().replace("\n", " ")
    t = re.sub(r'<[^>]+>', '', t).strip()
    m = _CH_NUM_TITLE_RE.match(t)
    if m:
        t = m.group(2).strip()
    m = _CH_ROMAN_PFX_RE.match(t)
    if m:
        t = m.group(1).strip()
    return t.lower()


def _extract_ch_num(text: str) -> int | None:
    """Extract chapter number from 'N. Title' or 'Chapter N' (with optional HTML tags)."""
    t = text.strip()
    m = _CH_NUM_TITLE_RE.match(t)
    if m:
        return int(m.group(1))
    m = _CH_WORD_TITLE_RE.match(t)
    if m:
        return int(m.group(1))
    return None


def _detect_endnotes_start(book_data: dict) -> str | None:
    if book_data.get("endnotes_start_page"):
        return book_data["endnotes_start_page"]
    pages = book_data.get("pages", [])
    n = len(pages)
    for i, page in enumerate(pages[n // 3:], start=n // 3):
        for area in (page.get("areas") or []):
            if area.get("type") != "chapter_title":
                continue
            title = (area.get("text") or "").strip().lower().replace("\n", " ")
            title_hint = any(h in title for h in _ENDNOTE_HINTS)
            # Check only the immediately following pages (tight window prevents
            # false-positives where a body chapter-title "sees" the real endnote
            # section several pages away)
            subsequent = pages[i + 1: i + 4]
            has_num_subs = any(
                _CH_NUM_TITLE_RE.match((a.get("text") or "").strip())
                for p in subsequent
                for a in (p.get("areas") or [])
                if a.get("type") == "subtitle"
            )
            has_entries = any(
                _ENTRY_LINE_RE.search(a.get("text") or "")
                for p in subsequent
                for a in (p.get("areas") or [])
                if a.get("type") == "main_text"
            )
            if has_num_subs and has_entries:
                return page["source_image"]
            if title_hint and (has_num_subs or has_entries):
                return page["source_image"]
    return None


def _build_chapter_endnote_data(book_data: dict) -> dict:
    pages          = book_data.get("pages", [])
    endnotes_start = _detect_endnotes_start(book_data)
    was_explicit   = bool(book_data.get("endnotes_start_page"))

    endnote_page_names: set[str] = set()
    in_end = False
    for page in pages:
        if page.get("source_image") == endnotes_start:
            in_end = True
        if in_end:
            endnote_page_names.add(page.get("source_image", ""))

    # Mapping for unnumbered chapters (e.g. Introduction, Epilogue) to unique negative indices
    unnum_title_to_idx = {}
    next_unnum_idx = -1

    # ── Body chapters ─────────────────────────────────────────────────────────
    body_chs:    dict[int, dict] = {}
    page_to_bch: dict[str, int]  = {}
    cur_bch: int | None = None

    for page in pages:
        pname = page.get("source_image", "")
        if pname in endnote_page_names:
            break
        if page.get("ignored"):
            continue
        for area in (page.get("areas") or []):
            if area.get("type") == "chapter_title":
                t = (area.get("text") or "").strip().replace("\n", " ")
                m = _CH_NUM_TITLE_RE.match(t)
                if m:
                    idx = int(m.group(1))
                    cur_bch = idx
                    if idx not in body_chs:
                        body_chs[idx] = {
                            "index": idx, "title_raw": t,
                            "title_norm": _normalize_ch_title(t),
                            "first_page": pname, "pages": [],
                        }
                else:
                    if not _is_inline_notes_title(t):
                        norm = _normalize_ch_title(t)
                        if norm not in unnum_title_to_idx:
                            unnum_title_to_idx[norm] = next_unnum_idx
                            next_unnum_idx -= 1
                        idx = unnum_title_to_idx[norm]
                        cur_bch = idx
                        if idx not in body_chs:
                            body_chs[idx] = {
                                "index": idx, "title_raw": t,
                                "title_norm": norm,
                                "first_page": pname, "pages": [],
                            }
        if cur_bch is not None:
            body_chs[cur_bch]["pages"].append(pname)
            page_to_bch[pname] = cur_bch

    for ch in body_chs.values():
        sups: set[int] = set()
        sup_to_page: dict[int, str] = {}
        for pname in ch["pages"]:
            pg = next((p for p in pages if p.get("source_image") == pname), None)
            if not pg:
                continue
            for area in (pg.get("areas") or []):
                if area.get("type") in _FN_BODY_TYPES:
                    for m2 in _FN_SUP_TAG_RE.finditer(_area_sup_text(area)):
                        n = int(m2.group(1))
                        sups.add(n)
                        sup_to_page.setdefault(n, pname)
        ch["body_sups"] = sorted(sups)
        ch["sup_to_page"] = sup_to_page

    # ── Endnote chapters ──────────────────────────────────────────────────────
    end_chs:     dict[int, dict] = {}
    page_to_ech: dict[str, int]  = {}
    cur_ech: int | None = None

    for page in pages:
        pname = page.get("source_image", "")
        if pname not in endnote_page_names or page.get("ignored"):
            continue
        page_ch_entries: dict[int, list[int]] = {}
        # Sort areas top-to-bottom using their polygon Y-coordinates to maintain correct reading/parsing order
        def get_top_y(a):
            poly = a.get("polygon")
            return min(pt[1] for pt in poly) if poly else 999999
        sorted_areas = sorted([a for a in (page.get("areas") or []) if a], key=get_top_y)
        for area in sorted_areas:
            atype = area.get("type")
            if atype in ("subtitle", "chapter_title"):
                t = (area.get("text") or "").strip().replace("\n", " ")
                # Use word-format only ("Chapter N") — avoids false-positives
                # where numbered endnote entries ("2. Ibid.") look like chapter headers.
                _m = _CH_WORD_TITLE_RE.match(t)
                idx = int(_m.group(1)) if _m else None
                if idx is None:
                    if not _is_inline_notes_title(t) and not _ENTRY_LINE_RE.match(t):
                        norm = _normalize_ch_title(t)
                        if norm not in unnum_title_to_idx:
                            unnum_title_to_idx[norm] = next_unnum_idx
                            next_unnum_idx -= 1
                        idx = unnum_title_to_idx[norm]
                if idx is not None:
                    cur_ech = idx
                    if idx not in end_chs:
                        end_chs[idx] = {
                            "index": idx, "title_raw": t,
                            "title_norm": _normalize_ch_title(t),
                            "pages": [], "entries": set(),
                        }
                    page_ch_entries.setdefault(idx, [])
            if atype == "main_text":
                raw = area.get("text") or ""
                first_line = raw.split("\n")[0].strip()
                _m = _CH_WORD_TITLE_RE.match(first_line)
                embedded_idx = int(_m.group(1)) if _m else None
                if embedded_idx is None:
                    if not _is_inline_notes_title(first_line) and not _ENTRY_LINE_RE.match(first_line) and len(first_line) < 100:
                        norm = _normalize_ch_title(first_line)
                        if norm in unnum_title_to_idx:
                            embedded_idx = unnum_title_to_idx[norm]
                if embedded_idx is not None:
                    cur_ech = embedded_idx
                    if embedded_idx not in end_chs:
                        end_chs[embedded_idx] = {
                            "index": embedded_idx, "title_raw": first_line,
                            "title_norm": _normalize_ch_title(first_line),
                            "pages": [], "entries": set(),
                        }
                    page_ch_entries.setdefault(embedded_idx, [])
                if cur_ech is not None:
                    nums = [int(em.group(1)) for em in _ENTRY_LINE_RE.finditer(raw)]
                    if nums:
                        page_ch_entries.setdefault(cur_ech, []).extend(nums)
                        end_chs[cur_ech]["entries"].update(nums)
                        end_chs[cur_ech].setdefault("entry_to_page", {})
                        for n in nums:
                            end_chs[cur_ech]["entry_to_page"].setdefault(n, pname)

        for ech_idx in page_ch_entries:
            if pname not in end_chs[ech_idx]["pages"]:
                end_chs[ech_idx]["pages"].append(pname)

        if page_ch_entries:
            dominant = max(page_ch_entries, key=lambda k: len(page_ch_entries[k]))
            page_to_ech[pname] = dominant
        elif cur_ech is not None:
            page_to_ech[pname] = cur_ech

    for ch in end_chs.values():
        ch["entries"] = sorted(ch["entries"])

    # ── Cross-match ───────────────────────────────────────────────────────────
    ch_lookup: dict[int, dict] = {}
    chapters_out = []
    
    # Sort keys by document order (appearance page)
    all_keys = set(body_chs) | set(end_chs)
    page_indices = {p.get("source_image", ""): i for i, p in enumerate(pages)}
    def get_ch_sort_key(ch_idx):
        bch = body_chs.get(ch_idx, {})
        ech = end_chs.get(ch_idx, {})
        first_page = bch.get("first_page") or (ech.get("pages", [None])[0])
        return page_indices.get(first_page, 999999)

    for idx in sorted(all_keys, key=get_ch_sort_key):
        bch = body_chs.get(idx, {})
        ech = end_chs.get(idx, {})
        body_sups = set(bch.get("body_sups", []))
        entries   = set(ech.get("entries", []))
        missing_entries = sorted(body_sups - entries)
        missing_sups    = sorted(entries - body_sups)

        b_raw  = bch.get("title_raw", "")
        e_raw  = ech.get("title_raw", "")
        b_norm = bch.get("title_norm", "")
        e_norm = ech.get("title_norm", "")

        _e_text = re.sub(r'<[^>]+>', '', e_raw).strip()
        if not body_sups and not entries:
            match_type, name_match = "exact", True
        elif not b_raw:
            match_type, name_match = "notes_only", False
        elif not e_raw:
            match_type, name_match = "body_only", False
        elif b_norm == e_norm:
            match_type = "exact" if b_raw.lower() == e_raw.lower() else "prefix_stripped"
            name_match = True
        elif isinstance(idx, int) and idx >= 0 and re.fullmatch(r'Chapter\s+\d+', _e_text, re.IGNORECASE):
            # Notes section only has "Chapter N" — no title to compare, match by number.
            match_type, name_match = "chapter_num_only", True
        else:
            if b_norm == e_norm or (isinstance(idx, int) and idx < 0 and (b_norm in e_norm or e_norm in b_norm)):
                match_type = "exact"
                name_match = True
            else:
                match_type, name_match = "mismatch", False

        ch_rec = {
            "index":            idx,
            "body_title_raw":   b_raw,
            "notes_title_raw":  e_raw,
            "body_title_norm":  b_norm,
            "notes_title_norm": e_norm,
            "name_match":       name_match,
            "name_match_type":  match_type,
            "body_pages":       bch.get("pages", []),
            "endnote_pages":    ech.get("pages", []),
            "body_sups":        sorted(body_sups),
            "endnote_entries":  sorted(entries),
            "sup_to_page":      {str(k): v for k, v in bch.get("sup_to_page", {}).items()},
            "entry_to_page":    {str(k): v for k, v in ech.get("entry_to_page", {}).items()},
            "missing_entries":  missing_entries,
            "missing_sups":     missing_sups,
            "state":            "green" if (not missing_entries and not missing_sups and name_match) else "red",
            "badge":            f"{len(body_sups)}↑·{len(entries)}fn",
        }
        chapters_out.append(ch_rec)
        ch_lookup[idx] = ch_rec

    # ── Per-page tiles ────────────────────────────────────────────────────────
    pages_out = []
    for page in pages:
        if page.get("ignored"):
            continue
        pname = page.get("source_image", "")
        # Sort areas top-to-bottom using their polygon Y-coordinates to maintain correct reading/parsing order
        def get_top_y(a):
            poly = a.get("polygon")
            return min(pt[1] for pt in poly) if poly else 999999
        areas = sorted([a for a in (page.get("areas") or []) if a], key=get_top_y)

        if pname in endnote_page_names:
            ch_idx = page_to_ech.get(pname)
            page_entries: set[int] = set()
            chapter_sub_starts: list[str] = []
            cur_tile_ech: int | None = ch_idx
            for area in areas:
                atype = area.get("type")
                if atype in ("subtitle", "chapter_title"):
                    t = (area.get("text") or "").strip()
                    _m = _CH_WORD_TITLE_RE.match(t)
                    if _m:
                        cur_tile_ech = int(_m.group(1))
                        chapter_sub_starts.append(t.replace("\n", " "))
                    else:
                        if not _is_inline_notes_title(t) and not _ENTRY_LINE_RE.match(t):
                            norm = _normalize_ch_title(t)
                            if norm in unnum_title_to_idx:
                                cur_tile_ech = unnum_title_to_idx[norm]
                                chapter_sub_starts.append(t.replace("\n", " "))
                if atype == "main_text":
                    raw = area.get("text") or ""
                    first_line = raw.split("\n")[0].strip()
                    _m = _CH_WORD_TITLE_RE.match(first_line)
                    if _m:
                        cur_tile_ech = int(_m.group(1))
                        chapter_sub_starts.append(first_line)
                    else:
                        if not _is_inline_notes_title(first_line) and not _ENTRY_LINE_RE.match(first_line) and len(first_line) < 100:
                            norm = _normalize_ch_title(first_line)
                            if norm in unnum_title_to_idx:
                                cur_tile_ech = unnum_title_to_idx[norm]
                                chapter_sub_starts.append(first_line)
                    if cur_tile_ech is not None:
                        for em in _ENTRY_LINE_RE.finditer(raw):
                            page_entries.add(int(em.group(1)))

            ch_data   = ch_lookup.get(ch_idx)
            body_sups = set(ch_data["body_sups"]) if ch_data else set()
            unlinked  = sorted(page_entries - body_sups)

            pstate = ("green" if page_entries and not unlinked
                      else "red" if unlinked else "blue")
            if page_entries:
                mn_e, mx_e = min(page_entries), max(page_entries)
                pbadge = f"{mn_e}–{mx_e}" if mn_e != mx_e else str(mn_e)
            else:
                pbadge = ""

            pages_out.append({
                "name":                    pname,
                "chapter_index":           ch_idx,
                "zone":                    "endnote",
                "state":                   pstate,
                "badge":                   pbadge,
                "entries":                 sorted(page_entries),
                "unlinked_entries":        unlinked,
                "endnotes_section_start":  pname == endnotes_start,
                "chapter_subtitle_starts": chapter_sub_starts,
            })
        else:
            ch_idx = page_to_bch.get(pname)
            sups:          set[int]   = set()
            chapter_start             = False
            chapter_title_text        = None
            section_starts: list[str] = []
            for area in areas:
                if area.get("type") in _FN_BODY_TYPES:
                    for m2 in _FN_SUP_TAG_RE.finditer(_area_sup_text(area)):
                        sups.add(int(m2.group(1)))
                if area.get("type") == "chapter_title":
                    t = (area.get("text") or "").strip()
                    tr = t.replace("\n", " ")
                    if _CH_NUM_TITLE_RE.match(t) or (not _is_inline_notes_title(t) and _normalize_ch_title(t) in unnum_title_to_idx):
                        chapter_start      = True
                        chapter_title_text = tr
                    else:
                        section_starts.append(tr)

            ch_data  = ch_lookup.get(ch_idx)
            entries  = set(ch_data["endnote_entries"]) if ch_data else set()
            unlinked = sorted(sups - entries) if sups else []
            sups_list = sorted(sups)
            internal_gap_missing = []
            for i in range(len(sups_list) - 1):
                if sups_list[i + 1] > sups_list[i] + 1:
                    internal_gap_missing.extend(range(sups_list[i] + 1, sups_list[i + 1]))

            pages_out.append({
                "name":                 pname,
                "chapter_index":        ch_idx,
                "zone":                 "body",
                "state":                "none" if not sups else ("red" if internal_gap_missing else "green"),
                "badge":                (f"{len(sups)}↑ [{min(sups)}–{max(sups)}]" if len(sups) > 1
                                         else f"1↑ [{min(sups)}]" if sups else ""),
                "sups":                 sups_list,
                "unlinked_sups":        unlinked,
                "internal_gap_missing": internal_gap_missing,
                "chapter_start":        chapter_start,
                "chapter_title":        chapter_title_text,
                "section_starts":       section_starts,
            })

    # ── Gap detection (body sup sequence gaps per chapter) ────────────────────
    gaps: list[dict] = []
    ch_page_sups: dict[int, list[tuple[str, list[int]]]] = {}
    for p in pages_out:
        if p.get("zone") == "body" and p.get("chapter_index") is not None and p.get("sups"):
            ch_page_sups.setdefault(p["chapter_index"], []).append((p["name"], p["sups"]))

    for ch_idx, page_sups_list in ch_page_sups.items():
        prev_num: int | None = None
        prev_page: str | None = None
        for pname, sups in page_sups_list:
            if not sups:
                continue
            if prev_num is None:
                # chapter doesn't start at 1
                if sups[0] > 1:
                    gaps.append({"before": pname, "missing": list(range(1, sups[0])), "chapter_index": ch_idx})
            elif sups[0] > prev_num + 1:
                # between-page gap: flag goes after prev tile
                gaps.append({"after": prev_page, "missing": list(range(prev_num + 1, sups[0])), "chapter_index": ch_idx})
            # within-page gaps are handled by marking the tile red (internal_gap_missing)
            prev_num = sups[-1]
            prev_page = pname

    return {
        "endnotes_start_page":     endnotes_start,
        "endnotes_start_detected": not was_explicit,
        "chapters":                chapters_out,
        "pages":                   pages_out,
        "gaps":                    gaps,
    }


@app.route("/projects/<pid>/review-chapter-endnotes")
def review_chapter_endnotes(pid: str):
    project = _get_project(pid)
    if not project:
        return "Project not found", 404
    bj = _book_json(project)
    regime = ""
    if bj and bj.exists():
        try:
            regime = json.loads(bj.read_text(encoding="utf-8")).get("footnote_regime") or ""
        except Exception:
            pass
    return render_template("review_chapter_endnotes.html", project=project, regime=regime)


@app.route("/projects/<pid>/review-chapter-endnotes/chapter")
def review_chapter_endnote_page(pid: str):
    project = _get_project(pid)
    if not project:
        return "Project not found", 404
    return render_template("review_chapter_endnote_page.html", project=project)


@app.route("/projects/<pid>/api/chapter-endnote-review")
def api_chapter_endnote_review(pid: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    bj = _book_json(project)
    if not bj or not bj.exists():
        return jsonify({"error": "no JSON"}), 404
    book_data = json.loads(bj.read_text(encoding="utf-8"))
    if book_data.get("footnote_regime") == "inline_endnotes":
        return jsonify(_build_inline_endnote_data(book_data))
    return jsonify(_build_chapter_endnote_data(book_data))


@app.route("/projects/<pid>/api/chapter-endnote-review/<ch_index>")
def api_chapter_endnote_review_ch(pid: str, ch_index: str):
    try:
        ch_idx = int(ch_index)
    except ValueError:
        return jsonify({"error": "invalid chapter index"}), 400
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    bj = _book_json(project)
    if not bj or not bj.exists():
        return jsonify({"error": "no JSON"}), 404
    book_data = json.loads(bj.read_text(encoding="utf-8"))
    builder = (_build_inline_endnote_data if book_data.get("footnote_regime") == "inline_endnotes"
               else _build_chapter_endnote_data)
    data = builder(book_data)
    ch = next((c for c in data["chapters"] if c["index"] == ch_idx), None)
    if not ch:
        return jsonify({"error": f"chapter {ch_index} not found"}), 404
    return jsonify(ch)


@app.route("/projects/<pid>/api/set-endnotes-start-page", methods=["POST"])
def api_set_endnotes_start_page(pid: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    bj = _book_json(project)
    if not bj or not bj.exists():
        return jsonify({"error": "no JSON"}), 404
    body = request.get_json(silent=True) or {}
    page_name = (body.get("page_name") or "").strip()
    def _apply(data: dict) -> None:
        if page_name:
            data["endnotes_start_page"] = page_name
        else:
            data.pop("endnotes_start_page", None)
    json_broker.mutate(bj, _apply)
    return jsonify({"ok": True, "endnotes_start_page": page_name or None})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=True, threaded=True)
