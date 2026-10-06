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

<!-- dcc-mcp-coverage-pointer:start -->
<!-- Generated from dcc-mcp-catalog.yml by scripts/generate_adapter_pointer.py in dcc-mcp/dcc-mcp-core. Do not edit by hand. -->
## Part of the DCC-MCP host matrix

**dcc-mcp-gimp** — GIMP 3 adapter with typed image, layer, save, and production export
workflows.

It is one of **38 host adapters** in the DCC-MCP catalog. Every adapter speaks the same
MCP protocol and builds on the same core runtime contract; each one exposes the tools
its own host needs on top of that.

- [All host adapters and install metadata](https://dcc-mcp.github.io/ecosystem)
- [Host matrix on the core README](https://github.com/dcc-mcp/dcc-mcp-core#readme)
- [Showcase](https://dcc-mcp.github.io/showcase)

This block is generated from the catalog entry in
[`dcc-mcp-catalog.yml`](https://github.com/dcc-mcp/dcc-mcp-core/blob/main/dcc-mcp-catalog.yml).
Re-run the generator after changing the catalog.
<!-- dcc-mcp-coverage-pointer:end -->

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

Restart GIMP after installation. Its no-argument persistent
`python-fu-dcc-mcp-gimp-bridge` procedure starts automatically. Follow the
machine-executable `next_steps` to start the adapter with the selected Python
and verify the exact host instance. The MCP endpoint defaults to
`http://127.0.0.1:8767/mcp`; the plug-in bridge defaults to `127.0.0.1:3848`.

## Typed capabilities

- Inspect bridge readiness, open images, active image metadata, and recursive
  layer trees.
- Create or open bounded images under configured roots.
- Create/select/fill/rename/show/hide/lock/fade/delete layers through typed
  parameters.
- Preserve layered work as XCF and export PNG, JPEG, WebP, or TIFF with byte
  counts and SHA-256 digests.
- Export bounded PNG previews from a native duplicate with `export_preview`;
  `max_width` and `max_height` are 1–2048, preserve aspect ratio and never upscale.
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
bounded layered image through the retained base image/layer workflow, saves XCF, exports PNG,
verifies both artifacts, reopens the XCF, and cleans up only bridge-owned
displays.

Official references: [GIMP 3 Python plug-ins](https://developer.gimp.org/resource/writing-a-plug-in/tutorial-python/),
[GIMP Image API](https://developer.gimp.org/api/3.0/libgimp/class.Image.html), and
[GIMP file save/export API](https://developer.gimp.org/api/3.0/libgimp/func.file_save.html).

See [the typed layered workflow](docs/native-poster-workflow.md) for the eight
additional bounded tools and their native readback/color contracts. The retained
base smoke does not cover all eight additions. The separate v2 current-main
native acceptance gate covered these additions on Linux GIMP 3.0.4 with the pinned
0.20.41 runtime; historical release-based evidence remains separate.

Shape painting restores selection masks with feather and antialias disabled,
preserves a recovery channel when restoration fails, and restores the user
context on cleanup. See the separate installed-wheel and fixed-fixture native
gate records for the exact code/version acceptance boundary.

The context-push failure path also removes the unused saved selection channel.
If that cleanup fails, both errors remain explicit. This failure-only revision
reuses the labeled v2 successful native evidence after a structural equivalence
check; it does not claim a new v3 native run.

Publication review also preserves combined selection/context cleanup errors,
checks the actual native text-layer pixel limit before insertion, and rejects
masked layer exports before allocation. These additions have inert regression
coverage; they do not claim a fresh native run of the final publication module.

## Native PNG previews

`export_preview(image_id, path, max_width, max_height, overwrite=false)` fits the
image within an integer bounding box, with each side at most 2048 pixels. The
limiting side is exact; the other side is floored to the nearest integer, with a
minimum of one pixel. For example, 2560 × 1600 becomes 1600 × 1000 in a 1600 × 1600
box. Smaller inputs keep their dimensions. PNG is the only output format.

The source must be RGB/RGBA in GIMP's `U8_NON_LINEAR` precision, at most 8192
pixels per side and 16,777,216 pixels total. At most 256 layer nodes and
134,217,728 aggregate layer pixels are admitted; groups count and masks add their
layer's area. Saved channels, paths, floating selections, and nonempty drawable filters are
unsupported.
These are admission limits, not a memory quota or an HDR/indexed conversion.

The host duplicates the native image, scales that copy with GIMP's NoHalo
interpolation, and exports through GIMP. It hashes every admitted native layer (including hidden layers), mask and
selection in bounded strips, and checks hierarchy, editable text attributes,
metadata, selected items, dirty state, and image-list order before publishing.
Mask flags, blend/composite spaces and modes, image component visibility/activity,
and effective ICC profile bytes are included in the read-only state fingerprint.
GIMP exposes no direct image projection buffer in this API; complete composited
RGBA equality is verified separately in the native fixture.
Native items with more than 256 parasites are rejected before duplication. The temporary image is deleted and the pushed global
context is restored before publication. A native image/context cleanup or source-state failure
prevents publication. Symlink/reparse paths are rejected. An unpredictable sibling
staging directory protects an existing destination on native export failure;
atomic no-replace publication rejects a late collision when overwrite is false.
The result includes output dimensions, byte count, SHA-256, and the measured source-state
fingerprint. A later filesystem staging-cleanup error explicitly reports when
the output was already published; it is not a rollback or retry-safe failure.
Path protections assume a trusted isolated host and caller-controlled allowed
roots; they do not claim resistance to hostile same-user filesystem mutation or
a cross-user Windows ACL. Windows native behavior is not qualified by the Linux
fixture. Raw native exception text is not returned. As with any after-start
bridge timeout, inspect the destination and host before retrying.

The new preview contract does not change the older `export_image` or
`export_layer` export behavior. In particular, preview source-state verification
and staging are specific to `export_preview`.
