"""The operator's controls: read the kill switch, throw it, release it.

Engaging and releasing are the operator role's alone (`control:kill`), and a machine
identity can hold neither through this route — the registry refuses to register one with
the scope. The drafter's circuit breaker engages its own principal scope in-process
(`services/agent/src/queue.py`); nothing releases a switch except a person.

The route is exempt from the switch it controls (`guard.HALT_EXEMPT_PATHS`). Otherwise the
first global engagement would be the last thing the API could be told.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from drawbridge_schemas.agents import Scope
from services.api.src import gates, killswitch
from services.api.src.auth import require
from services.api.src.sync_db import in_thread

router = APIRouter(prefix="/control", tags=["control"])


class SwitchChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["engage", "release"]
    scope_kind: Literal["global", "tenant", "principal"] = "global"
    scope_value: str = ""
    reason: Annotated[str, Field(min_length=killswitch.MIN_REASON_CHARS, max_length=2000)]


@router.get("/kill-switch", dependencies=[Depends(require(Scope.CONTROL_READ))])
async def status_of_switch(limit: Annotated[int, Field(ge=1, le=200)] = 20) -> dict[str, Any]:
    """What is engaged right now, and the recent history of who changed it and why."""

    def _read(session: Session) -> dict[str, Any]:
        killswitch.invalidate()
        current = killswitch.state(session)
        return {
            "readable": current.read_ok,
            "env_override": killswitch.env_engaged(),
            "engaged": [
                {"scope_kind": kind, "scope_value": value}
                for kind, value in sorted(current.engaged)
            ],
            "history": killswitch.history(session, limit),
        }

    return await in_thread(_read)


@router.post("/kill-switch", dependencies=[Depends(require(Scope.CONTROL_KILL))])
async def change_switch(body: SwitchChange) -> dict[str, Any]:
    """Engage or release one scope. Recorded append-only with the operator's identity."""
    actor = gates.current_actor()
    if actor.kind is gates.ActorKind.MACHINE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "forbidden", "message": "the kill switch is operated by a person"},
        )

    def _apply(session: Session) -> dict[str, Any]:
        change = killswitch.engage if body.action == "engage" else killswitch.release
        return change(
            session,
            actor=actor.name,
            actor_kind=actor.kind.value,
            reason=body.reason,
            scope_kind=body.scope_kind,
            scope_value=body.scope_value,
        )

    try:
        return await in_thread(_apply)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error": "invalid_change", "message": str(exc)},
        ) from exc
