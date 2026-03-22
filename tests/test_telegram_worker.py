from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.worker import process_telegram_x_info, process_telegram_x_save


class DummyContextManager:
    def __init__(self, value) -> None:
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


@pytest.mark.asyncio
async def test_process_telegram_x_info_updates_status_and_sends_final_message(monkeypatch) -> None:
    sent: list[str] = []
    edited: list[str] = []

    class FakeMessageService:
        def __init__(self, settings, db) -> None:
            pass

        async def get_message_by_provider_id(self, provider_message_id: str):
            return SimpleNamespace(received_at=123)

        async def find_latest_image(self, chat_jid: str, requester_phone: str | None = None, *, before_received_at=None):
            return SimpleNamespace(provider_message_id="img-1", media_url="telegram-file-1")

        async def save_identified_media(self, message, enriched) -> None:
            return None

    class FakeTelegram:
        async def send_text(self, chat_id: str, text: str) -> int | None:
            sent.append(text)
            return 10

        async def edit_text(self, chat_id: str, message_id: int, text: str) -> None:
            edited.append(text)

        async def send_chat_action(self, chat_id: str, action: str = "typing") -> None:
            return None

    class FakePipelineService:
        def __init__(self, settings) -> None:
            self.settings = SimpleNamespace(telegram_enable_chat_actions=True, telegram_enable_progress_messages=True)
            self.telegram = FakeTelegram()

        async def enrich_from_image(self, provider_message_id: str, media_url: str | None = None, **kwargs):
            return SimpleNamespace(
                title="Pulp Fiction",
                year=1994,
                media_type="movie",
                ratings={},
                providers=[],
                genres=[],
                release_date="1994-10-14",
                overview="Crime entrelacado.",
            )

        async def format_media_reply(self, enriched) -> str:
            return "Pulp Fiction (1994)"

        async def format_review_messages(self, enriched) -> list[str]:
            return ["Review 1"]

    monkeypatch.setattr("app.worker.get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr("app.worker.SessionLocal", lambda: DummyContextManager(object()))
    monkeypatch.setattr("app.worker.MessageService", FakeMessageService)
    monkeypatch.setattr("app.worker.PipelineService", FakePipelineService)

    await process_telegram_x_info({}, "321", "telegram_321", "cmd-1", 99)

    assert edited == [
        "[x-info] Etapa 2/6: localizando a ultima imagem valida.",
        "[x-info] Etapa 3/6: baixando a imagem do Telegram.",
        "[x-info] Etapa 4/6: analisando a imagem com o modelo de visao.",
        "[x-info] Etapa 5/6: consolidando catalogo, ratings e provedores.",
        "[x-info] Etapa 6/6: montando a resposta final.",
        "[x-info] Concluido com sucesso.",
    ]
    assert sent == ["Pulp Fiction (1994)", "Review 1"]


@pytest.mark.asyncio
async def test_process_telegram_x_save_reports_success(monkeypatch) -> None:
    sent: list[str] = []
    edited: list[str] = []

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

    class FakeMessageService:
        def __init__(self, settings, db) -> None:
            pass

        async def get_latest_identified_media(self, requester_phone: str):
            return SimpleNamespace(title="Pulp Fiction", media_type="movie", year=1994, imdb_id="tt0110912", tmdb_id=680)

    class FakeTelegram:
        async def send_text(self, chat_id: str, text: str) -> int | None:
            sent.append(text)
            return 10

        async def edit_text(self, chat_id: str, message_id: int, text: str) -> None:
            edited.append(text)

        async def send_chat_action(self, chat_id: str, action: str = "typing") -> None:
            return None

    class FakeTrakt:
        async def ensure_fresh_tokens(self, conn):
            return ("fresh-token", "refresh-2", None)

        async def add_to_watchlist(self, access_token, enriched) -> None:
            return None

    class FakePipelineService:
        def __init__(self, settings) -> None:
            self.settings = SimpleNamespace(telegram_enable_chat_actions=True, telegram_enable_progress_messages=True)
            self.telegram = FakeTelegram()
            self.trakt = FakeTrakt()

        def build_watchlist_item(self, identified):
            return SimpleNamespace(title=identified.title, media_type=identified.media_type, imdb_id=identified.imdb_id, tmdb_id=identified.tmdb_id)

    fake_db = FakeDB()
    monkeypatch.setattr("app.worker.get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr("app.worker.SessionLocal", lambda: DummyContextManager(fake_db))
    monkeypatch.setattr("app.worker.MessageService", FakeMessageService)
    monkeypatch.setattr("app.worker.PipelineService", FakePipelineService)

    await process_telegram_x_save({}, "321", "telegram_321", 88)

    assert fake_db.committed is True
    assert edited == [
        "[x-save] Etapa 2/5: validando o ultimo titulo identificado.",
        "[x-save] Etapa 3/5: validando sua conexao com o Trakt.",
        "[x-save] Etapa 4/5: enviando o titulo para a watchlist do Trakt.",
        "[x-save] Concluido com sucesso.",
    ]
    assert sent == ["Pulp Fiction foi salvo na sua watchlist do Trakt."]


@pytest.mark.asyncio
async def test_process_telegram_x_save_guides_user_to_trakt_connect_when_missing_connection(monkeypatch) -> None:
    sent: list[str] = []
    edited: list[str] = []

    profile = SimpleNamespace(id=1)

    class FakeResult:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class FakeDB:
        def __init__(self) -> None:
            self._results = [profile, None]

        async def execute(self, query):
            return FakeResult(self._results.pop(0))

        async def commit(self) -> None:
            return None

    class FakeMessageService:
        def __init__(self, settings, db) -> None:
            pass

        async def get_latest_identified_media(self, requester_phone: str):
            return SimpleNamespace(title="Pulp Fiction", media_type="movie", year=1994, imdb_id="tt0110912", tmdb_id=680)

    class FakeTelegram:
        async def send_text(self, chat_id: str, text: str) -> int | None:
            sent.append(text)
            return 10

        async def edit_text(self, chat_id: str, message_id: int, text: str) -> None:
            edited.append(text)

        async def send_chat_action(self, chat_id: str, action: str = "typing") -> None:
            return None

    class FakePipelineService:
        def __init__(self, settings) -> None:
            self.settings = SimpleNamespace(telegram_enable_chat_actions=True, telegram_enable_progress_messages=True)
            self.telegram = FakeTelegram()
            self.trakt = SimpleNamespace()

    fake_db = FakeDB()
    monkeypatch.setattr("app.worker.get_settings", lambda: SimpleNamespace(app_base_url="https://hooks.example", admin_shared_secret="secret"))
    monkeypatch.setattr("app.worker.SessionLocal", lambda: DummyContextManager(fake_db))
    monkeypatch.setattr("app.worker.MessageService", FakeMessageService)
    monkeypatch.setattr("app.worker.PipelineService", FakePipelineService)

    await process_telegram_x_save({}, "321", "telegram_321", 88)

    assert edited == [
        "[x-save] Etapa 2/5: validando o ultimo titulo identificado.",
        "[x-save] Etapa 3/5: validando sua conexao com o Trakt.",
        "[x-save] Falha durante o processamento.",
    ]
    assert sent == [
        "Falha ao salvar no Trakt: Conta Trakt ainda nao vinculada. Envie /trakt-connect, conclua a autorizacao da sua conta Trakt e depois envie x-save novamente."
    ]
