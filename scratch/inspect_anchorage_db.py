import json

db_path = "/Users/sergeymishenev/Desktop/Media Labs/книги/Anchorage From Its Humble Origins as a Railroad Construction Camp/Anchorage From Its Humble Origins as a Railroad Construction Camp.json"

with open(db_path, "r", encoding="utf-8") as f:
    data = json.load(f)

print("Keys of main dict:", data.keys())
pages = data.get("pages", {})
print("Type of pages:", type(pages))
if isinstance(pages, dict):
    print("Number of pages in main dict:", len(pages))
    p26_keys = [k for k in pages.keys() if '26' in k or '0026' in k]
    print("Page keys matching '26' or '0026':", p26_keys)
    for pk in p26_keys:
        page_val = pages[pk]
        print(f"\nPage key: {pk}")
        if isinstance(page_val, dict):
            areas = page_val.get("areas", [])
            print(f"Number of areas: {len(areas)}")
            for area in areas:
                print(f"  Area ID: {area.get('id')}, Type: {area.get('type')}, Polygon length: {len(area.get('polygon', []))}")
                print(f"  Polygon: {area.get('polygon')}")
        else:
            print("Value is not a dict:", type(page_val))
elif isinstance(pages, list):
    print("pages is a list of length:", len(pages))
    if len(pages) > 0:
        print("First element in pages list keys/type:", type(pages[0]))
        # Find elements matching page0026
        for p in pages:
            if isinstance(p, dict):
                src = p.get("source_image", "")
                if "page0026" in src or "page0026" in str(p.get("name", "")):
                    print("Found page0026:", p.get("name"), p.get("source_image"))
                    areas = p.get("areas", [])
                    print(f"Number of areas: {len(areas)}")
                    for area in areas:
                        print(f"  Area ID: {area.get('id')}, Type: {area.get('type')}, Polygon length: {len(area.get('polygon', []))}")
                        print(f"  Polygon: {area.get('polygon')}")
