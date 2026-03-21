from __future__ import annotations

import types

import pytest

from app.clients import TMDbClient
from app.config import Settings
from app.exceptions import AmbiguousTitleError
from app.models import IdentifiedMedia
from app.services import PipelineService


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
async def test_format_whatsapp_reply_formats_release_date() -> None:
    pipeline = PipelineService(build_settings())
    enriched = types.SimpleNamespace(
        title="Arrival",
        year=2016,
        media_type="movie",
        release_date="2016-11-10",
        genres=["Drama", "Sci-Fi"],
        ratings={"IMDb": "7.9/10"},
        providers=["Netflix (assinatura)"],
        overview="Contato com alienigenas muda a historia.",
        reviews=["Review 1"],
    )

    text = await pipeline.format_whatsapp_reply(enriched)

    assert "Lancamento: 10/11/2016" in text
    assert "IMDb: 7.9/10" in text
    assert "Netflix (assinatura)" in text
    assert "Reviews" not in text


@pytest.mark.asyncio
async def test_format_review_messages_returns_three_separate_messages() -> None:
    pipeline = PipelineService(build_settings())
    enriched = types.SimpleNamespace(reviews=["Review completa 1", "Review completa 2", "Review completa 3"])

    messages = await pipeline.format_review_messages(enriched)

    assert messages == [
        "Review 1\nReview completa 1",
        "Review 2\nReview completa 2",
        "Review 3\nReview completa 3",
    ]


def test_tmdb_client_detects_ambiguity() -> None:
    client = TMDbClient(build_settings())

    with pytest.raises(AmbiguousTitleError) as exc:
        client._raise_for_ambiguity(
            [
                (1.1, {"title": "The Office", "release_date": "2005-03-24", "media_type": "tv"}),
                (1.0, {"title": "The Office", "first_air_date": "2001-07-09", "media_type": "tv"}),
                (0.9, {"title": "Office Space", "release_date": "1999-02-19", "media_type": "movie"}),
            ]
        )

    assert "The Office (2005)" in exc.value.options[0]
    assert len(exc.value.options) >= 2


def test_build_watchlist_item_reuses_identified_ids() -> None:
    pipeline = PipelineService(build_settings())
    identified = IdentifiedMedia(
        requester_phone="5519988343888",
        source_message_id=1,
        media_type="movie",
        tmdb_id=329,
        imdb_id="tt0110912",
        title="Pulp Fiction",
        year=1994,
        confidence=0.99,
        overview="Crime entrelacado.",
        payload={"source": "tmdb"},
    )

    enriched = pipeline.build_watchlist_item(identified)

    assert enriched.title == "Pulp Fiction"
    assert enriched.tmdb_id == 329
    assert enriched.imdb_id == "tt0110912"
