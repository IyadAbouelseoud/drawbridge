"""The kill switch: one human decision that stops every agent from changing anything.

**What it stops.** Every mutation — every POST through the API, every write tool on an MCP
server, every memo the drafter would attach, every token the exchange would issue. What it
does *not* stop is reading. An incident is investigated by reading the ledger, the queue
and the claims, and a switch that blinded the investigators along with the agents would be
thrown less readily for exactly that reason.

**Three scopes**, so the response can be proportionate:

| scope | value | halts |
|---|---|---|
| `global` | `""` | everything |
| `tenant` | tenant uuid | every mutation against one tenant's data |
| `principal` | token `sub` | one agent (or one person) — the revocation primitive |

**Three ways to throw it**, because the component that went wrong might be the one you
would otherwise use:

1. `POST /control/kill-switch` — the operator role, a stated reason, recorded.
2. `scripts/killswitch.py` — the owner DSN, for when the API itself is the problem.
3. `DRAWBRIDGE_KILL_SWITCH=engaged` — a deployment variable, for when the database is.

**It fails closed.** If the switch's state cannot be read, every mutation is refused. A
kill switch that reports "not engaged" whenever its own storage is unreachable is a switch
that works only when nothing is wrong.

**How fast.** The state is read at most once a second per process and cached for that
second (`CACHE_SECONDS`). A single indexed query per second is what every mutating request
in the process shares; the cost of "instant" is a one-second bound, and the bound is
stated rather than rounded to zero.

**Preventing the need for it.** The switch is the last control, not the first. The
anomaly circuit breaker in `services/agent/src/queue.py` throws the `principal` scope for
the drafter on its own when its output keeps failing the guards — a model that has started
producing ungrounded figures or echoing injected instructions stops itself before an
analyst has to notice. Only a human releases it.
"""

from __future__ import annotations

import os
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import Session

log = structlog.get_logger()

#: How long one read of the switch's state is trusted. See the module docstring.
CACHE_SECONDS = 1.0

#: The value of `DRAWBRIDGE_KILL_SWITCH` that halts everything without the database.
ENV_VAR = "DRAWBRIDGE_KILL_SWITCH"
ENGAGED = "engaged"

#: A reason shorter than this is refused, by the API and by the table's CHECK constraint.
MIN_REASON_CHARS = 20

#: Set for the duration of a unit of work that writes: a mutating API request, an MCP
#: write tool, a drafter pass. `tenancy.set_tenant` consults it, which makes the moment a
#: transaction is scoped to a tenant the moment a tenant-scoped halt is enforced — every
#: tenant write in the codebase already passes through that one function.
MUTATING: ContextVar[bool] = ContextVar("drawbridge_mutating", default=False)
PRINCIPAL: ContextVar[str | None] = ContextVar("drawbridge_halt_principal", default=None)


class ScopeKind(StrEnum):
    GLOBAL = "global"
    TENANT = "tenant"
    PRINCIPAL = "principal"


class KillSwitchEngagedError(RuntimeError):
    """A mutation refused because a switch covering it is engaged.

    Carries which scope caught it, so the refusal an agent logs says whether the whole
    system was stopped or just this caller — the difference between an outage and a
    revocation.
    """

    def __init__(self, scope: tuple[str, str]) -> None:
        self.scope = scope
        kind, value = scope
        where = "globally" if kind == ScopeKind.GLOBAL else f"for {kind} {value}"
        super().__init__(f"kill switch engaged {where}; mutations are halted")


@dataclass(frozen=True, slots=True)
class SwitchState:
    """The engaged scopes, as of one read."""

    engaged: frozenset[tuple[str, str]]
    read_ok: bool = True

    def covering(
        self, *, principal: str | None = None, tenant_id: UUID | str | None = None
    ) -> tuple[str, str] | None:
        """The first engaged scope that covers this caller and tenant, if any."""
        if not self.read_ok:
            return (ScopeKind.GLOBAL.value, "unreadable")
        candidates = [(ScopeKind.GLOBAL.value, "")]
        if principal:
            candidates.append((ScopeKind.PRINCIPAL.value, principal))
        if tenant_id is not None:
            candidates.append((ScopeKind.TENANT.value, str(tenant_id)))
        for candidate in candidates:
            if candidate in self.engaged:
                return candidate
        return None


