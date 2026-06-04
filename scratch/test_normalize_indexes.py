import json
import re

def test_normalize_gaps():
    path = "/Users/sergeymishenev/Desktop/Media Labs/книги/Aurel Krause - The Tlingit Indians (1956)/Aurel Krause - The Tlingit Indians (1956).json"
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 1. Collect all existing index numbers in <sup>...</sup> in sequence
    all_sups = []
    for p_idx, page in enumerate(data.get("pages", [])):
        if page.get("ignored"):
            continue
        pnum = ""
        for a in page.get("areas", []):
            if a.get("type") == "page_number":
                pnum = (a.get("text") or "").strip()
        
        for a_idx, area in enumerate(page.get("areas", [])):
            if area.get("type") not in ("main_text", "chapter_title", "subtitle"):
                continue
            text = area.get("text") or ""
            for m in re.findall(r"<sup>(\d+)</sup>", text, re.I):
                all_sups.append({
                    "val": int(m),
                    "page_idx": p_idx,
                    "pnum": pnum,
                    "img": page.get("source_image", "")
                })

    print(f"Total existing superscripts: {len(all_sups)}")

    # 2. Identify all sequence gaps
    gaps = []
    for i in range(len(all_sups) - 1):
        curr = all_sups[i]
        nxt = all_sups[i+1]
        
        c_val = curr["val"]
        n_val = nxt["val"]
        
        if c_val < n_val and n_val - c_val > 1:
            gap_size = n_val - c_val - 1
            if gap_size <= 15: # Safe threshold for sequence gaps
                gaps.append({
                    "missing": list(range(c_val + 1, n_val)),
                    "p_start": curr["page_idx"],
                    "p_end": nxt["page_idx"],
                    "c_val": c_val,
                    "n_val": n_val
                })

    print(f"Total sequence gaps detected: {len(gaps)}")

    # 3. Apply normalization for each gap
    changes_count = 0
    for gap in gaps:
        missing = gap["missing"]
        p_start = gap["p_start"]
        p_end = gap["p_end"]
        
        # Scan pages from p_start to p_end (inclusive)
        for page_idx in range(p_start, p_end + 1):
            page = data["pages"][page_idx]
            pnum = ""
            for a in page.get("areas", []):
                if a.get("type") == "page_number":
                    pnum = (a.get("text") or "").strip()
            img = page.get("source_image", "")

            for a_idx, area in enumerate(page.get("areas", [])):
                if area.get("type") not in ("main_text", "chapter_title", "subtitle"):
                    continue
                text = area.get("text") or ""
                new_text = text

                for num in missing:
                    # Match when preceded immediately by letter/punctuation OR by letter/punctuation and a single space
                    pattern = rf"(?:(?<=[a-zA-Z.,?!;:\(\)\[\]\'\"”’“/\\—])|(?<=[a-zA-Z.,?!;:\(\)\[\]\'\"”’“/\\—]\s))({num})\b"
                    matches = list(re.finditer(pattern, new_text))
                    if matches:
                        print(f"\nFound missing index {num} in gap [{gap['c_val']}..{gap['n_val']}] on page_idx {page_idx} ({img}, PageNum {pnum}):")
                        for m in matches:
                            start, end = m.span()
                            context = new_text[max(0, start - 40):min(len(new_text), end + 40)]
                            print(f"  Context: ... {repr(context)} ...")
                        
                        new_text = re.sub(pattern, rf"<sup>\1</sup>", new_text)

                if new_text != text:
                    area["text"] = new_text
                    changes_count += 1

    print(f"\nTotal areas changed: {changes_count}")

if __name__ == "__main__":
    test_normalize_gaps()
