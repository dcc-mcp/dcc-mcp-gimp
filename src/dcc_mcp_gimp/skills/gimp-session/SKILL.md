---
name: gimp-session
description: >-
  Inspect the connected GIMP 3 session through the DCC-MCP Python plug-in
  bridge. Use for session health, open images, and active image metadata.
license: MIT
compatibility: "GIMP 3.0+; dcc-mcp-core 0.19+"
allowed-tools: "python"
metadata:
  dcc-mcp:
    dcc: gimp
    layer: domain
    version: "0.1.0"
    search-hint: "GIMP image editor session document active image layers"
    tags: "gimp,image-editing,session"
    tools: tools.yaml
    depends: "dcc-diagnostics"
---

# GIMP Session

Install and run the bundled GIMP 3 plug-in before using this skill. Calls use a
loopback JSON-lines bridge and never execute arbitrary GIMP/Python source.
