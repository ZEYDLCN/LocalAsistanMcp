"""Hub testleri için bilerek bozuk davranan küçük MCP sunucusu."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

delay = float(os.environ.get("FLAKY_START_DELAY", "0"))
if delay:
    time.sleep(delay)
if os.environ.get("FLAKY_EXIT_AT_START"):
    print("flaky: refusing to start", file=sys.stderr)
    raise SystemExit(5)

srv = MCPServer("flaky", instructions="A deliberately unreliable server for tests.")
RO = ToolAnnotations(read_only_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False)


@srv.tool(annotations=RO)
def echo(text: str) -> str:
    """Echo text back."""
    return text


@srv.tool(annotations=RO)
def slow(seconds: float) -> str:
    """Sleep, then answer."""
    time.sleep(seconds)
    return "done"


@srv.tool(annotations=RO)
def big(n: int) -> str:
    """Return n characters."""
    return "x" * n


@srv.tool(annotations=WRITE)
def write_then_die(path: str) -> str:
    """Record one line then crash the process (side effect + crash)."""
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("called\n")
    os._exit(3)


@srv.tool(annotations=RO)
def read_die_once(marker: str) -> str:
    """Crash the first time, succeed afterwards (marker file remembers)."""
    flag = Path(marker)
    if not flag.exists():
        flag.write_text("x")
        os._exit(3)
    return "recovered"


@srv.tool(annotations=RO)
def read_die_always() -> str:
    """Always crash."""
    os._exit(4)


@srv.tool()
def unannotated() -> str:
    """No annotations at all."""
    return "ok"


@srv.tool(annotations=RO)
def image() -> list:
    """Return an image block."""
    from mcp.server.mcpserver import Image

    return [Image(data=b"\x89PNG\r\n", format="png"), "caption"]


if __name__ == "__main__":
    srv.run()
