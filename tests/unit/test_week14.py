"""Secrets managers, the preparer notice, and the CROSS fetcher.

Three subjects that share a shape: each replaces something that worked on a laptop with
something that has to work in a deployment, and in each case the laptop version failed
quietly rather than loudly. A file backend that silently backstops an unreachable Vault
starts the service on stale plaintext. A preparer notice that substitutes a name and keeps
our disclaimer prints a false statement about a licence on a federal form. A ruling scorer
calibrated on three fixture rows returns nothing at all against real ones — and returning
nothing is what `search_rulings` did for eleven weeks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from scripts.ingest_cross import clean_body
from services.api.src.secrets import (
    BACKENDS,
    SECRET_FIELDS,
    AwsSecretsManagerProvider,
    SecretsError,
    VaultSecretProvider,
    backend_name,
    default_providers,
)
from services.packager.src.branding import DEFAULT_PREPARER, Preparer

# --------------------------------------------------------------------------- vault


def _vault_client(handler: Any) -> httpx.Client:
    """A Vault whose responses this test controls, over a real httpx transport.

    `MockTransport` rather than monkeypatching `httpx.Client.request`: the provider builds
    its own requests, and a test that stubs the method never checks that the URL, the
    header and the body were right. Here they arrive at the handler.
    """
    return httpx.Client(base_url="http://vault.test", transport=httpx.MockTransport(handler))


class TestTheVaultProvider:
    def test_it_logs_in_with_the_approle_and_reads_the_kv_v2_envelope(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/auth/approle/login":
                seen["login"] = request.read().decode()
                return httpx.Response(200, json={"auth": {"client_token": "s.issued"}})
            seen["read_token"] = request.headers.get("X-Vault-Token")
            seen["read_path"] = request.url.path
            return httpx.Response(
                200,
                json={"data": {"data": {"jwt_secret": "x" * 40}, "metadata": {"version": 3}}},
            )

        provider = VaultSecretProvider(
            address="http://vault.test",
            path="drawbridge",
            role_id="role",
            secret_id="secret",
            client=_vault_client(handler),
        )
        assert provider.load() == {"jwt_secret": "x" * 40}
        # The token used for the read is the one Vault issued, not the AppRole halves.
        assert seen["read_token"] == "s.issued"
        assert seen["read_path"] == "/v1/secret/data/drawbridge"
        assert "role" in seen["login"]

    def test_it_reads_data_data_and_not_the_version_envelope(self) -> None:
        """KV v2 wraps the secret. Reading the outer object yields keys `data` and
        `metadata`, which `_normalise_keys` rejects — the right failure, diagnosed wrong."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": {"jwt_secret": "x" * 40}})

        provider = VaultSecretProvider(
            address="http://vault.test", token="s.root", client=_vault_client(handler)
        )
        with pytest.raises(SecretsError, match="not a KV v2 secret"):
            provider.load()

    def test_it_refuses_without_credentials_rather_than_reading_anonymously(self) -> None:
        provider = VaultSecretProvider(address="http://vault.test")
        with pytest.raises(SecretsError, match="needs credentials"):
            provider.load()

    def test_a_missing_path_names_the_path_and_not_vaults_error_body(self) -> None:
        """Vault's 403 and 404 bodies name policies and paths. The status is enough."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"errors": ["policy 'topsecret-prod' denied"]})

        provider = VaultSecretProvider(
            address="http://vault.test", token="s.root", client=_vault_client(handler)
        )
        with pytest.raises(SecretsError) as caught:
            provider.load()
        assert "topsecret-prod" not in str(caught.value)

    def test_an_unknown_key_in_vault_is_an_error_not_a_shrug(self) -> None:
        """Same rule as the file backend. A typo'd secret name is indistinguishable at
        runtime from a secret nobody set."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": {"data": {"jwt_secrit": "x" * 40}}})

        provider = VaultSecretProvider(
            address="http://vault.test", token="s.root", client=_vault_client(handler)
        )
        with pytest.raises(SecretsError, match="unknown secret"):
            provider.load()


