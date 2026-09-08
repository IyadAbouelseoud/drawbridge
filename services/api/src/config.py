"""Runtime configuration.

Non-secret values come from the environment. Secrets do not: they are resolved by
`services.api.src.secrets` from a gitignored file, with the environment as a deliberate
override for orchestrators that inject. See that module for why the distinction is worth
the extra source.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Any, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from services.api.src.secrets import inject_password, resolve

if TYPE_CHECKING:
    from collections.abc import Mapping


class SecretsFileSource(PydanticBaseSettingsSource):
    """Feeds resolved secrets into `Settings` as one more settings source.

    A source rather than a `__init__` argument so that precedence is expressed once, in
    `settings_customise_sources`, instead of being re-derived at every construction site —
    and so the test suite can build a `Settings` with explicit keyword arguments and have
    them win, which is what every test in the suite relies on.
    """

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls)
        self._secrets: Mapping[str, str] | None = None

    def _resolved(self) -> Mapping[str, str]:
        if self._secrets is None:
            self._secrets = resolve()
        return self._secrets

    def get_field_value(
        self,
        field: Any,  # noqa: ARG002 - signature fixed by PydanticBaseSettingsSource
        field_name: str,
    ) -> tuple[Any, str, bool]:
        return self._resolved().get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return dict(self._resolved())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="DRAWBRIDGE_", extra="ignore")

    environment: str = "development"
    log_level: str = "INFO"

    database_url: str = Field(
        default="postgresql+asyncpg://drawbridge:drawbridge@postgres:5432/drawbridge"
    )
    redis_url: str = "redis://redis:6379/0"

    s3_endpoint_url: str = "http://minio:9000"
    s3_access_key: str = "drawbridge"
    s3_secret_key: str = "drawbridge"
    s3_bucket_documents: str = "drawbridge-documents"
    # Cold storage, separate from the documents bucket: an offboarded tenant's signed
    # ledger has to outlive the relationship by five years and wants its own
    # lifecycle rules. See scripts/tenant_offboard.py.
    s3_bucket_archive: str = "drawbridge-archive"

    # ------------------------------------------------------------------ identity
    # Authentik issues the tokens in a deployment and this service only verifies them, so
    # what is configured here is a key source and the three claims that must match.
    #
    # `auth_required` defaults on. Off is a development convenience and the API says so at
    # startup, because a deployment with it off is indistinguishable from a working one
    # from the outside: every request succeeds.
    auth_required: bool = True
    jwt_issuer: str = "drawbridge"
    jwt_audience: str = "drawbridge-api"
    jwt_tenant_claim: str = "tenant_id"
    # RS256 against Authentik. Set this in any deployment.
    oidc_jwks_url: str | None = None
    # HS256 against a shared secret. Local only — the test suite and `make token` use it,
    # and standing up an identity provider to run the suite would be its own dishonesty.
    #
    # Resolved from the secrets file, not from `.env`. See services/api/src/secrets.py.
    jwt_secret: str | None = None

    # What n8n presents. A service token acts for any tenant it names, which makes this
    # the one credential whose leak is a cross-tenant breach; it is carried here so the
    # posture check can see it, and read by the workflow layer from the same source.
    service_token: str | None = None

    # The unprivileged role's password, kept out of the DSN. `database_url` may be
    # configured without one and this is injected below.
    app_db_password: str | None = None

    # Secrets belonging to adjacent services, resolved here so one file covers the stack
    # and `check_secret_posture` can refuse on all of them at once.
    offboard_signing_key: str | None = None
    n8n_encryption_key: str | None = None
    authentik_secret_key: str | None = None

    # --------------------------------------------------------------- observability
    # Empty means spans are created and dropped. See services/api/src/telemetry.py: the
    # trace id reaches `audit_ledger` either way, so recordkeeping does not depend on a
    # collector being up.
    otel_exporter_endpoint: str = ""

    anthropic_api_key: str | None = None
    model_reasoning: str = "claude-opus-5"
    model_extraction: str = "claude-haiku-4-5-20251001"

    mcp_ace_url: str = "http://mcp-ace:8101/mcp"
    mcp_hts_url: str = "http://mcp-hts:8102/mcp"
    mcp_docs_url: str = "http://mcp-docs:8103/mcp"
    mcp_claims_url: str = "http://mcp-claims:8104/mcp"
    mcp_ledger_url: str = "http://mcp-ledger:8105/mcp"

    @model_validator(mode="after")
    def _apply_db_password(self) -> Self:
        """Put the resolved password into a DSN configured without one.

        Both DSNs, because a deployment that moved the application password into a secret
        and left the owner password inline has moved half of it. `inject_password` leaves
        an explicit inline password alone.
        """
        self.database_url = inject_password(self.database_url, self.app_db_password)
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """init > environment > secrets file > `.env`.

        The environment beats the secrets file because an orchestrator that injects a
        value is making a deliberate statement and must be able to override a stale file.
        The secrets file beats `.env` because moving secrets out of `.env` is the entire
        point: with both present, the file is what takes effect.
        """
        return (
            init_settings,
            env_settings,
            SecretsFileSource(settings_cls),
            dotenv_settings,
            file_secret_settings,
        )

    @property
    def sync_database_url(self) -> str:
        """The same database over the sync driver.

        The agent worker calls a blocking SDK in the middle of its transaction, so it runs
        on `psycopg` rather than `asyncpg`. Derived from `database_url` rather than
        configured separately so the two can never point at different databases.
        """
        return self.database_url.replace("+asyncpg", "+psycopg")


@lru_cache
def get_settings() -> Settings:
    return Settings()
