from __future__ import annotations

from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- DB ---
    postgres_host: str = "host.docker.internal"
    postgres_port: int = 5432
    postgres_user: str
    postgres_password: str
    postgres_db: str

    # --- Auth ---
    # Single admin. Password stored as an argon2id hash.
    admin_email: str
    admin_password_hash: str
    jwt_secret: str = Field(min_length=32)
    jwt_algorithm: str = "HS256"
    jwt_ttl_days: int = 30
    cookie_domain: str = ""       # e.g. ".yourdomain.com" — shared across sub-domains
    cookie_name: str = "cl_admin_session"
    cookie_secure: bool = True    # False only for local http dev

    # --- Ingest ---
    ingest_bearer_token: str = Field(min_length=16)

    # --- CORS ---
    # Comma-separated origins allowed for browser JS. Backend appends the
    # cookie only if the origin is in this list.
    cors_origins: str = ""

    # --- Ops ---
    log_level: str = "INFO"
    display_tz: str = "America/New_York"

    @property
    def dsn(self) -> str:
        """DSN used by psycopg directly (app runtime)."""
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def sqlalchemy_dsn(self) -> str:
        """DSN used by Alembic/SQLAlchemy — forces the psycopg (v3) driver so
        it doesn't try to import psycopg2."""
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def display_zoneinfo(self) -> ZoneInfo:
        return ZoneInfo(self.display_tz)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
