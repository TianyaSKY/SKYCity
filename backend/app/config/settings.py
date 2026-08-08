"""Application settings, loaded from environment / .env via pydantic-settings."""

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the AI Tiny World backend."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "AI Tiny World"
    database_url: str = "sqlite:///./ai_tiny_world.db"
    # NoDecode: pydantic-settings >=2.14 force-decodes complex fields from
    # .env as JSON; the CSV form needs the raw string for the validator below.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default=["http://localhost:5173", "http://127.0.0.1:5173"],
    )
    world_data_dir: Path = Path("../world_data")
    map_name: str = "tiny_world"
    log_level: str = "INFO"
    # Durable log output dir; categorized sinks write app.log / warning.log /
    # error.log there (rotated + retained). Override with LOG_DIR.
    log_dir: Path = Path("logs")

    # LLM agent decisions (M3).
    llm_provider: str = "auto"  # "auto" | "openai" | "fake"
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 30.0
    decision_min_interval: int = 5  # game minutes between decisions (floor)
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    # M6: stronger model used for the no-tool daily reflection prompt.
    llm_reflect_model: str = "gpt-4o-mini"
    # Thinking models (DeepSeek v4, qwen3.8-max, ...) reject a forced tool
    # choice. "required" keeps the one-action-per-decision contract (default);
    # "auto"/"none" let the model reply in text, which the provider degrades
    # into a wait action so the world keeps ticking.
    llm_tool_choice: str = "required"  # "required" | "auto" | "none"
    # Direct reasoning intensity knob, forwarded as chat-completions
    # reasoning_effort (openai-agents ModelSettings.reasoning.effort). None =
    # not sent (provider default). Only effort is portable: mode/context need
    # the Responses API and are rejected by chat completions.
    llm_reasoning_effort: str | None = None  # none|minimal|low|medium|high|xhigh|max
    # Third-party OpenAI-compatible endpoints usually only implement
    # /chat/completions; the SDK's default Responses API would 404. Keep
    # chat completions unless the endpoint explicitly supports /responses.
    llm_use_responses: bool = False
    # M8: stability / cost control.
    llm_max_concurrent: int = 2  # global cap on concurrent LLM calls
    world_daily_token_budget: int = 0  # per-world daily LLM token budget; 0 = unlimited
    observation_cache_window_minutes: int = 10  # skip identical observations within this window
    backoff_max_delay: int = 120  # cap on the degrade backoff schedule (game minutes)

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors_origins(cls, value: object) -> object:
        """Parse a comma-separated origin list from env vars (e.g. CORS_ORIGINS)."""
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("llm_tool_choice")
    @classmethod
    def _validate_llm_tool_choice(cls, value: str) -> str:
        """Only SDK-supported tool_choice values are meaningful here."""
        if value not in {"required", "auto", "none"}:
            raise ValueError(
                f"llm_tool_choice must be required|auto|none, got {value!r}"
            )
        return value

    @field_validator("llm_reasoning_effort")
    @classmethod
    def _validate_llm_reasoning_effort(cls, value: str | None) -> str | None:
        """Accept only values the SDK's Reasoning.effort enum understands."""
        if value is None:
            return value
        if value not in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError(
                f"llm_reasoning_effort must be none|minimal|low|medium|high|"
                f"xhigh|max or empty, got {value!r}"
            )
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings singleton."""
    return Settings()
