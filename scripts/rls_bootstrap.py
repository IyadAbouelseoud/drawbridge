"""Create the unprivileged role the application connects as, and grant it what it needs.

Separate from the migration on purpose. The migration installs the policies; this installs
the only thing that makes them bite. Policies do not apply to superusers or to roles with
BYPASSRLS, and the `drawbridge` role that owns these tables is both — so RLS is inert until
the services connect as `drawbridge_app`, and `FORCE ROW LEVEL SECURITY` would not change
that (it binds a table's owner only where the owner is not a superuser).

It is not in the migration because a migration that creates a login role has to put its
password somewhere, and the only places available are the migration file — which is in
version control — or a prompt, which no CI run can answer. Here the password comes from the
environment, which is where the deployment already keeps it.

Run once per database, and again after any migration that adds a table:

    DRAWBRIDGE_APP_DB_PASSWORD=... uv run python scripts/rls_bootstrap.py

Connects as the owner. Prints what the app role can and cannot see afterwards, because the
failure mode this guards against is silent: a role that is accidentally left with BYPASSRLS
passes every functional test in the suite and isolates nothing.
"""

from __future__ import annotations

import os
import sys

from sqlalchemy import create_engine, text

from services.api.src.config import get_settings
from services.api.src.tenancy import APP_ROLE, ensure_app_role, install_rls

PASSWORD_ENV = "DRAWBRIDGE_APP_DB_PASSWORD"


def main() -> int:
    password = os.environ.get(PASSWORD_ENV, "").strip()
    if not password:
        print(
            f"{PASSWORD_ENV} is not set. It is the password the API and the MCP servers "
            f"use in DRAWBRIDGE_DATABASE_URL as {APP_ROLE}.",
            file=sys.stderr,
        )
        return 1

    url = os.environ.get("DRAWBRIDGE_OWNER_DATABASE_URL") or get_settings().sync_database_url
    engine = create_engine(url.replace("+asyncpg", "+psycopg"))

    with engine.begin() as connection:
        install_rls(connection)
        ensure_app_role(connection, password)

        attributes = connection.execute(
            text("SELECT rolsuper, rolbypassrls, rolcanlogin FROM pg_roles WHERE rolname = :r"),
            {"r": APP_ROLE},
        ).one()
        protected = connection.execute(
            text(
                "SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND rowsecurity = true"
            )
        ).scalar_one()

    superuser, bypass, login = attributes
    print(f"{APP_ROLE}: superuser={superuser} bypassrls={bypass} login={login}")
    print(f"row-level security enabled on {protected} table(s)")

    if superuser or bypass:
        # Not a warning. A role in this state reads every tenant's rows and every test in
        # the suite still passes, which is the worst combination available.
        print(
            f"REFUSING: {APP_ROLE} would bypass every policy. Isolation is not in effect.",
            file=sys.stderr,
        )
        return 1

    print(
        f"point DRAWBRIDGE_DATABASE_URL at {APP_ROLE} — the policies do nothing while the "
        "services connect as the owner"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
