"""Filing identity, secret resolution, and the trace that crosses the MCP transport.

Three subjects that share one property: each is a place where a wrong answer is silent.
An IBAN with a transposed pair still looks like an IBAN. A secrets loader that ignores a
misspelled key starts the service with the value you were trying to replace. A trace that
does not join looks exactly like a trace that does until you go and read it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from pydantic import ValidationError

from drawbridge_schemas.jurisdiction import Jurisdiction
from drawbridge_schemas.tenant import TenantProfile, iban_checksum_ok
from services.api.src.config import Settings
from services.api.src.profiles import to_claimant
from services.api.src.secrets import (
    DEV_PLACEHOLDERS,
    GENERATED_FIELDS,
    SUPPLIED_FIELDS,
    AwsSecretsManagerProvider,
    FileSecretProvider,
    SecretsError,
    check_secret_posture,
    generate,
    inject_password,
    redact,
    resolve,
)

if TYPE_CHECKING:
    from pathlib import Path

# A structurally valid Saudi IBAN over a fictional account. Reused rather than repeated so
# that a change to it fails one place.
SA_IBAN = "SA0380000000608010167519"


def profile(**overrides: object) -> TenantProfile:
    base: dict[str, object] = {
        "tenant_id": uuid4(),
        "legal_name": "Northbridge Trading LLC",
        "address_line1": "4400 Harbor Scenic Drive",
        "city": "Long Beach",
        "country": "US",
    }
    return TenantProfile(**{**base, **overrides})  # type: ignore[arg-type]


class TestTheIdentifiersAreCheckedRatherThanStored:
    def test_an_ein_is_stored_without_its_hyphen(self) -> None:
        """Two spellings of one number is how a tenant acquires two identities."""
        assert profile(ein="95-4417293").ein == "954417293"
        assert profile(ein="954417293").ein == "954417293"

    def test_and_printed_with_it(self) -> None:
        assert profile(ein="954417293").ein_display == "95-4417293"

    def test_an_ein_of_the_wrong_length_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="9 digits"):
            profile(ein="95-441729")

    def test_a_transposed_iban_is_refused(self) -> None:
        """The error a human actually makes, and the one that moves money to a stranger.

        `iban_checksum_ok` is what catches it — the shape is still perfectly valid.
        """
        # Two adjacent digits swapped, well inside the account number. Same length, same
        # country, same character set — nothing but the check digits can catch it.
        transposed = "SA0308000000608010167519"
        assert sorted(transposed) == sorted(SA_IBAN)
        assert not iban_checksum_ok(transposed)
        with pytest.raises(ValidationError, match="mod-97"):
            profile(iban=transposed)

    def test_a_valid_iban_survives_the_way_a_bank_prints_it(self) -> None:
        spaced = " ".join(SA_IBAN[i : i + 4] for i in range(0, len(SA_IBAN), 4))
        assert profile(iban=spaced).iban == SA_IBAN

    def test_a_saudi_iban_of_the_wrong_length_is_refused(self) -> None:
        """Right shape and a checksum that genuinely validates — only the length is wrong.

        Worth its own case because the mod-97 check passes here: without the explicit
        length rule this number would be stored and paid to.
        """
        assert iban_checksum_ok("SA031234567890123456")
        with pytest.raises(ValidationError, match="24 characters"):
            profile(iban="SA031234567890123456")

    def test_a_broker_code_is_three_alphanumerics(self) -> None:
        assert profile(broker_code="j7k").broker_code == "J7K"
        # Four characters is caught by the field's max_length before the validator runs,
        # which is why this case asserts the refusal rather than the message.
        with pytest.raises(ValidationError):
            profile(broker_code="J7KL")
        with pytest.raises(ValidationError, match="three-character"):
            profile(broker_code="J7!")


class TestWhetherAProfileCanAddressAPacket:
    def test_a_us_profile_needs_an_ein(self) -> None:
        assert profile(ein="954417293").missing_for(Jurisdiction.US) == ()
        assert "ein" in profile().missing_for(Jurisdiction.US)

    def test_a_ksa_profile_needs_a_cr_number_and_an_iban(self) -> None:
        missing = profile().missing_for(Jurisdiction.KSA)
        assert "cr_number" in missing
        assert "iban" in missing

    def test_a_us_only_tenant_is_not_asked_for_a_cr_number(self) -> None:
        """Forcing one would put an invented number in a field ZATCA reads."""
        assert profile(ein="954417293").missing_for(Jurisdiction.US) == ()

    def test_a_broker_code_is_never_required(self) -> None:
        """A self-filer has none, and a placeholder there names a broker who does not exist."""
        assert "broker_code" not in profile(ein="954417293").missing_for(Jurisdiction.US)

    def test_require_for_names_every_missing_field_at_once(self) -> None:
        """One round trip per missing field is how onboarding takes a week."""
        with pytest.raises(ValueError, match="cr_number") as caught:
            profile(city="", iban=SA_IBAN).require_for(Jurisdiction.KSA)
        assert "city" in str(caught.value)

    def test_the_claimant_carries_the_jurisdiction_s_identifier(self) -> None:
        us = to_claimant(profile(ein="954417293"), Jurisdiction.US)
        assert us.identifier == "95-4417293"
        ksa = to_claimant(
            profile(cr_number="4030298871", iban=SA_IBAN, country="SA"), Jurisdiction.KSA
        )
        assert ksa.identifier == "4030298871"

    def test_a_packet_is_refused_before_it_is_printed(self) -> None:
        """A 7551 with an empty identifier box is not a draft; it is a rejected filing
        that somebody has already signed."""
        with pytest.raises(ValueError, match="missing ein"):
            to_claimant(profile(), Jurisdiction.US)


class TestWhereSecretsComeFrom:
    def test_the_file_is_read_and_its_keys_normalised(self, tmp_path: Path) -> None:
        path = tmp_path / "s.json"
        path.write_text(
            json.dumps({"_about": "a comment", "DRAWBRIDGE_JWT_SECRET": "x" * 40}),
            encoding="utf-8",
        )
        assert FileSecretProvider(path).load() == {"jwt_secret": "x" * 40}

    def test_an_absent_file_is_not_an_error(self, tmp_path: Path) -> None:
        """A test run and a fresh checkout both have no secrets file."""
        assert FileSecretProvider(tmp_path / "nope.json").load() == {}

    def test_an_unknown_key_is_refused(self, tmp_path: Path) -> None:
        """The common cause is a typo, and a typo'd secret name is indistinguishable at
        runtime from a secret nobody set."""
        path = tmp_path / "s.json"
        path.write_text(json.dumps({"jwt_secrets": "x" * 40}), encoding="utf-8")
        with pytest.raises(SecretsError, match="unknown secret"):
            FileSecretProvider(path).load()

    def test_malformed_json_names_the_line(self, tmp_path: Path) -> None:
        path = tmp_path / "s.json"
        path.write_text('{"jwt_secret": }', encoding="utf-8")
        with pytest.raises(SecretsError, match="not valid JSON"):
            FileSecretProvider(path).load()

    def test_the_environment_wins_over_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An orchestrator injecting a value must be able to override a stale file."""
        path = tmp_path / "s.json"
        path.write_text(json.dumps({"jwt_secret": "from-file" + "!" * 30}), encoding="utf-8")
        monkeypatch.setenv("DRAWBRIDGE_SECRETS_FILE", str(path))
        monkeypatch.setenv("DRAWBRIDGE_JWT_SECRET", "from-env" + "!" * 30)
        assert resolve()["jwt_secret"].startswith("from-env")

    def test_the_aws_provider_refuses_instead_of_returning_nothing(self) -> None:
        """Returning empty would let a deployment start with every secret missing and
        fail at the first request instead of at startup.

        Week 13 asserted this against a provider that was a deliberate seam. Week 14
        implemented it, and the assertion is unchanged in substance: with no region and no
        credentials configured, botocore raises before it reaches the network, and what
        comes out of this module is still a `SecretsError` and still not `{}`.
        """
        with pytest.raises(SecretsError, match="could not return"):
            AwsSecretsManagerProvider("drawbridge/prod").load()


