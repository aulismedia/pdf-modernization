import sys
from pathlib import Path
from PIL import Image

pages_dir = Path("/Users/sergeymishenev/Desktop/Media Labs/книги/Aurel Krause - The Tlingit Indians (1956)/Aurel Krause - The Tlingit Indians (1956) - pages")
img_path = pages_dir / "page0023.png"

if not img_path.exists():
    print(f"Error: Image not found at {img_path}")
    sys.exit(1)

img = Image.open(str(img_path))
gray = img.convert("L")
pixels = gray.load()

# Let's inspect the pixels in the y range 1607 to 1670, for x from 1010 to 1040
T, B = 1607, 1670
print(f"--- Detailed Pixel Inspection of right margin on page 23 (y from {T} to {B}) ---")
threshold = 210
gap_limit = 5

for y in range(T, B):
    # Scan from 993 (which is 1008 - 15) outward
    x_start_R = 993
    last_ink_x = None
    white_run = 0
    ink_string = ""
    for x in range(x_start_R, 1045):
        val = pixels[x, y]
        is_ink = val < threshold
        if is_ink:
            last_ink_x = x
            white_run = 0
            ink_string += "#"
        else:
            white_run += 1
            if white_run > gap_limit:
                ink_string += f"|[white gap of {white_run}px reached, stopping]"
                break
            ink_string += "."
            
    if last_ink_x is not None and last_ink_x >= 1003:
        print(f"y={y:4d}: last ink x={last_ink_x:4d} | Trace: {ink_string[:80]}")
