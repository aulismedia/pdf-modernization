import sys
from pathlib import Path
from PIL import Image

# Setup paths
pages_dir = Path("/Users/sergeymishenev/Desktop/Media Labs/книги/Aurel Krause - The Tlingit Indians (1956)/Aurel Krause - The Tlingit Indians (1956) - pages")
img_path = pages_dir / "page0023.png"

if not img_path.exists():
    print(f"Error: Image not found at {img_path}")
    sys.exit(1)

img = Image.open(str(img_path))
w, h = img.size
print(f"Image dimensions: {w}x{h}")

gray = img.convert("L")
pixels = gray.load()

# area_002 polygon in JSON is:
# L=37, T=233, R=1020, B=1600.
L, T, R, B = 37, 233, 1020, 1600
threshold = 210
max_snap = 25
gap_limit = 5

is_ink = lambda x, y: pixels[x, y] < threshold

print(f"\n--- Checking right border of area_002 (R={R}, scanning from R-15 to R+{max_snap}) ---")
# Let's count how many rows have ink to the right of R (from R to R + max_snap)
ink_rows_outward = 0
for y in range(T, B):
    row_has_ink = False
    ink_details = []
    # Scan from R - 15 outward
    x_start_R = R - 15
    last_ink_x = None
    white_run = 0
    x = x_start_R
    while x < w and x < R + max_snap:
        val = pixels[x, y]
        if val < threshold:
            last_ink_x = x
            white_run = 0
            row_has_ink = True
        else:
            white_run += 1
            if white_run > gap_limit:
                break
        x += 1
    
    if last_ink_x is not None and last_ink_x >= R - 5:
        ink_rows_outward += 1
        if ink_rows_outward <= 20: # print first 20 rows with ink found
            print(f"Row y={y}: last ink at x={last_ink_x} (pixel value {pixels[last_ink_x, y]}), new_R candidate = {last_ink_x + 1}")

print(f"\nTotal rows where ink snapping could expand rightwards: {ink_rows_outward} out of {B - T} rows.")

# Let's also check absolute ink presence to the right of R = 1020, regardless of the gap-tolerant starting point
print(f"\n--- Checking raw ink distribution to the right of R={R} up to x=w ---")
ink_cols = {}
for x in range(R, min(w, R + 100)):
    dark_pixels = 0
    for y in range(T, B):
        if pixels[x, y] < threshold:
            dark_pixels += 1
    if dark_pixels > 0:
        ink_cols[x] = dark_pixels

for x in sorted(ink_cols.keys()):
    print(f"Col x={x}: {ink_cols[x]} dark pixels")
