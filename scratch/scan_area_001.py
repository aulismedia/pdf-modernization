from pathlib import Path
from PIL import Image

pages_dir = Path("/Users/sergeymishenev/Desktop/Media Labs/книги/Aurel Krause - The Tlingit Indians (1956)/Aurel Krause - The Tlingit Indians (1956) - pages")
img_path = pages_dir / "page0023.png"

img = Image.open(str(img_path))
gray = img.convert("L")
pixels = gray.load()

threshold = 210

# Scan area_001 y range: 100 to 228
print("Scanning area_001 for dark pixels (y from 100 to 230):")
lines = []
in_line = False
start_y = None

for y in range(100, 230):
    has_ink = any(pixels[x, y] < threshold for x in range(490, 510))
    if has_ink:
        if not in_line:
            in_line = True
            start_y = y
    else:
        if in_line:
            in_line = False
            end_y = y
            lines.append((start_y, end_y))

for idx, (sy, ey) in enumerate(lines):
    mid_y = (sy + ey) // 2
    rightmost_x = None
    for y_val in range(sy, ey):
        for x_val in range(800, 1134):
            if pixels[x_val, y_val] < threshold:
                if rightmost_x is None or x_val > rightmost_x:
                    rightmost_x = x_val
    print(f"Area 001 Line {idx+1} (y {sy:3d}..{ey:3d}): rightmost ink at x={rightmost_x}")
