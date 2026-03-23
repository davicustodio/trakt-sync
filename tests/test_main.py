from fastapi import BackgroundTasks
import pytest
from types import SimpleNamespace

from app.config import Settings
from app.auth import is_authorized_self_chat
from app.main import dispatch_command, handle_normalized_message, reconcile_recent_owner_messages
from app.schemas import PendingIdentificationState
from app.schemas import NormalizedMessage


class DummyContextManager:
    def __init__(self, value) -> None:
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


def build_message(**overrides: object) -> NormalizedMessage:
    payload = {
        "event_name": "MESSAGES_UPSERT",
        "provider_message_id": "1",
        "chat_jid": "5519988343888@s.whatsapp.net",
        "requester_phone": "5519988343888",
        "sender_phone": "5519988343888",
        "is_from_me": True,
        "message_type": "text",
        "text_body": "x-info",
        "media_url": None,
        "media_mime_type": None,
        "raw_payload": {},
    }
    payload.update(overrides)
    return NormalizedMessage(**payload)


def test_is_authorized_self_chat_accepts_owner_self_message() -> None:
    settings = Settings.model_construct(
        evolution_base_url="https://example.com",
        evolution_api_key="test",
        evolution_instance="meu-whatsapp",
        evolution_owner_phone="5519988343888",
        openrouter_api_key="test",
        tmdb_api_token="test",
        omdb_api_key="test",
        trakt_client_id="test",
        trakt_client_secret="test",
    )
    assert is_authorized_self_chat(settings, build_message()) is True


def test_is_authorized_self_chat_rejects_foreign_message() -> None:
    settings = Settings.model_construct(
        evolution_base_url="https://example.com",
        evolution_api_key="test",
        evolution_instance="meu-whatsapp",
        evolution_owner_phone="5519988343888",
        openrouter_api_key="test",
        tmdb_api_token="test",
        omdb_api_key="test",
        trakt_client_id="test",
        trakt_client_secret="test",
    )
    message = build_message(chat_jid="5511999999999@s.whatsapp.net", requester_phone="5511999999999")
    assert is_authorized_self_chat(settings, message) is False


def test_is_authorized_self_chat_accepts_owner_lid_message() -> None:
    settings = Settings.model_construct(
        evolution_base_url="https://example.com",
        evolution_api_key="test",
        evolution_instance="meu-whatsapp",
        evolution_owner_phone="5519988343888",
        evolution_owner_lid="121036657934449@lid",
        openrouter_api_key="test",
        tmdb_api_token="test",
        omdb_api_key="test",
        trakt_client_id="test",
        trakt_client_secret="test",
    )
    message = build_message(
        chat_jid="121036657934449@lid",
        requester_phone="121036657934449",
        sender_phone="121036657934449",
    )
    assert is_authorized_self_chat(settings, message) is True


@pytest.mark.asyncio
async def test_dispatch_command_schedules_x_info_background_task(monkeypatch) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    async def fake_process_x_info(ctx: dict, chat_jid: str, requester_phone: str, trigger_message_id: str | None) -> None:
        return None

    def fake_add_task(func, *args, **kwargs) -> None:
        calls.append((func.__name__, args))

    monkeypatch.setattr("app.main.process_x_info", fake_process_x_info)
    background_tasks = BackgroundTasks()
    monkeypatch.setattr(background_tasks, "add_task", fake_add_task)

    await dispatch_command("x-info", "5511999999999@s.whatsapp.net", "5511999999999", background_tasks, "msg-1")

    assert calls == [("fake_process_x_info", ({}, "5511999999999@s.whatsapp.net", "5511999999999", "msg-1"))]


@pytest.mark.asyncio
async def test_dispatch_command_schedules_x_save_background_task(monkeypatch) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    async def fake_process_x_save(ctx: dict, chat_jid: str, requester_phone: str) -> None:
        return None

    def fake_add_task(func, *args, **kwargs) -> None:
        calls.append((func.__name__, args))

    monkeypatch.setattr("app.main.process_x_save", fake_process_x_save)
    background_tasks = BackgroundTasks()
    monkeypatch.setattr(background_tasks, "add_task", fake_add_task)

    await dispatch_command("x-save", "5511999999999@s.whatsapp.net", "5511999999999", background_tasks)

    assert calls == [("fake_process_x_save", ({}, "5511999999999@s.whatsapp.net", "5511999999999"))]


