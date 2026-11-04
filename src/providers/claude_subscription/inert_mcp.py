# Adapted from the MIT-licensed Hermes plugin "claude-subscription-directsdk-experimental"
# (Copyright (c) 2026 Nous Research and contributors). See docs/user-guide/claude-subscription.md.
"""MCP server that only lists RECALL's tool schemas; RECALL, not Claude Code, executes every tool."""
import json
import sys
from pathlib import Path


def main() -> None:
    manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    for line in sys.stdin:
        row = json.loads(line)
        method = row.get("method")
        result: dict = {}
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                "serverInfo": {"name": "recall-inert-inventory", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": manifest}
        elif method == "tools/call":
            result = {"isError": True, "content": [{"type": "text", "text": "Denied: RECALL executes its own tools."}]}
        if "id" in row:
            print(json.dumps({"jsonrpc": "2.0", "id": row["id"], "result": result}), flush=True)


if __name__ == "__main__":
    main()
