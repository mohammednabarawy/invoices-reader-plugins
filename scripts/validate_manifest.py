import os
import json
import sys
from pathlib import Path

REQUIRED_FIELDS = ["id", "name", "version", "author", "description", "main"]

def validate_manifests():
    plugins_dir = Path(__file__).parent.parent / "plugins"
    if not plugins_dir.exists():
        print(f"Error: Plugins directory not found at {plugins_dir}")
        sys.exit(1)
        
    has_errors = False
    
    print(f"Validating plugins in {plugins_dir}...")
    
    for item in plugins_dir.iterdir():
        if item.is_dir() and not item.name.startswith("."):
            manifest_path = item / "manifest.json"
            if not manifest_path.exists():
                print(f"Error: Missing manifest.json in {item.name}")
                has_errors = True
                continue
                
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
                    
                # Check required fields
                missing = [field for field in REQUIRED_FIELDS if field not in manifest]
                if missing:
                    print(f"Error: {item.name}/manifest.json missing fields: {missing}")
                    has_errors = True
                    
                # Check ID matches folder name
                if manifest.get("id") != item.name:
                    print(f"Error: {item.name}/manifest.json ID '{manifest.get('id')}' does not match folder name '{item.name}'")
                    has_errors = True
                    
                # Check main file exists
                main_file = manifest.get("main")
                if main_file and not (item / main_file).exists():
                     print(f"Error: {item.name}/manifest.json 'main' file '{main_file}' not found")
                     has_errors = True
                     
                print(f"  + Valid: {item.name}")
                
            except json.JSONDecodeError:
                print(f"Error: {item.name}/manifest.json is not valid JSON")
                has_errors = True
            except Exception as e:
                print(f"Error reading {item.name}/manifest.json: {e}")
                has_errors = True
                
    if has_errors:
        sys.exit(1)
    else:
        print("\nAll manifests valid.")

if __name__ == "__main__":
    validate_manifests()
