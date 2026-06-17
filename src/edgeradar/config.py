"""Central settings module.

Everything configurable (credentials, endpoints, storage paths) is read from the
environment via a single typed `Settings` object. No keys or paths are hardcoded
anywhere else in the codebase. Locally these come from a git-ignored `.env`;
in Docker/CI they come from real environment variables.

Usage:
    from edgeradar.config import get_settings
    settings = get_settings()
    print(settings.minio_endpoint)
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed application configuration.

    Field names map to UPPER_SNAKE_CASE env vars (e.g. `minio_endpoint`
    <- `MINIO_ENDPOINT`). All have safe local defaults except secrets,
    which are intentionally blank so the app fails loudly if you forget them.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Object storage (MinIO; S3-compatible) -----------------------------
    minio_endpoint: str = Field(default="http://localhost:9000")
    minio_root_user: str = Field(default="edgeradar")
    minio_root_password: str = Field(default="edgeradar-dev-secret")
    minio_bucket: str = Field(default="edgeradar")

    # --- Warehouse ---------------------------------------------------------
    # File-based DuckDB to start; structured so a later Postgres swap is easy.
    duckdb_path: str = Field(default="data/warehouse/edgeradar.duckdb")

    # --- Streaming (Redpanda / Kafka) -------------------------------------
    kafka_bootstrap_servers: str = Field(default="localhost:9092")
    kafka_topic_raw: str = Field(default="quotes_raw")
    kafka_consumer_group: str = Field(default="edgeradar-normalizer")

    # --- Local data lake zones (Parquet, partitioned by source/date) -------
    data_root: str = Field(default="data")

    # --- Source API credentials (filled in per phase) ----------------------
    kalshi_api_base: str = Field(default="https://api.elections.kalshi.com/trade-api/v2")
    kalshi_api_key_id: str = Field(default="")  # set in .env when you wire Kalshi
    kalshi_private_key_path: str = Field(default="")

    manifold_api_base: str = Field(default="https://api.manifold.markets/v0")

    odds_api_base: str = Field(default="https://api.the-odds-api.com/v4")
    odds_api_key: str = Field(default="")  # free tier ~500 req/mo — cache hard

    nws_api_base: str = Field(default="https://api.weather.gov")
    # NWS requires a descriptive User-Agent with contact info per their docs.
    nws_user_agent: str = Field(default="EdgeRadar (contact: set-me@example.com)")

    # --- Alerting (Phase 7) ------------------------------------------------
    discord_webhook_url: str = Field(default="")
    # Minimum net edge (probability units) for a signal to trigger an alert.
    alert_min_edge: float = Field(default=0.05)

    # --- Behaviour flags ---------------------------------------------------
    # Hard guardrail surfaced everywhere: this system never executes trades.
    enable_order_execution: bool = Field(default=False)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance (read once per process)."""
    return Settings()