@pytest.mark.asyncio
async def test_reconcile_recent_owner_messages_backfills_missed_webhooks(monkeypatch) -> None:
    settings = Settings.model_construct(
        evolution_base_url="https://example.com",
        evolution_api_key="test",
        evolution_instance="meu-whatsapp",
        evolution_owner_phone="5519988343888",
        evolution_owner_lid="121036657934449@lid",
        openrouter_api_key="test",
        tmdb_api_token="test",
        omdb_api_key="test",
        trakt_client_id="test",
        trakt_client_secret="test",
        self_chat_only_mode=True,
        evolution_polling_limit=5,
    )
    handled: list[tuple[str, str, str | None]] = []

    class FakeEvolutionClient:
        def __init__(self, settings_obj) -> None:
            self.settings = settings_obj

        async def fetch_recent_messages(self, remote_jid: str, limit: int = 10):
            if remote_jid == "121036657934449@lid":
                return [
                    {
                        "key": {"id": "img-1", "fromMe": True, "remoteJid": "121036657934449@lid"},
                        "messageTimestamp": 10,
                        "message": {"imageMessage": {"url": "https://example.com/img.jpg", "mimetype": "image/jpeg"}},
                    },
                    {
                        "key": {"id": "cmd-1", "fromMe": True, "remoteJid": "121036657934449@lid"},
                        "messageTimestamp": 11,
                        "message": {"conversation": "x-info"},
                    },
                ]
            return []

    async def fake_handle(normalized, settings_obj, db, **kwargs):
        handled.append((normalized.provider_message_id, normalized.message_type, normalized.text_body))
        return {"status": "accepted", "command": normalized.text_body}

    monkeypatch.setattr("app.main.EvolutionClient", FakeEvolutionClient)
    monkeypatch.setattr("app.main.handle_normalized_message", fake_handle)
    monkeypatch.setattr("app.main.SessionLocal", lambda: DummyContextManager(object()))

    await reconcile_recent_owner_messages(settings)

    assert handled == [("img-1", "image", None), ("cmd-1", "text", "x-info")]


@pytest.mark.asyncio
async def test_handle_normalized_message_routes_pending_confirmation(monkeypatch) -> None:
    settings = Settings.model_construct(
        evolution_base_url="https://example.com",
        evolution_api_key="test",
        evolution_instance="meu-whatsapp",
        evolution_owner_phone="5519988343888",
        openrouter_api_key="test",
        tmdb_api_token="test",
        omdb_api_key="test",
        trakt_client_id="test",
        trakt_client_secret="test",
    )
    calls: list[tuple[str, str]] = []

    class FakeMessageService:
        def __init__(self, settings_obj, db) -> None:
            pass

        async def persist_message(self, normalized):
            return SimpleNamespace(created=True)

        async def get_pending_identification(self, chat_jid: str, requester_phone: str | None = None):
            return PendingIdentificationState(mode="ambiguity", channel="whatsapp", options=[{"title": "The Gift"}])

    async def fake_process_x_info_confirmation(ctx: dict, chat_jid: str, requester_phone: str, selection: str) -> None:
        calls.append((chat_jid, selection))

    monkeypatch.setattr("app.main.MessageService", FakeMessageService)
    monkeypatch.setattr("app.main.process_x_info_confirmation", fake_process_x_info_confirmation)

    response = await handle_normalized_message(
        build_message(text_body="1"),
        settings,
        object(),
        force_inline_dispatch=True,
    )

    assert response == {"status": "accepted", "command": "x-info-confirmation"}
    assert calls == [("5519988343888@s.whatsapp.net", "1")]


@pytest.mark.asyncio
async def test_handle_normalized_message_auto_triggers_x_info_for_image(monkeypatch) -> None:
    settings = Settings.model_construct(
        evolution_base_url="https://example.com",
        evolution_api_key="test",
        evolution_instance="meu-whatsapp",
        evolution_owner_phone="5519988343888",
        openrouter_api_key="test",
        tmdb_api_token="test",
        omdb_api_key="test",
        trakt_client_id="test",
        trakt_client_secret="test",
    )
    calls: list[str] = []

    class FakeMessageService:
        def __init__(self, settings_obj, db) -> None:
            pass

        async def persist_message(self, normalized):
            return SimpleNamespace(created=True)

        async def get_pending_identification(self, chat_jid: str, requester_phone: str | None = None):
            return None

    async def fake_dispatch_command_inline(command: str, chat_jid: str, requester_phone: str, trigger_message_id: str | None = None):
        calls.append(command)

    monkeypatch.setattr("app.main.MessageService", FakeMessageService)
    monkeypatch.setattr("app.main.dispatch_command_inline", fake_dispatch_command_inline)

    response = await handle_normalized_message(
        build_message(message_type="image", text_body=None, media_url="https://example.com/image.jpg"),
        settings,
        object(),
        force_inline_dispatch=True,
    )

    assert response == {"status": "accepted", "command": "x-info"}
    assert calls == ["x-info"]
