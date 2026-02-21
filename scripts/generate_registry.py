import os
import json
import sys
from pathlib import Path

PLUGINS_DIR = Path(__file__).parent.parent / "plugins"
REGISTRY_FILE = Path(__file__).parent.parent / "registry.json"

def generate_registry():
    registry = []
    
    if not PLUGINS_DIR.exists():
        print(f"Plugins directory not found: {PLUGINS_DIR}")
        return

    print(f"Scanning plugins in {PLUGINS_DIR}...")
    
    for item in PLUGINS_DIR.iterdir():
        if item.is_dir() and not item.name.startswith("."):
            manifest_path = item / "manifest.json"
            if manifest_path.exists():
                try:
                    with open(manifest_path, "r", encoding="utf-8") as f:
                        manifest = json.load(f)
                        # Ensure ID matches folder name for consistency
                        manifest['id'] = item.name
                        # Add relative path for download reference
                        manifest['path'] = f"plugins/{item.name}"
                        
                        # Collect all files for targeted download
                        files = []
                        for root, dirs, filenames in os.walk(item):
                            # Exclude hidden and cache directories
                            dirs[:] = [d for d in dirs if not d.startswith('.') and d != '__pycache__' and 'session' not in d.lower()]
                            for f in filenames:
                                if f.startswith('.') or f.endswith('.pyc'):
                                    continue
                                rel_path = os.path.relpath(os.path.join(root, f), item)
                                files.append(rel_path.replace("\\", "/"))
                        
                        manifest['files'] = files
                        registry.append(manifest)
                        print(f"  + Added: {manifest.get('name', item.name)} ({len(files)} files)")
                except Exception as e:
                    print(f"  - Error reading {item.name}: {e}")
    
    with open(REGISTRY_FILE, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=4)
        print(f"\nRegistry generated at {REGISTRY_FILE} with {len(registry)} plugins.")

if __name__ == "__main__":
    generate_registry()
