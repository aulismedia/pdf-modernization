"""Loads the model priority list from detect_models.cfg."""

import re
from pathlib import Path

MODELS_FILE = Path(__file__).parent / "detect_models.cfg"

_cache: tuple[list[tuple[str, str]], tuple[str, str] | None] | None = None


def load_detect_models(
    force_reload: bool = False,
) -> tuple[list[tuple[str, str]], tuple[str, str] | None]:
    """Parse detect_models.cfg and return (priority_list, prohibited_entry).

    priority_list    – [(provider, model_id), ...] in declared order
    prohibited_entry – (provider, model_id) or None

    Entry format: [N] provider:model_id
      provider: gemini | openrouter
    Lines starting with # (including ## disabled entries) are skipped.
    """
    global _cache
    if _cache is not None and not force_reload:
        return _cache

    priority: list[tuple[str, str]] = []
    prohibited: tuple[str, str] | None = None

    for line in MODELS_FILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        m = re.match(r"\[(\w+)\]\s+(\S+)", stripped)
        if not m:
            continue

        tag, spec = m.group(1), m.group(2)

        if ":" not in spec:
            print(f"[models] Invalid entry '{spec}' in detect_models.cfg — use provider:model_id format, skipping")
            continue

        provider, model_id = spec.split(":", 1)
        provider = provider.lower()

        if provider not in ("gemini", "openrouter"):
            print(f"[models] Unknown provider '{provider}' in detect_models.cfg — use 'gemini' or 'openrouter', skipping")
            continue

        entry = (provider, model_id)
        if tag == "prohibited":
            prohibited = entry
        else:
            priority.append(entry)

    _cache = (priority, prohibited)
    return _cache


def active_model_labels() -> list[str]:
    """Human-readable labels of the currently active priority models."""
    priority, _ = load_detect_models()
    labels = []
    for provider, model_id in priority:
        if provider == "gemini":
            labels.append(f"gemini:{model_id}")
        else:
            labels.append(model_id)
    return labels
