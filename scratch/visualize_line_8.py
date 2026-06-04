from pathlib import Path
from PIL import Image

pages_dir = Path("/Users/sergeymishenev/Desktop/Media Labs/книги/Aurel Krause - The Tlingit Indians (1956)/Aurel Krause - The Tlingit Indians (1956) - pages")
img_path = pages_dir / "page0023.png"

img = Image.open(str(img_path))
gray = img.convert("L")
pixels = gray.load()

# Line 8 is around y=446 to 456. Let's dump ASCII art from x=900 to 1134 (end of image)
print("ASCII art of Line 8 (obtained per-):")
for y in range(440, 465):
    chars = []
    has_ink = False
    for x in range(900, 1134):
        val = pixels[x, y]
        if val < 210:
            chars.append("#")
            has_ink = True
        elif val < 240:
            chars.append(".")
            has_ink = True
        else:
            chars.append(" ")
    if has_ink:
        print(f"y={y:3d}: {''.join(chars)}")
