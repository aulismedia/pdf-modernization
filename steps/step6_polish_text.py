#!/usr/bin/env python3
"""
Step X: Polish text in main_text and footnote areas via OpenRouter (polisher prompt).

For each non-ignored page, batches all text areas into a single API call.
Applies OCR / footnote-index fixes in-place and writes page_join from the
last main_text area's result.

Saves the updated JSON in-place. Run step7_seamless_html.py afterward to
build the final flowing HTML.

Usage:
    python stepX_polish_text.py <path_to_pdf>
    python stepX_polish_text.py <path_to_pdf> --book-name "Title"
"""

import argparse
import copy
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from tqdm import tqdm

from utils.config import OPEN_ROUTER_APIKEY, GEMINI_API_KEY, POLISHER_MODEL, POLISHER_UNRESTRICTED_MODEL, POLISHER_FALLBACK_MODEL, book_dirs
from utils.rotation_broker import RotationBroker

POLISHER_PROMPT = (Path(__file__).parent.parent / "prompts" / "polisher.py").read_text(encoding="utf-8")

MAX_RETRIES = 5
_JSON_FENCE = re.compile(r"^```[a-z]*\s*|\s*```$", re.IGNORECASE | re.MULTILINE)
_INLINE_HYPHEN = re.compile(r"(\w)-(?=[ \t\n]|„|,,|-,)[ \t\n]*(?:„|,,|-,)?[ \t]*(\w)")
_SPACE_BEFORE_PUNCT = re.compile(r"(\w) +([,;])")


def _retry_wait(attempt: int) -> int:
    return min(2 ** attempt, 60)


def _parse_response(raw: str) -> list:
    cleaned = _JSON_FENCE.sub("", raw.strip())
    cleaned = re.sub(r'\\([^"\\/bfnrtu])', r'\1', cleaned)
    result = json.loads(cleaned)
    if not isinstance(result, list):
        raise ValueError(f"Expected JSON array, got {type(result).__name__}")
    return result



def _call_polisher_batch(items: list, model: str = POLISHER_MODEL) -> list | None:
    model_lower = model.lower()

    # Direct native Gemini call intercept
    if GEMINI_API_KEY and ("gemini" in model_lower or model_lower.startswith("gemini")):
        from utils.gemini import gemini_generate_content
        real_model = model.split(":", 1)[1] if ":" in model else model
        if "/" in real_model:
            real_model = real_model.split("/")[-1]
        if real_model in ("gemini-3.1-flash", "gemini-3.1-flash-image-preview", "gemini", "gemini-1.5-flash", "gemini-1.5-flash-latest"):
            real_model = "gemini-2.5-flash"

        print(f"    [gemini direct] polishing batch with {real_model}…", flush=True)
        try:
            raw = gemini_generate_content(
                prompt=POLISHER_PROMPT + "\n\nUser Content:\n" + json.dumps(items, ensure_ascii=False),
                model=real_model,
                temperature=0,
                max_tokens=4096,
                response_mime_type="application/json"
            )
            return _parse_response(raw)
        except Exception as e:
            from utils.gemini import GeminiQuotaExhaustedError
            if isinstance(e, GeminiQuotaExhaustedError):
                raise
            print(f"    [gemini direct failed] {e} — falling back to OpenRouter")


    headers = {
        "Authorization": f"Bearer {OPEN_ROUTER_APIKEY}",
        "Content-Type": "application/json",
    }

    def _try_model(m: str) -> list | None:
        payload = {
            "model": m,
            "messages": [
                {"role": "system", "content": POLISHER_PROMPT},
                {"role": "user", "content": json.dumps(items, ensure_ascii=False)},
            ],
            "temperature": 0,
            "max_tokens": 4096,
        }
        last_err: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = requests.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    json=payload,
                    headers=headers,
                    timeout=120,
                )
                if resp.status_code == 400:
                    return None  # signal: try fallback
                resp.raise_for_status()
                raw = resp.json()["choices"][0]["message"]["content"].strip()
                return _parse_response(raw)
            except Exception as e:
                last_err = e
                if attempt >= MAX_RETRIES:
                    break
                wait = _retry_wait(attempt)
                print(f"    [polisher retry {attempt}] {str(e)[:100]} — retrying in {wait}s")
                time.sleep(wait)
        print(f"    [polisher failed after {MAX_RETRIES} attempts] {last_err}")
        return None

    result = _try_model(model)
    if result is None and model != POLISHER_FALLBACK_MODEL:
        print(f"    [polisher fallback] {model} failed — retrying with {POLISHER_FALLBACK_MODEL}")
        result = _try_model(POLISHER_FALLBACK_MODEL)
    return result


