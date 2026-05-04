#!/usr/bin/env python3
"""Step 5: Convert table areas to HTML using an AI model."""

import argparse
import base64
import io
import json
import sys
import threading
from pathlib import Path

import requests
from PIL import Image

from utils.config import OPEN_ROUTER_APIKEY, book_dirs
from prompts.tables import TABLE_PROMPT

MAX_OUTPUT_TOKENS = 4000
_DEFAULT_MODEL = "anthropic/claude-sonnet-4-6"

_json_lock = threading.Lock()


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _crop_area(img_path: Path, polygon: list) -> bytes:
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
    with Image.open(img_path) as img:
        img = img.convert("RGB")
        cropped = img.crop((x1, y1, x2, y2))
    buf = io.BytesIO()
    cropped.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _call_model(image_bytes: bytes, model: str) -> str:
    if not OPEN_ROUTER_APIKEY:
        raise RuntimeError("No OpenRouter API key found. Set OPEN_ROUTER_APIKEY in .env")
    b64 = base64.b64encode(image_bytes).decode()
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": TABLE_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]}],
        "temperature": 0,
        "max_tokens": MAX_OUTPUT_TOKENS,
    }
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        json=payload,
        headers={
            "Authorization": f"Bearer {OPEN_ROUTER_APIKEY}",
            "Content-Type": "application/json",
        },
        timeout=120,
    )
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"].strip()
    # Strip markdown fences if model added them
    if text.startswith("```"):
        lines = text.splitlines()
        end = -1 if lines[-1].strip() == "```" else len(lines)
        text = "\n".join(lines[1:end]).strip()
    return text


def main():
    parser = argparse.ArgumentParser(description="Process table areas to HTML.")
    parser.add_argument("pdf", help="Path to PDF/DJVU/image-folder (used to locate pages)")
    parser.add_argument("--book-name", default=None)
    parser.add_argument("--model", default=_DEFAULT_MODEL)
    args = parser.parse_args()

    src = Path(args.pdf)
    book_dir = src if src.is_dir() else src.parent
    book_name = args.book_name or src.stem
    dirs = book_dirs(book_dir, book_name)
    pages_dir = dirs["pages"]
    json_path = dirs["json"] / f"{book_name}.json"

    if not json_path.exists():
        print(f"No areas JSON found at {json_path}", file=sys.stderr)
        sys.exit(1)

    data = json.loads(json_path.read_text(encoding="utf-8"))
    pages = data.get("pages", [])

    work = []
    for page in pages:
        if page.get("ignored"):
            continue
        for area in page.get("areas", []):
            if area.get("type") == "table" and not area.get("table_html"):
                work.append((page["source_image"], area))

    if not work:
        print("No pending table areas found.")
        return

    print(f"Found {len(work)} table area(s) to process [{args.model}]...")

    errors = 0
    done = 0
    for page_name, area in work:
        img_path = pages_dir / page_name
        if not img_path.exists():
            img_path = book_dir / page_name
        if not img_path.exists():
            print(f"  [skip] {page_name}: image not found")
            errors += 1
            continue

        area_id = area.get("id", "?")
        print(f"  {page_name} / {area_id}...", flush=True)
        try:
            image_bytes = _crop_area(img_path, area["polygon"])
            html = _call_model(image_bytes, model=args.model)

            with _json_lock:
                current = json.loads(json_path.read_text(encoding="utf-8"))
                for cp in current.get("pages", []):
                    if cp["source_image"] == page_name:
                        for ca in cp.get("areas", []):
                            if ca.get("id") == area_id:
                                ca["table_html"] = html
                                break
                        break
                _write_atomic(json_path, json.dumps(current, ensure_ascii=False, indent=2))

            print(f"    → {len(html)} chars")
            done += 1
        except Exception as e:
            print(f"  ERROR {page_name}/{area_id}: {e}")
            errors += 1

    print(f"\nDone. Processed: {done}  Errors: {errors}")


if __name__ == "__main__":
    main()
