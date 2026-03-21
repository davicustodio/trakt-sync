from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw, ImageFont

from app.clients import EvolutionClient, OpenRouterClient, TMDbClient
from app.config import Settings
from app.schemas import VisionCandidate


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
                "options": {"delay": 0, "presence": "composing"},
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


@pytest.mark.asyncio
async def test_identify_title_falls_back_when_local_ocr_raises(monkeypatch) -> None:
    client = OpenRouterClient(build_settings())
    client._ocr_engine = object()
    client._ocr_backend = "rapidocr"

    def boom(path: str):
        raise RuntimeError("ocr failed")

    client._ocr_engine = boom

    async def fake_query_candidate(image_b64: str, prompt: str, *, use_json_mode: bool) -> tuple[VisionCandidate, list[str]]:
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

    async def fake_query_candidate(image_b64: str, prompt: str, *, use_json_mode: bool) -> tuple[VisionCandidate, list[str]]:
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
async def test_query_candidate_uses_paid_fallback_after_free_models(monkeypatch) -> None:
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

    assert requested_models == ["google/gemma-3-27b-it:free", "google/gemini-2.5-flash"]
    assert candidate.detected_title == "Inland Empire"
    assert attempts == [
        "google/gemma-3-27b-it:free: RuntimeError",
        "google/gemini-2.5-flash: Inland Empire (confidence=0.97, type=movie)",
    ]


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