def _apply_fixes(text: str, fixes: list) -> str:
    for fix in fixes:
        ftype = fix.get("type", "")

        if ftype == "ocr_hyphen":
            part1 = fix.get("part1", "")
            part2 = fix.get("part2", "")
            if not part1 or not part2:
                continue
            pattern = re.escape(part1) + r"-\s*" + re.escape(part2)
            new_text = re.sub(pattern, part1 + part2, text, count=1)
            if new_text != text:
                text = new_text
        else:
            find = fix.get("find", "")
            replace = fix.get("replace", "")
            if not find:
                continue
            offset = text.find(find)
            if offset == -1:
                print(f"    [fix mismatch] {find!r} not found — skipped")
                continue
            text = text[:offset] + replace + text[offset + len(find):]

    return text


# <sup>1</sup>898 → 1898: model split a digit out of a multi-digit number
_BAD_SUP_RE = re.compile(r"<sup>(\d+)</sup>(\d)")

def _revert_bad_sups(text: str) -> str:
    prev = None
    while prev != text:
        prev = text
        text = _BAD_SUP_RE.sub(r"\1\2", text)
    return text


def _first_n_words(text: str, n: int = 10) -> str:
    return " ".join(text.split()[:n])


def _write_atomic(path: Path, text: str) -> None:
    """Write text to a temp file then rename — prevents truncation on crash."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def process_single_page_job(
    page_idx: int,
    page: dict,
    next_prefix: str | None,
    has_next_page: bool,
) -> dict | None:
    # Create a deep copy to ensure complete isolation
    page_copy = copy.deepcopy(page)
    
    page_w = page_copy.get("page_dimensions", {}).get("width", 1000)
    page_h = page_copy.get("page_dimensions", {}).get("height", 1000)
    rotation = RotationBroker.effective_rotation(page_copy)
    sorted_areas = RotationBroker.sort_areas(page_copy.get("areas", []), page_w, page_h, rotation)

    # Last main_text area in reading order drives page_join detection
    last_main: dict | None = None
    for a in reversed(sorted_areas):
        if a.get("type") == "main_text" and (a.get("text") or "").strip():
            last_main = a
            break

    # Pre-fix inline hyphens and stray spaces before punctuation
    for area in sorted_areas:
        if area.get("type") in ("main_text", "footnote") and area.get("text"):
            area["text"] = _INLINE_HYPHEN.sub(r"\1\2", area["text"])
            area["text"] = _SPACE_BEFORE_PUNCT.sub(r"\1\2", area["text"])

    # Collect areas eligible for polishing
    batch_areas: list[dict] = []
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
        batch_areas.append(area)

    if not batch_items:
        if has_next_page:
            page_copy["page_join"] = "new_paragraph"
        page_copy["polished"] = True
        return page_copy

    model = POLISHER_UNRESTRICTED_MODEL if page_copy.get("prohibited") else POLISHER_MODEL
    
    results = _call_polisher_batch(batch_items, model)
    if results is None:
        return None

    results_by_id = {r["id"]: r for r in results if isinstance(r, dict) and "id" in r}
    page_join: str | None = None

    for idx, area in enumerate(batch_areas):
        result = results_by_id.get(idx)
        if result is None:
            continue

        fixes = result.get("fixes") or []
        # If the model returned ocr_hyphen for a trailing fragment (cross-page split
        # that should have been page_join: merge_hyphen), catch it here.
        clean_fixes = []
        for fix in fixes:
            if fix.get("type") == "ocr_hyphen":
                part1 = fix.get("part1", "")
                text_stripped = (area.get("text") or "").rstrip()
                if part1 and re.search(re.escape(part1) + r"-\s*$", text_stripped):
                    page_join = "merge_hyphen"
                    continue
            clean_fixes.append(fix)
        if clean_fixes:
            area["text"] = _revert_bad_sups(
                _apply_fixes((area.get("text") or "").strip(), clean_fixes)
            )

        if "page_join" in result:
            page_join = result["page_join"]

    if page_join is not None:
        page_copy["page_join"] = page_join
    elif has_next_page:
        page_copy["page_join"] = "new_paragraph"

    page_copy["polished"] = True
    return page_copy


def main():
    parser = argparse.ArgumentParser(description="Polish text areas via OpenRouter.")
    parser.add_argument("pdf", help="Path to the source PDF file")
    parser.add_argument("--book-name", default=None)
    parser.add_argument("--pages", default=None,
                        help="Page range to process, e.g. 40-70 or a single page 42")
    parser.add_argument("--resume", action="store_true",
                        help="Skip pages already marked as polished")
    parser.add_argument("--workers", type=int, default=1,
                        help="Parallel workers (default: 1)")
    args = parser.parse_args()

    if not OPEN_ROUTER_APIKEY:
        print("OPEN_ROUTER_APIKEY not set in .env", file=sys.stderr)
        sys.exit(1)

    src = Path(args.pdf)
    book_dir = src if src.is_dir() else src.parent
    dirs = book_dirs(book_dir)
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

    if args.pages:
        parts = args.pages.split("-")
        try:
            page_from = int(parts[0])
            page_to = int(parts[1]) if len(parts) > 1 else page_from
        except ValueError:
            print(f"Invalid --pages value: {args.pages!r}. Use e.g. 40-70 or 42", file=sys.stderr)
            sys.exit(1)

        def _page_num(p):
            stem = Path(p.get("source_image", "")).stem  # e.g. "page0042"
            return int(stem.replace("page", "")) if stem.startswith("page") else -1

        active_pages = [p for p in active_pages if page_from <= _page_num(p) <= page_to]
        print(f"Polishing {len(active_pages)} pages in range {page_from}–{page_to}…")
    else:
        print(f"Polishing {len(active_pages)} pages ({len(all_pages) - len(active_pages)} ignored)…")

    if args.resume:
        already_done = sum(1 for p in active_pages if p.get("polished") or "page_join" in p)
        print(f"Resume mode: {already_done} pages already polished, {len(active_pages) - already_done} remaining.")

    # Pre-compute next page prefixes sequentially
    next_prefixes = []
    for page_idx, page in enumerate(active_pages):
        next_prefix = None
        if page_idx + 1 < len(active_pages):
            next_page = active_pages[page_idx + 1]
            next_w = next_page.get("page_dimensions", {}).get("width", 1000)
            next_h = next_page.get("page_dimensions", {}).get("height", 1000)
            next_rot = RotationBroker.effective_rotation(next_page)
            for a in RotationBroker.sort_areas(next_page.get("areas", []), next_w, next_h, next_rot):
                if a.get("type") == "main_text" and (a.get("text") or "").strip():
                    next_prefix = _first_n_words(a["text"])
                    break
        next_prefixes.append(next_prefix)

    # Collect jobs that need processing
    jobs = []
    for page_idx, page in enumerate(active_pages):
        if args.resume and (page.get("polished") or "page_join" in page):
            continue
        jobs.append((page_idx, page, next_prefixes[page_idx], page_idx + 1 < len(active_pages)))

    if not jobs:
        print("All pages are already polished or no active pages selected.")
        return

    print(f"Polishing {len(jobs)} pages using {args.workers} workers…")
    quota_error = None

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_to_job = {
            executor.submit(process_single_page_job, page_idx, page, next_pref, has_next): (page_idx, page)
            for page_idx, page, next_pref, has_next in jobs
        }

        try:
            for future in tqdm(as_completed(future_to_job), total=len(future_to_job), desc="Pages", unit="page"):
                page_idx, original_page = future_to_job[future]
                try:
                    updated_page = future.result()
                    if updated_page is not None:
                        # Serialize modification and file writing strictly on main thread
                        original_page.clear()
                        original_page.update(updated_page)
                        _write_atomic(json_path, json.dumps(book_data, ensure_ascii=False, indent=2))
                except Exception as e:
                    from utils.gemini import GeminiQuotaExhaustedError
                    if isinstance(e, GeminiQuotaExhaustedError) or "Google Gemini API free quota exhausted" in str(e):
                        quota_error = e
                        print("\n" + "="*80 + "\n[CRITICAL] Gemini Free Quota Exhausted! Stopping all threads immediately.\n" + "="*80 + "\n")
                        for f in future_to_job:
                            f.cancel()
                        break
                    else:
                        print(f"\n[ERROR] Page index {page_idx} failed: {e}")
        except KeyboardInterrupt:
            print("\n[INFO] Run cancelled by user. Shutting down...")
            for f in future_to_job:
                f.cancel()
            sys.exit(1)

    if quota_error:
        sys.exit(429)

    print(f"Saved: {json_path}")


if __name__ == "__main__":
    main()
