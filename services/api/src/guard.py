"""The request guard: what every call passes through before and after it reaches a route.

Two layers, placed either side of `AuthMiddleware` because each needs something the other
does not have:

```
OpenTelemetry          (outermost — a refused request still produces a span)
  EdgeMiddleware       before auth: body cap, per-address rate limit on /auth/token,
                       security headers, and the access log line written on the way out
    AuthMiddleware     who — puts the Principal on a context variable
      PrincipalGuard   after auth: per-principal rate limit, and the kill switch
        routes
```

**The access log is the "what did it access, when, and why" half of the audit trail.**
`audit_ledger` records what *changed* a claim, hash-chained, per tenant; it deliberately
does not record reads, because a chain that grows on every GET is a chain nobody can
afford to verify. Reads are where an exfiltration shows up, so every request — read or
write, allowed or refused — gets one structured line: principal, agent, roles, tenant,
route template, status, duration, trace id. The trace id is the join to the ledger rows
the same request wrote.

**Rate limits are per process, in memory.** A token bucket per principal (or per client
address before one is known), a tighter bucket on the credential exchange and on the
route that calls the model. Per process is the honest scope: two API replicas allow twice
the rate. It is a guard against a runaway loop or a guessing attack against one process,
not a quota system, and the key table is bounded so the limiter cannot itself be the
memory exhaustion it exists to prevent.

All three are pure ASGI rather than `BaseHTTPMiddleware`, for the reason `AuthMiddleware`
gives — measured in `docs/ARCHITECTURE.md` §24.
"""

from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

import structlog

from services.api.src import killswitch
from services.api.src.auth import current_principal
from services.api.src.telemetry import current_trace_id

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from services.api.src.config import Settings

log = structlog.get_logger("drawbridge.access")

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: Mutations the kill switch deliberately lets through. The switch's own route, or an
#: engaged switch could never be released through the API; and `/review/suspend`, which
#: only ever puts a claim *in front of* a person — the direction an incident wants claims
#: to move. `/auth/token` refuses for itself, per principal (routes/identity.py).
HALT_EXEMPT_PATHS = frozenset({"/control/kill-switch", "/review/suspend", "/auth/token"})

#: Headers every response carries. The on-prem Caddyfile sets the same at the edge; they
#: are repeated here so a development stack, and any deployment without that proxy, gets
#: them too. `no-store` because every response from this API is either tenant data or a
#: credential, and neither belongs in a shared cache.
SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cache-control", b"no-store"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
)

#: The documentation UI loads a script and a stylesheet from a CDN, which the policy above
#: forbids. It is only mounted in development, where the relaxed policy applies to it alone.
_DOCS_PATHS = frozenset({"/docs", "/redoc"})

_MAX_BUCKETS = 10_000


class TokenBucket:
    """A bounded map of token buckets, one per key."""

    def __init__(self, per_minute: int) -> None:
        self.capacity = float(max(per_minute, 1))
        self.refill_per_second = self.capacity / 60.0
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, float]:
        """Whether one more request is allowed, and if not, seconds until it would be."""
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.pop(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.refill_per_second)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                allowed, wait = True, 0.0
            else:
                self._buckets[key] = (tokens, now)
                allowed, wait = False, (1.0 - tokens) / self.refill_per_second
            while len(self._buckets) > _MAX_BUCKETS:
                self._buckets.popitem(last=False)
        return allowed, wait


def _client(scope: Scope) -> str:
    client = scope.get("client")
    return str(client[0]) if client else "unknown"


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", ()):
        if key == name:
            return str(value.decode("latin-1"))
    return None


async def _json(
    send: Send, status: int, body: dict[str, Any], extra: list[Any] | None = None
) -> None:
    payload = json.dumps(body).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(payload)).encode()),
        *(extra or []),
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})


class _BodyTooLargeError(Exception):
    pass


