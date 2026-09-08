"""Offboarding — export, sign, tombstone; and what each step refuses.

The archive step writes to MinIO and is the one part of `scripts/tenant_offboard.py` these
tests do not exercise: `build_artifact` produces the bytes and the signature, `tombstone`
writes the row, and `offboard(..., dry_run=True)` runs the whole procedure up to the point
where it would put an object. Covering the S3 call would mean asserting that boto3 works.

What is covered is everything that could silently produce a bad record: a chain exported
incompletely, a signature that verifies over content it does not cover, a tenant marked
gone with no artifact behind it, and a second offboarding overwriting the first.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import pairwise
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from scripts.tenant_offboard import (
    OffboardError,
    build_artifact,
    canonical,
    check_offboardable,
    offboard,
    tombstone,
    verify_artifact,
)
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from services.api.src.ledger import record, verify_chain
from services.api.src.models import Claim, Tenant

if TYPE_CHECKING:
    pass

# A fixed seed, so the signature over a fixed chain is reproducible within a run. Not a
# secret: the real one lives in the secret manager and never comes near this repository.
TEST_SEED = bytes(range(32))
NOW = datetime(2026, 3, 1, 9, 30, tzinfo=UTC)


@pytest.fixture
def key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(TEST_SEED)


@pytest.fixture
def departing(session: Session) -> Tenant:
    """A tenant with a finished claim and a short ledger chain."""
    row = Tenant(tenant_id=uuid4(), name="Departing Importer", default_jurisdiction="us")
    session.add(row)
    session.flush()

    claim = Claim(
        claim_id=uuid4(),
        tenant_id=row.tenant_id,
        state="filed",
        jurisdiction="us",
        currency="USD",
        lane="drawback",
        drawback_type="unused_substitution",
        period_start=date(2023, 1, 1),
        period_end=date(2023, 12, 31),
        filing_deadline=date(2026, 1, 9),
        absolute_bar_date=date(2026, 1, 9),
        total_refund=Decimal("25092.14"),
    )
    session.add(claim)
    session.flush()

    for event, subject in (
        ("document_ingested", "cbp_7501"),
        ("claim_persisted", "claim"),
        ("claim_transition", "filed"),
    ):
        record(
            session,
            tenant_id=row.tenant_id,
            event_type=event,
            actor="pipeline",
            claim_id=claim.claim_id,
            subject=subject,
        )
    session.flush()
    return row


def _lines(artifact_body: bytes) -> list[dict]:
    return [json.loads(line) for line in artifact_body.splitlines()]


class TestTheExportIsTheChain:
    def test_every_entry_is_present_in_sequence_order(
        self, session: Session, departing: Tenant, key: Ed25519PrivateKey
    ) -> None:
        artifact = build_artifact(session, departing, key=key, now=NOW)
        rows = _lines(artifact.body)

        assert len(rows) == 3
        assert [r["sequence"] for r in rows] == sorted(r["sequence"] for r in rows)
        assert [r["event_type"] for r in rows] == [
            "document_ingested",
            "claim_persisted",
            "claim_transition",
        ]

    def test_the_hashes_travel_with_the_rows(
        self, session: Session, departing: Tenant, key: Ed25519PrivateKey
    ) -> None:
        """Without `prev_hash` and `entry_hash` the artifact is a report, not evidence.

        The point of exporting them is that an auditor can recompute the chain from the
        file alone, years later, without our database and without trusting it.
        """
        rows = _lines(build_artifact(session, departing, key=key, now=NOW).body)
        assert rows[0]["prev_hash"] is None
        for previous, current in pairwise(rows):
            assert current["prev_hash"] == previous["entry_hash"]

    def test_the_manifest_records_the_chain_verdict_at_the_moment_of_export(
        self, session: Session, departing: Tenant, key: Ed25519PrivateKey
    ) -> None:
        manifest = build_artifact(session, departing, key=key, now=NOW).manifest
        assert manifest["chain"] == verify_chain(session, departing.tenant_id)
        assert manifest["chain"]["ok"] is True
        assert manifest["entries"] == 3
        assert manifest["tenant_id"] == str(departing.tenant_id)
        assert str(departing.tenant_id) in manifest["artifact_key"]

    def test_a_tenant_with_no_ledger_exports_an_empty_body_and_says_so(
        self, session: Session, key: Ed25519PrivateKey
    ) -> None:
        """An empty chain verifies, which is exactly the case week 10 flagged as
        undetectable from the inside. The manifest states the count rather than leaving
        an empty file to be read as a missing one."""
        fresh = Tenant(tenant_id=uuid4(), name="Never Traded", default_jurisdiction="us")
        session.add(fresh)
        session.flush()

        artifact = build_artifact(session, fresh, key=key, now=NOW)
        assert artifact.body == b""
        assert artifact.manifest["entries"] == 0
        assert artifact.manifest["first_sequence"] is None


class TestTheSignature:
    def test_it_verifies_over_the_manifest_and_the_body(
        self, session: Session, departing: Tenant, key: Ed25519PrivateKey
    ) -> None:
        artifact = build_artifact(session, departing, key=key, now=NOW)
        assert verify_artifact(artifact.manifest, artifact.body, artifact.signature)

    def test_an_altered_body_fails_even_though_the_signature_is_genuine(
        self, session: Session, departing: Tenant, key: Ed25519PrivateKey
    ) -> None:
        """The signature covers the manifest, and the manifest commits to the body digest.

        This is the attack the two-part structure exists for: the signature on the
        manifest is real and checks out, and the file it points at has been edited.
        """
        artifact = build_artifact(session, departing, key=key, now=NOW)
        tampered = artifact.body.replace(b'"pipeline"', b'"analyst"', 1)
        assert tampered != artifact.body
        assert not verify_artifact(artifact.manifest, tampered, artifact.signature)

    def test_a_manifest_edited_after_signing_fails(
        self, session: Session, departing: Tenant, key: Ed25519PrivateKey
    ) -> None:
        artifact = build_artifact(session, departing, key=key, now=NOW)
        forged = {**artifact.manifest, "entries": 99}
        assert not verify_artifact(forged, artifact.body, artifact.signature)

    def test_another_key_does_not_verify(
        self, session: Session, departing: Tenant, key: Ed25519PrivateKey
    ) -> None:
        artifact = build_artifact(session, departing, key=key, now=NOW)
        other = Ed25519PrivateKey.generate()
        forged = {
            **artifact.manifest,
            "public_key": other.public_key().public_bytes_raw().hex(),
        }
        assert not verify_artifact(forged, artifact.body, artifact.signature)

    def test_the_canonical_form_is_stable_across_key_order(self) -> None:
        """The signature is over bytes, so two dicts that mean the same thing must
        serialise the same way or verification depends on dict insertion order."""
        assert canonical({"b": 1, "a": 2}) == canonical({"a": 2, "b": 1})


class TestWhatItRefusesToDo:
    def test_a_tenant_with_claims_in_flight_is_refused(
        self, session: Session, departing: Tenant
    ) -> None:
        """Archiving a chain that is still growing produces an artifact that is wrong the
        moment it is signed."""
        session.add(
            Claim(
                claim_id=uuid4(),
                tenant_id=departing.tenant_id,
                state="analyst_review",
                jurisdiction="us",
                currency="USD",
                lane="drawback",
                drawback_type="unused_substitution",
                period_start=date(2024, 1, 1),
                period_end=date(2024, 12, 31),
                filing_deadline=date(2027, 1, 1),
                absolute_bar_date=date(2027, 1, 1),
                total_refund=Decimal("100.00"),
            )
        )
        session.flush()

        with pytest.raises(OffboardError, match="still in flight"):
            check_offboardable(session, departing, force=False)

        warnings = check_offboardable(session, departing, force=True)
        assert any("analyst_review" in warning for warning in warnings)

    def test_a_second_offboarding_is_refused_even_with_force(
        self, session: Session, departing: Tenant, key: Ed25519PrivateKey
    ) -> None:
        """Two signatures over one chain and no way to say which one the tenant left
        under. This is the one precondition `--force` does not override."""
        artifact = build_artifact(session, departing, key=key, now=NOW)
        tombstone(session, departing, artifact, now=NOW)

        with pytest.raises(OffboardError, match="already offboarded"):
            check_offboardable(session, departing, force=True)

    def test_an_unknown_tenant_is_refused(self, session: Session, key: Ed25519PrivateKey) -> None:
        with pytest.raises(OffboardError, match="no tenant"):
            offboard(session, uuid4(), key=key, bucket="drawbridge-archive", dry_run=True)

    def test_a_tombstone_without_an_artifact_is_refused_by_the_database(
        self, session: Session, departing: Tenant
    ) -> None:
        """The CHECK constraint, which is what stops the sequence being done by hand.

        A tenant marked gone whose ledger was never exported is the single state this
        whole mechanism exists to prevent, so it is refused below the application.
        """
        # In a savepoint, so the aborted transaction does not take the outer fixture's
        # rollback with it.
        with (
            pytest.raises(IntegrityError, match="ck_tenant_offboard_is_evidenced"),
            session.begin_nested(),
        ):
            session.execute(
                text("UPDATE tenants SET offboarded_at = :now WHERE tenant_id = :t"),
                {"now": datetime.now(UTC), "t": departing.tenant_id},
            )


class TestTheTombstone:
    def test_it_records_what_was_archived_and_leaves_the_rows_alone(
        self, session: Session, departing: Tenant, key: Ed25519PrivateKey
    ) -> None:
        before = session.execute(
            text("SELECT count(*) FROM audit_ledger WHERE tenant_id = :t"),
            {"t": departing.tenant_id},
        ).scalar_one()

        artifact = build_artifact(session, departing, key=key, now=NOW)
        tombstone(session, departing, artifact, now=NOW)

        assert departing.offboarded_at == NOW
        assert departing.offboard_artifact_key == artifact.key
        assert departing.offboard_signature == artifact.signature
        assert departing.offboard_public_key == artifact.public_key

        after = session.execute(
            text("SELECT count(*) FROM audit_ledger WHERE tenant_id = :t"),
            {"t": departing.tenant_id},
        ).scalar_one()
        assert after == before, "offboarding must not remove a single ledger row"

    def test_a_dry_run_signs_and_writes_nothing(
        self, session: Session, departing: Tenant, key: Ed25519PrivateKey
    ) -> None:
        artifact = offboard(
            session,
            departing.tenant_id,
            key=key,
            bucket="drawbridge-archive",
            dry_run=True,
            now=NOW,
        )
        assert verify_artifact(artifact.manifest, artifact.body, artifact.signature)
        assert departing.offboarded_at is None


class TestTheKeyIsNotInvented:
    def test_a_missing_signing_key_is_an_error_rather_than_a_generated_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A signature made with a key that lived for one process proves nothing."""
        from scripts.tenant_offboard import SIGNING_KEY_ENV, load_signing_key

        monkeypatch.delenv(SIGNING_KEY_ENV, raising=False)
        with pytest.raises(OffboardError, match="is not set"):
            load_signing_key()

    def test_a_malformed_key_is_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from scripts.tenant_offboard import SIGNING_KEY_ENV, load_signing_key

        monkeypatch.setenv(SIGNING_KEY_ENV, "not-hex")
        with pytest.raises(OffboardError, match="hex seed"):
            load_signing_key()

    def test_a_valid_seed_loads(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from scripts.tenant_offboard import SIGNING_KEY_ENV, load_signing_key

        monkeypatch.setenv(SIGNING_KEY_ENV, TEST_SEED.hex())
        assert isinstance(load_signing_key(), Ed25519PrivateKey)
