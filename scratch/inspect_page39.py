import json

def view_page_39():
    path = "/Users/sergeymishenev/Desktop/Media Labs/книги/Aurel Krause - The Tlingit Indians (1956)/Aurel Krause - The Tlingit Indians (1956).json"
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    for idx, page in enumerate(data.get("pages", [])):
        img = page.get("source_image", "")
        if "page0039" in img:
            print(json.dumps(page, indent=2, ensure_ascii=False))
            break

if __name__ == "__main__":
    view_page_39()
