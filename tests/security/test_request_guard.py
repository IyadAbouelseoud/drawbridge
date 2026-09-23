"""The request guard: body cap, rate limits, security headers, and the env kill switch.

Built against a four-route Starlette app wrapped in the real middleware, rather than the
whole API, so each property is tested on its own and none of them depends on a database.
The database-backed half of the kill switch is in `test_kill_switch.py`.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from services.api.src import killswitch
from services.api.src.auth import AuthMiddleware, mint
from services.api.src.config import Settings
from services.api.src.guard import SECURITY_HEADERS, EdgeMiddleware, PrincipalGuard, TokenBucket

SECRET = "guard-suite-secret-long-enough-for-hs256-xxx"


async def _ok(request: Request) -> JSONResponse:
    body = await request.body()
    return JSONResponse({"ok": True, "bytes": len(body)})


def _app(**overrides: object) -> TestClient:
    settings = Settings(
        jwt_secret=SECRET,
        auth_required=True,
        max_request_bytes=1024,
        rate_limit_per_minute=5,
        token_rate_limit_per_minute=2,
        draft_rate_limit_per_minute=1,
        **overrides,  # type: ignore[arg-type]
    )
    app = Starlette(
        routes=[
            Route("/read", _ok, methods=["GET"]),
            Route("/write", _ok, methods=["POST"]),
            Route("/auth/token", _ok, methods=["POST"]),
            Route("/review/draft", _ok, methods=["POST"]),
        ]
    )
    app.add_middleware(PrincipalGuard, settings=settings)
    app.add_middleware(AuthMiddleware, settings=settings)
    app.add_middleware(EdgeMiddleware, settings=settings)
    return TestClient(app)


def _bearer() -> dict[str, str]:
    from uuid import uuid4

    settings = Settings(jwt_secret=SECRET)
    token = mint(settings, subject=f"user-{uuid4()}", tenant_id=uuid4())
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _no_env_switch(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(killswitch.ENV_VAR, raising=False)
    yield


class TestTheBodyCap:
    def test_a_declared_length_over_the_cap_is_refused_before_reading(self) -> None:
        response = _app().post("/write", content=b"x" * 2048, headers=_bearer())
        assert response.status_code == 413
        assert response.json()["error"] == "payload_too_large"

    def test_a_body_under_the_cap_is_served(self) -> None:
        response = _app().post("/write", content=b"x" * 100, headers=_bearer())
        assert response.status_code == 200

    def test_a_chunked_body_is_counted_as_it_streams(self) -> None:
        def chunks() -> Iterator[bytes]:
            for _ in range(8):
                yield b"x" * 256

        response = _app().post("/write", content=chunks(), headers=_bearer())
        assert response.status_code == 413


class TestHeaders:
    def test_every_response_carries_the_security_headers(self) -> None:
        response = _app().get("/read", headers=_bearer())
        for name, value in SECURITY_HEADERS:
            assert response.headers[name.decode()] == value.decode()

    def test_a_refusal_carries_them_too(self) -> None:
        response = _app().get("/read")
        assert response.status_code == 401
        assert response.headers["x-content-type-options"] == "nosniff"


class TestRateLimits:
    def test_a_principal_is_limited(self) -> None:
        client = _app()
        headers = _bearer()
        codes = [client.get("/read", headers=headers).status_code for _ in range(7)]
        assert codes[:5] == [200] * 5
        assert 429 in codes[5:]

    def test_the_limit_is_per_principal(self) -> None:
        client = _app()
        for _ in range(6):
            client.get("/read", headers=_bearer())
        assert client.get("/read", headers=_bearer()).status_code == 200

    def test_the_credential_exchange_has_its_own_tighter_bucket(self) -> None:
        client = _app()
        codes = [client.post("/auth/token", content=b"{}").status_code for _ in range(4)]
        assert codes.count(429) >= 1
        refused = client.post("/auth/token", content=b"{}")
        assert refused.status_code == 429
        assert int(refused.headers["retry-after"]) >= 1

    def test_the_model_calling_route_has_its_own_too(self) -> None:
        client = _app()
        headers = _bearer()
        assert client.post("/review/draft", headers=headers).status_code == 200
        assert client.post("/review/draft", headers=headers).status_code == 429

    def test_the_bucket_table_is_bounded(self) -> None:
        bucket = TokenBucket(60)
        for index in range(12_000):
            bucket.allow(f"key-{index}")
        assert len(bucket._buckets) <= 10_000


class TestTheEnvKillSwitch:
    def test_engaged_halts_writes_and_leaves_reads(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(killswitch.ENV_VAR, "engaged")
        client = _app()
        headers = _bearer()
        assert client.get("/read", headers=headers).status_code == 200
        halted = client.post("/write", headers=headers)
        assert halted.status_code == 503
        assert halted.json()["error"] == "kill_switch_engaged"

    def test_unset_it_does_nothing(self) -> None:
        assert _app().post("/write", headers=_bearer()).status_code == 200


class TestSwitchStateSemantics:
    def test_an_unreadable_switch_is_an_engaged_one(self) -> None:
        state = killswitch.SwitchState(engaged=frozenset(), read_ok=False)
        assert state.covering(principal="anyone") == ("global", "unreadable")

    def test_scopes_cover_what_they_name_and_nothing_else(self) -> None:
        state = killswitch.SwitchState(engaged=frozenset({("principal", "agent:memo-drafter")}))
        assert state.covering(principal="agent:memo-drafter") is not None
        assert state.covering(principal="agent:n8n-pipeline") is None

    def test_a_reason_is_required(self) -> None:
        with pytest.raises(ValueError, match="reason"):
            killswitch._validate("global", "", "because")
