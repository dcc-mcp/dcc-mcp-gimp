"""Shared declarative Skill entry points for typed GIMP commands."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Callable, Optional

from dcc_mcp_core.skill import skill_entry, skill_success

from .bridge import get_bridge


def bridge_main(
    method: str,
    message: str,
    result_key: Optional[str] = None,
) -> Callable[..., dict[str, Any]]:
    @skill_entry
    def main(**kwargs: Any) -> dict[str, Any]:
        result = get_bridge().call(method, **kwargs)
        if result_key is not None:
            payload = {result_key: result}
        elif isinstance(result, Mapping):
            payload = dict(result)
        else:
            payload = {"result": result}
        return skill_success(message, **payload)

    return main
