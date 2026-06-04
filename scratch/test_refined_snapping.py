import sys
from pathlib import Path
from PIL import Image

def snap_bbox_to_ink_refined(img: Image.Image, bbox: tuple[int, int, int, int], threshold: int = 210, max_snap: int = 45, gap_limit: int = 5) -> tuple[int, int, int, int]:
    gray = img.convert("L")
    w, h = gray.size
    pixels = gray.load()
    
    L, T, R, B = bbox
    new_L, new_T, new_R, new_B = L, T, R, B
    
    is_ink = lambda x, y: pixels[x, y] < threshold
    
    # 1. Snap Left border outwards
    x_start_L = min(w - 1, L + 15)
    for y in range(max(0, T), min(h, B)):
        last_ink_x = None
        white_run = 0
        x = x_start_L
        while x >= 0 and x >= L - max_snap:
            if is_ink(x, y):
                last_ink_x = x
                white_run = 0
            else:
                if x < L:
                    white_run += 1
                    if white_run > gap_limit:
                        break
            x -= 1
        if last_ink_x is not None and last_ink_x <= L + 5:
            new_L = min(new_L, last_ink_x)
            
    # 2. Snap Right border outwards
    x_start_R = max(0, R - 15)
    for y in range(max(0, T), min(h, B)):
        last_ink_x = None
        white_run = 0
        x = x_start_R
        while x < w and x < R + max_snap:
            if is_ink(x, y):
                last_ink_x = x
                white_run = 0
            else:
                if x >= R:
                    white_run += 1
                    if white_run > gap_limit:
                        break
            x += 1
        if last_ink_x is not None and last_ink_x >= R - 5:
            new_R = max(new_R, last_ink_x + 1)
            
    # 3. Snap Top border outwards
    y_start_T = min(h - 1, T + 15)
    for x in range(max(0, L), min(w, R)):
        last_ink_y = None
        white_run = 0
        y = y_start_T
        while y >= 0 and y >= T - max_snap:
            if is_ink(x, y):
                last_ink_y = y
                white_run = 0
            else:
                if y < T:
                    white_run += 1
                    if white_run > gap_limit:
                        break
            y -= 1
        if last_ink_y is not None and last_ink_y <= T + 5:
            new_T = min(new_T, last_ink_y)
            
    # 4. Snap Bottom border outwards
    y_start_B = max(0, B - 15)
    for x in range(max(0, L), min(w, R)):
        last_ink_y = None
        white_run = 0
        y = y_start_B
        while y < h and y < B + max_snap:
            if is_ink(x, y):
                last_ink_y = y
                white_run = 0
            else:
                if y >= B:
                    white_run += 1
                    if white_run > gap_limit:
                        break
            y += 1
        if last_ink_y is not None and last_ink_y >= B - 5:
            new_B = max(new_B, last_ink_y + 1)
            
    return new_L, new_T, new_R, new_B

# Setup paths
pages_dir = Path("/Users/sergeymishenev/Desktop/Media Labs/книги/Aurel Krause - The Tlingit Indians (1956)/Aurel Krause - The Tlingit Indians (1956) - pages")
img_path = pages_dir / "page0022.png"

if not img_path.exists():
    print(f"Error: Image not found at {img_path}")
    sys.exit(1)

img = Image.open(str(img_path))
# Let's test with page 22's main text block (area_001) where the bottom B cuts at 1647
bbox = (97, 103, 1081, 1647)

print("Running snap_bbox_to_ink_refined on page 22...")
snapped = snap_bbox_to_ink_refined(img, bbox)
print(f"Original Bbox: {bbox}")
print(f"Snapped Bbox:  {snapped}")
