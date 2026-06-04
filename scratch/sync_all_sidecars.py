import json
from pathlib import Path

def sync_all_sidecars():
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
        print(f"Error: Project {project_id} not found in projects.json")
        return
        
    source_path = Path(project["source_path"])
    book_dir = source_path.parent
    book_json_path = book_dir / f"{source_path.stem}.json"
    pages_dir = book_dir / f"{source_path.stem} - pages"
    
    print(f"Book JSON path: {book_json_path}")
    print(f"Pages directory: {pages_dir}")
    
    if not book_json_path.exists():
        print(f"Error: Book JSON does not exist at {book_json_path}")
        return
        
    if not pages_dir.exists():
        print(f"Error: Pages directory does not exist at {pages_dir}")
        return
        
    with open(book_json_path, "r", encoding="utf-8") as f:
        book_data = json.load(f)
        
    synced_count = 0
    skipped_count = 0
    for page in book_data.get("pages", []):
        if page.get("ignored"):
            continue
            
        areas = page.get("areas")
        if not areas:
            continue
            
        source_image = page.get("source_image")
        if not source_image:
            continue
            
        sidecar_path = pages_dir / f"{Path(source_image).stem}-areas.json"
        if not sidecar_path.exists():
            skipped_count += 1
            continue
            
        # Let's compare existing sidecar with page dict
        with open(sidecar_path, "r", encoding="utf-8") as f:
            try:
                existing_data = json.load(f)
            except Exception as e:
                existing_data = {}
                
        # To avoid trivial layout/spacing diffs, let's check if the text matches
        # or if we just want to overwrite it cleanly
        page_str = json.dumps(page, ensure_ascii=False, indent=2)
        existing_str = json.dumps(existing_data, ensure_ascii=False, indent=2)
        
        if page_str != existing_str:
            # Atomic write
            temp_path = sidecar_path.with_suffix(".tmp")
            temp_path.write_text(page_str, encoding="utf-8")
            temp_path.replace(sidecar_path)
            synced_count += 1
            print(f"Synced {source_image} to {sidecar_path.name}")
            
    print(f"\nDone! Synced {synced_count} sidecars. Skipped {skipped_count} non-existent sidecars.")

if __name__ == "__main__":
    sync_all_sidecars()
