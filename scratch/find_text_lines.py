from pathlib import Path
from PIL import Image

pages_dir = Path("/Users/sergeymishenev/Desktop/Media Labs/книги/Aurel Krause - The Tlingit Indians (1956)/Aurel Krause - The Tlingit Indians (1956) - pages")
img_path = pages_dir / "page0023.png"

img = Image.open(str(img_path))
gray = img.convert("L")
pixels = gray.load()

T_limit = 234
B_limit = 1604
threshold = 210

# To find where lines of text are, let's look at a vertical column (say x=500)
# and find contiguous segments of y where pixels are dark.
lines = []
in_line = False
start_y = None

for y in range(T_limit, B_limit):
    # Check if there is any dark pixel in a small horizontal span around x=500
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

print(f"Found {len(lines)} lines of text:")
for idx, (sy, ey) in enumerate(lines):
    mid_y = (sy + ey) // 2
    # Let's find the rightmost ink in this line y range [sy, ey]
    rightmost_x = None
    rightmost_val = None
    rightmost_exact_y = None
    for y_val in range(sy, ey):
        for x_val in range(800, 1134):
            if pixels[x_val, y_val] < threshold:
                if rightmost_x is None or x_val > rightmost_x:
                    rightmost_x = x_val
                    rightmost_val = pixels[x_val, y_val]
                    rightmost_exact_y = y_val
                    
    # Let's print the text line info
    print(f"Line {idx+1:2d} (y {sy:4d}..{ey:4d}, mid={mid_y:4d}): rightmost ink at x={rightmost_x} (y={rightmost_exact_y}, val={rightmost_val})")