# ----------------------------------------------------------------------------- aws


class _FakeSecretsManager:
    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.asked: list[str] = []

    def get_secret_value(self, *, SecretId: str) -> Any:  # noqa: N803 - botocore's spelling
        self.asked.append(SecretId)
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class TestTheAwsProvider:
    def test_it_parses_one_json_object_out_of_one_call(self) -> None:
        client = _FakeSecretsManager({"SecretString": '{"jwt_secret": "%s"}' % ("x" * 40)})
        provider = AwsSecretsManagerProvider("drawbridge/prod", client=client)
        assert provider.load() == {"jwt_secret": "x" * 40}
        assert client.asked == ["drawbridge/prod"]

    def test_a_binary_secret_is_refused_rather_than_guessed_at(self) -> None:
        client = _FakeSecretsManager({"SecretBinary": b"\x00\x01"})
        with pytest.raises(SecretsError, match="binary"):
            AwsSecretsManagerProvider("drawbridge/prod", client=client).load()

    def test_every_botocore_failure_becomes_one_secrets_error(self) -> None:
        """Not found, access denied and an expired instance role are one situation here:
        the secret did not arrive. The original is kept in the exception chain."""
        client = _FakeSecretsManager(RuntimeError("AccessDeniedException"))
        with pytest.raises(SecretsError, match="could not return") as caught:
            AwsSecretsManagerProvider("drawbridge/prod", client=client).load()
        assert isinstance(caught.value.__cause__, RuntimeError)


# ------------------------------------------------------------------ backend choice


