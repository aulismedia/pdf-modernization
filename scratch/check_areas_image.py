from pathlib import Path
from PIL import Image

pages_dir = Path("/Users/sergeymishenev/Desktop/Media Labs/книги/Aurel Krause - The Tlingit Indians (1956)/Aurel Krause - The Tlingit Indians (1956) - pages")
img_path = pages_dir / "page0023-areas.png"

if not img_path.exists():
    print("Error: page0023-areas.png not found!")
    import sys
    sys.exit(1)

img = Image.open(str(img_path))
w, h = img.size
print(f"Visualization image dimensions: {w}x{h}")

# AREA_COLORS in utils.config has color for main_text: let's see what color it is.
# In config, it's green. Let's find green pixels on the right side of the image (say x from 900 to 1100)
# for y from 300 to 400 (where Wilhelm is).
# A green border pixel will have high G and low R, B.
pixels = img.load()
green_xs = set()
for y in range(350, 450):
    for x in range(900, 1100):
        r, g, b = pixels[x, y][:3]
        # Green border is solid green, e.g. R around 0-50, G around 150-255, B around 0-50
        if g > 150 and r < 100 and b < 100:
            green_xs.add(x)

print(f"X-coordinates with green border pixels in y=350..450: {sorted(list(green_xs))}")
