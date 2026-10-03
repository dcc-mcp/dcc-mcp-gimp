# Typed native layered workflow

This current-main integration adds eight bounded tools to the 16-tool image workflow:
`create_group`, `import_layer`, `place_layer`, `set_layer_mode`, `paint_shape`,
`create_text`, `list_fonts`, and `export_layer`. All authoring still runs inside
the persistent GIMP plug-in, marshalled to its GLib main thread. There is no
caller-provided code, action name, or PDB procedure.

## Native compatibility

The following failures and native API behavior were observed in the historical
release-based candidate. This integration retains the established fixes. The v2
current-main integration passed a bounded native gate on Linux GIMP 3.0.4; the v3
context-initialization cleanup revision described below reuses that evidence and
does not claim a fresh native run.

- Register `Gimp.Procedure.new` with one plug-in object as run data, and receive
  `(procedure, config, plugin)` in the callback. The previous extra value made
  the plug-in argument `None` on the qualified GIMP 3.0.4 host.
- Treat integer RGB inputs as encoded sRGB. GEGL `set_rgba` takes linear RGB;
  interpreting an encoded value of 128 as linear produced an exported value of
  188. The fixed hex parser produces 128.
- Foreground fills discard foreground alpha on the qualified host. Direct native
  GEGL drawable-buffer fills preserve straight alpha, while shape painting uses
  the native context opacity. Both paths exported requested RGBA
  `(80, 120, 200, 128)` exactly.
- Set native text color after image insertion. Before insertion, GIMP reset it
  to black. Native readback and reopened XCF now retain the intended color.

## Author, save, and read back

1. Discover the tools and native font resource names.
2. Create bounded RGB/RGBA layers and explicit groups.
3. Paint fixed polygon/rectangle/ellipse selections or import an allowed image.
4. Place layers, set a fixed blend mode, and create editable native text.
5. Save XCF, export PNG, reopen the XCF and compare native hierarchy, offsets,
   layer modes, visibility, opacity, text/font/color, precision and profile.
6. Inspect metadata before sharing. Dirty bridge-owned fixtures require explicit
   discard permission when they are closed after saved-artifact checks.

## App-to-app layer handoff

`export_layer` accepts image ID, layer ID, PNG path and explicit overwrite opt-in.
It accepts unmasked non-group drawables; masked layers are rejected before file
access or temporary-image allocation because the native drawable-copy API does
not copy layer masks. Apply or remove a mask explicitly in GIMP before this handoff.
It copies the selected drawable into a temporary native image with matching
canvas, precision and profile, retains the offsets, exports PNG and deletes only
that temporary image. It returns the PNG digest and actual source-layer readback.
The source image is checked independently before and after the export batch.

For downstream 3D geometry, use alpha contours from these actual PNG exports.
Keep creative design data separate from native result records. Labels and baked
cast shadows should not be silently included as geometry.

## Qualification boundaries

The v2 current-main integration passed a separate native MCP gate on Linux
GIMP 3.0.4 with Core, server and CLI 0.20.41. The gate recorded initialization,
discovery, all eight added tools, terminal jobs, exact gray/RGBA pixels, native
text/group/mode/offset readback, XCF reopen and unchanged source pixels after
isolated layer export. Resolved gateway port 0 and failover false were asserted.
The first gate attempt stopped at a discovery-name assertion before authoring;
its failed evidence is preserved. The successful gate calls the exact discovered
short tool aliases. This evidence is separate from the historical Paper River
poster acceptance. This does not establish native acceptance on macOS, Windows,
or other GIMP releases. The base `tests/live_gimp_smoke.py` exercises the original image
workflow; the separate current-main gate trace covers all added operations.

The canonical installer's resolution of a symlinked virtual-environment Python
was observed to lose that environment. The v2 native qualification
used a task-owned plug-in profile and an explicit adapter interpreter. It does
not claim a successful receipted install/verify lifecycle. Fix and independently
test that interpreter-selection issue before calling the canonical lifecycle
qualified for symlink-based virtual environments.

## Selection and context restoration

Shape painting saves the original selection before changing context. A failed
or missing save aborts before changing the selection or pixels. Restoration
disables feather and antialias explicitly; the native restore result must be
true before the saved channel is removed. A failed restore reports failure and
retains that channel for recovery. Context cleanup runs even when painting,
restoration or channel removal fails.

The nonempty-selection regression uses a separately labeled, fixed test-profile
MCP fixture overlay. It checks exact native mask bytes and the relevant user
context before/after feathered painting, linked to the installed production
paint_shape function. That overlay is not part of the production wheel or tool
catalog. Save-exception, missing-save and restore-failure cases are inert tests;
they are not described as induced failures in a user's native document.

If the context push itself fails after a successful mask save, the unused saved
channel is removed before reporting the original failure. A failed cleanup is
reported explicitly with the original context error retained as its cause. The
new failure branches have focused inert coverage. For the handed-off v3 module,
the successful native path is structurally unchanged from the accepted v2
installed-wheel and selection-mask gates. The accepted v2 production-module SHA-256 is
`d594ec6993d48abb626d871bb9316552325b2197471e783bea88c4b7576768a8`;
the handed-off v3 module is
`c39d68f4861f7bb96bc969a7105da55235e57d3ed4cad78791bf58fd3fbaf262`.
These hashes identify the historical modules before publication review fixes.

Publication review added five safeguards: dual selection/context cleanup errors
retain the recovery-channel information and original painting error; native text
dimensions exceeding the existing 100-million-pixel limit are rejected before
image insertion, after native text construction; and masked layer export is
rejected before allocation. Failed group insertion releases the newly allocated
detached group. Failed text initialization releases the new detached text item,
or removes that new layer if insertion succeeded before offsets or color failed.
Cleanup failures report both the setup and cleanup errors. These commands only
clean up their own newly allocated item; they do not promise to restore selection
or undo state. This is not a preconstruction text-memory limit. Twenty-seven
new inert regression cases exercise these guards and successful call ordering.
The final publication module SHA-256 is
`0886cf8a5cef5ccee583c6e8438ea80d2dcc779430139c49ee55721c4660ef44`.
This module
differs from the v3 source module above and has no fresh native qualification.
The historical native proof does not establish these new failure branches.
