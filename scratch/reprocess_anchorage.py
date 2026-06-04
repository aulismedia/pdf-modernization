import json
import sys
import re
from pathlib import Path
from PIL import Image

sys.path.append("/Users/sergeymishenev/Documents/development/pdf-modernization")
sys.path.append("/Users/sergeymishenev/Documents/development/pdf-modernization/steps")
from utils.rotation_broker import RotationBroker
from steps.step2_detect_areas import _clip_illustrations_from_text
from steps.step3_visualize_areas import visualize_page

# Define paths
book_dir = Path("/Users/sergeymishenev/Desktop/Media Labs/книги/Anchorage From Its Humble Origins as a Railroad Construction Camp")
book_name = "Anchorage From Its Humble Origins as a Railroad Construction Camp"
json_path = book_dir / f"{book_name}.json"
pages_dir = book_dir / f"{book_name} - pages"

if not json_path.exists():
    print(f"Error: {json_path} does not exist!")
    sys.exit(1)

# Load existing JSON
data = json.loads(json_path.read_text(encoding="utf-8"))
pages = data.get("pages", [])

updated_count = 0

for p_data in pages:
    pname = p_data.get("source_image")
    if not pname:
        continue
    m = re.search(r'page(\d+)\.png', pname)
    if m and int(m.group(1)) < 19:
        continue
    raw_path = pages_dir / f"{Path(pname).stem}.raw"
    if not raw_path.exists():
        continue
    
    # Load raw JSON
    try:
        raw_text = raw_path.read_text(encoding="utf-8")
        cleaned = re.sub(r"^```[a-z]*\s*", "", raw_text.strip(), flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = cleaned.replace(',"null,"', ',"')
        cleaned = re.sub(r'\\([^"\\/bfnrtu])', r'\1', cleaned)
        cleaned = re.sub(r'(\[\d+,\d+\])(,"text":)', r'\1]\2', cleaned)
        
        _escapes = {'\n': '\\n', '\r': '\\r', '\t': '\\t'}
        _json_structural = {',', '}', ']', ':'}
        result_chars = []
        in_string = False
        i = 0
        while i < len(cleaned):
            ch = cleaned[i]
            if ch == '\\' and in_string:
                result_chars.append(ch)
                i += 1
                if i < len(cleaned):
                    result_chars.append(cleaned[i])
                    i += 1
                continue
            if ch == '"':
                if in_string:
                    j = i + 1
                    while j < len(cleaned) and cleaned[j] in ' \t\r\n':
                        j += 1
                    if j < len(cleaned) and cleaned[j] not in _json_structural:
                        result_chars.append('\\"')
                    else:
                        in_string = False
                        result_chars.append(ch)
                else:
                    in_string = True
                    result_chars.append(ch)
            elif in_string and ch in _escapes:
                result_chars.append(_escapes[ch])
            else:
                result_chars.append(ch)
            i += 1
        raw_data = json.loads(''.join(result_chars))
    except Exception as e:
        print(f"Failed to parse raw file for {pname}: {e}")
        continue
        
    img_path = pages_dir / pname
    if not img_path.exists():
        print(f"Image not found: {img_path}")
        continue
        
    img = Image.open(img_path)
    orig_w, orig_h = img.size
    
    rotation = int(p_data.get("rotation") or 0)
    content_bbox = p_data.get("content_bbox")
    
    rot_w, rot_h = img.size
    if rotation:
        img_rot = img.rotate(-rotation, expand=True)
        rot_w, rot_h = img_rot.size
        
    rot_bbox = None
    if content_bbox:
        left = content_bbox.get("left", 0)
        top = content_bbox.get("top", 0)
        right = content_bbox.get("right", orig_w)
        bottom = content_bbox.get("bottom", orig_h)
        bbox_t = (left, top, right, bottom)
        rot_bbox = RotationBroker._rotate_bbox_cw(bbox_t, rotation, orig_w, orig_h) if rotation else bbox_t
        
    uses_gemini = "gemini" in p_data.get("detected_by", "").lower()
    
    result = {
        "page_dimensions": raw_data.get("page_dimensions", {}),
        "areas": raw_data.get("areas", [])
    }
    
    RotationBroker.normalize_model_result_coords(
        result, rot_w, rot_h, rot_bbox,
        uses_gemini_normalization=uses_gemini
    )
    
    if rotation:
        for area in result.get("areas", []):
            area["polygon"] = [
                list(RotationBroker.unrotate_point(x, y, rotation, orig_w, orig_h))
                for x, y in area["polygon"]
            ]
        result["page_dimensions"] = {"width": orig_w, "height": orig_h}
        
    RotationBroker.clamp_coords(result)
    _clip_illustrations_from_text(result.get("areas", []))
    
    p_data["page_dimensions"] = {"width": orig_w, "height": orig_h}
    p_data["areas"] = result.get("areas", [])
    
    sidecar_path = pages_dir / f"{Path(pname).stem}-areas.json"
    sidecar_path.write_text(json.dumps(p_data, ensure_ascii=False, indent=2), encoding="utf-8")
    
    try:
        visualize_page(img_path, p_data, pages_dir / f"{Path(pname).stem}-areas.png", 60)
    except Exception as e:
        print(f"Failed to visualize page {pname}: {e}")
        
    updated_count += 1
    print(f"Updated {pname}")

# Re-write main JSON
json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"Completed! Reprocessed {updated_count} pages.")
