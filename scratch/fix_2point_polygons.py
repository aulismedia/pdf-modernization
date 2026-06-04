import os
import json

db_path = "/Users/sergeymishenev/Desktop/Media Labs/книги/Anchorage From Its Humble Origins as a Railroad Construction Camp/Anchorage From Its Humble Origins as a Railroad Construction Camp.json"
pages_dir = "/Users/sergeymishenev/Desktop/Media Labs/книги/Anchorage From Its Humble Origins as a Railroad Construction Camp/Anchorage From Its Humble Origins as a Railroad Construction Camp - pages/"

def fix_polygon(poly):
    if not poly or len(poly) != 2:
        return poly
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    x1, x2 = min(xs), max(xs)
    y1, y2 = min(ys), max(ys)
    return [
        [x1, y1],
        [x2, y1],
        [x2, y2],
        [x1, y2]
    ]

def fix_main_db():
    print(f"Loading main DB: {db_path}")
    with open(db_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    modified_pages = 0
    total_fixed_areas = 0
    
    pages = data.get("pages", [])
    if isinstance(pages, list):
        for page_data in pages:
            if not isinstance(page_data, dict):
                continue
            page_name = page_data.get("source_image", page_data.get("name", "Unknown"))
            areas = page_data.get("areas", [])
            page_modified = False
            for area in areas:
                poly = area.get("polygon", [])
                if len(poly) == 2:
                    new_poly = fix_polygon(poly)
                    area["polygon"] = new_poly
                    print(f"  Fixed 2-point polygon in {page_name}, area {area.get('id')}: {poly} -> {new_poly}")
                    total_fixed_areas += 1
                    page_modified = True
            if page_modified:
                modified_pages += 1
            
    if total_fixed_areas > 0:
        print(f"Saving modified main DB with {total_fixed_areas} fixed areas across {modified_pages} pages...")
        with open(db_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    else:
        print("No 2-point polygons found in main DB.")

def fix_sidecars():
    print(f"Scanning sidecar files in {pages_dir}")
    if not os.path.exists(pages_dir):
        print("Pages directory does not exist.")
        return
        
    modified_files = 0
    total_fixed_areas = 0
    
    for filename in os.listdir(pages_dir):
        if filename.endswith("-areas.json"):
            filepath = os.path.join(pages_dir, filename)
            with open(filepath, "r", encoding="utf-8") as f:
                try:
                    sidecar_data = json.load(f)
                except Exception as e:
                    print(f"Error reading {filename}: {e}")
                    continue
                    
            if not isinstance(sidecar_data, dict):
                continue
                
            areas = sidecar_data.get("areas", [])
            if not isinstance(areas, list):
                continue
                
            file_modified = False
            for area in areas:
                poly = area.get("polygon", [])
                if len(poly) == 2:
                    new_poly = fix_polygon(poly)
                    area["polygon"] = new_poly
                    print(f"  [{filename}] Fixed 2-point polygon in area {area.get('id')}: {poly} -> {new_poly}")
                    total_fixed_areas += 1
                    file_modified = True
                    
            if file_modified:
                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(sidecar_data, f, ensure_ascii=False, indent=2)
                modified_files += 1
                
    print(f"Fixed {total_fixed_areas} areas across {modified_files} sidecar files.")

if __name__ == "__main__":
    fix_main_db()
    fix_sidecars()
