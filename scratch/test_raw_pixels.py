from pathlib import Path
from PIL import Image

pages_dir = Path("/Users/sergeymishenev/Desktop/Media Labs/книги/Aurel Krause - The Tlingit Indians (1956)/Aurel Krause - The Tlingit Indians (1956) - pages")
img_path = pages_dir / "page0023.png"

img = Image.open(str(img_path))
gray = img.convert("L")
pixels = gray.load()

# Let's inspect some rows where text ends to see if there is any faint ink or other content
# y=244 is the first line of area_002: "Several days later than Chirikof, on July 29/18, Bering saw"
# y=297 is the second line: "land and on July 31/20, St. Elias day of the year 1741, he an-"
# y=348 is another line
# Let's find where the lines actually are by looking for dark rows in general
print("--- Row pixel scan around right edge ---")
rows_to_check = [244, 297, 348, 451, 615]
for y in rows_to_check:
    print(f"\ny={y}:")
    row_pixels = []
    for x in range(1015, 1070):
        row_pixels.append((x, pixels[x, y]))
    # print all pixels that are < 255 (i.e. not pure white)
    non_white = [f"{x}:{val}" for x, val in row_pixels if val < 254]
    print(" ".join(non_white))