class TestWhatTheGeneratorWillAndWillNotMint:
    def test_every_generated_secret_clears_the_hmac_floor(self) -> None:
        """Under 32 bytes and PyJWT warns; a warning people learn to ignore is how a short
        key survives to production."""
        assert all(len(value) >= 32 for value in generate().values())

    def test_it_does_not_invent_an_anthropic_key_or_a_service_token(self) -> None:
        """A random string in either field reports a configured credential and fails at
        the first request that uses it — an API 401 and an n8n 401 respectively."""
        minted = generate()
        assert SUPPLIED_FIELDS.isdisjoint(minted)
        assert set(minted) >= GENERATED_FIELDS

    def test_the_offboard_key_is_hex(self) -> None:
        """It is an Ed25519 seed parsed with `bytes.fromhex`. A URL-safe token here fails
        at the moment a tenant is offboarded, which is when the signature is the point."""
        bytes.fromhex(generate()["offboard_signing_key"])

    def test_two_runs_do_not_agree(self) -> None:
        assert generate()["jwt_secret"] != generate()["jwt_secret"]


class TestTheStartupPostureCheck:
    def test_production_refuses_a_placeholder(self) -> None:
        with pytest.raises(SecretsError, match="refusing to start"):
            check_secret_posture(
                Settings(environment="production", jwt_secret="dev-only-change-me")
            )

    def test_development_allows_it(self) -> None:
        """Otherwise the test suite and `make token` stop working, and the secure path
        becomes the one people disable."""
        check_secret_posture(Settings(environment="development", jwt_secret="dev-only-change-me"))

    def test_a_password_left_inline_in_the_dsn_is_caught(self) -> None:
        """Moving the password out of its own variable and leaving it in the DSN has not
        moved it."""
        with pytest.raises(SecretsError, match="password inline"):
            check_secret_posture(
                Settings(
                    environment="production",
                    database_url="postgresql+asyncpg://drawbridge_app:drawbridge@h:5432/d",
                )
            )

    def test_a_real_secret_passes(self) -> None:
        check_secret_posture(
            Settings(
                environment="production",
                jwt_secret="Xk9" + "q" * 40,
                database_url="postgresql+asyncpg://drawbridge_app:Zt4qq@h:5432/d",
                # Explicit, so the test does not pass only on a machine whose .secrets.json
                # happens to supply a real one — it failed the first CI run that reached it.
                s3_secret_key="Rk7" + "w" * 30,
            )
        )

    def test_every_placeholder_is_a_value_somebody_actually_shipped(self) -> None:
        """Guarding the list itself: a substring rule here would refuse real passwords."""
        assert "dev-only-change-me" in DEV_PLACEHOLDERS
        assert all(" " not in value for value in DEV_PLACEHOLDERS)


