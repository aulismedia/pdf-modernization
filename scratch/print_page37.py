import json
path = "/Users/sergeymishenev/Desktop/Media Labs/книги/Aurel Krause - The Tlingit Indians (1956)/Aurel Krause - The Tlingit Indians (1956).json"
with open(path, "r", encoding="utf-8") as f:
    data = json.load(f)
page = data["pages"][37]
for idx, a in enumerate(page.get("areas", [])):
    print(f"Area {idx} ({a.get('type')}):")
    print(a.get("text"))
    print("-" * 50)
