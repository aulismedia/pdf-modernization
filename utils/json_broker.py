"""
Single authority for reading and writing book JSON files.

Rules:
  - All writes go through `mutate()` or `mutate_page()`.
  - A per-file threading.Lock prevents concurrent writes from clobbering each other.
  - `read()` is the only way to read; it never writes.
  - Nothing in this module runs implicitly or periodically.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Callable

# ── internal state ────────────────────────────────────────────────────────────

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _locks_guard:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# ── public API ────────────────────────────────────────────────────────────────

def read(path: Path) -> dict:
    """Read the book JSON.  Returns {} if the file is missing or unreadable."""
    try:
        if path.exists() and path.stat().st_size > 2:
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def mutate(path: Path, fn: Callable[[dict], Any]) -> Any:
    """
    Read → fn(data) → atomic write, all under a per-file lock.

    fn receives the full book dict and may modify it in place.
    The return value of fn is returned to the caller.
    Never call this to read — use read() for that.
    """
    with _lock_for(path):
        data = read(path)
        result = fn(data)
        _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))
        return result


def mutate_page(path: Path, source_image: str, fn: Callable[[dict], Any]) -> Any:
    """
    Mutate a single page entry under the per-file lock.

    fn receives the page dict (created if absent) and may modify it in place.
    """
    def _apply(data: dict) -> Any:
        pages_map = {p["source_image"]: p for p in data.get("pages", [])}
        page = pages_map.setdefault(source_image, {"source_image": source_image})
        result = fn(page)
        data["pages"] = sorted(pages_map.values(), key=lambda p: p["source_image"])
        return result
    return mutate(path, _apply)