_STATE_SQL = text("""
    SELECT scope_kind, scope_value
      FROM (
        SELECT DISTINCT ON (scope_kind, scope_value)
               scope_kind, scope_value, event_type
          FROM control_events
         WHERE event_type IN ('kill_switch_engaged', 'kill_switch_released')
         ORDER BY scope_kind, scope_value, event_id DESC
      ) latest
     WHERE event_type = 'kill_switch_engaged'
""")


def env_engaged() -> bool:
    """The deployment-level override, from the process environment or the settings file.

    Both, because `Settings.kill_switch` reads `.env` as well as the environment and an
    override set in one place and silently ignored in the other is a switch that is off
    while someone believes it is on.
    """
    if os.environ.get(ENV_VAR, "").strip().lower() == ENGAGED:
        return True
    from services.api.src.config import get_settings

    try:
        return get_settings().kill_switch.strip().lower() == ENGAGED
    except Exception:
        # Settings that cannot load stop the process at startup long before this runs; the
        # database-backed read below is what fails closed.
        return False


class _Cache:
    """One process's view of the switch, refreshed at most every `CACHE_SECONDS`."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: SwitchState | None = None
        self._read_at = 0.0

    def fresh(self) -> SwitchState | None:
        with self._lock:
            if self._state is not None and time.monotonic() - self._read_at < CACHE_SECONDS:
                return self._state
            return None

    def store(self, state: SwitchState) -> SwitchState:
        with self._lock:
            self._state = state
            self._read_at = time.monotonic()
        return state

    def clear(self) -> None:
        with self._lock:
            self._state = None
            self._read_at = 0.0


_cache = _Cache()


def invalidate() -> None:
    """Forget the cached state. Called after this process engages or releases a switch,
    so the process that threw it is never the one still running for the next second."""
    _cache.clear()


def _state_from_rows(rows: Any) -> SwitchState:
    return SwitchState(engaged=frozenset((str(k), str(v)) for k, v in rows))


def state(session: Session) -> SwitchState:
    """The switch's state, from the database, cached for one second. Never raises."""
    if env_engaged():
        return SwitchState(engaged=frozenset({(ScopeKind.GLOBAL.value, "")}))
    cached = _cache.fresh()
    if cached is not None:
        return cached
    try:
        rows = session.execute(_STATE_SQL).all()
    except Exception as exc:
        # Fail closed, and do not cache the failure: the next request should try again
        # rather than inherit a second of refusals from a transient error.
        log.error("killswitch.unreadable", error=type(exc).__name__)
        return SwitchState(engaged=frozenset(), read_ok=False)
    return _cache.store(_state_from_rows(rows))


async def state_async(session: AsyncSession) -> SwitchState:
    """`state`, over the API's async session."""
    if env_engaged():
        return SwitchState(engaged=frozenset({(ScopeKind.GLOBAL.value, "")}))
    cached = _cache.fresh()
    if cached is not None:
        return cached
    try:
        rows = (await session.execute(_STATE_SQL)).all()
    except Exception as exc:
        log.error("killswitch.unreadable", error=type(exc).__name__)
        return SwitchState(engaged=frozenset(), read_ok=False)
    return _cache.store(_state_from_rows(rows))


def assert_running(
    session: Session, *, principal: str | None = None, tenant_id: UUID | str | None = None
) -> None:
    """Raise if a switch covering this caller or tenant is engaged."""
    covering = state(session).covering(principal=principal, tenant_id=tenant_id)
    if covering is not None:
        raise KillSwitchEngagedError(covering)


_RECORD = text("""
    INSERT INTO control_events
        (event_type, scope_kind, scope_value, actor, actor_kind, reason, detail, trace_id)
    VALUES
        (:event_type, :scope_kind, :scope_value, :actor, :actor_kind, :reason,
         CAST(:detail AS jsonb), :trace_id)
    RETURNING event_id, recorded_at
""")


