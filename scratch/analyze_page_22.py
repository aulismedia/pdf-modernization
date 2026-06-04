import sys
from pathlib import Path
from PIL import Image

pages_dir = Path("/Users/sergeymishenev/Desktop/Media Labs/книги/Aurel Krause - The Tlingit Indians (1956)/Aurel Krause - The Tlingit Indians (1956) - pages")
img_path = pages_dir / "page0022.png"

if not img_path.exists():
    print(f"Error: Image not found at {img_path}")
    sys.exit(1)

img = Image.open(str(img_path))
w, h = img.size
print(f"Image dimensions: {w}x{h}")

gray = img.convert("L")
pixels = gray.load()

threshold = 210
# Check rows at the bottom of the text zone (y from 1600 to 1690)
# Print horizontal projection of black pixels (how many black pixels are in each row)
print("\n--- Page 22 Bottom Row Ink Analysis (y from 1600 to 1690) ---")
for y in range(1600, 1690):
    ink_count = sum(1 for x in range(50, w - 50) if pixels[x, y] < threshold)
    if ink_count > 0:
        print(f"Row y={y:4d}: {ink_count:3d} dark pixels")
