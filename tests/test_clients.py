from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw, ImageFont

from app.clients import EvolutionClient, LetterboxdReviewClient, OpenRouterClient, TMDbClient, TelegramClient
from app.config import Settings
from app.exceptions import VisionIdentificationError
from app.schemas import VisionCandidate


def build_settings() -> Settings:
    return Settings.model_construct(
        evolution_base_url="https://example.com",
        evolution_api_key="test",
        evolution_instance="meu-whatsapp",
        evolution_owner_phone="5519988343888",
        evolution_owner_lid="121036657934449@lid",
        telegram_bot_token="telegram-token",
        openrouter_api_key="test",
        tmdb_api_token="test",
        omdb_api_key="test",
        trakt_client_id="test",
        trakt_client_secret="test",
    )


@pytest.mark.asyncio
async def test_fetch_media_bytes_prefers_evolution_base64_media(monkeypatch) -> None:
    class FakeResponse:
        status_code = 201

        def json(self) -> dict[str, str]:
            return {"base64": base64.b64encode(b"decoded-bytes").decode("ascii")}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            self.base_url = kwargs.get("base_url")

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, path: str, headers: dict[str, str], json: dict[str, object]) -> FakeResponse:
            assert path == "/chat/getBase64FromMediaMessage/meu-whatsapp"
            assert json == {"message": {"key": {"id": "abc123"}}, "convertToMp4": False}
            return FakeResponse()

    monkeypatch.setattr("app.clients.httpx.AsyncClient", FakeAsyncClient)

    client = EvolutionClient(build_settings())
    payload = await client.fetch_media_bytes("abc123", "https://example.com/poster.jpg")

    assert payload == b"decoded-bytes"


@pytest.mark.asyncio
async def test_send_text_maps_owner_lid_to_owner_phone(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, path: str, headers: dict[str, str], json: dict[str, object]) -> FakeResponse:
            calls.append({"path": path, "json": json})
            return FakeResponse()

    monkeypatch.setattr("app.clients.httpx.AsyncClient", FakeAsyncClient)

    client = EvolutionClient(build_settings())
    await client.send_text("121036657934449@lid", "teste")

    assert calls == [
        {
            "path": "/message/sendText/meu-whatsapp",
            "json": {
                "number": "5519988343888",
                "text": "teste",
                "textMessage": {"text": "teste"},
                "options": {"delay": 0},
            },
        }
    ]


@pytest.mark.asyncio
async def test_fetch_media_bytes_falls_back_to_media_url_with_ssl_bypass(monkeypatch) -> None:
    calls: list[bool] = []

    class Base64Response:
        status_code = 500

        def json(self) -> dict[str, str]:
            return {}

    class MediaResponse:
        content = b"image-bytes"

        def raise_for_status(self) -> None:
            return None

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            self.verify = kwargs.get("verify")
            if self.verify is not None:
                calls.append(self.verify)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, path: str, headers: dict[str, str], json: dict[str, object]) -> Base64Response:
            return Base64Response()

        async def get(self, media_url: str) -> MediaResponse:
            assert media_url == "https://example.com/poster.jpg"
            return MediaResponse()

    monkeypatch.setattr("app.clients.httpx.AsyncClient", FakeAsyncClient)

    client = EvolutionClient(build_settings())
    payload = await client.fetch_media_bytes("abc123", "https://example.com/poster.jpg")

    assert payload == b"image-bytes"
    assert calls == [False]


@pytest.mark.asyncio
async def test_telegram_send_text_returns_message_id(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"result": {"message_id": 44}}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            self.base_url = kwargs.get("base_url")

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, path: str, json: dict[str, object]) -> FakeResponse:
            calls.append((path, json))
            return FakeResponse()

    monkeypatch.setattr("app.clients.httpx.AsyncClient", FakeAsyncClient)

    client = TelegramClient(build_settings())
    message_id = await client.send_text("321", "teste")

    assert message_id == 44
    assert calls == [
        (
            "/sendMessage",
            {"chat_id": "321", "text": "teste"},
        )
    ]


