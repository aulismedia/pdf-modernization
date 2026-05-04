#!/usr/bin/env python3
"""
Step 2: Send each page image to an AI model for area detection and OCR.

Model selection is driven by utils/detect_models.cfg.  Comment out any entry
with ## to disable it temporarily (e.g. after a quota exhaustion).

Usage:
    python step2_detect_areas.py <path_to_pdf> [--book-name "Title"]
                                 [--pages-dir output/pages] [--json-dir output/json]
                                 [--page 57]   # single page, list (9,10,11), range (10-20), or mixed
                                 [--model anthropic/claude-opus-4-7]  # force a specific model
                                 [--workers N] # parallel workers (default: 1)
"""

import argparse
import base64
import io
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from PIL import Image
from tqdm import tqdm

from utils.config import (
    OPEN_ROUTER_APIKEY,
    OPENROUTER_MODEL,
    book_dirs,
)
from utils.models import load_detect_models, active_model_labels
from prompts.detect_areas import DETECT_AREAS_PROMPT, DETECT_AREAS_STYLES_ADDON
from step3_visualize_areas import visualize_page

# Overridden in main() when --styles is passed
_ACTIVE_PROMPT = DETECT_AREAS_PROMPT

# ---------------------------------------------------------------------------
# Image pre-processing
# ---------------------------------------------------------------------------

MAX_PX = 1500          # longest edge; larger than OCR-only tasks to preserve detail
MAX_OUTPUT_TOKENS = 20000


