---
name: gimp-session
description: >-
  Inspect and author bounded GIMP 3 images through the authenticated DCC-MCP
  persistent plug-in bridge. Use for image/layer lifecycle, solid-color layers,
  XCF saves, deterministic exports, validation, and safe bridge-owned cleanup.
license: MIT
compatibility: "GIMP 3.0+; dcc-mcp-core 0.19.91+"
allowed-tools: "python"
metadata:
  dcc-mcp:
    dcc: gimp
    layer: domain
    version: "0.4.0"  # x-release-please-version
    search-hint: "GIMP image editor image layer XCF PNG export authoring"
    tags: "gimp,image-editing,layers,export"
    tools: tools.yaml
    depends: "dcc-diagnostics"
---

# GIMP Image Authoring

Install the bundled plug-in, configure `DCC_MCP_GIMP_ALLOWED_ROOTS`, restart
GIMP, and run the persistent bridge before loading this Skill. All host API
calls are typed and marshalled onto GIMP's GLib main thread. The bridge accepts
only authenticated loopback JSON-lines requests and never executes arbitrary
Python, Script-Fu, PDB procedure names, or actions supplied by a caller.

Use instance-scoped `image_id` and `layer_id` values only within the current
GIMP process. Save layered work as XCF before exporting a delivery format.
Flattening, overwriting, deleting, and closing require the explicit typed
contracts in `tools.yaml`; `close_image` refuses displays not opened by this
bridge.