@pytest.mark.asyncio
async def test_telegram_fetch_media_bytes_uses_get_file_and_download(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    class FileResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"result": {"file_path": "photos/file_1.jpg"}}

    class DownloadResponse:
        content = b"telegram-image"

        def raise_for_status(self) -> None:
            return None

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            self.base_url = kwargs.get("base_url")

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, path: str, json: dict[str, object]) -> FileResponse:
            calls.append(("post", path))
            assert json == {"file_id": "file-123"}
            return FileResponse()

        async def get(self, url: str) -> DownloadResponse:
            calls.append(("get", url))
            return DownloadResponse()

    monkeypatch.setattr("app.clients.httpx.AsyncClient", FakeAsyncClient)

    client = TelegramClient(build_settings())
    payload = await client.fetch_media_bytes("file-123")

    assert payload == b"telegram-image"
    assert calls == [
        ("post", "/getFile"),
        ("get", "https://api.telegram.org/file/bottelegram-token/photos/file_1.jpg"),
    ]


def test_identify_title_uses_local_ocr_for_title_cards() -> None:
    image = Image.new("RGB", (1200, 800), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.load_default(size=40)
    except TypeError:
        font = ImageFont.load_default()
    draw.text((50, 100), "TAKEN (2008)", fill="black", font=font)
    draw.text((50, 180), "Liam Neeson", fill="black", font=font)

    from io import BytesIO

    buffer = BytesIO()
    image.save(buffer, format="PNG")

    client = OpenRouterClient(build_settings())
    candidate = client._identify_title_from_ocr(buffer.getvalue())

    assert candidate is not None
    assert candidate.detected_title == "TAKEN"
    assert candidate.year == 2008
    assert candidate.media_type == "movie"


def test_identify_title_uses_ocr_for_visible_title_without_year() -> None:
    image = Image.new("RGB", (900, 1200), "white")
    draw = ImageDraw.Draw(image)
    try:
        title_font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 72)
    except OSError:
        title_font = ImageFont.load_default()
    draw.text((80, 700), "INLAND EMPIRE", fill="black", font=title_font)
    draw.text((80, 120), "movifiedbollywood", fill="gray", font=title_font)

    from io import BytesIO

    buffer = BytesIO()
    image.save(buffer, format="PNG")

    client = OpenRouterClient(build_settings())
    candidate = client._identify_title_from_ocr(buffer.getvalue())

    assert candidate is not None
    assert candidate.detected_title == "Inland Empire"
    assert candidate.year is None


def test_extract_title_from_context_lines_handles_social_header_clues() -> None:
    client = OpenRouterClient(build_settings())

    title = client._extract_title_from_context_lines(
        [
            "J Jeff Garber - Virgin River Main",
            "final de semana",
        ]
    )

    assert title == "Virgin River"


@pytest.mark.asyncio
async def test_letterboxd_review_client_extracts_visible_reviews(monkeypatch) -> None:
    html = """
    <section class="film-reviews">
      <div class="body-text -prose -reset js-review-body js-collapsible-text"><p>First original review.</p></div>
      <div class="body-text -prose -reset js-review-body js-collapsible-text" data-full-text-url="/s/full-text/viewing:1/"><div class="collapsed-text"><p>Truncated review...</p></div></div>
      <div class="body-text -prose -reset js-review-body js-collapsible-text"><p>Second original review.</p></div>
    </section>
    """

    class FakeResponse:
        status_code = 200
        text = html

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url: str, headers: dict[str, str]) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr("app.clients.httpx.AsyncClient", FakeAsyncClient)

    client = LetterboxdReviewClient(build_settings())
    reviews = await client.fetch_reviews("Pulp Fiction")

    assert reviews == ["First original review.", "Second original review."]


@pytest.mark.asyncio
async def test_identify_title_falls_back_when_local_ocr_raises(monkeypatch) -> None:
    client = OpenRouterClient(build_settings())
    client._ocr_engine = object()
    client._ocr_backend = "rapidocr"

    def boom(path: str):
        raise RuntimeError("ocr failed")

    client._ocr_engine = boom

    async def fake_query_candidate(
        image_b64: str,
        prompt: str,
        *,
        use_json_mode: bool,
        models=None,
    ) -> tuple[VisionCandidate, list[str]]:
        return (
            VisionCandidate(detected_title="Inland Empire", media_type="movie", year=2006, confidence=0.95),
            ["google/gemini-2.5-flash: Inland Empire"],
        )

    monkeypatch.setattr(client, "_query_candidate", fake_query_candidate)

    candidate = await client.identify_title(b"fake-image-bytes")

    assert candidate.detected_title == "Inland Empire"
    assert candidate.year == 2006