class TestThePasswordGoesIntoTheDsn:
    def test_a_dsn_without_one_receives_it(self) -> None:
        assert inject_password("postgresql+asyncpg://app@h:5432/d", "pw") == (
            "postgresql+asyncpg://app:pw@h:5432/d"
        )

    def test_an_explicit_inline_password_is_left_alone(self) -> None:
        """Somebody wrote it there on purpose; silently overriding it is the more
        surprising behaviour."""
        dsn = "postgresql+asyncpg://app:already@h:5432/d"
        assert inject_password(dsn, "pw") == dsn

    def test_no_password_is_a_no_op(self) -> None:
        dsn = "postgresql+asyncpg://app@h:5432/d"
        assert inject_password(dsn, None) == dsn

    def test_settings_applies_it_to_both_drivers(self) -> None:
        settings = Settings(
            database_url="postgresql+asyncpg://drawbridge_app@postgres:5432/drawbridge",
            app_db_password="resolved-pw",
        )
        assert ":resolved-pw@" in settings.database_url
        assert ":resolved-pw@" in settings.sync_database_url


class TestRedaction:
    def test_it_does_not_reverse(self) -> None:
        secret = "s3cret-value-nobody-should-see"
        assert secret not in redact(secret)

    def test_it_tells_two_secrets_apart(self) -> None:
        """The question it exists to answer is "which key is this service using"."""
        assert redact("aaaa" + "x" * 30) != redact("bbbb" + "x" * 30)

    def test_a_short_secret_shows_only_its_length(self) -> None:
        """A four-character prefix of a six-character secret is most of it."""
        assert redact("abc123") == "<6 chars>"

    def test_an_unset_secret_is_not_confused_with_an_empty_one(self) -> None:
        assert redact(None) == "<unset>"
        assert redact("") == "<unset>"
