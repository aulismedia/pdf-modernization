import json
import re

def test_norm():
    path = "/Users/sergeymishenev/Desktop/Media Labs/книги/Aurel Krause - The Tlingit Indians (1956)/Aurel Krause - The Tlingit Indians (1956).json"
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 1. Force page0038.png area_001 to have 62 without <sup>
    page_38 = None
    for page in data.get("pages", []):
        if page.get("source_image") == "page0038.png":
            page_38 = page
            break

    if not page_38:
        print("Error: page0038.png not found")
        return

    area = page_38["areas"][0]
    original_text = area["text"]
    
    # Replace <sup>62</sup> with 62
    if "<sup>62</sup>" in original_text:
        area["text"] = original_text.replace("<sup>62</sup>", "62")
        print("Simulated raw text containing 'Ltua Gulf 62'")
    else:
        print("Text already contains '62' without <sup>")

    # 2. Run the gap detection & normalization algorithm from app.py
    # A. Collect all existing index numbers in <sup>...</sup> in sequence
    all_sups = []
    for p_idx, page in enumerate(data.get("pages", [])):
        if page.get("ignored"):
            continue
        for a_idx, area_obj in enumerate(page.get("areas", [])):
            if area_obj.get("type") not in ("main_text", "chapter_title", "subtitle"):
                continue
            text = area_obj.get("text") or ""
            for m in re.findall(r"<sup>(\d+)</sup>", text, re.I):
                all_sups.append({
                    "val": int(m),
                    "page_idx": p_idx
                })

    # B. Identify all sequence gaps (with safe gap size threshold)
    gaps = []
    for i in range(len(all_sups) - 1):
        curr = all_sups[i]
        nxt = all_sups[i+1]
        c_val = curr["val"]
        n_val = nxt["val"]
        if c_val < n_val and n_val - c_val > 1:
            gap_size = n_val - c_val - 1
            if gap_size <= 15:  # Safe threshold to filter out chapter resets
                gaps.append({
                    "missing": list(range(c_val + 1, n_val)),
                    "p_start": curr["page_idx"],
                    "p_end": nxt["page_idx"]
                })

    print(f"Total gaps detected: {len(gaps)}")
    # Print the gap containing 62 if it exists
    gap_62 = [g for g in gaps if 62 in g["missing"]]
    if gap_62:
        print(f"Found gap for 62: {gap_62}")
    else:
        print("WARNING: No gap containing 62 was detected!")
        # Let's print the sups around 62
        around = [s for s in all_sups if 55 <= s["val"] <= 70]
        print(f"Existing superscripts around 62: {around}")

    # C. Resolve gaps by wrapping missing integers in candidate page ranges
    changes = []
    for gap in gaps:
        missing = gap["missing"]
        p_start = gap["p_start"]
        p_end = gap["p_end"]
        
        for page_idx in range(p_start, p_end + 1):
            page = data["pages"][page_idx]
            if page.get("ignored"):
                continue
            
            for area_obj in page.get("areas", []):
                if area_obj.get("type") not in ("main_text", "chapter_title", "subtitle"):
                    continue
                text = area_obj.get("text") or ""
                new_text = text
                
                for num in missing:
                    pattern = rf"(?:(?<=[a-zA-Z.,?!;:\(\)\[\]\'\"”’“/\\—])|(?<=[a-zA-Z.,?!;:\(\)\[\]\'\"”’“/\\—]\s))({num})\b"
                    new_text = re.sub(pattern, rf"<sup>\1</sup>", new_text)
                
                if new_text != text:
                    changes.append({
                        "page": page.get("source_image"),
                        "from": text[max(0, text.find("Ltua Gulf") - 20):text.find("Ltua Gulf") + 40],
                        "to": new_text[max(0, new_text.find("Ltua Gulf") - 20):new_text.find("Ltua Gulf") + 40]
                    })

    print(f"Total changes: {len(changes)}")
    for c in changes:
        print(f"Change on page {c['page']}:")
        print(f"  Before: {repr(c['from'])}")
        print(f"  After:  {repr(c['to'])}")

if __name__ == "__main__":
    test_norm()
