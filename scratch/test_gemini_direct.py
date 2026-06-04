import sys
from pathlib import Path

# Add project root to python path
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.gemini import gemini_generate_content, GeminiQuotaExhaustedError
from utils.config import GEMINI_API_KEY

def test_text_generation():
    print("Testing direct Gemini API text generation...")
    print(f"Loaded GEMINI_API_KEY: {'[SET]' if GEMINI_API_KEY else '[MISSING]'}")
    
    if not GEMINI_API_KEY:
        print("Error: No GEMINI_API_KEY set in environment or .env file.")
        sys.exit(1)
        
    try:
        response = gemini_generate_content(
            prompt="Hello! Say 'Google Gemini direct integration is successful!' in Russian.",
            model="google/gemini-3.1-flash-image-preview",
            temperature=0.7
        )
        print("\nGemini Response:")
        print(response)
        print("\nTest completed successfully!")
    except GeminiQuotaExhaustedError as e:
        print(f"\n[CRITICAL] Gemini Free Quota Exhausted: {e}")
    except Exception as e:
        print(f"\nError occurred: {e}")

if __name__ == "__main__":
    test_text_generation()
