# dcc-mcp-gimp

GIMP 3 adapter for the DCC Model Context Protocol ecosystem.

The adapter uses a small GIMP 3 Python plug-in and a loopback JSON-lines bridge.
The MCP server runs in the normal Python environment; GIMP API calls stay inside
the plug-in process. It does not expose arbitrary Python or Script-Fu execution.

## Install

```bash
pip install dcc-mcp-gimp
dcc-mcp-gimp-install
```

Restart GIMP, run **Filters → Development → DCC-MCP GIMP Bridge**, then start:

```bash
dcc-mcp-gimp
```

The MCP endpoint defaults to `http://127.0.0.1:8767/mcp`; the plug-in bridge uses
`127.0.0.1:3848`. Override the latter with `DCC_MCP_GIMP_BRIDGE_PORT` before
starting both processes.

## Current tools

- Check GIMP bridge status and version.
- List open images with dimensions.
- Inspect the active image.

The first release targets safe session discovery. Image mutation and export will
be added only through typed GIMP procedures, not arbitrary source evaluation.

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest
python -m ruff check src tests tools
python tools/lint_skills.py
python -m build
python -m twine check dist/*
```

GIMP 3 plug-in API reference: https://developer.gimp.org/api/3.0/