def _validate(scope_kind: str, scope_value: str, reason: str) -> tuple[str, str, str]:
    kind = ScopeKind(scope_kind)
    value = scope_value.strip()
    if kind is ScopeKind.GLOBAL:
        value = ""
    elif not value:
        msg = f"a {kind.value} switch needs a value naming what it covers"
        raise ValueError(msg)
    if kind is ScopeKind.TENANT:
        value = str(UUID(value))
    stated = (reason or "").strip()
    if len(stated) < MIN_REASON_CHARS:
        msg = (
            f"a kill-switch change requires a reason of at least {MIN_REASON_CHARS} "
            f"characters; got {len(stated)}. It is the first thing the incident review reads."
        )
        raise ValueError(msg)
    return kind.value, value, stated


def record_event(
    session: Session,
    *,
    event_type: str,
    actor: str,
    actor_kind: str,
    reason: str,
    scope_kind: str = ScopeKind.GLOBAL.value,
    scope_value: str = "",
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one row to `control_events`. The caller commits."""
    import json

    from services.api.src.telemetry import current_trace_id

    row = session.execute(
        _RECORD,
        {
            "event_type": event_type,
            "scope_kind": scope_kind,
            "scope_value": scope_value,
            "actor": actor[:128],
            "actor_kind": actor_kind,
            "reason": reason,
            "detail": json.dumps(detail or {}, default=str),
            "trace_id": current_trace_id(),
        },
    ).one()
    return {"event_id": int(row.event_id), "recorded_at": row.recorded_at.isoformat()}


def engage(
    session: Session,
    *,
    actor: str,
    actor_kind: str,
    reason: str,
    scope_kind: str = ScopeKind.GLOBAL.value,
    scope_value: str = "",
    event_type: str = "kill_switch_engaged",
) -> dict[str, Any]:
    """Throw a switch. The caller commits; the cache is dropped immediately."""
    kind, value, stated = _validate(scope_kind, scope_value, reason)
    out = record_event(
        session,
        event_type="kill_switch_engaged",
        actor=actor,
        actor_kind=actor_kind,
        reason=stated,
        scope_kind=kind,
        scope_value=value,
        detail={"via": event_type},
    )
    if event_type != "kill_switch_engaged":
        # A circuit breaker trip is recorded as what caused it *and* as the engagement it
        # produced, so "why did the drafter stop" is one row and "is it stopped" another.
        record_event(
            session,
            event_type=event_type,
            actor=actor,
            actor_kind=actor_kind,
            reason=stated,
            scope_kind=kind,
            scope_value=value,
        )
    invalidate()
    log.warning("killswitch.engaged", scope_kind=kind, scope_value=value, actor=actor)
    return {"engaged": True, "scope_kind": kind, "scope_value": value, **out}


def release(
    session: Session,
    *,
    actor: str,
    actor_kind: str,
    reason: str,
    scope_kind: str = ScopeKind.GLOBAL.value,
    scope_value: str = "",
) -> dict[str, Any]:
    """Release a switch. Humans only — enforced by the callers, recorded here."""
    kind, value, stated = _validate(scope_kind, scope_value, reason)
    if actor_kind == "machine":
        msg = "a machine identity may engage a kill switch and never release one"
        raise PermissionError(msg)
    out = record_event(
        session,
        event_type="kill_switch_released",
        actor=actor,
        actor_kind=actor_kind,
        reason=stated,
        scope_kind=kind,
        scope_value=value,
    )
    invalidate()
    log.warning("killswitch.released", scope_kind=kind, scope_value=value, actor=actor)
    return {"engaged": False, "scope_kind": kind, "scope_value": value, **out}


_HISTORY = text("""
    SELECT event_id, event_type, scope_kind, scope_value, actor, actor_kind, reason,
           recorded_at
      FROM control_events
     WHERE event_type IN ('kill_switch_engaged', 'kill_switch_released',
                          'circuit_breaker_tripped')
     ORDER BY event_id DESC
     LIMIT :limit
""")


def history(session: Session, limit: int = 50) -> list[dict[str, Any]]:
    rows = session.execute(_HISTORY, {"limit": limit}).mappings().all()
    return [{**dict(row), "recorded_at": row["recorded_at"].isoformat()} for row in rows]
