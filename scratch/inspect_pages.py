import json
import re

path = "/Users/sergeymishenev/Desktop/Media Labs/книги/Aurel Krause - The Tlingit Indians (1956)/Aurel Krause - The Tlingit Indians (1956).json"
with open(path, "r", encoding="utf-8") as f:
    data = json.load(f)

for idx, page in enumerate(data.get("pages", [])):
    pnum = ""
    for a in page.get("areas", []):
        if a.get("type") == "page_number":
            pnum = (a.get("text") or "").strip()
    
    if (20 <= idx <= 30):
        print(f"\n--- Page Index {idx} | PageNum {pnum} | Image {page.get('source_image')} ---")
        for a in page.get("areas", []):
            atype = a.get("type")
            text = a.get("text") or ""
            if atype in ("main_text", "chapter_title", "subtitle", "footnote"):
                sups = re.findall(r"<sup>(\d+)</sup>", text, re.I)
                print(f"[{atype}] sups={sups} | text: {repr(text[:140])}...")
