from pathlib import Path
from PIL import Image

pages_dir = Path("/Users/sergeymishenev/Desktop/Media Labs/книги/Aurel Krause - The Tlingit Indians (1956)/Aurel Krause - The Tlingit Indians (1956) - pages")
img_path = pages_dir / "page0023.png"

img = Image.open(str(img_path))
gray = img.convert("L")
pixels = gray.load()

threshold = 210

print("Scanning for dark pixels (x from 950 to 1050, y from 200 to 300):")
for y in range(200, 300):
    dark_xs = []
    for x in range(950, 1050):
        if pixels[x, y] < threshold:
            dark_xs.append(x)
    if dark_xs:
        print(f"y={y:3d}: x from {min(dark_xs)} to {max(dark_xs)} (count={len(dark_xs)})")
