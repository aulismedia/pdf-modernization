import json
import re

path = "/Users/sergeymishenev/Desktop/Media Labs/книги/Aurel Krause - The Tlingit Indians (1956)/Aurel Krause - The Tlingit Indians (1956).json"
with open(path, "r", encoding="utf-8") as f:
    data = json.load(f)

# Collect all sups with their page index and area index
all_sups = []
for p_idx, page in enumerate(data.get("pages", [])):
    if page.get("ignored"):
        continue
    for a_idx, area in enumerate(page.get("areas", [])):
        if area.get("type") not in ("main_text", "chapter_title", "subtitle"):
            continue
        text = area.get("text") or ""
        for m in re.findall(r"<sup>(\d+)</sup>", text, re.I):
            all_sups.append({
                "val": int(m),
                "page_idx": p_idx,
                "area_idx": a_idx,
                "img": page.get("source_image", "")
            })

print(f"Total superscripts found: {len(all_sups)}")

# Find gaps between consecutive superscripts in the sequence
for i in range(len(all_sups) - 1):
    curr = all_sups[i]
    nxt = all_sups[i+1]
    
    c_val = curr["val"]
    n_val = nxt["val"]
    
    if c_val < n_val and n_val - c_val > 1:
        gap_size = n_val - c_val - 1
        if gap_size <= 15: # threshold for standard gaps
            gap_nums = list(range(c_val + 1, n_val))
            print(f"\nGap detected: {gap_nums} (size {gap_size})")
            print(f"  Left sup:  {c_val} on page {curr['page_idx']} ({curr['img']})")
            print(f"  Right sup: {n_val} on page {nxt['page_idx']} ({nxt['img']})")
