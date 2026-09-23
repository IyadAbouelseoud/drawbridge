"""The kill switch, from a shell, over the owner DSN.

    python scripts/killswitch.py status
    python scripts/killswitch.py engage  --reason "..." [--scope tenant --value <uuid>]
    python scripts/killswitch.py release --reason "..." [--scope ... --value ...]

The API route (`POST /control/kill-switch`) is the ordinary way to throw the switch. This
exists for the case the route cannot serve: the API is the thing that has gone wrong, or is
unreachable, or an operator's token cannot be issued because the identity provider is down.
It writes the same append-only `control_events` row the route writes, so the record of who
stopped the system does not depend on which door they used. For the case where the
database itself is the problem, set `DRAWBRIDGE_KILL_SWITCH=engaged` on the deployment.

The actor recorded is the operating-system user running the script, prefixed `local:`.
It is an operator's claim rather than a verified identity — whoever holds the owner DSN
could write anything to the table — and the prefix says so.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from services.api.src import killswitch

OWNER_DSN_ENV = "DRAWBRIDGE_OWNER_DATABASE_URL"


def _dsn() -> str:
    dsn = os.environ.get(OWNER_DSN_ENV, "")
    if not dsn:
        print(f"set {OWNER_DSN_ENV}", file=sys.stderr)
        raise SystemExit(2)
    return dsn.replace("+asyncpg", "+psycopg")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("status", "engage", "release"))
    parser.add_argument("--scope", default="global", choices=("global", "tenant", "principal"))
    parser.add_argument("--value", default="")
    parser.add_argument("--reason", default="")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)

    engine = create_engine(_dsn())
    with Session(engine) as session:
        if args.action == "status":
            current = killswitch.state(session)
            print(
                json.dumps(
                    {
                        "env_override": killswitch.env_engaged(),
                        "readable": current.read_ok,
                        "engaged": sorted(current.engaged),
                        "history": killswitch.history(session, args.limit),
                    },
                    indent=2,
                    default=str,
                )
            )
            return 0

        change = killswitch.engage if args.action == "engage" else killswitch.release
        try:
            result = change(
                session,
                actor=f"local:{getpass.getuser()}",
                actor_kind="local",
                reason=args.reason,
                scope_kind=args.scope,
                scope_value=args.value,
            )
        except ValueError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
        session.commit()
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
