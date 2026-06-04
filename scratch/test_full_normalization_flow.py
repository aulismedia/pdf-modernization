import json
import re
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import app

def run_integration_test():
    project_id = "0a62ca94"
    projects_file = Path("/Users/sergeymishenev/Documents/development/pdf-modernization/projects.json")
    
    with open(projects_file, "r", encoding="utf-8") as f:
        projects_data = json.load(f)
        
    project = None
    for p in projects_data.get("projects", []):
        if p.get("id") == project_id:
            project = p
            break
            
    if not project:
        print(f"Error: Project {project_id} not found")
        return
        
    source_path = Path(project["source_path"])
    book_dir = source_path.parent
    book_json_path = book_dir / f"{source_path.stem}.json"
    pages_dir = book_dir / f"{source_path.stem} - pages"
    sidecar_path = pages_dir / "page0038-areas.json"
    
    print("--- 1. Preparation: Reverting 62 on Page 38 in both master JSON and sidecar JSON ---")
    
    # Revert sidecar JSON
    if sidecar_path.exists():
        with open(sidecar_path, "r", encoding="utf-8") as f:
            sidecar_data = json.load(f)
        area = sidecar_data["areas"][0]
        if "<sup>62</sup>" in area["text"]:
            area["text"] = area["text"].replace("<sup>62</sup>", "62")
            with open(sidecar_path, "w", encoding="utf-8") as f:
                json.dump(sidecar_data, f, ensure_ascii=False, indent=2)
            print("Successfully reverted sidecar JSON to raw '62'")
        else:
            print("Sidecar JSON already contains raw '62'")
            
    # Revert master book JSON
    if book_json_path.exists():
        with open(book_json_path, "r", encoding="utf-8") as f:
            book_data = json.load(f)
        
        page_38 = None
        for page in book_data.get("pages", []):
            if page.get("source_image") == "page0038.png":
                page_38 = page
                break
                
        if page_38:
            area = page_38["areas"][0]
            if "<sup>62</sup>" in area["text"]:
                area["text"] = area["text"].replace("<sup>62</sup>", "62")
                with open(book_json_path, "w", encoding="utf-8") as f:
                    json.dump(book_data, f, ensure_ascii=False, indent=2)
                print("Successfully reverted master book JSON to raw '62'")
            else:
                print("Master book JSON already contains raw '62'")
                
    print("\n--- 2. Execution: Sending POST request to /projects/0a62ca94/api/normalize-all ---")
    client = app.test_client()
    res = client.post(f"/projects/{project_id}/api/normalize-all")
    print("Response Status Code:", res.status_code)
    print("Response Body:", res.get_data(as_text=True))
    
    print("\n--- 3. Verification: Checking that both master JSON and sidecar JSON have <sup>62</sup> ---")
    
    # Check sidecar JSON
    with open(sidecar_path, "r", encoding="utf-8") as f:
        sidecar_data = json.load(f)
    sidecar_text = sidecar_data["areas"][0]["text"]
    if "<sup>62</sup>" in sidecar_text:
        print("✅ SUCCESS: Sidecar page0038-areas.json has been correctly normalized with <sup>62</sup>!")
    else:
        print("❌ FAILURE: Sidecar page0038-areas.json does NOT contain <sup>62</sup>!")
        
    # Check master book JSON
    with open(book_json_path, "r", encoding="utf-8") as f:
        book_data = json.load(f)
    for page in book_data.get("pages", []):
        if page.get("source_image") == "page0038.png":
            book_text = page["areas"][0]["text"]
            if "<sup>62</sup>" in book_text:
                print("✅ SUCCESS: Master book JSON has been correctly normalized with <sup>62</sup>!")
            else:
                print("❌ FAILURE: Master book JSON does NOT contain <sup>62</sup>!")

if __name__ == "__main__":
    run_integration_test()