@pytest.mark.asyncio
async def test_identify_title_uses_llm_to_normalize_collapsed_ocr_title(monkeypatch) -> None:
    client = OpenRouterClient(build_settings())

    monkeypatch.setattr(
        client,
        "_identify_title_from_ocr",
        lambda image_bytes: VisionCandidate(
            detected_title="Inlandempire",
            media_type="unknown",
            confidence=0.9,
            visible_text=["INLANDEMPIRE"],
        ),
    )

    captured_prompts: list[str] = []

    async def fake_query_candidate(
        image_b64: str,
        prompt: str,
        *,
        use_json_mode: bool,
        models=None,
    ) -> tuple[VisionCandidate, list[str]]:
        captured_prompts.append(prompt)
        return (
            VisionCandidate(detected_title="Inland Empire", media_type="movie", year=2006, confidence=0.96),
            ["google/gemini-2.5-flash: Inland Empire"],
        )

    monkeypatch.setattr(client, "_query_candidate", fake_query_candidate)

    candidate = await client.identify_title(b"fake-image")

    assert candidate.detected_title == "Inland Empire"
    assert "restore the natural title spacing" in captured_prompts[0]


@pytest.mark.asyncio
async def test_identify_title_tries_assertive_paid_models_when_initial_pass_is_weak(monkeypatch) -> None:
    client = OpenRouterClient(build_settings())
    client.settings.openrouter_paid_vision_models = ["google/gemini-2.5-flash", "openai/gpt-4.1-mini"]

    monkeypatch.setattr(client, "_identify_title_from_ocr", lambda image_bytes: None)

    calls: list[list[str] | None] = []

    async def fake_query_candidate(image_b64: str, prompt: str, *, use_json_mode: bool, models=None):
        calls.append(models)
        if models is None:
            return VisionCandidate(confidence=0.3), ["free-model: empty title"]
        return (
            VisionCandidate(detected_title="The Gift", media_type="movie", year=2015, confidence=0.88),
            ["openai/gpt-4.1-mini: The Gift (confidence=0.88, type=movie)"],
        )

    monkeypatch.setattr(client, "_query_candidate", fake_query_candidate)
    monkeypatch.setattr(client, "_build_llm_vision_variants", lambda image_bytes: [("original", image_bytes)])

    candidate = await client.identify_title(b"fake-image")

    assert candidate.detected_title == "The Gift"
    assert calls[0] is None
    assert calls[1][0] == "google/gemini-3-flash-preview"


@pytest.mark.asyncio
async def test_refine_title_from_user_feedback_uses_llm_result(monkeypatch) -> None:
    client = OpenRouterClient(build_settings())

    async def fake_run_text_json_task(prompt: str):
        assert "Resposta do usuario" in prompt
        return {
            "detected_title": "The Gift",
            "media_type": "movie",
            "year": 2015,
            "confidence": 0.98,
            "alt_titles": ["The Gift (2015)", "The Gift (2000)"],
            "visible_text": ["user said option 1"],
            "need_clarification": False,
        }

    monkeypatch.setattr(client, "_run_text_json_task", fake_run_text_json_task)

    candidate = await client.refine_title_from_user_feedback(
        "1",
        [{"label": "The Gift (2015) - Filme", "title": "The Gift", "year": 2015, "media_type": "movie"}],
    )

    assert candidate.detected_title == "The Gift"
    assert candidate.year == 2015


@pytest.mark.asyncio
async def test_query_candidate_only_uses_explicit_model_sequence(monkeypatch) -> None:
    requested_models: list[str] = []

    class FakeResponse:
        def __init__(self, model: str) -> None:
            self.model = model

        def raise_for_status(self) -> None:
            if self.model.endswith(":free"):
                raise RuntimeError("free model failed")

        def json(self) -> dict:
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"detected_title":"Inland Empire","media_type":"movie","year":2006,"confidence":0.97}'
                        }
                    }
                ]
            }

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, path: str, headers: dict[str, str], json: dict[str, object]) -> FakeResponse:
            requested_models.append(str(json["model"]))
            return FakeResponse(str(json["model"]))

    monkeypatch.setattr("app.clients.httpx.AsyncClient", FakeAsyncClient)

    settings = build_settings()
    settings.openrouter_vision_models = ["google/gemma-3-27b-it:free"]
    settings.openrouter_paid_vision_models = ["google/gemini-2.5-flash"]
    client = OpenRouterClient(settings)

    candidate, attempts = await client._query_candidate("ZmFrZQ==", "prompt", use_json_mode=True)

    assert requested_models == ["google/gemma-3-27b-it:free"]
    assert candidate.detected_title is None
    assert attempts == ["google/gemma-3-27b-it:free: RuntimeError"]


