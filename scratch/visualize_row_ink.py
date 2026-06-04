from pathlib import Path
from PIL import Image

pages_dir = Path("/Users/sergeymishenev/Desktop/Media Labs/книги/Aurel Krause - The Tlingit Indians (1956)/Aurel Krause - The Tlingit Indians (1956) - pages")
img_path = pages_dir / "page0023.png"

img = Image.open(str(img_path))
gray = img.convert("L")
pixels = gray.load()

threshold = 210

print("ASCII visualization of text right edge (x from 900 to 1050, y from 230 to 300):")
for y in range(230, 300):
    row_chars = []
    has_ink = False
    for x in range(900, 1050):
        val = pixels[x, y]
        if val < threshold:
            row_chars.append("#")
            has_ink = True
        elif val < 240:
            row_chars.append(".")
            has_ink = True
        else:
            row_chars.append(" ")
    if has_ink:
        # print y and the rightmost part that isn't just spaces
        row_str = "".join(row_chars)
        # trim trailing spaces for display but keep coordinate alignment
        print(f"y={y:3d} (x=900): {row_str}")
