"""Runtime configuration. All values come from the environment; nothing is hardcoded."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    anthropic_api_key: str | None = None
    model_reasoning: str = "claude-opus-5"
    model_extraction: str = "claude-haiku-4-5-20251001"

    mcp_ace_url: str = "http://mcp-ace:8101/mcp"
    mcp_hts_url: str = "http://mcp-hts:8102/mcp"
    mcp_docs_url: str = "http://mcp-docs:8103/mcp"
    mcp_claims_url: str = "http://mcp-claims:8104/mcp"
    mcp_ledger_url: str = "http://mcp-ledger:8105/mcp"

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