@pytest.mark.asyncio
async def test_identify_title_uses_paid_scene_rescue_after_free_failures(monkeypatch) -> None:
    client = OpenRouterClient(build_settings())
    client.settings.openrouter_vision_models = ["free-a:free", "free-b:free"]
    client.settings.openrouter_paid_vision_models = ["google/gemini-2.5-flash-lite", "openai/gpt-4.1-mini"]

    monkeypatch.setattr(client, "_identify_title_from_ocr", lambda image_bytes: None)
    monkeypatch.setattr(client, "_build_llm_vision_variants", lambda image_bytes: [("original", image_bytes)])

    calls: list[tuple[list[str] | None, bool]] = []

    async def fake_query_candidate(image_b64: str, prompt: str, *, use_json_mode: bool, models=None):
        calls.append((models, use_json_mode))
        if models is None:
            return VisionCandidate(), ["free-a: RuntimeError", "free-b: RuntimeError"]
        return (
            VisionCandidate(detected_title="Hostage", media_type="movie", year=2005, confidence=0.62),
            ["google/gemini-2.5-flash-lite: Hostage (confidence=0.62, type=movie)"],
        )

    monkeypatch.setattr(client, "_query_candidate", fake_query_candidate)

    candidate = await client.identify_title(b"fake-image")

    assert candidate.detected_title == "Hostage"
    assert candidate.year == 2005
    assert calls[0] == (None, True)
    assert calls[1][1] is True
    assert "openai/gpt-4.1-mini" in (calls[1][0] or [])


@pytest.mark.asyncio
async def test_identify_title_stops_when_time_budget_is_exceeded(monkeypatch) -> None:
    client = OpenRouterClient(build_settings())
    client.settings.openrouter_vision_total_timeout_seconds = 0.01
    monkeypatch.setattr(client, "_identify_title_from_ocr", lambda image_bytes: None)

    async def fake_query_candidate(image_b64: str, prompt: str, *, use_json_mode: bool, models=None):
        await asyncio.sleep(0.05)
        return VisionCandidate(), ["slow-model: timeout"]

    monkeypatch.setattr(client, "_query_candidate", fake_query_candidate)

    with pytest.raises(VisionIdentificationError) as exc_info:
        await client.identify_title(b"fake-image")

    assert "vision-time-budget-exceeded" in exc_info.value.attempts


@pytest.mark.asyncio
async def test_translate_reviews_tries_multiple_free_models_until_success(monkeypatch) -> None:
    requested_models: list[str] = []

    class FakeResponse:
        def __init__(self, model: str) -> None:
            self.model = model

        def raise_for_status(self) -> None:
            if self.model != "model-c:free":
                raise RuntimeError("model failed")

        def json(self) -> dict:
            return {"choices": [{"message": {"content": '{"reviews":["Review em pt-BR"]}'}}]}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, path: str, headers: dict[str, str], json: dict[str, object]) -> FakeResponse:
            requested_models.append(str(json["model"]))
            return FakeResponse(str(json["model"]))

    monkeypatch.setattr("app.clients.httpx.AsyncClient", FakeAsyncClient)

    settings = build_settings()
    settings.openrouter_free_text_models = ["model-a:free", "model-b:free", "model-c:free"]
    settings.openrouter_emergency_router = "openrouter/free"
    OpenRouterClient._free_text_models_cache = None
    OpenRouterClient._free_text_models_updated_at = None
    client = OpenRouterClient(settings)
    OpenRouterClient._free_text_models_cache = ["model-a:free", "model-b:free", "model-c:free"]

    translated = await client.translate_reviews_to_pt_br(["Original review"], title="Movie")

    assert translated == ["Review em pt-BR"]
    assert requested_models == ["model-a:free", "model-b:free", "model-c:free"]


