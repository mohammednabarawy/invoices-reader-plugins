# Invoices Reader Community Plugins

Official community plugin repository for **Invoices Reader**.

## Repository Structure

- `plugins/`: All community plugins (one folder per plugin ID).
- `templates/`: Starter templates for new plugins.
- `docs/`: Developer documentation for plugin APIs and best practices.
- `scripts/`: Validation and maintenance utilities.

## Included Plugins (Current)

- `whatsapp_quick_share`: Quick share action to open WhatsApp with a pre-filled invoice summary and copy invoice image to clipboard.
- `whatsapp_automation_agent`: Background WhatsApp agent for inbound/outbound invoice flow, including incoming file intake (images/documents) and receive acknowledgements.

## How To Use

### For Users

1. Clone this repository:
   ```bash
   git clone https://github.com/mohammednabarawy/invoices-reader-plugins.git
   ```
2. Copy the desired plugin folder from `plugins/` to your local Invoices Reader `plugins/` directory.
3. Restart Invoices Reader.

### For Developers

1. Fork this repository.
2. Create a new plugin folder in `plugins/` (for example `plugins/my-awesome-plugin`).
3. Add `manifest.json` (see `templates/basic_plugin/manifest.json`).
4. Implement your plugin.
5. Submit a pull request.

## Documentation

- [Plugin Developer Manual](docs/plugin_developer_manual.md)
- [Contribution Guidelines](CONTRIBUTING.md)

## License

Each plugin defines its own license. Check plugin-level `LICENSE` files where provided.
