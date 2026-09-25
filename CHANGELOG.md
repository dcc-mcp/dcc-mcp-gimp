# Changelog

## [0.4.2](https://github.com/dcc-mcp/dcc-mcp-gimp/compare/v0.4.1...v0.4.2) (2026-09-25)


### Bug Fixes

* allow pinned interpreter symlinks ([7a1c1fc](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/7a1c1fca4703a1db137c8ebe30ddc011ba30305c))
* avoid unsupported macOS fd staging paths ([d8d402b](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/d8d402b9d6aa461f04ec3e027591f94ab4e95bf7))
* bind bootstrap rotation temporary identity ([e57b751](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/e57b751aa114d35974db024f7266a5f24737fa96))
* bind GIMP lifecycle mutations to physical identities ([13edfe2](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/13edfe2ae8da0544c14acdd739335f90d8d9f406))
* bind install lifecycle side effects to identities ([fdfab3c](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/fdfab3cfbbaab28f6cfae3a04fdddeba5d5806e6))
* bind install ownership across mutations ([b5d4425](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/b5d4425ebebd02e8b7d630d6f6f861a87aa7121f))
* bind lifecycle cleanup and staging identities ([ed5344c](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/ed5344c2824bc278a7f017aa450f0b77c1064463))
* bind lifecycle side effects to physical identities ([cc8ab36](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/cc8ab36397007110bb0eccc3698495c99fca7260))
* bind POSIX install lifecycle races ([7d7baf2](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/7d7baf279aa1750468d18d013a65a9aa4408533d))
* bind receipt and bootstrap writes to objects ([eeed6b2](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/eeed6b20cd4371d8cd05f9745b092d55bf7fb346))
* bind recovery cleanup to physical identities ([cd2ff51](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/cd2ff510b3137c74461565be204715539da24542))
* close GIMP install path race boundaries ([c736016](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/c736016862dad8e14eb137870f53c96e58d6e259))
* close install identity gaps ([158b737](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/158b737ff7a62cf71ebdd5f81f804f815a43b455))
* close lifecycle and bootstrap identity races ([10a4eaa](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/10a4eaa0cdf15efb3e7d2369368d9a0b4ac2f750))
* close lifecycle identity and bootstrap races ([0024395](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/0024395233459e21d4bd951934a812b1424b527b))
* close posix lifecycle identity races ([839c607](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/839c6072cf0bd9d3b72c23a21f3b117f8306c636))
* close receipt and bootstrap identity races ([e77a5b8](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/e77a5b89760a8bb25eb843254dafe83f82d84a7b))
* close remaining lifecycle race seams ([37a1698](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/37a16989aa728aa6b5b38fa53fda1b85f8d1b929))
* copy macOS recovery trees through directory handles ([460ae86](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/460ae862832d2004c251319450073b67e3b05009))
* fail closed on profile access errors ([60fd871](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/60fd871a819b83cc4ae74bb1b3a60e2e171bfcb6))
* harden GIMP install receipt contract ([2270537](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/22705372e5804be79202c263dd584e02490d52a1))
* harden install transaction publication ([78bb6e7](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/78bb6e7ac0e6af0bf6187622e433eeaac7ae68bf))
* harden recovery and bootstrap race handling ([d989cdf](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/d989cdfca38c39d6ee012f94d3366b8a3eea63c1))
* preserve atomic receipt and bootstrap writes ([c9ac6fe](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/c9ac6fe6be299d7746898e7b1a96e8cc1300b8c9))
* preserve install verdicts and profile path normalisation ([49b2ebb](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/49b2ebb8bf698354a63723be383c12efb23c0ef9))
* preserve install verdicts through failed rollbacks ([b97e29d](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/b97e29d668630847e034113f9d053976a22dd451))
* preserve lifecycle compatibility across platforms ([aac23d1](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/aac23d1faf9c1d061cdd340068b8d95eaeccb7a8))
* preserve macOS receipt lifecycle compatibility ([d6e45b3](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/d6e45b3aacf5dca8bd7386e6df2469fc1edf264f))
* preserve posix recovery transaction semantics ([02f70b9](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/02f70b9a2bc8e4f86b8151403789e77ce8c406a5))
* preserve structured receipt race failures ([32f0d51](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/32f0d51e470846a66df3a6696170925d71784259))
* resolve physical recovery staging parent ([638db68](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/638db683e54f5cbab4a40b9d9238d66e3ecf5668))
* validate POSIX executable descriptor aliases ([9fc543d](https://github.com/dcc-mcp/dcc-mcp-gimp/commit/9fc543d97727228bb1837f9f42127956993df090))

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
