import os
import json
import sys
from pathlib import Path

REQUIRED_FIELDS = ["id", "name", "version", "description", "author", "min_app_version", "main", "plugin_class"]
PLUGINS_DIR = Path("plugins")

def validate_plugin(plugin_path):
    manifest_path = plugin_path / "manifest.json"
    
    # 1. Check if manifest exists
    if not manifest_path.exists():
        print(f"❌ [FAIL] {plugin_path.name}: Missing manifest.json")
        return False
    
    # 2. Parse JSON
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except json.JSONDecodeError as e:
        print(f"❌ [FAIL] {plugin_path.name}: Invalid JSON in manifest.json - {e}")
        return False
        
    # 3. Check required fields
    missing = [field for field in REQUIRED_FIELDS if field not in manifest]
    if missing:
        print(f"❌ [FAIL] {plugin_path.name}: Missing required fields in manifest: {missing}")
        return False
        
    # 4. Check folder name matches ID
    plugin_id = manifest.get("id")
    if plugin_id != plugin_path.name:
        print(f"❌ [FAIL] {plugin_path.name}: manifest ID '{plugin_id}' does not match folder name '{plugin_path.name}'")
        return False
        
    # 5. Check dependencies (optional but structure check)
    if "dependencies" in manifest and not isinstance(manifest["dependencies"], list):
         print(f"❌ [FAIL] {plugin_path.name}: 'dependencies' must be a list")
         return False

    print(f"✅ [PASS] {plugin_path.name}")
    return True

def main():
    if not PLUGINS_DIR.exists():
        print("Plugins directory not found.")
        sys.exit(1)
        
    failed = False
    
    # Iterate over immediate subdirectories of plugins/
    for item in PLUGINS_DIR.iterdir():
        if item.is_dir() and not item.name.startswith("."):
            if not validate_plugin(item):
                failed = True
                
    if failed:
        print("\n🚫 Verification failed for one or more plugins.")
        sys.exit(1)
    else:
        print("\n🎉 All plugins verified successfully!")
        sys.exit(0)

if __name__ == "__main__":
    main()
