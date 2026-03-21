from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, db_dep, settings_dep
from app.schemas import NormalizedMessage
from app.services import PersistMessageResult


def build_settings() -> Settings:
    return Settings.model_construct(
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
    )


def build_payload(
    *,
    provider_id: str = "1",
    from_me: bool = True,
    phone: str = "5519988343888",
    text: str = "x-info",
    remote_jid: str | None = None,
    participant: str | None = None,
) -> dict:
    return {
        "event": "MESSAGES_UPSERT",
        "data": {
            "key": {
                "id": provider_id,
                "remoteJid": remote_jid or f"{phone}@s.whatsapp.net",
                "participant": participant or f"{phone}@s.whatsapp.net",
                "fromMe": from_me,
            },
            "message": {"conversation": text},
        },
    }


async def fake_db_dep() -> AsyncIterator[object]:
    yield object()


def test_webhook_ignores_foreign_number(monkeypatch) -> None:
    app.dependency_overrides[settings_dep] = lambda: build_settings()
    app.dependency_overrides[db_dep] = fake_db_dep
    called = False

    async def fake_persist(self, normalized: NormalizedMessage) -> PersistMessageResult:
        nonlocal called
        called = True
        return PersistMessageResult(message=SimpleNamespace(id=1), created=True)

    monkeypatch.setattr("app.main.MessageService.persist_message", fake_persist)

    with TestClient(app) as client:
        response = client.post("/webhooks/evolution/messages", json=build_payload(from_me=False, phone="5511999999999"))

    app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "reason": "self-chat-only"}
    assert called is False


def test_webhook_ignores_duplicate_event(monkeypatch) -> None:
    app.dependency_overrides[settings_dep] = lambda: build_settings()
    app.dependency_overrides[db_dep] = fake_db_dep

    async def fake_persist(self, normalized: NormalizedMessage) -> PersistMessageResult:
        return PersistMessageResult(message=SimpleNamespace(id=1), created=False)

    monkeypatch.setattr("app.main.MessageService.persist_message", fake_persist)

    with TestClient(app) as client:
        response = client.post("/webhooks/evolution/messages", json=build_payload(provider_id="dup-1"))

    app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "reason": "duplicate-event"}


def test_webhook_schedules_x_info_for_owner_self_chat(monkeypatch) -> None:
    app.dependency_overrides[settings_dep] = lambda: build_settings()
    app.dependency_overrides[db_dep] = fake_db_dep
    scheduled: list[tuple[str, str]] = []

    async def fake_persist(self, normalized: NormalizedMessage) -> PersistMessageResult:
        return PersistMessageResult(message=SimpleNamespace(id=1), created=True)

    async def fake_dispatch(command: str, chat_jid: str, requester_phone: str) -> None:
        scheduled.append((chat_jid, requester_phone))

    monkeypatch.setattr("app.main.MessageService.persist_message", fake_persist)
    monkeypatch.setattr("app.main.dispatch_command", fake_dispatch)

    with TestClient(app) as client:
        response = client.post("/webhooks/evolution/messages", json=build_payload(provider_id="ok-1"))

    app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json() == {"status": "accepted", "command": "x-info"}
    assert scheduled == [("5519988343888@s.whatsapp.net", "5519988343888")]


def test_webhook_schedules_x_info_for_owner_lid_chat(monkeypatch) -> None:
    app.dependency_overrides[settings_dep] = lambda: build_settings()
    app.dependency_overrides[db_dep] = fake_db_dep
    scheduled: list[tuple[str, str]] = []

    async def fake_persist(self, normalized: NormalizedMessage) -> PersistMessageResult:
        assert normalized.requester_phone == "5519988343888"
        assert normalized.sender_phone == "5519988343888"
        return PersistMessageResult(message=SimpleNamespace(id=1), created=True)

    async def fake_dispatch(command: str, chat_jid: str, requester_phone: str) -> None:
        scheduled.append((chat_jid, requester_phone))

    monkeypatch.setattr("app.main.MessageService.persist_message", fake_persist)
    monkeypatch.setattr("app.main.dispatch_command", fake_dispatch)

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/evolution/messages",
            json=build_payload(
                provider_id="lid-1",
                text="x-info",
                remote_jid="121036657934449@lid",
                participant="121036657934449@lid",
            ),
        )

    app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json() == {"status": "accepted", "command": "x-info"}
    assert scheduled == [("121036657934449@lid", "5519988343888")]
