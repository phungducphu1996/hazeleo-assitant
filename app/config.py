from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = Field(default="local", validation_alias="APP_ENV")
    app_host: str = Field(default="0.0.0.0", validation_alias="APP_HOST")
    app_port: int = Field(default=8030, validation_alias="APP_PORT")
    app_timezone: str = Field(default="Asia/Ho_Chi_Minh", validation_alias="APP_TIMEZONE")

    openai_api_key: str | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4.1-mini", validation_alias="OPENAI_MODEL")
    openai_api_base_url: str = Field(default="https://api.openai.com/v1", validation_alias="OPENAI_API_BASE_URL")
    openai_temperature: float = Field(default=0.2, validation_alias="OPENAI_TEMPERATURE")

    zalo_worker_url: str = Field(default="http://127.0.0.1:8787", validation_alias="ZALO_WORKER_URL")
    zalo_shared_secret: str | None = Field(default=None, validation_alias="ZALO_SHARED_SECRET")

    telegram_bot_token: str | None = Field(default=None, validation_alias="TELEGRAM_BOT_TOKEN")
    telegram_api_base_url: str = Field(default="https://api.telegram.org", validation_alias="TELEGRAM_API_BASE_URL")
    telegram_polling_enabled: bool = Field(default=False, validation_alias="TELEGRAM_POLLING_ENABLED")
    telegram_poll_timeout_seconds: int = Field(default=20, validation_alias="TELEGRAM_POLL_TIMEOUT_SECONDS")
    telegram_poll_interval_seconds: int = Field(default=1, validation_alias="TELEGRAM_POLL_INTERVAL_SECONDS")
    telegram_webhook_secret: str | None = Field(default=None, validation_alias="TELEGRAM_WEBHOOK_SECRET")
    telegram_allowed_chat_ids: str | None = Field(default=None, validation_alias="TELEGRAM_ALLOWED_CHAT_IDS")

    reminder_poll_interval_seconds: int = Field(default=30, validation_alias="REMINDER_POLL_INTERVAL_SECONDS")
    reminder_max_attempts: int = Field(default=5, validation_alias="REMINDER_MAX_ATTEMPTS")
    conversation_turn_retention_days: int = Field(default=5, validation_alias="CONVERSATION_TURN_RETENTION_DAYS")
    conversation_turn_context_limit: int = Field(default=30, validation_alias="CONVERSATION_TURN_CONTEXT_LIMIT")

    data_dir: Path = Field(default=Path("data"), validation_alias="DATA_DIR")
    agent_prompt_path: Path = Field(default=Path("AGENT.md"), validation_alias="AGENT_PROMPT_PATH")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
        populate_by_name=True,
    )

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.app_timezone)

    @property
    def normalized_openai_base_url(self) -> str:
        return self.openai_api_base_url.rstrip("/")

    @property
    def normalized_zalo_worker_url(self) -> str:
        return self.zalo_worker_url.rstrip("/")

    @property
    def normalized_telegram_api_base_url(self) -> str:
        return self.telegram_api_base_url.rstrip("/")

    @property
    def telegram_allowed_chat_id_set(self) -> set[str]:
        raw = self.telegram_allowed_chat_ids or ""
        return {item.strip() for item in raw.split(",") if item.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
