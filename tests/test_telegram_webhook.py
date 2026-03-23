from __future__ import annotations

from collections.abc import AsyncIterator
from urllib.parse import parse_qs, urlparse


class FakeDB:
    async def commit(self) -> None:
        return None

    async def execute(self, statement):
        return type("FakeResult", (), {"scalar_one_or_none": lambda self: None})()

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, db_dep, settings_dep
from app.utils import decode_state, encode_state


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
        telegram_require_approval=False,
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


def build_document_payload() -> dict:
    return {
        "update_id": 9002,
        "message": {
            "message_id": 78,
            "from": {"id": 321, "first_name": "Davi", "username": "davi"},
            "chat": {"id": 321, "type": "private"},
            "document": {
                "file_id": "doc-image-1",
                "mime_type": "image/png",
                "file_name": "image.png",
            },
        },
    }


def test_telegram_webhook_acknowledges_and_schedules_x_info(monkeypatch) -> None:
    app.dependency_overrides[settings_dep] = lambda: build_settings()
    app.dependency_overrides[db_dep] = fake_db_dep
    sent: list[tuple[str, str]] = []
    scheduled: list[tuple[str, str, str, int | None]] = []
    refreshed: list[str] = []

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

    class FakeOpenRouterClient:
        def __init__(self, settings) -> None:
            pass

        async def refresh_free_text_models_if_due(self) -> None:
            refreshed.append("ok")

    monkeypatch.setattr("app.main.TelegramClient", FakeTelegramClient)
    monkeypatch.setattr("app.main.OpenRouterClient", FakeOpenRouterClient)
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
    assert refreshed == ["ok"]


def test_telegram_webhook_auto_triggers_on_photo_without_x_info(monkeypatch) -> None:
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

    class FakeOpenRouterClient:
        def __init__(self, settings) -> None:
            pass

        async def refresh_free_text_models_if_due(self) -> None:
            return None

    monkeypatch.setattr("app.main.TelegramClient", FakeTelegramClient)
    monkeypatch.setattr("app.main.OpenRouterClient", FakeOpenRouterClient)
    monkeypatch.setattr("app.main.MessageService.persist_message", fake_persist)
    monkeypatch.setattr("app.main.dispatch_telegram_command", fake_dispatch)

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/telegram",
            headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret"},
            json=build_payload(text="", include_photo=True),
        )

    app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json() == {"status": "accepted", "command": "x-info"}
    assert sent == [
        ("321", "Recebi sua imagem. Estou analisando agora."),
        ("321", "[x-info] Etapa 1/6: preparando o processamento."),
    ]
    assert scheduled == [("x-info", "321", "telegram_321", 500)]


def test_telegram_webhook_auto_triggers_on_image_document_without_x_info(monkeypatch) -> None:
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

    class FakeOpenRouterClient:
        def __init__(self, settings) -> None:
            pass

        async def refresh_free_text_models_if_due(self) -> None:
            return None

    monkeypatch.setattr("app.main.TelegramClient", FakeTelegramClient)
    monkeypatch.setattr("app.main.OpenRouterClient", FakeOpenRouterClient)
    monkeypatch.setattr("app.main.MessageService.persist_message", fake_persist)
    monkeypatch.setattr("app.main.dispatch_telegram_command", fake_dispatch)

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/telegram",
            headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret"},
            json=build_document_payload(),
        )

    app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json() == {"status": "accepted", "command": "x-info"}
    assert sent == [
        ("321", "Recebi sua imagem. Estou analisando agora."),
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
        "Bot ativo.\nEnvie uma foto e eu identifico automaticamente o filme ou a serie.\nUse /trakt-connect para ligar sua conta Trakt."
    ]


def test_telegram_webhook_handles_trakt_connect_inline(monkeypatch) -> None:
    app.dependency_overrides[settings_dep] = lambda: build_settings()
    app.dependency_overrides[db_dep] = fake_db_dep
    sent: list[str] = []

    class FakeTelegramClient:
        def __init__(self, settings) -> None:
            pass

        async def send_text(self, chat_id: str, text: str) -> int | None:
            sent.append(text)
            return 1

    async def fake_persist(self, normalized):
        from app.services import PersistMessageResult
        from types import SimpleNamespace

        return PersistMessageResult(message=SimpleNamespace(id=1), created=True)

    monkeypatch.setattr("app.main.TelegramClient", FakeTelegramClient)
    monkeypatch.setattr("app.main.MessageService.persist_message", fake_persist)

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/telegram",
            headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret"},
            json=build_payload(text="/trakt-connect"),
        )

    app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json() == {"status": "accepted", "command": "/trakt-connect"}
    assert sent
    assert sent == [
        "Abra este link para conectar sua conta Trakt:\nhttp://localhost:8000/admin/trakt/connect/telegram_321"
    ]


def test_telegram_webhook_handles_admin_help_inline(monkeypatch) -> None:
    settings = build_settings()
    settings.admin_shared_secret = "admin123"
    app.dependency_overrides[settings_dep] = lambda: settings
    app.dependency_overrides[db_dep] = fake_db_dep
    sent: list[str] = []

    class FakeTelegramClient:
        def __init__(self, settings) -> None:
            pass

        async def send_text(self, chat_id: str, text: str) -> int | None:
            sent.append(text)
            return 1

    async def fake_persist(self, normalized):
        from app.services import PersistMessageResult
        from types import SimpleNamespace

        return PersistMessageResult(message=SimpleNamespace(id=1), created=True)

    monkeypatch.setattr("app.main.TelegramClient", FakeTelegramClient)
    monkeypatch.setattr("app.main.MessageService.persist_message", fake_persist)

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/telegram",
            headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret"},
            json=build_payload(text="/admin-help"),
        )

    app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json() == {"status": "accepted", "command": "/admin-help"}
    assert sent == [
        "Controle de acesso do bot\n"
        "1. No Dokploy, abra a app e edite as env vars.\n"
        "2. Defina TELEGRAM_REQUIRE_APPROVAL=true.\n"
        "3. Defina TELEGRAM_AUTO_APPROVED_USER_KEYS=telegram_321 para manter seu acesso.\n"
        "4. Compartilhe o bot com seus amigos e peca para enviarem /start.\n"
        "5. Abra o painel admin: http://localhost:8000/admin/trakt?token=admin123\n"
        "6. Na coluna Acesso Telegram, clique em Aprovar ou Revogar para cada usuario."
    ]


