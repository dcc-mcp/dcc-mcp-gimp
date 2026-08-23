"""Pure Install SOP v1 contract shared by the GIMP lifecycle layers."""

from __future__ import annotations

import re
from pathlib import Path

_PLUGIN_NAME = "dcc_mcp_gimp"
SCHEMA_VERSION = "1"
MIN_CORE_VERSION = "0.19.38"
EXIT_OK, EXIT_PREFLIGHT, EXIT_ACQUIRE = 0, 10, 20
EXIT_INSTALL, EXIT_VERIFY, EXIT_REQUIRES_RESTART = 30, 40, 50
_VERBS = {"install", "status", "verify", "uninstall", "upgrade"}
RECEIPT_RELATIVE_PATH = Path(".dcc-mcp") / "receipts" / "gimp.json"


class InstallFailure(ValueError):
    """A lifecycle stage failed with a stable Install SOP exit code."""

    def __init__(self, exit_code: int, stage: str, reason: str):
        super().__init__(reason)
        self.exit_code, self.stage, self.reason = exit_code, stage, reason


def _version_tuple(value: str) -> tuple[int, ...]:
    match = re.match(r"^(\d+(?:\.\d+)*)", value)
    return tuple(int(part) for part in match.group(1).split(".")) if match else ()
