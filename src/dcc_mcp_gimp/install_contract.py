"""Pure Install SOP v1 contract shared by the GIMP lifecycle layers."""

from __future__ import annotations

import re
from pathlib import Path

_PLUGIN_NAME = "dcc_mcp_gimp"
SCHEMA_VERSION = 1
MIN_CORE_VERSION = "0.19.91"
EXIT_OK, EXIT_PREFLIGHT, EXIT_ACQUIRE = 0, 10, 20
EXIT_INSTALL, EXIT_VERIFY, EXIT_REQUIRES_RESTART = 30, 40, 50
_VERBS = {"install", "status", "verify", "uninstall", "upgrade"}
RECEIPT_RELATIVE_PATH = Path(".dcc-mcp") / "receipts" / "gimp.json"


class InstallFailure(ValueError):
    """A lifecycle stage failed with a stable Install SOP exit code."""

    def __init__(self, exit_code: int, stage: str, reason: str):
        super().__init__(reason)
        self.exit_code, self.stage, self.reason = exit_code, stage, reason


_VERSION_COMPONENT = r"(?:0|[1-9][0-9]{0,5})"
_VERSION_RE = re.compile(rf"^{_VERSION_COMPONENT}\.{_VERSION_COMPONENT}\.{_VERSION_COMPONENT}$")
_MAX_VERSION_LENGTH = 32


def _version_tuple(value: object) -> tuple[int, ...]:
    """Parse one bounded canonical final release before integer conversion."""
    if not isinstance(value, str) or not 0 < len(value) <= _MAX_VERSION_LENGTH:
        return ()
    if _VERSION_RE.fullmatch(value) is None:
        return ()
    return tuple(int(part) for part in value.split("."))
