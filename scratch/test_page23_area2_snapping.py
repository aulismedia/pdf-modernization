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

# Let's inspect rows of area_002 (y from 234 to 1604)
# Let's find the absolute rightmost ink pixel for each row, and print rows where ink goes beyond 1037
threshold = 210
B_limit = 1604
T_limit = 234

print("Checking rows of area_002 for ink right of 1030:")
rows_with_far_ink = 0
for y in range(T_limit, B_limit):
    # Find the rightmost dark pixel in this row in a wide search band (e.g. from 1000 to 1100)
    rightmost_ink = None
    for x in range(1000, 1120):
        if pixels[x, y] < threshold:
            rightmost_ink = x
            
    if rightmost_ink is not None and rightmost_ink > 1030:
        rows_with_far_ink += 1
        if rows_with_far_ink <= 30:
            # Let's print the pixel values around the rightmost ink to see if it's solid or noise
            context = [pixels[x, y] for x in range(rightmost_ink - 5, min(1134, rightmost_ink + 10))]
            print(f"y={y:4d}: rightmost ink x={rightmost_ink:4d} (val={pixels[rightmost_ink, y]}) | context={context}")

print(f"Total rows with ink > 1030: {rows_with_far_ink}")
