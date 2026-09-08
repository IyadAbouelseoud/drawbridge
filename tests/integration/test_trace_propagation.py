"""Does an MCP tool call join the trace of whatever asked for it?

Week 12 recorded that nothing propagated `traceparent` across the MCP transport, so an
analyst's tool call was its own root trace with no way back to the request that prompted
it. Week 13 went to write the propagation and found the MCP SDK already does both halves:
the client dispatcher injects W3C trace context into the JSON-RPC `_meta` (SEP-414), and
`OpenTelemetryMiddleware` — installed by default on every server — extracts it.

So there is no propagation code in this repository, and that is the finding. What there
is instead is this test, because a property nobody wrote is a property nobody notices
losing. An SDK upgrade that drops the default middleware, or a server built with
`middleware=[]`, would silently return the codebase to week 12's position: every tool call
its own island, and no way to answer "what else did this run touch" four years later when
the question is being asked by an auditor.

In-memory transport rather than a live container. The property under test is whether the
trace context survives a serialise/deserialise round trip through the protocol, and that
does not need a port.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from mcp.client.client import Client
from mcp.server.mcpserver import MCPServer
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from services.api.src.telemetry import configure_tracing, current_trace_id, tracer

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    """Collect spans off the process-wide provider.

    A second `TracerProvider` would be the obvious thing and would prove nothing: OpenTelemetry
    resolves `get_tracer` through the *global* provider, so the SDK's middleware would record
    into the global one while the test read an empty local one. So the exporter is attached to
    whichever provider is installed, and `configure_tracing` guarantees there is one.
    """
    configure_tracing("integration-test")
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider), "no real provider installed"
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


def _server() -> MCPServer:
    server = MCPServer("trace-probe")

    @server.tool()
    def echo(value: str) -> str:
        """Returns what it was given. The point is the span, not the answer."""
        return value

    return server


class TestTheDefaultMiddlewareIsStillThere:
    def test_a_server_installs_opentelemetry_without_being_asked(self) -> None:
        """The whole propagation story rests on this being on by default.

        Asserted by name rather than by behaviour so that the failure message points at
        the cause — an SDK that stopped shipping it — rather than at a missing span.
        """
        installed = [type(m).__name__ for m in _server().middleware]
        assert "OpenTelemetryMiddleware" in installed

    def test_it_is_outermost(self) -> None:
        """Outermost means a request refused by a later middleware still gets a span, so a
        burst of refusals is visible rather than absent."""
        installed = [type(m).__name__ for m in _server().middleware]
        assert installed[0] == "OpenTelemetryMiddleware"


class TestTheTraceCrossesTheTransport:
    async def test_the_server_span_carries_the_client_s_trace_id(
        self, spans: InMemorySpanExporter
    ) -> None:
        """The property week 12 said was missing, and the reason this file exists.

        One provider serves both ends here, which is what makes the assertion meaningful:
        if the context did not cross, the tool call would open a *new* trace id rather
        than inherit, and the two would differ.
        """
        server = _server()

        async with Client(server) as session:
            with tracer("test").start_as_current_span("analyst asks"):
                parent = current_trace_id()
                await session.call_tool("echo", {"value": "hello"})

        finished = spans.get_finished_spans()
        assert finished, "no spans were exported at all"
        tool_spans = [s for s in finished if "echo" in s.name or "tools/call" in s.name]
        assert tool_spans, f"no tool-call span among {[s.name for s in finished]}"
        assert parent is not None
        assert all(format(s.context.trace_id, "032x") == parent for s in tool_spans)

    async def test_two_calls_in_one_trace_share_it(self, spans: InMemorySpanExporter) -> None:
        """What an auditor actually wants is every row one run touched, not one step."""
        server = _server()

        async with Client(server) as session:
            with tracer("test").start_as_current_span("one run"):
                await session.call_tool("echo", {"value": "first"})
                await session.call_tool("echo", {"value": "second"})
                parent = current_trace_id()

        under_the_run = [
            s for s in spans.get_finished_spans() if format(s.context.trace_id, "032x") == parent
        ]
        # Both calls, both their server-side counterparts, and the span they ran under.
        assert len(under_the_run) >= 4, f"only {len(under_the_run)} spans joined the run"


class TestOurServersConfigureAProvider:
    def test_every_mcp_server_calls_configure_tracing(self) -> None:
        """Extraction is the SDK's; having somewhere for the span to go is ours.

        Without a configured provider the middleware still runs and its spans are dropped,
        which looks identical to working instrumentation until someone reads Jaeger.
        """
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2] / "mcp_servers"
        servers = sorted(root.glob("*/server.py"))
        assert len(servers) == 5, f"expected 5 MCP servers, found {len(servers)}"
        missing = [
            path.parent.name
            for path in servers
            if "configure_tracing" not in path.read_text(encoding="utf-8")
        ]
        assert not missing, f"no tracer provider configured in: {missing}"
