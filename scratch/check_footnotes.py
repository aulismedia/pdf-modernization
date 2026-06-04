import json
path = "/Users/sergeymishenev/Desktop/Media Labs/книги/Aurel Krause - The Tlingit Indians (1956)/Aurel Krause - The Tlingit Indians (1956).json"
with open(path, "r", encoding="utf-8") as f:
    data = json.load(f)

# Print footnotes on page index 37 and 38
for p in (37, 38):
    print(f"\n--- Page {p} Footnotes ---")
    for a in data["pages"][p].get("areas", []):
        if a.get("type") == "footnote":
            print(a.get("text"))
