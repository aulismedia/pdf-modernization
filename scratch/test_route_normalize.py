import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import app

def test_endpoint():
    print("Initializing Flask test client...")
    client = app.test_client()
    
    pid = "0a62ca94"
    print(f"Sending POST request to /projects/{pid}/api/normalize-all ...")
    res = client.post(f"/projects/{pid}/api/normalize-all")
    
    print("\n--- Response ---")
    print("Status Code:", res.status_code)
    print("Body:", res.get_data(as_text=True))

if __name__ == "__main__":
    test_endpoint()
