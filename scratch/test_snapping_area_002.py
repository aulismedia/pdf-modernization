import sys
from pathlib import Path
from PIL import Image
from pipeline_flow.step3_detect_layout import snap_bbox_to_ink

pages_dir = Path("/Users/sergeymishenev/Desktop/Media Labs/книги/Aurel Krause - The Tlingit Indians (1956)/Aurel Krause - The Tlingit Indians (1956) - pages")
img_path = pages_dir / "page0023.png"

if not img_path.exists():
    print(f"Error: Image not found at {img_path}")
    sys.exit(1)

img = Image.open(str(img_path))

# Let's test with initial Bbox coordinates for area_002 on page 23
# The model returned area_002 as [37, 1607, 1008, 1670] (approx) or similar
# Let's see what happens with R=1008, R=1010, R=1015
bboxes = [
    (37, 1607, 1008, 1670),
    (37, 1607, 1010, 1670),
    (37, 1607, 1015, 1670)
]

for bbox in bboxes:
    print(f"\n--- Initial Bbox: {bbox} ---")
    res_default = snap_bbox_to_ink(img, bbox)
    print(f"Snapped (default max_snap=25, gap_limit=5): {res_default}")
    
    res_50 = snap_bbox_to_ink(img, bbox, max_snap=50)
    print(f"Snapped (max_snap=50, gap_limit=5):         {res_50}")
    
    res_50_8 = snap_bbox_to_ink(img, bbox, max_snap=50, gap_limit=8)
    print(f"Snapped (max_snap=50, gap_limit=8):         {res_50_8}")
