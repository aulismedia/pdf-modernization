import os
import sys
from google import genai
from google.genai import types
from utils.config import GEMINI_API_KEY

class GeminiQuotaExhaustedError(RuntimeError):
    """Exception raised when the Google Gemini API free tier quota is exhausted."""
    pass

_client = None

def get_gemini_client():
    """Retrieve or initialize the global Google GenAI Client."""
    global _client
    if _client is None:
        if not GEMINI_API_KEY:
            raise RuntimeError(
                "No Gemini API key found. Please set GEMINI_API_KEY, "
                "GEMINI_FREE_KEY, or GEMINI_PAID_KEY in .env"
            )
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client

def _is_rate_limited(exc: Exception) -> bool:
    """Identify if the given exception is a rate limit (429) or quota exhausted error."""
    code = getattr(exc, 'code', None) or getattr(exc, 'status_code', None)
    if code == 429:
        return True
    msg = str(exc).lower()
    return '429' in msg or 'quota exceeded' in msg or 'resource_exhausted' in msg or 'rate limit' in msg

def gemini_generate_content(
    prompt: str,
    image_bytes: bytes = None,
    model: str = "gemini-2.5-flash",
    temperature: float = 0.0,
    max_tokens: int = 4000,
    response_mime_type: str = None
) -> str:
    """Generate content directly from the Google Gemini API.

    Catches 429 quota exceptions and raises GeminiQuotaExhaustedError to signal halting.
    """
    client = get_gemini_client()
    contents = []

    # If image is provided, construct a proper multi-part message
    if image_bytes:
        contents.append(types.Part.from_bytes(data=image_bytes, mime_type='image/jpeg'))
    contents.append(prompt)

    # Normalize model name for direct Gemini calling
    # In direct Gemini, provider prefixes (e.g. google/) are removed.
    model_name = model
    if "/" in model_name:
        model_name = model_name.split("/")[-1]
    
    # Map generic or decommissioned legacy models to the stable Gemini 2.5 Flash model
    if model_name.lower() in (
        "gemini", 
        "gemini-1.5-flash", 
        "gemini-1.5-flash-latest", 
        "gemini-1.5-flash-001", 
        "gemini-1.5-flash-002", 
        "gemini-1.5-flash-8b"
    ):
        model_name = "gemini-2.5-flash"

    config = types.GenerateContentConfig(
        temperature=temperature,
        max_output_tokens=max_tokens,
        response_mime_type=response_mime_type,
    )


    try:
        response = client.models.generate_content(
            model=model_name,
            contents=contents,
            config=config
        )
        return response.text or ""
    except Exception as e:
        if _is_rate_limited(e):
            raise GeminiQuotaExhaustedError(
                "Google Gemini API free quota exhausted. Stopping execution as requested."
            ) from e
        raise e
