"""The kill switch against the real API, on the unprivileged role.

What has to be true for "a human can shut the system down instantly" to mean anything:
the switch stops writes at every scope it claims to, it leaves reads alone so an incident
can be investigated, only an operator can move it, its history cannot be rewritten, and
the credential exchange stops issuing to a halted agent. Each is asserted here.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from services.api.src import killswitch
from tests.security.conftest import (
    PIPELINE_SECRET,
    make_world,
    release_all,
    requires_db,
    token,
)

pytest_plugins = ("tests.integration.conftest",)
pytestmark = requires_db

REASON = "incident drill: halting mutations while the ledger is inspected"


@pytest.fixture(autouse=True)
def _switches_released(owner: Session) -> Iterator[None]:
    """No test inherits a halt, or leaves one behind."""
    release_all(owner)
    yield
    release_all(owner)


@pytest.fixture
def world(owner: Session) -> dict[str, Any]:
    return make_world(owner)


def _operator() -> dict[str, str]:
    from uuid import uuid4

    return token(uuid4(), subject="ops@drawbridge.example", roles=("operator",))


def _engage(api: TestClient, scope_kind: str = "global", scope_value: str = "") -> None:
    response = api.post(
        "/control/kill-switch",
        json={
            "action": "engage",
            "scope_kind": scope_kind,
            "scope_value": scope_value,
            "reason": REASON,
        },
        headers=_operator(),
    )
    assert response.status_code == 200, response.text


def _transition(api: TestClient, world: dict[str, Any], headers: dict[str, str]) -> int:
    return api.post(
        "/claims/transition",
        json={"claim_id": str(world["claim"]), "to_state": "matching", "reason": "re-run"},
        headers=headers,
    ).status_code


class TestWhatItStops:
    def test_a_global_halt_stops_writes_and_not_reads(
        self, api: TestClient, world: dict[str, Any]
    ) -> None:
        user = token(world["tenant"])
        _engage(api)
        assert _transition(api, world, user) == 503
        assert api.get(f"/claims/{world['claim']}", headers=user).status_code == 200

    def test_releasing_it_restores_writes(self, api: TestClient, world: dict[str, Any]) -> None:
        user = token(world["tenant"])
        _engage(api)
        response = api.post(
            "/control/kill-switch",
            json={"action": "release", "reason": REASON},
            headers=_operator(),
        )
        assert response.status_code == 200
        assert _transition(api, world, user) != 503

    def test_a_principal_halt_stops_that_agent_only(
        self, api: TestClient, world: dict[str, Any]
    ) -> None:
        _engage(api, "principal", "agent:n8n-pipeline")
        assert _transition(api, world, token(agent="agent:n8n-pipeline")) == 503
        assert _transition(api, world, token(world["tenant"])) != 503

    def test_a_tenant_halt_stops_that_tenant_only(
        self, api: TestClient, world: dict[str, Any], owner: Session
    ) -> None:
        other = make_world(owner)
        _engage(api, "tenant", str(world["tenant"]))
        assert _transition(api, world, token(world["tenant"])) == 503
        assert _transition(api, other, token(other["tenant"])) != 503

    def test_the_credential_exchange_stops_issuing_to_a_halted_agent(self, api: TestClient) -> None:
        body = {
            "grant_type": "client_credentials",
            "client_id": "agent:n8n-pipeline",
            "client_secret": PIPELINE_SECRET,
        }
        assert api.post("/auth/token", data=body).status_code == 200
        _engage(api, "principal", "agent:n8n-pipeline")
        assert api.post("/auth/token", data=body).status_code == 503


class TestWhoMayMoveIt:
    def test_an_analyst_may_not(self, api: TestClient, world: dict[str, Any]) -> None:
        response = api.post(
            "/control/kill-switch",
            json={"action": "engage", "reason": REASON},
            headers=token(world["tenant"], roles=("analyst",)),
        )
        assert response.status_code == 403

    def test_the_pipeline_may_not(self, api: TestClient) -> None:
        response = api.post(
            "/control/kill-switch",
            json={"action": "engage", "reason": REASON},
            headers=token(agent="agent:n8n-pipeline"),
        )
        assert response.status_code == 403

    def test_a_reason_is_required(self, api: TestClient) -> None:
        response = api.post(
            "/control/kill-switch",
            json={"action": "engage", "reason": "because"},
            headers=_operator(),
        )
        assert response.status_code == 422

    def test_the_operator_can_read_the_history(self, api: TestClient) -> None:
        _engage(api)
        response = api.get("/control/kill-switch", headers=_operator())
        assert response.status_code == 200
        body = response.json()
        assert {"scope_kind": "global", "scope_value": ""} in body["engaged"]
        assert body["history"][0]["actor"] == "ops@drawbridge.example"
        assert body["history"][0]["reason"] == REASON


class TestTheRecord:
    @pytest.mark.parametrize(
        "statement",
        [
            "UPDATE control_events SET reason = 'x'",
            "DELETE FROM control_events",
            "TRUNCATE control_events",
        ],
    )
    def test_it_is_append_only(self, owner: Session, statement: str) -> None:
        killswitch.engage(owner, actor="t", actor_kind="local", reason=REASON)
        owner.commit()
        with pytest.raises(DBAPIError, match="append-only"):
            owner.execute(text(statement))
        owner.rollback()

    def test_the_exchange_records_every_token_it_issues(
        self, api: TestClient, owner: Session
    ) -> None:
        before = owner.execute(
            text("SELECT count(*) FROM control_events WHERE event_type = 'token_issued'")
        ).scalar_one()
        response = api.post(
            "/auth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "agent:n8n-pipeline",
                "client_secret": PIPELINE_SECRET,
            },
        )
        assert response.status_code == 200
        assert response.json()["expires_in"] <= 900
        after = owner.execute(
            text("SELECT count(*) FROM control_events WHERE event_type = 'token_issued'")
        ).scalar_one()
        assert after == before + 1


class TestTheExchange:
    @pytest.mark.parametrize(
        ("client_id", "secret"),
        [
            ("agent:n8n-pipeline", "wrong-secret"),
            ("agent:unknown", PIPELINE_SECRET),
            ("agent:memo-drafter", PIPELINE_SECRET),
            ("agent:n8n-pipeline", ""),
        ],
    )
    def test_every_failure_looks_the_same(
        self, api: TestClient, client_id: str, secret: str
    ) -> None:
        response = api.post(
            "/auth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": secret,
            },
        )
        assert response.status_code == 401
        assert response.json() == {"detail": {"error": "invalid_client"}}

    def test_an_issued_token_works_and_is_the_agent(
        self, api: TestClient, world: dict[str, Any]
    ) -> None:
        issued = api.post(
            "/auth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "agent:n8n-pipeline",
                "client_secret": PIPELINE_SECRET,
            },
        ).json()
        headers = {"Authorization": f"Bearer {issued['access_token']}"}
        assert api.get(f"/claims/{world['claim']}", headers=headers).status_code == 200

    def test_it_will_not_issue_what_the_agent_is_not_registered_for(self, api: TestClient) -> None:
        issued = api.post(
            "/auth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "agent:n8n-pipeline",
                "client_secret": PIPELINE_SECRET,
                "scope": "claims:release control:kill claims:read",
            },
        ).json()
        assert issued["scope"] == "claims:read"
