from fastapi import BackgroundTasks
import pytest

from app.config import Settings
from app.auth import is_authorized_self_chat
from app.main import dispatch_command
from app.schemas import NormalizedMessage


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

    async def fake_process_x_info(ctx: dict, chat_jid: str, requester_phone: str) -> None:
        return None

    def fake_add_task(func, *args, **kwargs) -> None:
        calls.append((func.__name__, args))

    monkeypatch.setattr("app.main.process_x_info", fake_process_x_info)
    background_tasks = BackgroundTasks()
    monkeypatch.setattr(background_tasks, "add_task", fake_add_task)

    await dispatch_command("x-info", "5511999999999@s.whatsapp.net", "5511999999999", background_tasks)

    assert calls == [("fake_process_x_info", ({}, "5511999999999@s.whatsapp.net", "5511999999999"))]


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
