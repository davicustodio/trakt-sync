from __future__ import annotations

from app.config import Settings


def test_settings_parse_csv_list_values() -> None:
    settings = Settings.model_validate(
        {
            "evolution_base_url": "https://example.com",
            "evolution_api_key": "test",
            "evolution_instance": "meu-whatsapp",
            "evolution_owner_phone": "5519988343888",
            "telegram_auto_approved_user_keys": "telegram_1,telegram_2",
            "openrouter_api_key": "test",
            "openrouter_vision_models": "free-a:free,free-b:free",
            "openrouter_paid_vision_models": "paid-a,paid-b",
            "tmdb_api_token": "test",
            "omdb_api_key": "test",
            "trakt_client_id": "test",
            "trakt_client_secret": "test",
        }
    )

    assert settings.telegram_auto_approved_user_keys == ["telegram_1", "telegram_2"]
    assert settings.openrouter_vision_models == ["free-a:free", "free-b:free"]
    assert settings.openrouter_paid_vision_models == ["paid-a", "paid-b"]
