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
    version: "0.4.1"  # x-release-please-version
    search-hint: "GIMP image editor image layer XCF PNG export authoring"
    tags: "gimp,image-editing,layers,export"
    tools: tools.yaml
    depends: "dcc-diagnostics"
---

# GIMP Image Authoring

Use the agent-first lifecycle before loading this Skill. Run
`dcc-mcp-gimp install --dry-run --json` to inspect the selected GIMP executable,
target interpreter, profile, and receipt; apply it with `install --yes`, then
follow the machine-readable `next_steps[]` and require
`verify.directly_usable: true`. Use `status`, `upgrade`, and `uninstall` for the
same receipted profile. Configure `DCC_MCP_GIMP_ALLOWED_ROOTS`, and treat
exit 50 as a fail-closed restart/lock boundary rather than proof of readiness.
Use the exact `next_steps[]` command, including any explicit `--instance-id` or
`--host-pid` selectors. A legacy receipt without `entry_point_executable` is
not migrated: status reports `repair`, upgrade and uninstall refuse mutation.
All host API calls are typed and marshalled onto GIMP's GLib main thread. The
bridge
accepts only authenticated loopback JSON-lines requests and never executes
arbitrary Python, Script-Fu, PDB procedure names, or actions supplied by a
caller.

Use instance-scoped `image_id` and `layer_id` values only within the current
GIMP process. Save layered work as XCF before exporting a delivery format.
Flattening, overwriting, deleting, and closing require the explicit typed
contracts in `tools.yaml`; `close_image` refuses displays not opened by this
bridge.


The additional typed tools support group creation, imported image layers,
placement, bounded shape painting, blend modes, editable text, bounded font
discovery and isolated native PNG layer export. Tool colors are encoded sRGB
integers with straight alpha. Inspect native hierarchy, offsets, text and color
readback after mutation; save/reopen XCF and inspect actual exported PNG pixels.
Queued requests retain the bridge's existing before-start timeout cancellation.
An after-start timeout remains an unknown host outcome and must be inspected
before retrying a mutation.


For web and asset-library sizes, use `export_preview` with a PNG path and explicit
`max_width`/`max_height` (1–2048). It duplicates the native image, uses NoHalo to
fit inside the box, floors the non-limiting dimension to an integer, and never
upscales. It supports RGB/RGBA U8_NON_LINEAR sources within its image/layer
admission bounds; saved channels, paths and floating selections are rejected.
The source state and context are checked and the temporary image is deleted
before atomic publication. The default refuses overwrites, including a target
created while GIMP is exporting. Inspect dimensions and PNG alpha/pixels after
export. Do not substitute a generic Python or PDB execution request.
