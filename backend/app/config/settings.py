"""Application settings, loaded from environment / .env via pydantic-settings."""

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the AI Tiny World backend."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "AI Tiny World"
    database_url: str = "sqlite:///./ai_tiny_world.db"
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    world_data_dir: Path = Path("../world_data")
    map_name: str = "tiny_world"
    log_level: str = "INFO"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors_origins(cls, value: object) -> object:
        """Parse a comma-separated origin list from env vars (e.g. CORS_ORIGINS)."""
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings singleton."""
    return Settings()
