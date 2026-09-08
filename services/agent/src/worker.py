"""The drafting worker: a loop around `draft_pending`, and nothing else.

    python -m services.agent.src.worker                 # run until stopped
    python -m services.agent.src.worker --once          # one pass, for cron or a test

`services/agent/src/queue.py` has been able to draft memos since week 8, and until now the
only things that called it were a test and a script. There was no process. That is why the
compose stack has never had an agent container and why "a live agent run against real queue
rows" has been carried on the roadmap for six weeks: the code was ready and nothing ran it.

**It polls, and it should.** The alternative is `LISTEN`/`NOTIFY` from the trigger that
writes a review row, which is fewer wasted queries and one more thing to lose silently: a
dropped notification is a memo that never appears and no evidence that anything happened.
A poll that finds nothing is one indexed query against `review_queue`, and the interval is
minutes because an analyst arrives minutes to hours after the suspension.

**It holds no state and owns no rows.** `draft_pending` selects `FOR UPDATE SKIP LOCKED`
and commits per row, so two workers against one queue is a supported configuration, an
interrupted pass loses at most the row in flight, and a restart resumes by construction.

**It runs unscoped, deliberately.** `tenant_id=None` drafts across every tenant, which is
the same posture as the service token n8n carries and for the same reason: one worker
serves whichever tenants have queued work. It therefore connects as the owner role, and
`docker-compose.onprem.yml` gives it its own credential so that the blast radius of the
one cross-tenant process in the deployment is a credential somebody can revoke.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from types import FrameType

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from services.agent.src.queue import draft_pending
from services.api.src.config import get_settings

logger = logging.getLogger("drawbridge.agent.worker")

#: Between passes. Minutes rather than seconds because the consumer of a memo is a person
#: who is not yet at their desk, and seconds rather than minutes would be a query per
#: second against a table that is empty most of the day.
DEFAULT_INTERVAL = 120.0

#: Rows per pass. Bounded so a backlog is drained steadily rather than in one transaction
#: that holds locks for as long as the model takes to answer forty times.
DEFAULT_BATCH = 20


class _Stop:
    """Set by SIGTERM and SIGINT so a pass finishes before the process exits.

    Killing the worker mid-`draft_pending` is safe — the commit is per row — but it wastes
    the model call in flight, and a container that stops cleanly on the first signal does
    not have to be killed by the orchestrator's timeout.
    """

    def __init__(self) -> None:
        self.requested = False

    def __call__(self, signum: int, _frame: FrameType | None) -> None:
        logger.info("worker.stopping", extra={"signal": signum})
        self.requested = True


def run(*, interval: float, batch: int, once: bool) -> int:
    settings = get_settings()
    engine = create_engine(settings.sync_database_url, pool_pre_ping=True)

    stop = _Stop()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    passes = drafted_total = 0
    while not stop.requested:
        with Session(engine) as session:
            report = draft_pending(session, limit=batch)
        passes += 1
        drafted_total += report.drafted
        if report.drafted or report.skipped or report.unavailable:
            logger.info(
                "worker.pass drafted=%d skipped=%d unavailable=%d",
                report.drafted,
                report.skipped,
                report.unavailable,
            )
        if once:
            break
        # Slept in short slices so a signal is honoured within a second rather than after
        # the full interval. A container that takes two minutes to stop looks hung.
        waited = 0.0
        while waited < interval and not stop.requested:
            time.sleep(min(1.0, interval - waited))
            waited += 1.0

    logger.info("worker.stopped passes=%d drafted=%d", passes, drafted_total)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    parser.add_argument("--once", action="store_true", help="one pass, then exit")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    return run(interval=args.interval, batch=args.batch, once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
