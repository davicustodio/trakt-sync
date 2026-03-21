from app.config import Settings
from app.auth import is_authorized_self_chat
from app.main import dispatch_command
from app.schemas import NormalizedMessage

import pytest


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


class FakeRedis:
    def __init__(self, worker_health: bytes | None) -> None:
        self.worker_health = worker_health
        self.jobs: list[tuple[str, tuple[str, ...]]] = []
        self.closed = False

    async def get(self, key: str) -> bytes | None:
        return self.worker_health

    async def enqueue_job(self, name: str, *args: str) -> None:
        self.jobs.append((name, args))

    async def close(self, close_connection_pool: bool = True) -> None:
        self.closed = close_connection_pool


@pytest.mark.asyncio
async def test_dispatch_command_falls_back_to_local_task_without_worker_health(monkeypatch) -> None:
    redis = FakeRedis(worker_health=None)
    scheduled: list[tuple[str, tuple[object, ...]]] = []

    async def fake_process_x_info(ctx: dict, chat_jid: str, requester_phone: str) -> None:
        return None

    def fake_create_task(coro) -> str:
        scheduled.append((coro.cr_code.co_name, coro.cr_frame.f_locals["chat_jid"], coro.cr_frame.f_locals["requester_phone"]))
        coro.close()
        return "task"

    async def fake_redis_pool() -> FakeRedis:
        return redis

    monkeypatch.setattr("app.main.get_redis_pool", fake_redis_pool)
    monkeypatch.setattr("app.main.process_x_info", fake_process_x_info)
    monkeypatch.setattr("app.main.asyncio.create_task", fake_create_task)

    await dispatch_command("x-info", "5511999999999@s.whatsapp.net", "5511999999999")

    assert redis.jobs == []
    assert redis.closed is True
    assert scheduled == [("fake_process_x_info", "5511999999999@s.whatsapp.net", "5511999999999")]


@pytest.mark.asyncio
async def test_dispatch_command_enqueues_when_worker_health_is_present(monkeypatch) -> None:
    redis = FakeRedis(worker_health=b"healthy")
    scheduled: list[str] = []

    def fake_create_task(coro) -> str:
        scheduled.append("called")
        coro.close()
        return "task"

    async def fake_redis_pool() -> FakeRedis:
        return redis

    monkeypatch.setattr("app.main.get_redis_pool", fake_redis_pool)
    monkeypatch.setattr("app.main.asyncio.create_task", fake_create_task)

    await dispatch_command("x-info", "5511999999999@s.whatsapp.net", "5511999999999")

    assert redis.jobs == [("process_x_info", ("5511999999999@s.whatsapp.net", "5511999999999"))]
    assert redis.closed is True
    assert scheduled == []
