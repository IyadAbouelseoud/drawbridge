"""The performance audit: what the v1.1.0 controls cost, and whether the hot queries use
their indexes. Every number the documentation quotes comes from running this.

    python scripts/perf_audit.py --dsn postgresql+psycopg://drawbridge:...@host:5432/drawbridge

**Four measurements, each against the thing it is about:**

1. **The request path.** An authenticated GET through the v1.1.0 stack (edge, auth, guard —
   all pure ASGI) against the same route behind the *v1.0.0* `AuthMiddleware`, reconstructed
   from git, which was a `BaseHTTPMiddleware`. In-process over `httpx.ASGITransport`, so the
   difference is middleware and nothing else.
2. **The primitives.** JWT verification, the kill-switch read cold and cached, the injection
   scan and the output guard, building an Anthropic client (which the drafter used to do
   once per memo).
3. **The query plans.** `EXPLAIN (ANALYZE, BUFFERS)` for every query a hot path runs, over
   synthetic rows seeded inside a transaction that is rolled back — so the audit can run
   against a database it must not change.
4. **Nothing is extrapolated.** Counts and timings are what one machine measured; the
   output says which machine and how many repetitions.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import httpx  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

from services.api.src import killswitch  # noqa: E402
from services.api.src.auth import AuthMiddleware, decode, mint  # noqa: E402
from services.api.src.config import Settings  # noqa: E402
from services.api.src.guard import EdgeMiddleware, PrincipalGuard  # noqa: E402

SECRET = "perf-audit-secret-long-enough-for-hs256-xxxxx"


def _timeit(fn: Any, repeat: int) -> dict[str, float]:
    samples = []
    for _ in range(repeat):
        started = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - started) * 1e6)
    samples.sort()
    return {
        "median_us": round(statistics.median(samples), 1),
        "p95_us": round(samples[int(len(samples) * 0.95) - 1], 1),
        "n": repeat,
    }


async def _ok(_request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


def _old_auth_class() -> Any:
    """The v1.0.0 `AuthMiddleware`, loaded from git as it was."""
    source = subprocess.run(
        ["git", "show", "v1.0.0:services/api/src/auth.py"],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(source)
        path = f.name
    spec = importlib.util.spec_from_file_location("auth_v100", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["auth_v100"] = module  # `dataclass` resolves the module by name
    spec.loader.exec_module(module)
    return module.AuthMiddleware


async def _request_path(repeat: int) -> dict[str, Any]:
    settings = Settings(jwt_secret=SECRET, auth_required=True, rate_limit_per_minute=10**9)
    old_settings = settings
    headers = {
        "Authorization": "Bearer "
        + mint(settings, subject="perf@tenant", tenant_id=uuid4(), ttl_seconds=900)
    }

    new_app = Starlette(routes=[Route("/r", _ok)])
    new_app.add_middleware(PrincipalGuard, settings=settings)
    new_app.add_middleware(AuthMiddleware, settings=settings)
    new_app.add_middleware(EdgeMiddleware, settings=settings)

    old_app = Starlette(routes=[Route("/r", _ok)])
    old_app.add_middleware(_old_auth_class(), settings=old_settings)

    bare = Starlette(routes=[Route("/r", _ok)])

    out: dict[str, Any] = {}
    for label, app in (
        ("no_middleware", bare),
        ("v1.0.0_auth", old_app),
        ("v1.1.0_stack", new_app),
    ):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            for _ in range(50):
                await client.get("/r", headers=headers)
            samples = []
            for _ in range(repeat):
                started = time.perf_counter()
                response = await client.get("/r", headers=headers)
                samples.append((time.perf_counter() - started) * 1e6)
                assert response.status_code == 200, (label, response.status_code)
        samples.sort()
        out[label] = {
            "median_us": round(statistics.median(samples), 1),
            "p95_us": round(samples[int(len(samples) * 0.95) - 1], 1),
            "n": repeat,
        }
    return out


def _primitives(dsn: str | None, repeat: int) -> dict[str, Any]:
    from services.agent.src import output_guard
    from services.agent.src.injection import scan
    from services.agent.src.schemas import ExceptionMemo
    from tests.golden.test_agent import EXCEPTION_MEMO

    settings = Settings(jwt_secret=SECRET)
    token = mint(settings, subject="perf@tenant", tenant_id=uuid4())
    memo = ExceptionMemo.model_validate(EXCEPTION_MEMO)
    facts = {"summary": "duty_amount read at 0.71", "payload": {"d": ["x" * 60] * 20}}
    out: dict[str, Any] = {
        "jwt_decode_hs256": _timeit(lambda: decode(token, settings), repeat),
        "injection_scan_facts": _timeit(lambda: scan(facts), repeat // 4),
        "output_guard_memo": _timeit(lambda: output_guard.validate(memo), repeat // 4),
    }
    try:
        from anthropic import Anthropic

        out["anthropic_client_construction"] = _timeit(
            lambda: Anthropic(api_key="sk-ant-perf-audit-not-a-key"), 50
        )
    except ImportError:  # pragma: no cover
        out["anthropic_client_construction"] = "anthropic not installed"

    if dsn:
        engine = create_engine(dsn)
        with Session(engine) as session:

            def cold() -> None:
                killswitch.invalidate()
                killswitch.state(session)

            out["killswitch_read_cold"] = _timeit(cold, 200)
            killswitch.invalidate()
            killswitch.state(session)
            out["killswitch_read_cached"] = _timeit(lambda: killswitch.state(session), repeat)
    return out


def _plan_nodes(node: dict[str, Any]) -> list[str]:
    """A plan tree flattened to node types, with the index each scan used."""
    label = node["Node Type"]
    if node.get("Index Name"):
        label += f" ({node['Index Name']})"
    out = [label]
    for child in node.get("Plans", []):
        out.extend(_plan_nodes(child))
    return out


def _plans(dsn: str, tenants: int, rows_per_tenant: int) -> dict[str, Any]:
    """EXPLAIN ANALYZE the hot queries over seeded rows, then roll everything back."""
    engine = create_engine(dsn)
    plans: dict[str, Any] = {}
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            tenant_ids = [uuid4() for _ in range(tenants)]
            connection.execute(
                text(
                    "INSERT INTO tenants (tenant_id, name, default_jurisdiction) "
                    "SELECT t, 'perf-' || t::text, 'us' FROM unnest(CAST(:ids AS uuid[])) AS t"
                ),
                {"ids": [str(t) for t in tenant_ids]},
            )
            connection.execute(
                text("""
                    INSERT INTO review_queue (review_id, tenant_id, reason, severity, summary,
                                              payload, resume_token, state, agent_memo,
                                              agent_model, created_at, resolution, resolved_at)
                    SELECT gen_random_uuid(), t, 'threshold_near_miss',
                           (ARRAY['low','normal','high','blocking'])[1 + (g % 4)],
                           'seeded', '{}'::jsonb, md5(random()::text || g::text || t::text),
                           CASE WHEN g % 10 = 0 THEN 'open' ELSE 'resolved' END,
                           CASE WHEN g % 20 = 0 THEN NULL ELSE '{}'::jsonb END,
                           NULL, now() - (g || ' minutes')::interval,
                           CASE WHEN g % 10 = 0 THEN NULL ELSE 'approved' END,
                           CASE WHEN g % 10 = 0 THEN NULL ELSE now() END
                      FROM unnest(CAST(:ids AS uuid[])) AS t, generate_series(1, :n) AS g
                """),
                {"ids": [str(t) for t in tenant_ids], "n": rows_per_tenant},
            )
            connection.execute(
                text("""
                    INSERT INTO control_events (event_type, scope_kind, scope_value, actor,
                                                actor_kind, reason, detail)
                    SELECT 'token_issued', 'principal', 'agent:n8n-pipeline', 'agent:n8n-pipeline',
                           'machine', 'client_credentials exchange for the pipeline', '{}'::jsonb
                      FROM generate_series(1, 5000)
                """)
            )
            connection.execute(
                text("ANALYZE review_queue; ANALYZE control_events; ANALYZE tenants")
            )
            probe = str(tenant_ids[len(tenant_ids) // 2])
            queries = {
                "drafter_poll": (
                    "SELECT review_id FROM review_queue WHERE agent_memo IS NULL "
                    "AND agent_model IS NULL AND state = 'open' AND tenant_id = CAST(:t AS uuid) "
                    "ORDER BY created_at LIMIT 1",
                    {"t": probe},
                ),
                "drafter_tenant_lookup": ("SELECT app_tenants_with_undrafted_reviews()", {}),
                "killswitch_state": (killswitch._STATE_SQL.text, {}),
                "queue_ordered": (
                    "SELECT review_id FROM review_queue WHERE state = 'open' "
                    "AND tenant_id = CAST(:t AS uuid) ORDER BY CASE severity "
                    "WHEN 'blocking' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 "
                    "WHEN 'low' THEN 3 ELSE 9 END, created_at LIMIT 50",
                    {"t": probe},
                ),
            }
            for name, (sql, params) in queries.items():
                rows = connection.execute(
                    text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}"), params
                ).scalar_one()
                plan = rows[0]
                top = plan["Plan"]
                nodes = _plan_nodes(top)
                plans[name] = {
                    "execution_ms": round(plan["Execution Time"], 3),
                    "nodes": nodes,
                }
            plans["_seeded"] = {
                "tenants": tenants,
                "review_rows": tenants * rows_per_tenant,
                "control_events": 5000,
            }
        finally:
            transaction.rollback()
    return plans


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dsn", default=None, help="owner DSN; omit to skip the database half")
    parser.add_argument("--repeat", type=int, default=2000)
    parser.add_argument("--tenants", type=int, default=50)
    parser.add_argument("--rows", type=int, default=400)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    report: dict[str, Any] = {
        "machine": {"python": platform.python_version(), "platform": platform.platform()},
        "request_path": asyncio.run(_request_path(args.repeat)),
        "primitives": _primitives(args.dsn, args.repeat),
        "query_plans": _plans(args.dsn, args.tenants, args.rows) if args.dsn else "skipped",
    }
    body = json.dumps(report, indent=2, default=str)
    if args.out:
        args.out.write_text(body + "\n", encoding="utf-8")
    print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