def test_telegram_webhook_blocks_unapproved_user_when_approval_required(monkeypatch) -> None:
    settings = build_settings()
    settings.telegram_require_approval = True
    app.dependency_overrides[settings_dep] = lambda: settings

    class BlockingDB:
        async def commit(self) -> None:
            return None

        async def execute(self, statement):
            profile = type("Profile", (), {"telegram_access_granted": False})()
            return type("FakeResult", (), {"scalar_one_or_none": lambda self: profile})()

    async def blocking_db_dep() -> AsyncIterator[object]:
        yield BlockingDB()

    app.dependency_overrides[db_dep] = blocking_db_dep
    sent: list[str] = []

    class FakeTelegramClient:
        def __init__(self, settings) -> None:
            pass

        async def send_text(self, chat_id: str, text: str) -> int | None:
            sent.append(text)
            return 1

    async def fake_persist(self, normalized):
        from app.services import PersistMessageResult
        from types import SimpleNamespace

        return PersistMessageResult(message=SimpleNamespace(id=1), created=True)

    class FakeOpenRouterClient:
        def __init__(self, settings) -> None:
            pass

        async def refresh_free_text_models_if_due(self) -> None:
            return None

    monkeypatch.setattr("app.main.TelegramClient", FakeTelegramClient)
    monkeypatch.setattr("app.main.OpenRouterClient", FakeOpenRouterClient)
    monkeypatch.setattr("app.main.MessageService.persist_message", fake_persist)

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/telegram",
            headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret"},
            json=build_payload(text="/start"),
        )

    app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "reason": "telegram-access-pending"}
    assert sent == [
        "Seu acesso ao bot ainda nao foi liberado.\nSeu cadastro foi recebido e o administrador precisa aprovar seu usuario antes do uso."
    ]


def test_trakt_callback_preserves_telegram_requester_key(monkeypatch) -> None:
    app.dependency_overrides[settings_dep] = lambda: build_settings()
    received: list[str] = []

    class FakeConnection:
        access_token = "token"
        trakt_username = None

    class FakePipelineService:
        def __init__(self, settings) -> None:
            self.trakt = self

        async def exchange_code(self, code: str):
            return {"access_token": "token", "expires_in": 3600}

        async def persist_trakt_callback(self, db, phone_number: str, token_payload: dict):
            received.append(phone_number)
            return FakeConnection()

        async def get_profile(self, access_token: str):
            return {"user": {"username": "davi"}}

    class FakeSession:
        async def commit(self) -> None:
            return None

    class DummyContextManager:
        def __init__(self, value) -> None:
            self.value = value

        async def __aenter__(self):
            return self.value

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    monkeypatch.setattr("app.main.PipelineService", FakePipelineService)
    monkeypatch.setattr("app.main.SessionLocal", lambda: DummyContextManager(FakeSession()))

    state = encode_state({"phone_number": "telegram_321", "generated_at": "2026-03-22T00:00:00+00:00"}, "test")

    with TestClient(app) as client:
        response = client.get(f"/auth/trakt/callback?code=abc&state={state}")

    app.dependency_overrides.clear()
    assert response.status_code == 200
    assert received == ["telegram_321"]


def test_admin_trakt_connect_preserves_telegram_requester_key(monkeypatch) -> None:
    app.dependency_overrides[settings_dep] = lambda: build_settings()

    with TestClient(app) as client:
        response = client.get("/admin/trakt/connect/telegram_321?token=test", follow_redirects=False)

    app.dependency_overrides.clear()
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("https://trakt.tv/oauth/authorize?")
    state = parse_qs(urlparse(location).query)["state"][0]
    payload = decode_state(state, "test")
    assert payload["phone_number"] == "telegram_321"


def test_admin_telegram_access_preserves_telegram_requester_key(monkeypatch) -> None:
    settings = build_settings()
    settings.admin_shared_secret = "test"
    app.dependency_overrides[settings_dep] = lambda: settings

    saved: list[tuple[str, bool]] = []

    class FakeProfile:
        telegram_access_granted = False

    class FakeMessageService:
        def __init__(self, settings, db) -> None:
            pass

        async def upsert_phone_profile(self, phone_number: str, whatsapp_jid: str | None = None):
            saved.append((phone_number, False))
            return FakeProfile()

    class FakeSession:
        async def commit(self) -> None:
            saved[-1] = (saved[-1][0], True)

    async def fake_db_dep() -> AsyncIterator[object]:
        yield FakeSession()

    app.dependency_overrides[db_dep] = fake_db_dep
    monkeypatch.setattr("app.main.MessageService", FakeMessageService)

    with TestClient(app) as client:
        response = client.post(
            "/admin/telegram/access?token=test",
            data={"phone_number": "telegram_321", "granted": "true"},
            follow_redirects=False,
        )

    app.dependency_overrides.clear()
    assert response.status_code == 303
    assert saved == [("telegram_321", True)]
