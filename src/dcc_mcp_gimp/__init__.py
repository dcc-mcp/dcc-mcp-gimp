"""Public adapter API with a dependency-light bridge import path."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .server import GimpMcpServer

__all__ = ["GimpMcpServer"]


def __getattr__(name: str) -> Any:
    if name == "GimpMcpServer":
        from .server import GimpMcpServer

        return GimpMcpServer
    raise AttributeError(name)