class TestChoosingABackend:
    def test_the_default_is_the_file_so_the_suite_runs_without_a_manager(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DRAWBRIDGE_SECRETS_PROVIDER", raising=False)
        assert backend_name() == "file"

    def test_a_misspelled_backend_is_fatal_rather_than_a_fallback_to_the_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`PROVIDER=valut` reading the local file would produce a deployment that starts,
        works, and is not using the manager anyone believes it is using."""
        monkeypatch.setenv("DRAWBRIDGE_SECRETS_PROVIDER", "valut")
        with pytest.raises(SecretsError, match="unknown DRAWBRIDGE_SECRETS_PROVIDER"):
            backend_name()

    def test_naming_a_manager_removes_the_file_from_the_chain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The point of the whole exercise. A chain that falls back to disk when Vault is
        unreachable starts on whatever stale plaintext was last checked out."""
        monkeypatch.setenv("DRAWBRIDGE_SECRETS_PROVIDER", "vault")
        monkeypatch.setenv("VAULT_ADDR", "http://vault.test")
        describes = [provider.describe for provider in default_providers()]
        assert describes[0] == "environment"
        assert len(describes) == 2
        assert not any("file:" in d for d in describes)

    def test_vault_without_an_address_refuses_at_startup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DRAWBRIDGE_SECRETS_PROVIDER", "vault")
        monkeypatch.delenv("VAULT_ADDR", raising=False)
        with pytest.raises(SecretsError, match="VAULT_ADDR is unset"):
            default_providers()

    def test_every_backend_is_reachable_by_name(self) -> None:
        assert set(BACKENDS) == {"file", "vault", "aws"}


# ------------------------------------------------------------------- white label


class TestThePreparerNotice:
    def test_the_default_is_unchanged_so_existing_packets_reproduce(self) -> None:
        """Byte-for-byte what `PREPARER_NOTICE` has been since week 4. The golden claims
        reproduce to the cent and the documents reproduce to the character."""
        assert DEFAULT_PREPARER.notice == (
            "Prepared by Drawbridge for filing by a licensed customs broker. Drawbridge "
            "is not a customs broker and does not transmit to CBP. Every figure below "
            "traces to a source-document span retained under 19 CFR 163; the supporting "
            "schedule accompanies this form."
        )

    def test_a_broker_gets_a_notice_that_is_true_of_a_broker(self) -> None:
        """Substituting the name and keeping our disclaimer would print a false statement
        about the preparer's own licence on a form filed with CBP."""
        notice = Preparer(
            name="Harborline Customs Brokers, Inc.",
            is_licensed_broker=True,
            filer_code="J7K",
        ).notice
        assert "is not a customs broker" not in notice
        assert "is a licensed customs broker (filer code J7K)" in notice
        assert "Power of Attorney" in notice

    def test_claiming_a_licence_without_a_filer_code_is_refused(self) -> None:
        """An unverifiable claim of licensure on a customs filing. The failure belongs at
        construction, not on the page."""
        with pytest.raises(ValueError, match="no valid three-character filer code"):
            Preparer(name="Harborline", is_licensed_broker=True)

    def test_a_lowercase_filer_code_is_accepted_and_printed_uppercase(self) -> None:
        assert (
            "(filer code J7K)"
            in Preparer(name="Harborline", is_licensed_broker=True, filer_code="j7k").notice
        )

    def test_a_trailing_period_in_a_legal_name_does_not_double(self) -> None:
        """ "Inc.." on a document filed with a federal agency."""
        notice = Preparer(name="Northbridge Trading Co.").notice
        assert "Co.." not in notice

    def test_an_empty_name_is_refused_rather_than_printing_prepared_by(self) -> None:
        with pytest.raises(ValueError, match="must name its preparer"):
            Preparer(name="   ")

    def test_the_provenance_sentence_is_not_brandable(self) -> None:
        """19 CFR 163 is a fact about the record, not a claim about the preparer. A
        deployment that could remove it could present an unsupported figure as supported."""
        for preparer in (
            DEFAULT_PREPARER,
            Preparer(name="Harborline", is_licensed_broker=True, filer_code="J7K"),
            Preparer(name="Anyone", contact="x@example.test"),
        ):
            assert "19 CFR 163" in preparer.notice


# -------------------------------------------------------------------- cross bodies


class TestCleaningARulingBody:
    def test_typewriter_indentation_is_collapsed(self) -> None:
        """CROSS serves 1989 rulings as the typewritten page. `embed_corpus` embeds the
        first 4000 characters, and forty columns of leading spaces on every line is forty
        columns of that budget spent on nothing."""
        raw = "\r          HQ 085583\r\r          CATEGORY:  Classification\r"
        assert clean_body(raw) == "HQ 085583\n\nCATEGORY:  Classification"

    def test_paragraph_breaks_survive(self) -> None:
        """FACTS, ISSUE, LAW AND ANALYSIS, HOLDING are carried entirely by blank lines,
        and an auditor reading a cited ruling needs to find the holding."""
        assert clean_body("FACTS:\r\r  a\r\r\r\rHOLDING:\r\r  b") == "FACTS:\n\na\n\nHOLDING:\n\nb"

    def test_a_form_feed_becomes_a_break_rather_than_a_control_character(self) -> None:
        assert "\x0c" not in clean_body("\x0cN234546\rNovember 16, 2012")

    def test_markup_is_stripped_if_it_ever_appears(self) -> None:
        """Not observed in the sampled corpus — thirty bodies across six chapters carried
        no tags at all. The path exists for the day the endpoint changes."""
        assert clean_body("<p>Ruling&nbsp;text</p>") == "Ruling\xa0text"


def test_no_secret_field_is_named_in_the_committed_example_env() -> None:
    """`.env.example` documents non-secret configuration. A real value there is a secret
    in version control, and the whole point of the file backend is that there is not one."""
    text = Path(".env.example").read_text(encoding="utf-8")
    for field in SECRET_FIELDS:
        line = f"DRAWBRIDGE_{field.upper()}="
        for row in text.splitlines():
            if row.strip().startswith(line):
                assert row.strip() == line, f"{field} carries a value in .env.example"
