from __future__ import annotations

from collections.abc import AsyncIterator


class FakeDB:
    async def commit(self) -> None:
        return None

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, db_dep, settings_dep


async def fake_db_dep() -> AsyncIterator[object]:
    yield FakeDB()


def build_settings() -> Settings:
    return Settings.model_construct(
        evolution_base_url="https://example.com",
        evolution_api_key="test",
        evolution_instance="meu-whatsapp",
        evolution_owner_phone="5519988343888",
        telegram_bot_token="telegram-token",
        telegram_webhook_secret="telegram-secret",
        openrouter_api_key="test",
        tmdb_api_token="test",
        omdb_api_key="test",
        trakt_client_id="test",
        trakt_client_secret="test",
    )


def build_payload(*, text: str = "x-info", include_photo: bool = False) -> dict:
    message: dict[str, object] = {
        "message_id": 77,
        "from": {"id": 321, "first_name": "Davi", "username": "davi"},
        "chat": {"id": 321, "type": "private"},
    }
    if include_photo:
        message["caption"] = text
        message["photo"] = [{"file_id": "small-photo"}, {"file_id": "large-photo"}]
    else:
        message["text"] = text
    return {"update_id": 9001, "message": message}


def test_telegram_webhook_acknowledges_and_schedules_x_info(monkeypatch) -> None:
    app.dependency_overrides[settings_dep] = lambda: build_settings()
    app.dependency_overrides[db_dep] = fake_db_dep
    sent: list[tuple[str, str]] = []
    scheduled: list[tuple[str, str, str, int | None]] = []

    class FakeTelegramClient:
        def __init__(self, settings) -> None:
            pass

        async def send_text(self, chat_id: str, text: str) -> int | None:
            sent.append((chat_id, text))
            if "Etapa 1/" in text:
                return 500
            return 400

    async def fake_persist(self, normalized):
        from app.services import PersistMessageResult
        from types import SimpleNamespace

        return PersistMessageResult(message=SimpleNamespace(id=1), created=True)

    async def fake_dispatch(command, chat_id, requester_key, background_tasks, trigger_message_id=None, status_message_id=None):
        scheduled.append((command, chat_id, requester_key, status_message_id))

    monkeypatch.setattr("app.main.TelegramClient", FakeTelegramClient)
    monkeypatch.setattr("app.main.MessageService.persist_message", fake_persist)
    monkeypatch.setattr("app.main.dispatch_telegram_command", fake_dispatch)

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/telegram",
            headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret"},
            json=build_payload(text="x-info"),
        )

    app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json() == {"status": "accepted", "command": "x-info"}
    assert sent == [
        ("321", "Recebi sua solicitacao. O x-info esta em processamento."),
        ("321", "[x-info] Etapa 1/6: preparando o processamento."),
    ]
    assert scheduled == [("x-info", "321", "telegram_321", 500)]


def test_telegram_webhook_handles_start_inline(monkeypatch) -> None:
    app.dependency_overrides[settings_dep] = lambda: build_settings()
    app.dependency_overrides[db_dep] = fake_db_dep
    sent: list[str] = []

    class FakeTelegramClient:
        def __init__(self, settings) -> None:
            pass

        async def send_text(self, chat_id: str, text: str) -> int | None:
            sent.append(text)
            return 1

    class FakeProfile:
        display_name = None

    class FakeMessageService:
        def __init__(self, settings, db) -> None:
            pass

        async def persist_message(self, normalized):
            from app.services import PersistMessageResult
            from types import SimpleNamespace

            return PersistMessageResult(message=SimpleNamespace(id=1), created=True)

        async def upsert_phone_profile(self, phone_number: str, whatsapp_jid: str | None = None):
            return FakeProfile()

    monkeypatch.setattr("app.main.TelegramClient", FakeTelegramClient)
    monkeypatch.setattr("app.main.MessageService", FakeMessageService)

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/telegram",
            headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret"},
            json=build_payload(text="/start"),
        )

    app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json() == {"status": "accepted", "command": "/start"}
    assert sent == [
        "Bot ativo.\nEnvie uma foto com `x-info` na legenda ou envie a foto e depois `x-info`.\nUse `/trakt-connect` para ligar sua conta Trakt."
    ]
