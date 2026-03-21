from __future__ import annotations

from functools import lru_cache

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "trakt-sync"
    app_env: str = "development"
    app_base_url: str = "http://localhost:8000"
    log_level: str = "INFO"
    admin_shared_secret: str | None = None

    database_url: str = "sqlite+aiosqlite:///./trakt_sync.db"
    redis_url: str = "redis://localhost:6379/0"

    evolution_base_url: str
    evolution_api_key: str
    evolution_instance: str
    evolution_owner_phone: str
    evolution_owner_lid: str | None = None
    evolution_webhook_secret: str | None = None
    self_chat_only_mode: bool = True

    openrouter_api_key: str
    openrouter_site_url: str | None = None
    openrouter_app_name: str = "trakt-sync"
    openrouter_vision_models: list[str] = Field(
        default_factory=lambda: [
            "mistralai/mistral-small-3.1-24b-instruct:free",
            "google/gemma-3-27b-it:free",
            "nvidia/nemotron-nano-12b-v2-vl:free",
            "google/gemma-3-12b-it:free",
            "google/gemma-3-4b-it:free",
        ]
    )
    openrouter_emergency_router: str = "openrouter/free"
    openrouter_confidence_threshold: float = 0.80

    tmdb_api_token: str
    tmdb_api_key: str | None = None
    tmdb_region: str = "BR"
    tmdb_language: str = "pt-BR"

    omdb_api_key: str

    trakt_client_id: str
    trakt_client_secret: str
    trakt_redirect_uri: str | None = None

    image_command_ttl_minutes: int = 30
    save_command_ttl_hours: int = 24

    @computed_field
    @property
    def computed_trakt_redirect_uri(self) -> str:
        return self.trakt_redirect_uri or f"{self.app_base_url.rstrip('/')}/auth/trakt/callback"


@lru_cache
def get_settings() -> Settings:
    return Settings()
