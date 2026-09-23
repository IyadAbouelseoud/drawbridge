"""ACE / entry-summary data access.

Client-exported ACE reports only. Portal scraping violates CBP terms - never add it.

Exposed over streamable-HTTP on port 8101 so the pipeline, n8n, and a human analyst in
Claude Code all reach the same tools. See docs/ARCHITECTURE.md section 4.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from mcp_servers.security import READ_ONLY, guarded, run, server_kwargs
from services.api.src.config import get_settings
from services.api.src.telemetry import configure_tracing

server = MCPServer(
    "mcp-ace",
    **server_kwargs("agent:mcp-ace", get_settings().mcp_ace_url),
)


@server.tool(annotations=READ_ONLY)
@guarded(None)
def ping() -> str:
    """Liveness probe. Week 1 placeholder - real tools land in the milestone below."""
    return "mcp-ace ok"


def main() -> None:
    # A provider per process, so a tool call made on behalf of an API request lands in
    # the same trace. Without an exporter configured the spans are created and dropped —
    # see services/api/src/telemetry.py; the server starts either way.
    configure_tracing("drawbridge-mcp-ace", endpoint=get_settings().otel_exporter_endpoint)
    run(server, name="mcp-ace", host_alias="mcp-ace", port=8101)


if __name__ == "__main__":
    main()