class EdgeMiddleware:
    """Body cap, credential-exchange rate limit, security headers, access log."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.max_bytes = settings.max_request_bytes
        self.token_bucket = TokenBucket(settings.token_rate_limit_per_minute)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status_holder = {"status": 500, "started": False}
        path = scope["path"]

        async def send_wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                status_holder["started"] = True
                headers = list(message.get("headers", []))
                if path not in _DOCS_PATHS:
                    headers.extend(SECURITY_HEADERS)
                message = {**message, "headers": headers}
            await send(message)

        try:
            declared = _header(scope, b"content-length")
            if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
                await _json(
                    send_wrapped,
                    413,
                    {"error": "payload_too_large", "max_bytes": self.max_bytes},
                )
                return

            if path == "/auth/token":
                allowed, wait = self.token_bucket.allow(_client(scope))
                if not allowed:
                    await _json(
                        send_wrapped,
                        429,
                        {"error": "rate_limited", "retry_after_seconds": round(wait, 1)},
                        [(b"retry-after", str(max(1, int(wait))).encode())],
                    )
                    return

            received = 0

            async def receive_capped() -> Message:
                nonlocal received
                message = await receive()
                if message["type"] == "http.request":
                    received += len(message.get("body", b""))
                    if received > self.max_bytes:
                        raise _BodyTooLargeError
                return message

            try:
                await self.app(scope, receive_capped, send_wrapped)
            except _BodyTooLargeError:
                if not status_holder["started"]:
                    await _json(
                        send_wrapped,
                        413,
                        {"error": "payload_too_large", "max_bytes": self.max_bytes},
                    )
        finally:
            self._log(scope, status_holder["status"], time.perf_counter() - started)

    @staticmethod
    def _log(scope: Scope, status: Any, elapsed: float) -> None:
        if scope["path"] in {"/health", "/ready"}:
            return
        principal = scope.get("state", {}).get("principal")
        route = scope.get("route")
        log.info(
            "access",
            method=scope["method"],
            path=scope["path"],
            route=getattr(route, "path", None),
            status=status,
            duration_ms=round(elapsed * 1000, 2),
            principal=getattr(principal, "subject", None),
            agent_id=getattr(principal, "agent_id", None),
            roles=sorted(getattr(principal, "roles", ()) or ()),
            token_tenant=str(principal.tenant_id) if principal and principal.tenant_id else None,
            token_id=getattr(principal, "token_id", None),
            client=_client(scope),
            trace_id=current_trace_id(),
        )


class PrincipalGuard:
    """Per-principal rate limit, and the kill switch for mutating requests."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.general = TokenBucket(settings.rate_limit_per_minute)
        self.draft = TokenBucket(settings.draft_rate_limit_per_minute)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        principal = current_principal()
        key = principal.subject if principal else f"addr:{_client(scope)}"
        path = scope["path"]

        bucket = self.draft if path == "/review/draft" else self.general
        allowed, wait = bucket.allow(key)
        if not allowed:
            log.warning("rate_limited", principal=key, path=path)
            await _json(
                send,
                429,
                {"error": "rate_limited", "retry_after_seconds": round(wait, 1)},
                [(b"retry-after", str(max(1, int(wait))).encode())],
            )
            return

        if scope["method"] in SAFE_METHODS or path in HALT_EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        subject = principal.subject if principal else None
        covering = await self._covering(scope, subject)
        if covering is not None:
            log.warning("killswitch.refused", principal=subject, path=path, scope=covering)
            await _json(
                send,
                503,
                {
                    "error": "kill_switch_engaged",
                    "scope_kind": covering[0],
                    "scope_value": covering[1],
                    "message": "mutations are halted; reads remain available",
                },
            )
            return

        mutating = killswitch.MUTATING.set(True)
        acting = killswitch.PRINCIPAL.set(subject)
        try:
            await self.app(scope, receive, send)
        finally:
            killswitch.PRINCIPAL.reset(acting)
            killswitch.MUTATING.reset(mutating)

    @staticmethod
    async def _covering(scope: Scope, subject: str | None) -> tuple[str, str] | None:
        app = scope.get("app")
        maker = getattr(getattr(app, "state", None), "sessionmaker", None)
        if maker is None:
            # No database configured on this app (a unit test's bare router). The env
            # override still applies; the table cannot be consulted.
            if killswitch.env_engaged():
                return ("global", "")
            return None
        async with maker() as session:
            return (await killswitch.state_async(session)).covering(principal=subject)