def prepare_image(
    path: Path,
    content_bbox: tuple[int, int, int, int] | None = None,
) -> tuple[bytes, int, int]:
    """Returns (jpeg_bytes, orig_width, orig_height).

    If content_bbox is given the image is cropped to that rectangle before
    resizing, so the model receives only the content area at full resolution.
    orig_width/orig_height are always the full page dimensions.
    """
    img = Image.open(path).convert("L").convert("RGB")
    orig_w, orig_h = img.size
    if content_bbox:
        img = img.crop(content_bbox)
    send_w, send_h = img.size
    scale = min(MAX_PX / send_w, MAX_PX / send_h, 1.0)
    if scale < 1.0:
        img = img.resize((int(send_w * scale), int(send_h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue(), orig_w, orig_h


# ---------------------------------------------------------------------------
# Retry helpers
# ---------------------------------------------------------------------------

def _is_unavailable(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in ("503", "unavailable", "overloaded", "rate limit", "429"))


def _retry_wait(attempt: int) -> int:
    return min(2 ** attempt, 60)


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

class _AreaValidationError(Exception):
    """Raised when a model returns structurally invalid area data. Never retried."""


def _escape_control_chars_in_strings(s: str) -> str:
    """Escape literal control chars and unescaped double quotes inside JSON string values."""
    _escapes = {'\n': '\\n', '\r': '\\r', '\t': '\\t'}
    # Characters that can legally follow a closing string quote in JSON
    _json_structural = {',', '}', ']', ':'}
    result = []
    in_string = False
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == '\\' and in_string:
            result.append(ch)
            i += 1
            if i < len(s):
                result.append(s[i])
                i += 1
            continue
        if ch == '"':
            if in_string:
                # Peek at next non-whitespace character. If it is not a JSON structural
                # character, this quote is unescaped content (e.g. OCR'd quotation mark
                # inside a text field) — escape it instead of closing the string.
                j = i + 1
                while j < len(s) and s[j] in ' \t\r\n':
                    j += 1
                if j < len(s) and s[j] not in _json_structural:
                    result.append('\\"')
                else:
                    in_string = False
                    result.append(ch)
            else:
                in_string = True
                result.append(ch)
        elif in_string and ch in _escapes:
            result.append(_escapes[ch])
        else:
            result.append(ch)
        i += 1
    return ''.join(result)


def _recover_partial_areas(cleaned: str) -> dict | None:
    """Extract all complete area objects from a truncated/malformed JSON response."""
    pd_match = re.search(r'"page_dimensions"\s*:\s*(\{[^}]+\})', cleaned)
    page_dims = json.loads(pd_match.group(1)) if pd_match else {}

    areas_start = cleaned.find('"areas"')
    if areas_start < 0:
        return None
    bracket_pos = cleaned.find('[', areas_start)
    if bracket_pos < 0:
        return None

    content = cleaned[bracket_pos + 1:]
    areas = []
    depth = 0
    in_string = False
    obj_start = None
    i = 0
    while i < len(content):
        ch = content[i]
        if ch == '\\' and in_string:
            i += 2
            continue
        if ch == '"':
            in_string = not in_string
        elif not in_string:
            if ch == '{':
                if depth == 0:
                    obj_start = i
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0 and obj_start is not None:
                    try:
                        areas.append(json.loads(content[obj_start:i + 1]))
                    except Exception:
                        pass
                    obj_start = None
        i += 1

    if not areas:
        return None
    result: dict = {"areas": areas}
    if page_dims:
        result["page_dimensions"] = page_dims
    return result


def _patch_truncated_text_string(cleaned: str) -> dict | None:
    """Recover a response cut off inside a JSON string value.

    The partial text is already valid JSON string content — we just need to
    close the open string and any unclosed containers in correct nesting order.
    """
    try:
        try:
            json.loads(cleaned)
            return None
        except json.JSONDecodeError as e:
            if "Unterminated string" not in e.msg:
                return None
            err = e

        # Walk to the unterminated string's opening quote, tracking open
        # containers on a stack so we can close them in reverse order.
        stack: list[str] = []
        in_str = False
        i = 0
        while i < err.pos:
            ch = cleaned[i]
            if ch == '\\' and in_str:
                i += 2
                continue
            if ch == '"':
                in_str = not in_str
            elif not in_str:
                if   ch == '{': stack.append('}')
                elif ch == '[': stack.append(']')
                elif ch in ']}': stack.pop() if stack else None
            i += 1

        # Partial text is already valid JSON-escaped content.
        # Strip a trailing bare backslash (incomplete escape sequence).
        partial = cleaned[err.pos + 1:].rstrip()
        if partial.endswith('\\') and not partial.endswith('\\\\'):
            partial = partial[:-1]

        close = ''.join(reversed(stack))

        # Try as a string value first, then as a key (no ":" follows → add :"").
        for mid in ('', ':""'):
            candidate = cleaned[:err.pos] + '"' + partial + '"' + mid + close
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
        return None
    except Exception:
        return None


def _extract_json(raw: str) -> dict:
    cleaned = re.sub(r"^```[a-z]*\s*", "", raw.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    # Gemini sometimes emits a spurious ,"null," token between properties — strip it
    cleaned = cleaned.replace(',"null,"', ',"')
    # Models sometimes emit invalid JSON escapes like \v — strip the backslash
    cleaned = re.sub(r'\\([^"\\/bfnrtu])', r'\1', cleaned)
    # Gemini sometimes omits the closing ] of the polygon array before ,"text":
    cleaned = re.sub(r'(\[\d+,\d+\])(,"text":)', r'\1]\2', cleaned)
    # Escape literal control characters (newlines, tabs) inside string values
    cleaned = _escape_control_chars_in_strings(cleaned)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # Gemini sometimes prepends truncated/garbage text before a fenced code block
        # containing the real JSON — extract the last ```json...``` block and retry.
        fenced = re.search(r"```[a-z]*\s*(\{.*?\})\s*```", raw, re.DOTALL | re.IGNORECASE)
        if fenced:
            candidate = fenced.group(1).replace(',"null,"', ',"')
            try:
                data = json.loads(candidate)
                _validate_areas(data)
                return data
            except (json.JSONDecodeError, Exception):
                pass
        # Try to salvage complete areas from a truncated/malformed response
        recovered = _recover_partial_areas(cleaned)
        if recovered:
            print(f"    [partial recovery: {len(recovered.get('areas', []))} area(s) salvaged]")
            _validate_areas(recovered)
            return recovered
        # Try to patch a response truncated mid-text-string (unterminated string in "text" field)
        patched = _patch_truncated_text_string(cleaned)
        if patched:
            print(f"    [truncated-text recovery: patched mid-string cutoff]")
            _validate_areas(patched)
            return patched
        # Re-raise with context from the original cleaned string
        try:
            json.loads(cleaned)
        except json.JSONDecodeError as e:
            ctx_start = max(0, e.pos - 120)
            ctx_end   = min(len(cleaned), e.pos + 120)
            print(f"\n--- JSON ERROR at char {e.pos}: {e.msg} ---")
            print(f"...{cleaned[ctx_start:ctx_end]}...")
            print(f"    {' ' * (e.pos - ctx_start - 3)}^\n--- END ---\n")
            raise
    _validate_areas(data)
    return data


def _validate_areas(data: dict) -> None:
    valid = []
    for area in data.get("areas", []):
        polygon = area.get("polygon", [])
        # Qwen sometimes encodes a bbox as a single [x1,y1,x2,y2] vertex — expand it
        if len(polygon) == 1 and len(polygon[0]) == 4:
            x1, y1, x2, y2 = polygon[0]
            area["polygon"] = [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
            polygon = area["polygon"]
        # Qwen sometimes mixes a 4-value first vertex with normal vertices — strip extra coords
        elif any(len(pt) > 2 for pt in polygon):
            area["polygon"] = [[pt[0], pt[1]] for pt in polygon]
            polygon = area["polygon"]

        unique = list(dict.fromkeys(tuple(pt) for pt in polygon))
        if len(unique) < 4:
            # Try to expand to a bounding-box rectangle
            xs = [p[0] for p in unique]
            ys = [p[1] for p in unique]
            x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
            area["polygon"] = [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
            polygon = area["polygon"]

        if len(polygon) != 4:
            print(f"\n--- INVALID AREA ---\n{json.dumps(area, indent=2)}\n--- END ---\n")
            raise _AreaValidationError(
                f"Polygon in {area.get('id')} has {len(polygon)} vertices (must be exactly 4)"
            )

        if len({tuple(pt) for pt in polygon}) < 4:
            # Still degenerate after repair (zero-size point or line) — drop this area only
            print(f"    [skip] {area.get('id')} is zero-size after repair — dropped")
            continue

        valid.append(area)

    data["areas"] = valid


def _write_error(img_path: Path, raw: str) -> None:
    error_path = img_path.with_suffix(".error")
    error_path.write_text(raw, encoding="utf-8")
    print(f"    [error saved → {error_path.name}]")


def detect_openrouter(image_bytes: bytes, img_path: Path, model: str = OPENROUTER_MODEL) -> dict:
    if not OPEN_ROUTER_APIKEY:
        raise RuntimeError("No OpenRouter API key found. Set OPEN_ROUTER_APIKEY in .env")

    b64 = base64.b64encode(image_bytes).decode()
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": _ACTIVE_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]}],
        "temperature": 0,
        "max_tokens": MAX_OUTPUT_TOKENS,
    }
    last_raw = ""
    for attempt in range(1, 6):
        try:
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                json=payload,
                headers={
                    "Authorization": f"Bearer {OPEN_ROUTER_APIKEY}",
                    "Content-Type": "application/json",
                },
                timeout=120,
            )
            if not resp.ok:
                error_body = (
                    f"HTTP {resp.status_code}\n"
                    f"Headers: {dict(resp.headers)}\n"
                    f"Body: {resp.text or '(empty)'}\n"
                    f"Payload model: {model}"
                )
                last_raw = error_body
            resp.raise_for_status()
            last_raw = resp.json()["choices"][0]["message"]["content"].strip()
            finish = resp.json()["choices"][0].get("finish_reason", "?")
            if finish not in ("stop", "end_turn"):
                print(f"    [finish_reason={finish}]")
            return _extract_json(last_raw)
        except _AreaValidationError:
            _write_error(img_path, last_raw)
            raise
        except (json.JSONDecodeError, Exception) as e:
            if last_raw:
                _write_error(img_path, last_raw)
            if attempt >= 5:
                raise RuntimeError(f"OpenRouter failed after {attempt} attempts: {e}")
            wait = _retry_wait(attempt)
            print(f"    [OpenRouter {type(e).__name__}] {str(e)[:120]} — retrying in {wait}s")
            time.sleep(wait)


def detect(image_bytes: bytes, img_path: Path, model: str = None) -> tuple[dict, str, str]:
    """Try each model from detect_models.cfg in priority order.

    Returns (result, backend, model_id). backend is always 'openrouter'.
    If *model* is given it is used directly, bypassing the priority list.
    """
    if model:
        return detect_openrouter(image_bytes, img_path, model=model), "openrouter", model

    priority, prohibited = load_detect_models()
    if not priority:
        raise RuntimeError(
            "No active models in detect_models.cfg — enable at least one [N] entry"
        )

    last_exc: Exception | None = None
    for _backend, model_id in priority:
        label = model_id.split("/")[-1] if "/" in model_id else model_id
        print(f"    [{label}] sending request…", flush=True)
        try:
            return detect_openrouter(image_bytes, img_path, model=model_id), "openrouter", model_id
        except Exception as e:
            print(f"    [{label}] {type(e).__name__}: {str(e)[:80]} — trying next model")
            last_exc = e

    raise last_exc or RuntimeError("All models in detect_models.cfg failed")


# ---------------------------------------------------------------------------
# Per-page worker
# ---------------------------------------------------------------------------

def _normalize_coords(
    result: dict,
    orig_w: int,
    orig_h: int,
    crop_bbox: tuple[int, int, int, int] | None = None,
) -> None:
    """Convert Gemini's 0-1000 normalised coordinates to original image pixel space."""
    if crop_bbox:
        left, top, right, bottom = crop_bbox
        crop_w, crop_h = right - left, bottom - top
        for area in result.get("areas", []):
            area["polygon"] = [
                [round(x / 1000 * crop_w) + left, round(y / 1000 * crop_h) + top]
                for x, y in area["polygon"]
            ]
    else:
        for area in result.get("areas", []):
            area["polygon"] = [
                [round(x / 1000 * orig_w), round(y / 1000 * orig_h)]
                for x, y in area["polygon"]
            ]
    result["page_dimensions"] = {"width": orig_w, "height": orig_h}


def _scale_to_original(
    result: dict,
    orig_w: int,
    orig_h: int,
    crop_bbox: tuple[int, int, int, int] | None = None,
) -> None:
    """Scale OpenRouter pixel coordinates (in the resized JPEG space) to original image space."""
    model_dims = result.get("page_dimensions", {})
    model_w = model_dims.get("width") or orig_w
    model_h = model_dims.get("height") or orig_h
    if crop_bbox:
        left, top, right, bottom = crop_bbox
        crop_w, crop_h = right - left, bottom - top
        for area in result.get("areas", []):
            area["polygon"] = [
                [round(x * crop_w / model_w) + left, round(y * crop_h / model_h) + top]
                for x, y in area["polygon"]
            ]
    else:
        for area in result.get("areas", []):
            area["polygon"] = [
                [round(x * orig_w / model_w), round(y * orig_h / model_h)]
                for x, y in area["polygon"]
            ]
    result["page_dimensions"] = {"width": orig_w, "height": orig_h}


def _clamp_coords_to_page(result: dict) -> None:
    """Clamp all polygon vertices to the page boundary."""
    dims = result.get("page_dimensions", {})
    w, h = dims.get("width", 0), dims.get("height", 0)
    if not w or not h:
        return
    for area in result.get("areas", []):
        area["polygon"] = [
            [max(0, min(w, x)), max(0, min(h, y))]
            for x, y in area["polygon"]
        ]


_TEXT_TYPES = frozenset({
    "main_text", "footnote", "illustration_caption",
    "header", "footer", "page_number", "chapter_title", "decoration",
})
_CLIP_MARGIN = 4  # px gap to leave between illustration and text edges


def _poly_bbox(poly: list) -> tuple[int, int, int, int]:
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return min(xs), min(ys), max(xs), max(ys)


def _bboxes_overlap(a: tuple, b: tuple) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _clip_poly_halfplane(poly: list, axis: int, limit: int, keep_low: bool) -> list:
    """Sutherland-Hodgman clip of poly against one axis-aligned half-plane."""
    if not poly:
        return poly
    out = []
    n = len(poly)
    for i in range(n):
        cur = poly[i]
        prv = poly[(i - 1) % n]
        cv, pv = cur[axis], prv[axis]
        inside = cv <= limit if keep_low else cv >= limit
        was_inside = pv <= limit if keep_low else pv >= limit
        if inside:
            if not was_inside:
                t = (limit - pv) / (cv - pv)
                pt = [round(prv[j] + t * (cur[j] - prv[j])) for j in range(2)]
                pt[axis] = limit
                out.append(pt)
            out.append(cur)
        elif was_inside:
            t = (limit - pv) / (cv - pv)
            pt = [round(prv[j] + t * (cur[j] - prv[j])) for j in range(2)]
            pt[axis] = limit
            out.append(pt)
    return out


def _clip_illustrations_from_text(areas: list[dict]) -> None:
    """Clip illustration polygons so they don't overlap with text area bounding boxes."""
    illustrations = [a for a in areas if a.get("type") == "illustration"]
    texts = [a for a in areas if a.get("type") in _TEXT_TYPES and a.get("polygon")]
    if not illustrations or not texts:
        return

    text_bboxes = [_poly_bbox(t["polygon"]) for t in texts]

    for illus in illustrations:
        poly = illus.get("polygon")
        if not poly:
            continue

        for tb in text_bboxes:
            ib = _poly_bbox(poly)
            if not _bboxes_overlap(ib, tb):
                continue

            illus_cy = (ib[1] + ib[3]) / 2
            text_cy  = (tb[1] + tb[3]) / 2
            illus_cx = (ib[0] + ib[2]) / 2
            text_cx  = (tb[0] + tb[2]) / 2

            dy = abs(illus_cy - text_cy)
            dx = abs(illus_cx - text_cx)

            if dy >= dx:
                if illus_cy < text_cy:
                    poly = _clip_poly_halfplane(poly, axis=1, limit=tb[1] - _CLIP_MARGIN, keep_low=True)
                else:
                    poly = _clip_poly_halfplane(poly, axis=1, limit=tb[3] + _CLIP_MARGIN, keep_low=False)
            else:
                if illus_cx < text_cx:
                    poly = _clip_poly_halfplane(poly, axis=0, limit=tb[0] - _CLIP_MARGIN, keep_low=True)
                else:
                    poly = _clip_poly_halfplane(poly, axis=0, limit=tb[2] + _CLIP_MARGIN, keep_low=False)

        if poly and len(poly) >= 3:
            illus["polygon"] = poly


def _parse_page_spec(spec: str, pages_dir: Path) -> list[Path]:
    """Accept: single number (57), list (9,10,11), range (10-20), mixed (9,15-20,25),
    or a literal filename / path (page0057.png)."""
    if re.match(r'^[\d,\-\s]+$', spec):
        paths = []
        for token in spec.split(","):
            token = token.strip()
            if "-" in token:
                start, end = token.split("-", 1)
                paths.extend(pages_dir / f"page{n:04d}.png" for n in range(int(start), int(end) + 1))
            else:
                paths.append(pages_dir / f"page{int(token):04d}.png")
        return paths
    p = Path(spec)
    if not p.exists():
        p = pages_dir / spec
    return [p]


def _uses_gemini_normalization(backend: str, model_id: str) -> bool:
    """Gemini always returns 0-1000 normalized coords regardless of the access path.
    This applies to the native Gemini API and to any google/gemini-* model via OpenRouter."""
    if backend == "gemini":
        return True
    return "gemini" in model_id.lower()


def process_page(
    img_path: Path,
    model: str = None,
    content_bbox: tuple[int, int, int, int] | None = None,
) -> dict:
    image_bytes, orig_w, orig_h = prepare_image(img_path, content_bbox=content_bbox)
    result, backend, model_id = detect(image_bytes, img_path, model=model)
    if _uses_gemini_normalization(backend, model_id):
        _normalize_coords(result, orig_w, orig_h, crop_bbox=content_bbox)
    else:
        _scale_to_original(result, orig_w, orig_h, crop_bbox=content_bbox)
    _clamp_coords_to_page(result)
    _clip_illustrations_from_text(result.get("areas", []))
    result["source_image"] = img_path.name
    result["detected_by"] = f"{backend}:{model_id}"
    return result


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

_json_lock = threading.Lock()


def _write_atomic(path: Path, text: str) -> None:
    """Write text to a temp file then rename — prevents truncation on crash."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _load_from_sidecars(pages_dir: Path) -> dict[str, dict]:
    """Read all *-areas.json sidecar files and return a source_image → page_data map."""
    pages: dict[str, dict] = {}
    for sc in sorted(pages_dir.glob("*-areas.json")):
        try:
            data = json.loads(sc.read_text(encoding="utf-8"))
            src = data.get("source_image")
            if src:
                pages[src] = data
        except Exception:
            pass
    return pages


def _rebuild_main_json(json_path: Path, book_name: str, pages: dict[str, dict]) -> None:
    sorted_pages = sorted(pages.values(), key=lambda p: p["source_image"])
    output = {
        "book":        book_name,
        "total_pages": len(sorted_pages),
        "pages":       sorted_pages,
    }
    _write_atomic(json_path, json.dumps(output, ensure_ascii=False, indent=2))


def _upsert_page_json(
    json_path: Path, pages_dir: Path, book_name: str, page_data: dict
) -> None:
    """Merge one page result into the JSON, preserving all other pages and user edits."""
    # Write per-page sidecar first — survives a main-JSON corruption
    sidecar = pages_dir / f"{Path(page_data['source_image']).stem}-areas.json"
    _write_atomic(sidecar, json.dumps(page_data, ensure_ascii=False, indent=2))

    with _json_lock:
        existing: dict = {}
        if json_path.exists():
            try:
                existing = json.loads(json_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        pages_map = {p["source_image"]: p for p in existing.get("pages", [])}
        # Preserve ignored flag that may have been set via the review UI since step2 started
        if pages_map.get(page_data["source_image"], {}).get("ignored"):
            page_data = {**page_data, "ignored": True}
        pages_map[page_data["source_image"]] = page_data
        sorted_pages = sorted(pages_map.values(), key=lambda p: p["source_image"])
        output = {
            **existing,
            "book":        book_name,
            "total_pages": len(sorted_pages),
            "pages":       sorted_pages,
        }
        _write_atomic(json_path, json.dumps(output, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Detect page areas via Gemini / OpenRouter.")
    parser.add_argument("pdf", help="Path to the source PDF file")
    parser.add_argument("--book-name", default=None,
                        help="Output JSON filename stem, e.g. 'Reay Tannahill - Sex In History' "
                             "(default: PDF filename stem)")
    parser.add_argument("--pages-dir", type=Path, default=None,
                        help="Page images directory (default: <pdf-dir>/pages/)")
    parser.add_argument("--json-dir", type=Path, default=None,
                        help="JSON output directory (default: <pdf-dir>/)")
    parser.add_argument("--page",
                        help="Page(s) to process: number (57), list (9,10,11), "
                             "range (10-20), mixed (9,15-20,25), or filename (page0057.png)")
    parser.add_argument("--model", default=None,
                        help="Force a specific model (e.g. anthropic/claude-opus-4-7); "
                             "bypasses detect_models.cfg")
    parser.add_argument("--workers", type=int, default=1,
                        help="Parallel workers (default: 1)")
    parser.add_argument("--force", action="store_true",
                        help="Re-process and overwrite already-processed pages")
    parser.add_argument("--process-later-only", action="store_true",
                        help="Process only pages flagged as process_later, using Opus")
    parser.add_argument("--recover", action="store_true",
                        help="Rebuild main JSON from per-page sidecar (*-areas.json) files")
    parser.add_argument("--styles", action="store_true",
                        help="Enable bold/italic detection: model wraps bold in <strong> and italic in <i> for main_text areas")
    args = parser.parse_args()

    if args.styles:
        global _ACTIVE_PROMPT
        _ACTIVE_PROMPT = DETECT_AREAS_PROMPT + DETECT_AREAS_STYLES_ADDON

    src = Path(args.pdf)
    book_dir  = src if src.is_dir() else src.parent
    book_name = args.book_name or src.stem
    dirs = book_dirs(book_dir, book_name)
    args.pages_dir = args.pages_dir or dirs["pages"]
    args.json_dir  = args.json_dir  or dirs["json"]

    json_path = args.json_dir / f"{book_name}.json"

    args.json_dir.mkdir(parents=True, exist_ok=True)

    # --recover: rebuild main JSON from sidecar files and exit
    if args.recover:
        sidecars = _load_from_sidecars(args.pages_dir)
        if not sidecars:
            print(f"No sidecar files found in {args.pages_dir}", file=sys.stderr)
            sys.exit(1)
        _rebuild_main_json(json_path, book_name, sidecars)
        print(f"Recovered {len(sidecars)} pages → {json_path}")
        return

    # Always load existing data so previously processed pages are never re-sent
    pages: dict[str, dict] = {}  # source_image → page_data
    if json_path.exists():
        try:
            existing = json.loads(json_path.read_text(encoding="utf-8"))
            for page in existing.get("pages", []):
                pages[page["source_image"]] = page
        except Exception:
            pass

    # If main JSON is empty/corrupt, auto-recover from sidecars before deciding what to process
    if not pages and args.pages_dir.exists():
        sidecars = _load_from_sidecars(args.pages_dir)
        if sidecars:
            print(f"Main JSON empty/corrupt — recovering {len(sidecars)} pages from sidecar files.")
            _rebuild_main_json(json_path, book_name, sidecars)
            pages = sidecars

    if args.page:
        all_pages = _parse_page_spec(args.page, args.pages_dir)
    elif pages:
        # Use JSON as the canonical page list — only non-ignored entries
        all_pages = sorted(
            args.pages_dir / name
            for name, data in pages.items()
            if not data.get("ignored") and (args.pages_dir / name).exists()
        )
    else:
        all_pages = sorted(p for p in args.pages_dir.glob("page*.png")
                           if not p.name.endswith("-areas.png")
                           and not p.name.endswith("-content.png"))

    if not all_pages:
        print(f"No page images found in {args.pages_dir}", file=sys.stderr)
        sys.exit(1)

    process_later_names = {n for n, d in pages.items() if d.get("process_later")}

    if args.force:
        for p in all_pages:
            pages.pop(p.name, None)

    all_names = {p.name for p in all_pages}
    already_done = sum(1 for n in all_names if pages.get(n, {}).get("page_dimensions"))
    ignored_count = sum(1 for n in pages if pages[n].get("ignored"))
    process_later_count = len(process_later_names & all_names)

    if args.process_later_only:
        if not args.model:
            args.model = "anthropic/claude-opus-4-7"
        pending = [
            p for p in all_pages
            if p.name in process_later_names
            and not (pages.get(p.name, {}).get("detected_by") or "").startswith("opus:later:")
        ]
        parts = [f"{process_later_count} process-later total", f"{len(pending)} pending"]
    else:
        pending = [
            p for p in all_pages
            if (p.name not in pages or not pages[p.name].get("page_dimensions"))
            and p.name not in process_later_names
        ]
        parts = [f"{len(all_pages)} total", f"{len(pending)} pending"]
        if already_done:
            parts.append(f"{already_done} already done")
        if ignored_count:
            parts.append(f"{ignored_count} ignored")
        if process_later_count:
            parts.append(f"{process_later_count} process-later (skipped)")
    print(f"Pages: {' · '.join(parts)}")

    if not pending:
        print("Nothing to do.")
        return

    if args.model:
        pipeline_label = args.model
    else:
        labels = active_model_labels()
        pipeline_label = " → ".join(labels) if labels else "none"
    print(f"Processing {len(pending)} pages [{pipeline_label}, {args.workers} worker(s)]...")

    errors: list[tuple[str, str]] = []
    done = 0

    def _parse_bbox(d: dict | None) -> tuple[int, int, int, int] | None:
        if not d:
            return None
        try:
            return d["left"], d["top"], d["right"], d["bottom"]
        except KeyError:
            return None

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(
                process_page, p, args.model,
                _parse_bbox(pages.get(p.name, {}).get("content_bbox")),
            ): p
            for p in pending
        }
        for fut in tqdm(as_completed(futures), total=len(futures),
                        desc="Detecting areas", unit="page",
                        disable=not sys.stderr.isatty()):
            img_path = futures[fut]
            try:
                result = fut.result()
                if args.process_later_only:
                    result["detected_by"] = "opus:later:" + result.get("detected_by", "")
                # Preserve ignored flag from any pre-existing stub
                if pages.get(result["source_image"], {}).get("ignored"):
                    result["ignored"] = True
                pages[result["source_image"]] = result
                done += 1
                _upsert_page_json(json_path, args.pages_dir, book_name, result)
                tqdm.write(f"  {img_path.name} → {len(result.get('areas', []))} areas")
                out_path = args.pages_dir / f"{img_path.stem}-areas.png"
                try:
                    visualize_page(img_path, result, out_path, alpha=60)
                    tqdm.write(f"  [viz → {out_path.name}]")
                except Exception as viz_exc:
                    tqdm.write(f"  [viz error {img_path.stem}: {viz_exc}]")
            except Exception as exc:
                errors.append((img_path.name, str(exc)))
                tqdm.write(f"  ERROR {img_path.name}: {exc}")

    print(f"\nDone. Processed: {done}  Errors: {len(errors)}")
    print(f"Output: {json_path}")
    if errors:
        print("Failed pages:")
        for name, msg in errors:
            print(f"  {name}: {msg}")


if __name__ == "__main__":
    main()
