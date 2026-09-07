"""Claim assembly, validation, state transitions.

The only writer of claim state. n8n calls this; it never writes Postgres directly.

Exposed over streamable-HTTP on port 8104 so the pipeline, n8n, and a human analyst in
Claude Code all reach the same tools. See docs/ARCHITECTURE.md section 4.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

server = MCPServer("mcp-claims")


@server.tool()
def ping() -> str:
    """Liveness probe. Week 1 placeholder - real tools land in the milestone below."""
    return "mcp-claims ok"


def main() -> None:
    # MCP SDK 2.x takes the bind address on run(), not on the constructor.
    server.run(transport="streamable-http", host="0.0.0.0", port=8104)


if __name__ == "__main__":
    main()
