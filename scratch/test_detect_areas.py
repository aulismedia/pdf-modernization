import sys
from pathlib import Path
import json

# Add parent directory and steps directory to sys.path
root_dir = Path(__file__).resolve().parents[1]
sys.path.append(str(root_dir))
sys.path.append(str(root_dir / "steps"))

from steps.step2_detect_areas import process_page

img_path = Path("/Users/sergeymishenev/Desktop/Media Labs/книги/Aurel Krause - The Tlingit Indians (1956)/Aurel Krause - The Tlingit Indians (1956) - pages/page0086.png")

print(f"Processing page: {img_path}")
result = process_page(
    img_path=img_path,
    model="google/gemini-3.1-flash-image-preview",
    content_bbox=(86, 74, 1085, 1721),  # page0086 content_bbox from main JSON
    rotation=0
)

print(json.dumps(result, indent=2, ensure_ascii=False))
