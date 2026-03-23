from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.exceptions import AmbiguousTitleError, VisionIdentificationError
from app.schemas import PendingIdentificationState
from app.worker import process_x_info, process_x_info_confirmation, process_x_save, process_x_watchlist_reply


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

        async def get_message_by_provider_id(self, provider_message_id: str):
            assert provider_message_id == "cmd-1"
            return SimpleNamespace(received_at=123)

        async def find_latest_image(
            self, chat_jid: str, requester_phone: str | None = None, *, before_received_at=None
        ):
            assert before_received_at == 123
            return SimpleNamespace(
                provider_message_id="abc123",
                media_url="https://example.com/poster.jpg",
                chat_jid=chat_jid,
                requester_phone="5511",
            )

        async def save_identified_media(self, message, enriched) -> None:
            raise AssertionError("ambiguous result must not be persisted")

        async def store_pending_identification(self, chat_jid: str, requester_phone: str, pending) -> None:
            sent_messages.append(f"pending:{pending.mode}:{len(pending.options)}")

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

        def build_pending_options(self, options: list[str], *, channel: str, image_message_id: int | None = None):
            return PendingIdentificationState(mode="ambiguity", channel=channel, image_message_id=image_message_id, options=[])

        async def format_review_messages(self, enriched) -> list[str]:
            return []

        async def format_watchlist_question(self, enriched) -> str:
            return "Voce quer salvar este filme na sua watchlist do Trakt? Responda aqui com sim ou nao."

    monkeypatch.setattr("app.worker.get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr("app.worker.SessionLocal", lambda: DummyContextManager(object()))
    monkeypatch.setattr("app.worker.MessageService", FakeMessageService)
    monkeypatch.setattr("app.worker.PipelineService", FakePipelineService)

    await process_x_info({}, "5519988343888@s.whatsapp.net", "5519988343888", "cmd-1")

    assert sent_messages == ["pending:ambiguity:0", "Dark (2017) - Serie\n1899 (2022) - Serie"]


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

        async def clear_pending_identification(self, chat_jid: str, requester_phone: str | None = None) -> None:
            return None

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

        async def get_message_by_provider_id(self, provider_message_id: str):
            return SimpleNamespace(received_at=123)

        async def find_latest_image(
            self, chat_jid: str, requester_phone: str | None = None, *, before_received_at=None
        ):
            return SimpleNamespace(
                provider_message_id="abc123",
                media_url="https://example.com/poster.jpg",
                chat_jid=chat_jid,
                requester_phone="5511",
            )

        async def store_pending_identification(self, chat_jid: str, requester_phone: str, pending) -> None:
            return None

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

        async def format_review_messages(self, enriched) -> list[str]:
            return []

        def build_pending_manual_input(self, *, channel: str, image_message_id: int | None = None, attempts=None):
            return PendingIdentificationState(mode="manual-input", channel=channel, attempts=attempts or [])

        async def format_manual_help_reply(self, attempts: list[str]) -> str:
            return (
                "Nao consegui identificar o titulo com confianca suficiente, mesmo apos tentar OCR e modelos de visao mais fortes.\n"
                "Se voce souber o titulo, responda nesta conversa com algo como `The Gift (2015)` ou `Coherence (2013)`.\n"
                "Se preferir, envie outra imagem ou mais contexto e eu tento de novo.\n\n"
                "Modelos e etapas testados:\n"
                "- ocr: no confident local text match\n"
                "- google/gemini-2.5-flash: RuntimeError"
            )

    monkeypatch.setattr("app.worker.get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr("app.worker.SessionLocal", lambda: DummyContextManager(object()))
    monkeypatch.setattr("app.worker.MessageService", FakeMessageService)
    monkeypatch.setattr("app.worker.PipelineService", FakePipelineService)

    await process_x_info({}, "5519988343888@s.whatsapp.net", "5519988343888", "cmd-1")

    assert sent_messages == [
        "Nao consegui identificar o titulo com confianca suficiente, mesmo apos tentar OCR e modelos de visao mais fortes.\n"
        "Se voce souber o titulo, responda nesta conversa com algo como `The Gift (2015)` ou `Coherence (2013)`.\n"
        "Se preferir, envie outra imagem ou mais contexto e eu tento de novo.\n\n"
        "Modelos e etapas testados:\n"
        "- ocr: no confident local text match\n"
        "- google/gemini-2.5-flash: RuntimeError"
    ]


@pytest.mark.asyncio
async def test_process_x_save_reports_connect_link_when_trakt_missing(monkeypatch) -> None:
    sent_messages: list[str] = []

    class FakeResult:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class FakeDB:
        async def execute(self, query):
            return FakeResult(None)

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

    class FakePipelineService:
        def __init__(self, settings) -> None:
            self.evolution = FakeEvolution()
            self.trakt = SimpleNamespace()

    monkeypatch.setattr(
        "app.worker.get_settings",
        lambda: SimpleNamespace(app_base_url="https://trakt-sync.example.com", admin_shared_secret="secret"),
    )
    monkeypatch.setattr("app.worker.SessionLocal", lambda: DummyContextManager(FakeDB()))
    monkeypatch.setattr("app.worker.MessageService", FakeMessageService)
    monkeypatch.setattr("app.worker.PipelineService", FakePipelineService)

    await process_x_save({}, "5519988343888@s.whatsapp.net", "5519988343888")

    assert sent_messages == [
        "Falha ao salvar no Trakt: Telefone sem perfil cadastrado para Trakt. "
        "Abra https://trakt-sync.example.com/admin/trakt/connect/5519988343888?token=secret "
        "para conectar sua conta Trakt e depois envie x-save novamente."
    ]


@pytest.mark.asyncio
async def test_process_x_info_confirmation_reuses_user_selection_and_persists(monkeypatch) -> None:
    sent_messages: list[str] = []
    saved: list[str] = []

    class FakeMessageService:
        def __init__(self, settings, db) -> None:
            pass

        async def get_pending_identification(self, chat_jid: str, requester_phone: str):
            return PendingIdentificationState(
                mode="ambiguity",
                channel="whatsapp",
                image_message_id=12,
                options=[
                    {"label": "The Gift (2015) - Filme", "title": "The Gift", "year": 2015, "media_type": "movie"}
                ],
            )

        async def get_message_by_id(self, message_id: int):
            assert message_id == 12
            return SimpleNamespace(id=12, requester_phone="5511", chat_jid="5511@s.whatsapp.net")

        async def save_identified_media(self, message, enriched) -> None:
            saved.append(enriched.title)

        async def store_pending_identification(self, chat_jid: str, requester_phone: str, pending) -> None:
            saved.append(pending.mode)

    class FakeEvolution:
        async def send_text(self, chat_jid: str, text: str) -> None:
            sent_messages.append(text)

    class FakePipelineService:
        def __init__(self, settings) -> None:
            self.evolution = FakeEvolution()

        async def enrich_from_user_confirmation(self, selection: str, pending):
            assert selection == "1"
            return SimpleNamespace(
                title="The Gift",
                original_title="The Gift",
                localized_title="O Presente",
                year=2015,
                media_type="movie",
                ratings={},
                providers=[],
                genres=[],
                release_date="2015-08-07",
                overview="Thriller.",
            )

        async def format_whatsapp_reply(self, enriched) -> str:
            return "O Presente (2015)\nTitulo original: The Gift\nTitulo em portugues: O Presente"

        async def format_review_messages(self, enriched) -> list[str]:
            return ["Review 1"]

        def build_pending_watchlist_confirmation(self, *, channel: str, identified_media_id: int | None = None):
            return PendingIdentificationState(mode="watchlist-confirmation", channel=channel)

        async def format_watchlist_question(self, enriched) -> str:
            return "Voce quer salvar este filme na sua watchlist do Trakt? Responda aqui com sim ou nao."

    monkeypatch.setattr("app.worker.get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr("app.worker.SessionLocal", lambda: DummyContextManager(object()))
    monkeypatch.setattr("app.worker.MessageService", FakeMessageService)
    monkeypatch.setattr("app.worker.PipelineService", FakePipelineService)

    await process_x_info_confirmation({}, "5511@s.whatsapp.net", "5511", "1")

    assert saved == ["The Gift", "watchlist-confirmation"]
    assert sent_messages == [
        "O Presente (2015)\nTitulo original: The Gift\nTitulo em portugues: O Presente",
        "Review 1",
        "Voce quer salvar este filme na sua watchlist do Trakt? Responda aqui com sim ou nao.",
    ]


@pytest.mark.asyncio
async def test_process_x_watchlist_reply_accepts_no_and_clears_pending(monkeypatch) -> None:
    sent_messages: list[str] = []
    cleared: list[str] = []

    class FakeMessageService:
        def __init__(self, settings, db) -> None:
            pass

        async def get_pending_identification(self, chat_jid: str, requester_phone: str):
            return PendingIdentificationState(mode="watchlist-confirmation", channel="whatsapp")

        async def clear_pending_identification(self, chat_jid: str, requester_phone: str | None = None) -> None:
            cleared.append(chat_jid)

    class FakeEvolution:
        async def send_text(self, chat_jid: str, text: str) -> None:
            sent_messages.append(text)

    class FakePipelineService:
        def __init__(self, settings) -> None:
            self.evolution = FakeEvolution()

        async def format_watchlist_declined(self) -> str:
            return "Certo, nao vou salvar este titulo na watchlist do Trakt."

        async def format_watchlist_retry(self) -> str:
            return "retry"

    monkeypatch.setattr("app.worker.get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr("app.worker.SessionLocal", lambda: DummyContextManager(object()))
    monkeypatch.setattr("app.worker.MessageService", FakeMessageService)
    monkeypatch.setattr("app.worker.PipelineService", FakePipelineService)

    await process_x_watchlist_reply({}, "5511@s.whatsapp.net", "5511", "nao")

    assert cleared == ["5511@s.whatsapp.net"]
    assert sent_messages == ["Certo, nao vou salvar este titulo na watchlist do Trakt."]
