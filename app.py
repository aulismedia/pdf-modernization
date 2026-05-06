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

PROJECT_ROOT = Path(__file__).parent
PROJECTS_FILE = PROJECT_ROOT / "projects.json"
app = Flask(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

_SMART_QUOTE_CHARS = "‘’""′″ʼ"

# ── Footnote review helpers ───────────────────────────────────────────────────

_FN_USUP_CHARS = "⁰¹²³⁴⁵⁶⁷⁸⁹"
_FN_BODY_TYPES = {"main_text", "chapter_title", "subtitle"}
_FN_SUP_TAG_RE       = re.compile(r"<sup>(\d+)</sup>", re.IGNORECASE)
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
        text = area.get("text") or ""
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
    """Canonical stem for the areas JSON: source file stem, or 'author - title' for folders."""
    if _source_type(project) == "folder":
        author = (project.get("author") or "").strip()
        title  = (project.get("title")  or "").strip()
        return f"{author} - {title}" if author else title
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


def _try_recover_from_sidecars(project: dict, json_path: Path) -> None:
    """Rebuild main JSON from per-page sidecar files if they exist."""
    pages_dir = _pages_dir(project)
    if not pages_dir.exists():
        return
    pages: list[dict] = []
    for sc in sorted(pages_dir.glob("*-areas.json")):
        try:
            data = json.loads(sc.read_text(encoding="utf-8"))
            if data.get("source_image"):
                pages.append(data)
        except Exception:
            pass
    if not pages:
        return
    output = {
        "book":        project.get("title", _json_stem(project)),
        "total_pages": len(pages),
        "pages":       sorted(pages, key=lambda p: p["source_image"]),
    }
    _write_atomic(json_path, json.dumps(output, ensure_ascii=False, indent=2))
    print(f"[recovery] Rebuilt {json_path.name} from {len(pages)} sidecar files.")


def _book_json(project: dict) -> Path | None:
    p = _book_dir(project) / f"{_json_stem(project)}.json"
    if p.exists():
        if p.stat().st_size > 2:
            return p
        # File exists but is empty/near-empty — try sidecar recovery
        _try_recover_from_sidecars(project, p)
        return p if p.exists() else None
    # File missing — try sidecar recovery
    _try_recover_from_sidecars(project, p)
    return p if p.exists() else None


def _write_meta_to_json(project: dict) -> None:
    """Sync project metadata into the areas JSON top-level meta field."""
    book_dir = _book_dir(project)
    if not book_dir.exists():
        return
    json_path = book_dir / f"{_json_stem(project)}.json"
    data: dict = {}
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    data.setdefault("book", project.get("title", ""))
    data["meta"] = {
        "title":      project.get("title", ""),
        "author":     project.get("author", ""),
        "year":       project.get("year", ""),
        "cover_path": project.get("cover_path", ""),
    }
    _write_atomic(json_path, json.dumps(data, ensure_ascii=False, indent=2))


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
            area_pages      = sum(1 for p in ps if p.get("areas"))
            total_areas     = sum(len(p.get("areas", [])) for p in ps)
            ignored_pages   = sum(1 for p in ps if p.get("ignored"))
            processed_pages = sum(1 for p in ps if p.get("page_dimensions"))
            step2_done      = processed_pages > 0
            # pending = non-ignored pages in JSON that haven't been processed yet
            pending_area_pages = sum(
                1 for p in ps
                if not p.get("ignored") and not p.get("page_dimensions")
            )
            process_later_pending = sum(
                1 for p in ps
                if not p.get("ignored")
                and p.get("process_later")
                and not (p.get("detected_by") or "").startswith("claudecode")
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


def _run_step2_with_step3(pid: str, src: Path, book_name: str | None = None, styles: bool = False) -> None:
    """Run step 2; visualization is done inline per page inside step2."""
    cmd = [sys.executable, "-u", "steps/step2_detect_areas.py", str(src)]
    if book_name:
        cmd += ["--book-name", book_name]
    if styles:
        cmd += ["--styles"]
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


def _run_process_later_opus(pid: str, src: Path, book_name: str | None = None, styles: bool = False) -> None:
    cmd = [sys.executable, "-u", "steps/step2_detect_areas.py", str(src),
           "--process-later-only"]
    if book_name:
        cmd += ["--book-name", book_name]
    if styles:
        cmd += ["--styles"]
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
    if book_name:
        step6_cmd += ["--book-name", book_name]
    _run_sequence(pid, [
        (step6_cmd, "Step 6: Cleanup Text"),
        (_consolidate_cmd(src, book_name), "Consolidate Footnotes"),
    ])


def _run_step6_resume(pid: str, src: Path, book_name: str | None = None) -> None:
    step6_cmd = [sys.executable, "-u", "steps/step6_polish_text.py", str(src), "--resume"]
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
        _image_exts = {".png", ".jpg", ".jpeg", ".tif", ".tiff"}
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
        elif (regime == "endnotes"
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

    seq_gaps = _fn_sequence_gaps(pages)

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
    allowed = {None, "", "per_page", "endnotes", "mixed"}
    if regime not in allowed:
        return jsonify({"error": f"invalid regime: {regime!r}"}), 400
    book_data = json.loads(bj.read_text(encoding="utf-8"))
    if regime:
        book_data["footnote_regime"] = regime
    else:
        book_data.pop("footnote_regime", None)
    bj.write_text(json.dumps(book_data, ensure_ascii=False, indent=2), encoding="utf-8")
    return jsonify({"ok": True, "footnote_regime": regime or None})


@app.route("/projects/<pid>/api/normalize-all", methods=["POST"])
def api_normalize_all(pid: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    bj = _book_json(project)
    if not bj or not bj.exists():
        return jsonify({"error": "no JSON"}), 404

    book_data = json.loads(bj.read_text(encoding="utf-8"))
    pages     = book_data.get("pages", [])

    pages_changed = 0
    for page in pages:
        if page.get("ignored"):
            continue
        areas = [a for a in (page.get("areas") or []) if a]
        if not any(a.get("type") in ("main_text", "footnote") for a in areas):
            continue
        new_areas, changed = _normalize_page_areas(areas)
        if changed:
            page["areas"] = new_areas
            pages_changed += 1

    if pages_changed:
        bj.write_text(json.dumps(book_data, ensure_ascii=False, indent=2), encoding="utf-8")

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

    data  = json.loads(bj.read_text(encoding="utf-8"))
    pages = data.get("pages", [])

    # Find the consolidated area
    target_page = next((p for p in pages if p.get("source_image") == page_name), None)
    if not target_page:
        return jsonify({"error": "page not found"}), 404
    cons_area = next(
        (a for a in (target_page.get("areas") or []) if a.get("id") == area_id),
        None,
    )
    if not cons_area:
        return jsonify({"error": "area not found"}), 404
    if not cons_area.get("consolidated"):
        return jsonify({"error": "area is not consolidated"}), 400

    cont_text = (cons_area.get("text") or "").strip()

    # Walk pages forward (same as consolidate) to find which area received the append.
    # last_fn_area just before processing this consolidated area is the recipient.
    last_fn_area = None
    found = False
    for page in pages:
        if page.get("ignored"):
            last_fn_area = None
            continue
        fn_areas = [a for a in (page.get("areas") or []) if a and a.get("type") == "footnote"]
        if not fn_areas:
            last_fn_area = None
            continue
        for area in fn_areas:
            if area.get("id") == area_id and page.get("source_image") == page_name:
                found = True
                break
            if not area.get("consolidated"):
                last_fn_area = area
        if found:
            break

    if not last_fn_area:
        return jsonify({"error": "could not locate recipient area"}), 500

    # Reverse _join_text: strip cont_text from the end of last_fn_area["text"]
    base = last_fn_area.get("text") or ""
    if base.endswith(" " + cont_text):
        last_fn_area["text"] = base[: -(len(cont_text) + 1)]
    elif base.endswith(cont_text):
        # hyphen-merge case: restore the hyphen
        last_fn_area["text"] = base[: -len(cont_text)] + "-"
    # else: text was already manually edited — leave it, just unmark the area

    # Unmark the consolidated area
    cons_area.pop("consolidated", None)
    cons_area["is_running_continuation"] = False

    # Recompute footnote_consolidated flag on the target page
    has_any_cons = any(
        a.get("consolidated")
        for a in (target_page.get("areas") or [])
    )
    if has_any_cons:
        target_page["footnote_consolidated"] = True
    else:
        target_page.pop("footnote_consolidated", None)

    bj.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return jsonify({"ok": True, "recipient_area_id": last_fn_area.get("id")})


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

    data  = json.loads(bj.read_text(encoding="utf-8"))
    pages = data.get("pages", [])

    target_page = next((p for p in pages if p.get("source_image") == page_name), None)
    if not target_page:
        return jsonify({"error": "page not found"}), 404
    area = next(
        (a for a in (target_page.get("areas") or []) if a.get("id") == area_id),
        None,
    )
    if not area:
        return jsonify({"error": "area not found"}), 404
    if area.get("consolidated"):
        return jsonify({"error": "area is already consolidated"}), 400

    cont_text = (area.get("text") or "").strip()

    # Walk forward to find last non-consolidated footnote area before this one
    last_fn_area = None
    found = False
    for page in pages:
        if page.get("ignored"):
            last_fn_area = None
            continue
        fn_areas = [a for a in (page.get("areas") or []) if a and a.get("type") == "footnote"]
        if not fn_areas:
            last_fn_area = None
            continue
        for a in fn_areas:
            if a.get("id") == area_id and page.get("source_image") == page_name:
                found = True
                break
            if not a.get("consolidated"):
                last_fn_area = a
        if found:
            break

    if not last_fn_area:
        return jsonify({"error": "no previous footnote area to consolidate into"}), 400

    def _join_text(base: str, cont: str) -> str:
        base = base.rstrip()
        cont = cont.strip()
        if not cont:
            return base
        if base.endswith("-"):
            return base[:-1] + cont
        return base + " " + cont

    last_fn_area["text"] = _join_text(last_fn_area.get("text") or "", cont_text)
    area["consolidated"] = True
    area.pop("is_running_continuation", None)
    target_page["footnote_consolidated"] = True

    bj.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return jsonify({"ok": True, "recipient_area_id": last_fn_area.get("id")})


@app.route("/projects/<pid>/api/book")
def api_book(pid: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    folder = _book_dir(project)
    bj     = _book_json(project)

    bj_data: dict | None = None
    if bj:
        bj_data = json.loads(bj.read_text(encoding="utf-8"))
        ps = bj_data.get("pages", [])
        if ps:
            return jsonify({
                "book":      bj_data.get("book", project["title"]),
                "has_areas": any(p.get("page_dimensions") for p in ps),
                "pages": [
                    {
                        "name":          p["source_image"],
                        "prohibited":    p.get("prohibited", False),
                        "ignored":       p.get("ignored", False),
                        "process_later": p.get("process_later", False),
                        "detected_by":   p.get("detected_by", None),
                    }
                    for p in ps
                ],
            })

    # No areas yet — list raw page images; ignored flags come from JSON stubs
    ignored_set: set[str] = set()
    if bj_data:
        ignored_set = {p["source_image"] for p in bj_data.get("pages", []) if p.get("ignored")}

    pages_dir = _pages_dir(project)
    src_type  = _source_type(project)
    if not pages_dir.exists():
        if src_type == "folder":
            exts = ("*.png", "*.jpg", "*.jpeg", "*.tif", "*.tiff")
            pgs  = [f for ext in exts for f in sorted(folder.glob(ext))]
            if pgs:
                return jsonify({
                    "book":      project["title"],
                    "has_areas": False,
                    "pages": [
                        {"name": f.name, "prohibited": False, "ignored": f.name in ignored_set}
                        for f in pgs
                    ],
                })
        return jsonify({"error": "no pages"}), 404

    pgs = sorted(pages_dir.glob("page*.png"))
    return jsonify({
        "book":      project["title"],
        "has_areas": False,
        "pages": [
            {"name": f.name, "prohibited": False, "ignored": f.name in ignored_set}
            for f in pgs
        ],
    })


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
    if bj_data:
        for p in bj_data.get("pages", []):
            if p["source_image"] == page_name:
                ignored       = p.get("ignored", False)
                process_later = p.get("process_later", False)
                content_bbox  = p.get("content_bbox")
                break

    page_resp = {
        "source_image":    page_name,
        "ignored":         ignored,
        "process_later":   process_later,
        "page_dimensions": {"width": w, "height": h},
        "areas":           [],
    }
    if content_bbox:
        page_resp["content_bbox"] = content_bbox
    return jsonify(page_resp)


@app.route("/projects/<pid>/api/page/<page_name>", methods=["POST"])
def api_save_page(pid: str, page_name: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    bj = _book_json(project)
    if not bj or not bj.exists():
        return jsonify({"error": "no areas JSON — run detection first"}), 400
    data    = json.loads(bj.read_text(encoding="utf-8"))
    payload = request.get_json()
    for p in data["pages"]:
        if p["source_image"] == page_name:
            p["areas"] = payload["areas"]
            break
    _write_atomic(bj, json.dumps(data, ensure_ascii=False, indent=2))
    return jsonify({"ok": True})


@app.route("/projects/<pid>/api/page/<page_name>/content-bbox", methods=["POST"])
def api_save_content_bbox(pid: str, page_name: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    json_path = _book_dir(project) / f"{_json_stem(project)}.json"
    data: dict = {}
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    payload = request.get_json()
    cb = payload.get("content_bbox")
    if not cb or not all(k in cb for k in ("left", "top", "right", "bottom")):
        return jsonify({"error": "invalid content_bbox"}), 400
    pages_map = {p["source_image"]: p for p in data.get("pages", [])}
    if page_name not in pages_map:
        pages_map[page_name] = {"source_image": page_name}
    pages_map[page_name]["content_bbox"] = cb
    data["pages"] = sorted(pages_map.values(), key=lambda p: p["source_image"])
    _write_atomic(json_path, json.dumps(data, ensure_ascii=False, indent=2))
    return jsonify({"ok": True})


@app.route("/projects/<pid>/api/page/<page_name>/process-later", methods=["POST"])
def api_toggle_process_later(pid: str, page_name: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404

    json_path = _book_dir(project) / f"{_json_stem(project)}.json"
    data: dict = {}
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    pages_map = {p["source_image"]: p for p in data.get("pages", [])}
    if page_name not in pages_map:
        pages_map[page_name] = {"source_image": page_name}
    entry = pages_map[page_name]
    entry["process_later"] = not entry.get("process_later", False)
    new_state = entry["process_later"]
    data["pages"] = sorted(pages_map.values(), key=lambda p: p["source_image"])
    _write_atomic(json_path, json.dumps(data, ensure_ascii=False, indent=2))
    return jsonify({"ok": True, "process_later": new_state})


@app.route("/projects/<pid>/api/page/<page_name>/rotation", methods=["POST"])
def api_save_rotation(pid: str, page_name: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404
    json_path = _book_dir(project) / f"{_json_stem(project)}.json"
    data: dict = {}
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    payload = request.get_json() or {}
    rotation = int(payload.get("rotation", 0)) % 360
    pages_map = {p["source_image"]: p for p in data.get("pages", [])}
    if page_name not in pages_map:
        pages_map[page_name] = {"source_image": page_name}
    if rotation:
        pages_map[page_name]["rotation"] = rotation
    else:
        pages_map[page_name].pop("rotation", None)
    data["pages"] = sorted(pages_map.values(), key=lambda p: p["source_image"])
    _write_atomic(json_path, json.dumps(data, ensure_ascii=False, indent=2))
    return jsonify({"ok": True})


@app.route("/projects/<pid>/api/page/<page_name>/ignore", methods=["POST"])
def api_toggle_ignore(pid: str, page_name: str):
    project = _get_project(pid)
    if not project:
        return jsonify({"error": "not found"}), 404

    json_path = _book_dir(project) / f"{_json_stem(project)}.json"
    data: dict = {}
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    pages_map = {p["source_image"]: p for p in data.get("pages", [])}
    if page_name not in pages_map:
        pages_map[page_name] = {"source_image": page_name}
    entry = pages_map[page_name]
    entry["ignored"] = not entry.get("ignored", False)
    new_state = entry["ignored"]
    data["pages"] = sorted(pages_map.values(), key=lambda p: p["source_image"])
    _write_atomic(json_path, json.dumps(data, ensure_ascii=False, indent=2))
    return jsonify({"ok": True, "ignored": new_state})


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
    from utils.config import OPEN_ROUTER_APIKEY
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

    if not OPEN_ROUTER_APIKEY:
        return jsonify({"error": "No OpenRouter API key configured"}), 500

    b64 = base64.b64encode(image_bytes).decode()
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        json={
            "model": "anthropic/claude-sonnet-4-6",
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

    for p in data.get("pages", []):
        if p["source_image"] == page_name:
            for a in p.get("areas", []):
                if a.get("id") == area_id:
                    a["table_html"] = html
                    break
            break
    _write_atomic(bj, json.dumps(data, ensure_ascii=False, indent=2))
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


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=True, threaded=True)
