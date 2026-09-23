"""Mint a local HS256 token, for development and for the end-to-end script.

In a deployment Authentik issues tokens and this script has nothing to sign with — it
refuses rather than falling back, because a second issuer nobody registered is a second
way in. See `services/api/src/auth.py`.

Two shapes, and the difference matters:

    python scripts/mint_token.py --tenant <uuid> [--role analyst]   # a person
    python scripts/mint_token.py --agent agent:e2e-harness          # a registered agent

**v1.1.0.** An agent token names a registered identity (`drawbridge_schemas.agents`) and
lives no longer than that identity's ceiling — fifteen minutes for the pipeline and the
harness — because the verifier refuses anything longer. `--service`, which minted an
unregistered cross-tenant token for a day and could save it into `.secrets.json` for n8n,
is gone: n8n now exchanges its client secret at `/auth/token` on every run and holds no
bearer token at rest. `--service` survives as an alias for `--agent agent:e2e-harness` so
an old invocation fails over to the short-lived shape rather than to an error.
"""

from __future__ import annotations

import argparse
import sys
from uuid import UUID

from drawbridge_schemas.agents import AgentKind, Role, agent
from services.api.src.auth import SERVICE_SCOPE, mint
from services.api.src.config import get_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mint a development bearer token.")
    parser.add_argument("--tenant", type=UUID, help="tenant a person's token is bound to")
    parser.add_argument(
        "--role",
        action="append",
        choices=[role.value for role in Role],
        help="role(s) for a person's token; omit for the environment's default",
    )
    parser.add_argument("--agent", help="a registered API-client agent id, e.g. agent:e2e-harness")
    parser.add_argument(
        "--service", action="store_true", help="alias for --agent agent:e2e-harness"
    )
    parser.add_argument("--subject", default=None, help="the sub claim for a person")
    parser.add_argument("--ttl", type=int, default=None, help="lifetime in seconds")
    args = parser.parse_args(argv)

    agent_id = args.agent or ("agent:e2e-harness" if args.service else None)
    if bool(agent_id) == bool(args.tenant):
        print("give exactly one of --tenant <uuid> or --agent <id>", file=sys.stderr)
        return 2

    settings = get_settings()
    try:
        if agent_id:
            identity = agent(agent_id)
            if identity is None or identity.kind is not AgentKind.API_CLIENT:
                print(f"{agent_id} is not a registered API-client agent", file=sys.stderr)
                return 2
            ttl = min(args.ttl or identity.max_token_ttl_seconds, identity.max_token_ttl_seconds)
            token = mint(
                settings,
                subject=identity.agent_id,
                scopes=(SERVICE_SCOPE,) if identity.cross_tenant else (),
                ttl_seconds=ttl,
            )
        else:
            ceiling = settings.max_user_token_ttl_seconds
            token = mint(
                settings,
                subject=args.subject or f"dev:{args.tenant}",
                tenant_id=args.tenant,
                roles=tuple(args.role or ()),
                ttl_seconds=min(args.ttl or ceiling, ceiling),
            )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    # Bare on stdout, so `TOKEN=$(python scripts/mint_token.py --agent ...)` works.
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
