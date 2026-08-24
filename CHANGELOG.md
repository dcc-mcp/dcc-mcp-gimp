# Changelog

## [0.4.1](https://github.com/dcc-mcp/dcc-mcp-gimp/compare/v0.4.0...v0.4.1) (2026-08-24)


### Bug Fixes

* preserve GIMP lifecycle recovery ([efe1561](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/efe156130ffec3176f295f3412c6ffe2617a8d39))

## [0.4.0](https://github.com/dcc-mcp/dcc-mcp-gimp/compare/v0.3.0...v0.4.0) (2026-08-24)


### Features

* adopt GIMP Install SOP v1 ([7b368ec](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/7b368ec1b57c92f7a60eaa6e589f6d87242f87b6))


### Bug Fixes

* harden GIMP install lifecycle ([45b5798](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/45b5798026e421df4a28eff219eb4809cc94fb76))

## [0.3.0](https://github.com/dcc-mcp/dcc-mcp-gimp/compare/v0.2.0...v0.3.0) (2026-08-12)


### Features

* ship production-ready GIMP authoring ([#2](https://github.com/dcc-mcp/dcc-mcp-gimp/issues/2)) ([8310688](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/83106889832dc685190231b2b34bc03651a74a83))

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