@pytest.mark.asyncio
async def test_refresh_free_text_models_if_due_updates_cache_and_file(monkeypatch, tmp_path) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "data": [
                    {
                        "id": "paid-model",
                        "pricing": {"prompt": "0.1", "completion": "0.2"},
                        "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
                        "context_length": 128000,
                    },
                    {
                        "id": "free-vision-only:free",
                        "pricing": {"prompt": "0", "completion": "0"},
                        "architecture": {"input_modalities": ["image"], "output_modalities": ["text"]},
                        "context_length": 128000,
                    },
                    {
                        "id": "openai/gpt-oss-120b:free",
                        "pricing": {"prompt": "0", "completion": "0"},
                        "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
                        "context_length": 128000,
                    },
                    {
                        "id": "another-free:free",
                        "pricing": {"prompt": "0", "completion": "0"},
                        "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
                        "context_length": 64000,
                    },
                ]
            }

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, path: str, headers: dict[str, str]) -> FakeResponse:
            assert path == "/models"
            return FakeResponse()

    monkeypatch.setattr("app.clients.httpx.AsyncClient", FakeAsyncClient)

    settings = build_settings()
    settings.openrouter_free_text_models = ["openai/gpt-oss-120b:free", "openrouter/free"]
    settings.openrouter_free_models_cache_file = str(tmp_path / "openrouter_free_models.json")
    settings.openrouter_free_models_refresh_interval_seconds = 1
    OpenRouterClient._free_text_models_cache = None
    OpenRouterClient._free_text_models_updated_at = None

    client = OpenRouterClient(settings)
    await client.refresh_free_text_models_if_due()

    assert client._text_task_model_sequence()[:2] == ["openai/gpt-oss-120b:free", "another-free:free"]
    cache_payload = json.loads((tmp_path / "openrouter_free_models.json").read_text(encoding="utf-8"))
    assert cache_payload["models"][:2] == ["openai/gpt-oss-120b:free", "another-free:free"]


def test_identify_title_falls_back_to_legacy_ocr_backend() -> None:
    client = OpenRouterClient(build_settings())
    client._ocr_backend = "rapidocr"

    class BrokenRapidOCR:
        def __call__(self, path: str):
            raise RuntimeError("rapidocr failed")

    class LegacyRapidOCR:
        def __call__(self, path: str):
            return (
                [
                    (
                        [[10, 10], [300, 10], [300, 60], [10, 60]],
                        "INLAND EMPIRE",
                        0.98,
                    )
                ],
                None,
            )

    client._ocr_engine = BrokenRapidOCR()
    client._build_legacy_ocr_engine = lambda: setattr(client, "_ocr_engine", LegacyRapidOCR()) or client._ocr_engine

    candidate = client._identify_title_from_ocr(b"fake-image-bytes")

    assert candidate is not None
    assert candidate.detected_title == "Inland Empire"


@pytest.mark.asyncio
async def test_tmdb_prefers_unique_exact_title_and_year_match(monkeypatch) -> None:
    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self.payload

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, endpoint: str, headers: dict[str, str], params: dict[str, str]) -> FakeResponse:
            if endpoint == "/search/multi":
                return FakeResponse(
                    {
                        "results": [
                            {
                                "id": 680,
                                "media_type": "movie",
                                "title": "Taken",
                                "original_title": "Taken",
                                "release_date": "2008-02-18",
                                "popularity": 50,
                            },
                            {
                                "id": 2155,
                                "media_type": "movie",
                                "title": "Taken",
                                "original_title": "Taken",
                                "release_date": "1999-09-15",
                                "popularity": 52,
                            },
                            {
                                "id": 1227773,
                                "media_type": "movie",
                                "title": "TAKEN",
                                "original_title": "TAKEN",
                                "release_date": "2025-01-01",
                                "popularity": 80,
                            },
                        ]
                    }
                )
            return FakeResponse(
                {
                    "id": 680,
                    "title": "Busca Implacável",
                    "original_title": "Taken",
                    "release_date": "2008-02-18",
                    "overview": "Bryan Mills parte para o resgate.",
                    "genres": [{"name": "Acao"}],
                    "vote_average": 7.4,
                    "external_ids": {"imdb_id": "tt0936501"},
                    "reviews": {"results": []},
                    "watch/providers": {"results": {"BR": {"flatrate": [{"provider_name": "Apple TV"}]}}},
                }
            )

    monkeypatch.setattr("app.clients.httpx.AsyncClient", FakeAsyncClient)

    client = TMDbClient(build_settings())
    enriched = await client.search_and_enrich(
        VisionCandidate(detected_title="Taken", media_type="movie", year=2008, confidence=0.98)
    )

    assert enriched.tmdb_id == 680
    assert enriched.title == "Busca Implacável"
