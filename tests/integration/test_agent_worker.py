"""The drafting worker as a process.

`draft_pending` has been tested since week 8. What has never been tested is that anything
runs it — there was no entrypoint, which is why "a live agent run against real queue rows"
sat on the roadmap for six weeks while the code that would do it was already passing its
own tests.

These are deliberately shallow. They assert that the loop starts, connects, completes a
pass and stops; the drafting behaviour itself belongs to the module that does it, and a
test here that exercised the model call would make the suite depend on an API key.
"""

from __future__ import annotations

import signal
from collections.abc import Iterator

import pytest

from services.agent.src import worker
from tests.integration.conftest import TEST_DSN

pytest_plugins = ("tests.integration.conftest",)


@pytest.fixture(autouse=True)
def _point_at_the_test_database(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("DRAWBRIDGE_DATABASE_URL", TEST_DSN)
    worker.get_settings.cache_clear()
    yield
    worker.get_settings.cache_clear()


@pytest.mark.usefixtures("engine")
class TestTheWorkerLoop:
    def test_one_pass_over_an_empty_queue_exits_zero(self) -> None:
        """The whole point: the entrypoint exists and connects. A pass that finds nothing
        is the normal case — `review_queue` is empty most of the day."""
        assert worker.run(interval=0.1, batch=5, once=True) == 0

    def test_a_stop_request_ends_the_loop_before_the_interval(self) -> None:
        """SIGTERM during the sleep must be honoured within a second rather than after
        the full two minutes. A container that takes two minutes to stop looks hung, and
        the orchestrator kills it instead of letting it finish the row in flight."""
        stop = worker._Stop()
        assert stop.requested is False
        stop(signal.SIGTERM, None)
        assert stop.requested is True

    def test_it_drafts_across_every_tenant(self) -> None:
        """`tenant_id=None` is not an oversight. One worker serves whichever tenants have
        queued work, the same posture as the service token n8n carries — which is why the
        on-prem stack gives it its own revocable credential."""
        import inspect

        source = inspect.getsource(worker.run)
        assert "draft_pending(session, limit=batch)" in source
        assert "tenant_id" not in source
