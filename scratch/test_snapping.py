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
bbox = (37, 233, 1020, 1600)

print("Running snap_bbox_to_ink with original parameters (max_snap=25, gap_limit=5)...")
res_default = snap_bbox_to_ink(img, bbox)
print(f"Original Bbox: {bbox}")
print(f"Snapped Bbox (default): {res_default}")

print("\nRunning with increased max_snap (max_snap=50, gap_limit=5)...")
res_50 = snap_bbox_to_ink(img, bbox, max_snap=50)
print(f"Snapped Bbox (max_snap=50): {res_50}")

print("\nRunning with increased max_snap & gap_limit (max_snap=50, gap_limit=8)...")
res_50_8 = snap_bbox_to_ink(img, bbox, max_snap=50, gap_limit=8)
print(f"Snapped Bbox (max_snap=50, gap_limit=8): {res_50_8}")
