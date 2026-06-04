"""Shared OCR prompts and OpenRouter API wrappers for layout area transcription."""

import base64
import re
import time
import requests

from utils.config import OPEN_ROUTER_APIKEY, OPENROUTER_MODEL

OCR_PROMPT = """\
You are an expert high-fidelity document OCR transcriber.
Transcribe the text in the provided image of a document segment.
Follow these rules exactly:

1. High-Fidelity Transcription:
   - Perform verbatim transcription of the text visible in the image.
   - Do NOT translate, summarize, or edit the text.
   - Preserve all original spelling, punctuation, capitalization, and formatting.

2. Pre-Reform Russian & Archaic Languages:
   - Pre-reform Russian spellings are LEGITIMATE. Do NOT correct or modernize archaic characters like: ъ, ѣ, і, ѳ, ѵ at word endings or roots (e.g., keep "ъ" at the end of words ending in a hard consonant).
   - Preserve archaic spelling forms, dialectal variants, and historical terminology. Only correct obvious, non-ambiguous OCR character failures, but when in doubt, transcribe exactly what is printed.

3. Paragraphing and Line Breaks:
   - Use double newlines (\\n\\n) ONLY to separate distinct paragraphs.
   - Remove line-wrap breaks: when a sentence or paragraph continues on the next line without a hyphen, join the lines with a single space instead of a single newline (\\n).
   - Remove soft hyphens: when a word is broken across a line with a hyphen at the end of a line, reconstruct the word without the hyphen (e.g., "пози-\\nтивный" → "позитивный").
   - Use a single newline (\\n) only for intentional hard breaks that carry meaning (such as poetry/verse lines, lists, or explicit visual breaks). Never use a newline merely because the printed line ended.

4. Hyphens and Dashes:
   - Distinguish hyphens (-) from dashes (— / –).
   - A hyphen joins compound words (e.g., "красно-бурый") and has no spaces around it.
   - An em dash (—) or en dash (–) separates clauses or parenthetical phrases and should be rendered with a space on each side (e.g., "слово — слово"). Never substitute a plain hyphen for a dash.

5. Direct Output:
   - Return ONLY the raw transcribed text. Do not add any introduction, explanations, or wrapper code blocks (like ```text).
"""

OCR_PROMPT_STYLES_ADDON = """
6. Bold and Italic text styles:
   - Wrap bold text in <strong>...</strong> and italic text in <i>...</i>.
   - Only <strong> and <i> are permitted — no other HTML tags or markdown formatting may appear in the text content.
"""


def ocr_area_api(image_bytes: bytes, model: str, prompt: str) -> str:
    """Send image bytes directly to direct Gemini API or OpenRouter OCR API with standard retries."""
    from utils.config import GEMINI_API_KEY

    model_lower = model.lower()
    if GEMINI_API_KEY and ("gemini" in model_lower or model_lower.startswith("gemini")):
        from utils.gemini import gemini_generate_content
        real_model = model.split(":", 1)[1] if ":" in model else model
        if "/" in real_model:
            real_model = real_model.split("/")[-1]
        if real_model in ("gemini", "gemini-1.5-flash", "gemini-1.5-flash-latest"):
            real_model = "gemini-2.5-flash"

        print(f"    [gemini direct ocr] transcription with {real_model}…")
        return gemini_generate_content(
            prompt=prompt,
            image_bytes=image_bytes,
            model=real_model,
            temperature=0,
            max_tokens=4000
        )

    if not OPEN_ROUTER_APIKEY:
        raise RuntimeError("No OpenRouter API key found. Set OPEN_ROUTER_APIKEY in .env")

    b64 = base64.b64encode(image_bytes).decode()
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]}],
        "temperature": 0,
        "max_tokens": 4000,
    }

    for attempt in range(1, 5):
        try:
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                json=payload,
                headers={
                    "Authorization": f"Bearer {OPEN_ROUTER_APIKEY}",
                    "Content-Type": "application/json",
                },
                timeout=90,
            )
            resp.raise_for_status()
            raw = resp.json()["choices"][0]["message"]["content"].strip()
            return raw
        except Exception as e:
            if attempt >= 4:
                raise RuntimeError(f"OCR API call failed: {e}")
            wait = min(2 ** attempt, 30)
            time.sleep(wait)

    raise RuntimeError("Failed to OCR area.")


def clean_ocr_text(text: str) -> str:
    """Strip out markdown code blocks and wrappers added by models."""
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()
