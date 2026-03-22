from __future__ import annotations

from pathlib import Path

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


def test_settings_parse_json_list_values() -> None:
    settings = Settings.model_validate(
        {
            "evolution_base_url": "https://example.com",
            "evolution_api_key": "test",
            "evolution_instance": "meu-whatsapp",
            "evolution_owner_phone": "5519988343888",
            "openrouter_api_key": "test",
            "openrouter_vision_models": '["free-a:free","free-b:free"]',
            "tmdb_api_token": "test",
            "omdb_api_key": "test",
            "trakt_client_id": "test",
            "trakt_client_secret": "test",
        }
    )

    assert settings.openrouter_vision_models == ["free-a:free", "free-b:free"]


def test_settings_parse_csv_list_values_from_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "EVOLUTION_BASE_URL=https://example.com",
                "EVOLUTION_API_KEY=test",
                "EVOLUTION_INSTANCE=meu-whatsapp",
                "EVOLUTION_OWNER_PHONE=5519988343888",
                "OPENROUTER_API_KEY=test",
                "TMDB_API_TOKEN=test",
                "OMDB_API_KEY=test",
                "TRAKT_CLIENT_ID=test",
                "TRAKT_CLIENT_SECRET=test",
                "TELEGRAM_AUTO_APPROVED_USER_KEYS=telegram_1,telegram_2",
                'OPENROUTER_VISION_MODELS=["free-a:free","free-b:free"]',
            ]
        )
    )

    settings = Settings(_env_file=env_file)

    assert settings.telegram_auto_approved_user_keys == ["telegram_1", "telegram_2"]
    assert settings.openrouter_vision_models == ["free-a:free", "free-b:free"]
