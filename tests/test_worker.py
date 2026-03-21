from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.exceptions import AmbiguousTitleError, VisionIdentificationError
from app.worker import process_x_info, process_x_save


class DummyContextManager:
    def __init__(self, value) -> None:
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


@pytest.mark.asyncio
async def test_process_x_info_sends_ambiguity_message(monkeypatch) -> None:
    sent_messages: list[str] = []

    class FakeMessageService:
        def __init__(self, settings, db) -> None:
            pass

        async def find_latest_image(self, chat_jid: str, requester_phone: str | None = None):
            return SimpleNamespace(
                provider_message_id="abc123",
                media_url="https://example.com/poster.jpg",
                chat_jid=chat_jid,
                requester_phone="5511",
            )

        async def save_identified_media(self, message, enriched) -> None:
            raise AssertionError("ambiguous result must not be persisted")

    class FakeEvolution:
        async def send_text(self, chat_jid: str, text: str) -> None:
            sent_messages.append(text)

    class FakePipelineService:
        def __init__(self, settings) -> None:
            self.evolution = FakeEvolution()

        async def enrich_from_image(self, provider_message_id: str, media_url: str | None = None):
            assert provider_message_id == "abc123"
            assert media_url == "https://example.com/poster.jpg"
            raise AmbiguousTitleError(["Dark (2017) - Serie", "1899 (2022) - Serie"])

        async def format_ambiguous_reply(self, options: list[str]) -> str:
            return "\n".join(options)

    monkeypatch.setattr("app.worker.get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr("app.worker.SessionLocal", lambda: DummyContextManager(object()))
    monkeypatch.setattr("app.worker.MessageService", FakeMessageService)
    monkeypatch.setattr("app.worker.PipelineService", FakePipelineService)

    await process_x_info({}, "5519988343888@s.whatsapp.net", "5519988343888")

    assert sent_messages == ["Dark (2017) - Serie\n1899 (2022) - Serie"]


@pytest.mark.asyncio
async def test_process_x_save_uses_identified_ids_without_tmdb_lookup(monkeypatch) -> None:
    sent_messages: list[str] = []
    added_payloads: list[tuple[str | None, int | None, str]] = []

    profile = SimpleNamespace(id=1)
    connection = SimpleNamespace(access_token="old", refresh_token="refresh", expires_at=None)

    class FakeResult:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class FakeDB:
        def __init__(self) -> None:
            self._results = [profile, connection]
            self.committed = False

        async def execute(self, query):
            return FakeResult(self._results.pop(0))

        async def commit(self) -> None:
            self.committed = True

    fake_db = FakeDB()

    class FakeMessageService:
        def __init__(self, settings, db) -> None:
            pass

        async def get_latest_identified_media(self, requester_phone: str):
            return SimpleNamespace(
                title="Pulp Fiction",
                media_type="movie",
                year=1994,
                confidence=0.99,
                imdb_id="tt0110912",
                tmdb_id=680,
                overview="Crime entrelacado.",
                payload={"source": "tmdb"},
            )

    class FakeEvolution:
        async def send_text(self, chat_jid: str, text: str) -> None:
            sent_messages.append(text)

    class FakeTrakt:
        async def ensure_fresh_tokens(self, conn):
            return ("fresh-token", "fresh-refresh", None)

        async def add_to_watchlist(self, access_token, enriched) -> None:
            added_payloads.append((enriched.imdb_id, enriched.tmdb_id, access_token))

    class FakePipelineService:
        def __init__(self, settings) -> None:
            self.evolution = FakeEvolution()
            self.trakt = FakeTrakt()

        def build_watchlist_item(self, identified):
            return SimpleNamespace(
                title=identified.title,
                media_type=identified.media_type,
                year=identified.year,
                tmdb_id=identified.tmdb_id,
                imdb_id=identified.imdb_id,
            )

    monkeypatch.setattr("app.worker.get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr("app.worker.SessionLocal", lambda: DummyContextManager(fake_db))
    monkeypatch.setattr("app.worker.MessageService", FakeMessageService)
    monkeypatch.setattr("app.worker.PipelineService", FakePipelineService)

    await process_x_save({}, "5519988343888@s.whatsapp.net", "5519988343888")

    assert added_payloads == [("tt0110912", 680, "fresh-token")]
    assert fake_db.committed is True
    assert sent_messages == ["Pulp Fiction foi salvo na sua watchlist do Trakt."]


@pytest.mark.asyncio
async def test_process_x_info_reports_model_attempts_on_vision_failure(monkeypatch) -> None:
    sent_messages: list[str] = []

    class FakeMessageService:
        def __init__(self, settings, db) -> None:
            pass

        async def find_latest_image(self, chat_jid: str, requester_phone: str | None = None):
            return SimpleNamespace(
                provider_message_id="abc123",
                media_url="https://example.com/poster.jpg",
                chat_jid=chat_jid,
                requester_phone="5511",
            )

    class FakeEvolution:
        async def send_text(self, chat_jid: str, text: str) -> None:
            sent_messages.append(text)

    class FakePipelineService:
        def __init__(self, settings) -> None:
            self.evolution = FakeEvolution()

        async def enrich_from_image(self, provider_message_id: str, media_url: str | None = None):
            raise VisionIdentificationError(
                "Nao consegui identificar o titulo com confianca suficiente.",
                ["ocr: no confident local text match", "google/gemini-2.5-flash: RuntimeError"],
            )

    monkeypatch.setattr("app.worker.get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr("app.worker.SessionLocal", lambda: DummyContextManager(object()))
    monkeypatch.setattr("app.worker.MessageService", FakeMessageService)
    monkeypatch.setattr("app.worker.PipelineService", FakePipelineService)

    await process_x_info({}, "5519988343888@s.whatsapp.net", "5519988343888")

    assert sent_messages == [
        "Falha ao analisar a imagem para o x-info.\n"
        "Motivo: Nao consegui identificar o titulo com confianca suficiente.\n"
        "Modelos e etapas testados:\n"
        "- ocr: no confident local text match\n"
        "- google/gemini-2.5-flash: RuntimeError\n"
        "\n"
        "Se esta imagem for um print do Instagram/WhatsApp, envie uma captura mais fechada no poster ou frame."
    ]
