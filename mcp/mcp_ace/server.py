"""ACE / entry-summary data access.

Client-exported ACE reports only. Portal scraping violates CBP terms - never add it.

Exposed over streamable-HTTP on port 8101 so the pipeline, n8n, and a human analyst in
Claude Code all reach the same tools. See docs/ARCHITECTURE.md section 4.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("mcp-ace", host="0.0.0.0", port=8101)


@mcp.tool()
def ping() -> str:
    """Liveness probe. Week 1 placeholder - real tools land in the milestone below."""
    return "mcp-ace ok"


def main() -> None:
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
