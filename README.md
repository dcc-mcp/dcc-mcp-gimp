# dcc-mcp-gimp

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/dcc-mcp-gimp-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="docs/assets/dcc-mcp-gimp.svg">
    <img src="docs/assets/dcc-mcp-gimp.svg" alt="DCC-MCP · GIMP" width="600">
  </picture>
</p>

Production-oriented GIMP 3 adapter for the DCC Model Context Protocol ecosystem.

![GIMP typed image-authoring workflow](docs/images/gimp-showcase.webp)

_Illustrative workflow generated with OpenAI ImageGen from the retained source in `docs/images/sources`; it is not a GIMP screenshot or host-validation artifact._

The adapter keeps GIMP API ownership inside a persistent GIMP 3 Python plug-in.
An authenticated loopback JSON-lines bridge accepts a fixed catalog of typed
commands, bounds connections, requests, responses, queue depth, image size,
layer traversal, paths, file size, and execution time, then marshals every host
operation onto GIMP's GLib main thread. It exposes no arbitrary Python,
Script-Fu, action, or PDB-procedure execution.

## Install

See the canonical [Install SOP v1 guide](install.md) for agent-first plan,
status, verification, upgrade, uninstall, receipts, exit codes, and platform
troubleshooting.

```bash
pip install dcc-mcp-gimp
dcc-mcp-gimp install --yes --json --dcc-path /path/to/gimp-3.0 --python /path/to/python
dcc-mcp-gimp verify --json --dcc-path /path/to/gimp-3.0 --python /path/to/python
dcc-mcp-gimp-doctor
```

Set at least one allowed file root before launching GIMP and the MCP server:

```bash
export DCC_MCP_GIMP_ALLOWED_ROOTS=/absolute/project/root
dcc-mcp-gimp
```

On Windows, separate multiple roots with `;`; on POSIX, use `:`. The plug-in and
client share a per-user token at `~/.dcc-mcp/gimp-bridge-token` by default. An
explicit token may instead be supplied through `DCC_MCP_GIMP_BRIDGE_TOKEN` and
must contain at least 32 characters. Token values are never returned by status
or diagnostics.

Restart GIMP after installation, then invoke the registered persistent
`python-fu-dcc-mcp-gimp-bridge` procedure. The MCP endpoint defaults to
`http://127.0.0.1:8767/mcp`; the plug-in bridge defaults to
`127.0.0.1:3848`.

## Typed capabilities

- Inspect bridge readiness, open images, active image metadata, and recursive
  layer trees.
- Create or open bounded images under configured roots.
- Create/select/fill/rename/show/hide/lock/fade/delete layers through typed
  parameters.
- Preserve layered work as XCF and export PNG, JPEG, WebP, or TIFF with byte
  counts and SHA-256 digests.
- Flatten only with `confirm=true`; overwrite only with `overwrite=true`.
- Close only bridge-opened displays, and require `discard_changes=true` for
  dirty images.

GIMP image and layer IDs are process-local and must be rediscovered after a
restart. File paths outside configured roots are rejected; paths attached to
untrusted user images are redacted to a basename.

## Architecture and validation

The GIMP host remains the sole owner of image state and main-thread affinity.
The Python package owns MCP lifecycle, typed Skill declarations, installation,
diagnostics, and the authenticated bridge client. No generic code evaluation
crosses this boundary.

```bash
python -m pip install -e ".[dev]"
python -m pytest
python -m ruff check src tests tools
python tools/lint_skills.py
python -m build
python -m twine check dist/*
```

The real-host acceptance script is `tests/live_gimp_smoke.py`. It creates a
bounded layered image, exercises all typed commands, saves XCF, exports PNG,
verifies both artifacts, reopens the XCF, and cleans up only bridge-owned
displays.

Official references: [GIMP 3 Python plug-ins](https://developer.gimp.org/resource/writing-a-plug-in/tutorial-python/),
[GIMP Image API](https://developer.gimp.org/api/3.0/libgimp/class.Image.html), and
[GIMP file save/export API](https://developer.gimp.org/api/3.0/libgimp/func.file_save.html).
