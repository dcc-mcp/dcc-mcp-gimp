# Changelog

## 0.3.0

- Add 16 typed image, layer, XCF save, export, and bridge-owned cleanup tools.
- Authenticate loopback requests and bound connections, payloads, queue depth,
  traversal, paths, file sizes, pixels, and command duration.
- Marshal every GIMP host call through the GLib main thread and reject arbitrary
  Python, Script-Fu, action, and PDB-procedure execution.
- Add transactional plug-in installation, doctor diagnostics, Python 3.9
  compatibility coverage, artifact digests, and a full real-host smoke chain.

## [0.2.0](https://github.com/dcc-mcp/dcc-mcp-gimp/compare/v0.1.0...v0.2.0) (2026-07-24)


### Features

* add GIMP MCP adapter ([4fcda51](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/4fcda51b29d551e59175a81fa4fafcea5d2e8252))


### Bug Fixes

* keep persistent GIMP bridge process alive ([441aa0c](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/441aa0c8d7a8c2bf3178457e931ec2835115061c))
* match GIMP plugin folder to module name ([63799cb](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/63799cbf1c60e53c7c56367b01b54d6290af76fb))
* use GIMP persistent procedure callback signature ([9489a4f](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/9489a4f452b5d0bd947a3108b761ba26b0256aff))
* verify GIMP AppImage checksum ([573314b](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/573314bc3bf1daf6e1e10ea722d03f66927a2eaa))

## 0.1.0

- Initial GIMP 3 session bridge and MCP adapter.
