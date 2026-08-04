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

    # --- Draft generation ---
    # Provider keys are normally stored (encrypted) in generation_settings and
    # edited in the dashboard. These are the break-glass fallback, read only
    # when nothing is stored or when a stored key will not decrypt — which is
    # the recovery path if JWT_SECRET is ever rotated, since that is what the
    # provider keys are encrypted with. See DESIGN_PROVIDERS.md decision 7.
    #
    # Without any key, generation still runs but every draft falls back to the
    # workbook copy in seed_ads, so the queue keeps filling either way.
    minimax_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""

    # --- CompanyCam photo import ---
    # Not a generation provider: nothing here writes copy or draws pictures, it
    # only pulls the crews' own job-site photos into the image stack. So it is a
    # plain env var rather than an entry in generation_settings' encrypted
    # provider blob.
    #
    # docker-compose bakes `env_file` at container creation, so a token added to
    # .env.prod is invisible to an already-running container. The importer takes
    # --token for exactly that reason; this is the fallback once you restart.
    companycam_api_token: str = ""
    companycam_api_base: str = "https://api.companycam.com/v2"
    # How often the background top-up checks queue depth. 0 disables the loop
    # (useful if you would rather drive generation from host cron).
    generation_interval_minutes: int = 30

    # Where image bytes live. A mounted volume in production, anywhere writable
    # in dev. Content-addressed, so the directory is safe to rsync or back up.
    images_dir: str = "/var/lib/cl/images"

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
