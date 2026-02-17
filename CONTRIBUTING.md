# Contributing to Invoices Reader Plugins

Thank you for your interest in contributing! This guide will help you submit your plugin to the community repository.

## ✅ Prerequisites

1.  **Valid Manifest**: Every plugin must have a `manifest.json` file in its root.
2.  **Unique ID**: The plugin ID in `manifest.json` must be unique and match the folder name (e.g., `plugins/my-plugin/manifest.json` -> `"id": "my-plugin"`).
3.  **Documentation**: Includes a `README.md` explaining what your plugin does.

## 🛠️ Step-by-Step Guide

### 1. Fork and Clone
Fork this repository to your GitHub account, then clone it locally:
```bash
git clone https://github.com/mohammednabarawy/invoices-reader-plugins.git
cd invoices-reader-plugins
```

### 2. Create Your Plugin
You can start from a template:
```bash
cp -r templates/basic_plugin plugins/your-plugin-id
```
Renaming the folder to your unique plugin ID is required.

### 3. Configure `manifest.json`
Edit `plugins/your-plugin-id/manifest.json`:
```json
{
  "id": "your-plugin-id",
  "name": "Your Plugin Name",
  "version": "1.0.0",
  "description": "Short description",
  "author": "Your Name",
  "min_app_version": "1.0.0"
}
```

### 4. Validate Locally
Run the validation script to ensure your plugin meets all requirements:
```bash
python scripts/validate_manifest.py
```
Fix any errors reported by the script.

### 5. Submit a Pull Request
1.  Push your changes to your fork.
2.  Open a Pull Request (PR) against the `main` branch of this repository.
3.  Our CI system will automatically check your plugin.

## ⚠️ Rules
- **No Malicious Code**: Plugins must not execute harmful operations.
- **Sandboxing**: Respect the application's sandbox API.
- **Size**: Keep plugins lightweight. Avoid large binary assets if possible.
