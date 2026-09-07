"""USITC HTS schedule and CBP CROSS rulings.

Backed by pgvector over the CROSS corpus. Every classification answer cites rulings.

Exposed over streamable-HTTP on port 8102 so the pipeline, n8n, and a human analyst in
Claude Code all reach the same tools. See docs/ARCHITECTURE.md section 4.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("mcp-hts", host="0.0.0.0", port=8102)


@mcp.tool()
def ping() -> str:
    """Liveness probe. Week 1 placeholder - real tools land in the milestone below."""
    return "mcp-hts ok"


def main() -> None:
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
