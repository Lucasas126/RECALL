"""A minimal stdio MCP server used by the MCP tests; it echoes its per-user environment."""

from __future__ import annotations

import os
import sys
import time

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

server = MCPServer("fake")


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def whoami() -> str:
    """Return the token path this process was started with."""
    return os.environ.get("TOKEN_PATH", "")


@server.tool()
def add_event(title: str) -> str:
    """Create an event with the given title."""
    return f"created {title}"


@server.tool()
def fail() -> str:
    """Always fail."""
    raise ValueError("boom")


@server.tool()
def hidden() -> str:
    """A tool the catalog hides."""
    return "hidden"


# Tools that exist only while a marker file is in the working directory (the user's state folder), so a
# test can make the server gain tools between two connections.
if os.path.exists("late.flag"):
    @server.tool()
    def late() -> str:
        """A tool added after the first connection."""
        return "late"


if os.path.exists("slow.flag"):
    @server.tool()
    def slow(seconds: float) -> str:
        """Wait, then answer."""
        time.sleep(seconds)
        return "done"


def auth() -> None:
    """Imitate a sign-in command: print a URL, then store the credential file."""
    print("Authentication URL: https://example.test/consent?state=1", flush=True)
    with open(os.environ["TOKEN_PATH"], "w", encoding="utf-8") as fh:
        fh.write("{}")


if __name__ == "__main__":
    if sys.argv[1:] == ["auth"]:
        auth()
    else:
        server.run("stdio")
