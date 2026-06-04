from pathlib import Path
from PIL import Image

pages_dir = Path("/Users/sergeymishenev/Desktop/Media Labs/книги/Aurel Krause - The Tlingit Indians (1956)/Aurel Krause - The Tlingit Indians (1956) - pages")
img_path = pages_dir / "page0023.png"

img = Image.open(str(img_path))
gray = img.convert("L")
pixels = gray.load()

# Let's print ASCII representation of y from 210 to 330, x from 30 to 1050 with step x=2
threshold = 210

print("ASCII page scan (y 210 to 330):")
for y in range(210, 330):
    chars = []
    has_ink = False
    for x in range(30, 1050, 2):
        val = pixels[x, y]
        if val < threshold:
            chars.append("#")
            has_ink = True
        elif val < 240:
            chars.append(".")
            has_ink = True
        else:
            chars.append(" ")
    if has_ink:
        # Print y and the line
        print(f"y={y:3d}: {''.join(chars)}")
