"""Mint a local HS256 token, for development and for the end-to-end script.

In a deployment Authentik issues tokens and this script has nothing to sign with — it
refuses rather than falling back, because a second issuer nobody registered is a second
way in. See `services/api/src/auth.py`.

Two shapes, and the difference matters:

    python scripts/mint_token.py --tenant <uuid>     # a user, bound to one tenant
    python scripts/mint_token.py --service           # the pipeline, bound to none

The service token may act for any tenant and is what n8n carries. It is the most valuable
secret in a deployment and it is minted separately so that nobody produces one by leaving
out an argument.
"""

from __future__ import annotations

import argparse
import sys
from uuid import UUID

from services.api.src.auth import SERVICE_SCOPE, mint
from services.api.src.config import get_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mint a development bearer token.")
    parser.add_argument("--tenant", type=UUID, help="tenant this token is bound to")
    parser.add_argument(
        "--service",
        action="store_true",
        help="mint a cross-tenant pipeline token instead (n8n, the e2e script)",
    )
    parser.add_argument("--subject", default=None, help="the sub claim; defaults per shape")
    parser.add_argument("--ttl", type=int, default=3600, help="lifetime in seconds")
    args = parser.parse_args(argv)

    if args.service == bool(args.tenant):
        # Both or neither. A token that is somehow both would make "may this caller act
        # for tenant X" depend on which field is read first, which `auth.decode` refuses
        # anyway — this is the same refusal, earlier and with a better message.
        print("give exactly one of --tenant <uuid> or --service", file=sys.stderr)
        return 2

    settings = get_settings()
    try:
        if args.service:
            token = mint(
                settings,
                subject=args.subject or "drawbridge-pipeline",
                scopes=(SERVICE_SCOPE,),
                ttl_seconds=args.ttl,
            )
        else:
            token = mint(
                settings,
                subject=args.subject or f"dev:{args.tenant}",
                tenant_id=args.tenant,
                ttl_seconds=args.ttl,
            )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    # Bare on stdout, so `TOKEN=$(python scripts/mint_token.py --service)` works.
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
