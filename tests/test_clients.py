from __future__ import annotations

from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw, ImageFont

from app.clients import EvolutionClient, OpenRouterClient
from app.config import Settings


def build_settings() -> Settings:
    return Settings.model_construct(
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


@pytest.mark.asyncio
async def test_fetch_media_bytes_disables_ssl_verification(monkeypatch) -> None:
    calls: list[bool] = []

    class FakeResponse:
        content = b"image-bytes"

        def raise_for_status(self) -> None:
            return None

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            calls.append(kwargs["verify"])

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, media_url: str) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr("app.clients.httpx.AsyncClient", FakeAsyncClient)

    client = EvolutionClient(build_settings())
    payload = await client.fetch_media_bytes("https://example.com/poster.jpg")

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
