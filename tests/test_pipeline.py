from __future__ import annotations

import types

import pytest

from app.clients import TMDbClient
from app.config import Settings
from app.exceptions import AmbiguousTitleError
from app.models import IdentifiedMedia
from app.services import PipelineService
from fastapi import HTTPException


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
        original_title="Arrival",
        localized_title="A Chegada",
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

    assert "Titulo original: Arrival" in text
    assert "Titulo em portugues: A Chegada" in text
    assert "Lancamento: 10/11/2016" in text
    assert "IMDb: 7.9/10" in text
    assert "Netflix (assinatura)" in text
    assert "Reviews" not in text


@pytest.mark.asyncio
async def test_format_review_messages_returns_three_separate_messages() -> None:
    pipeline = PipelineService(build_settings())
    enriched = types.SimpleNamespace(reviews=["Review completa 1", "Review completa 2", "Review completa 3"])

    async def fake_fetch_reviews(_enriched):
        return ["Review completa 1", "Review completa 2", "Review completa 3"]

    async def fake_translate(reviews, *, title=None):
        return reviews

    pipeline.reviews.fetch_reviews = fake_fetch_reviews
    pipeline.openrouter.translate_reviews_to_pt_br = fake_translate

    messages = await pipeline.format_review_messages(enriched)

    assert messages == [
        "Review 1\nReview completa 1",
        "Review 2\nReview completa 2",
        "Review 3\nReview completa 3",
    ]


@pytest.mark.asyncio
async def test_format_review_messages_reports_missing_reviews_when_tmdb_has_no_reviews() -> None:
    pipeline = PipelineService(build_settings())
    enriched = types.SimpleNamespace(
        title="2067",
        year=2020,
        media_type="movie",
        overview="Ficcao cientifica distopica.",
        genres=["Ficcao cientifica"],
        ratings={"TMDb": "5.4/10"},
        reviews=[],
    )

    async def fake_fetch_reviews(_enriched):
        return []

    pipeline.reviews.fetch_reviews = fake_fetch_reviews

    messages = await pipeline.format_review_messages(enriched)

    assert messages == ["Reviews\nNao encontrei reviews publicas integrais disponiveis via Trakt para este titulo."]


@pytest.mark.asyncio
async def test_format_review_messages_translates_existing_reviews_without_synthesizing_more() -> None:
    pipeline = PipelineService(build_settings())
    enriched = types.SimpleNamespace(
        title="The Godfather",
        year=1972,
        media_type="movie",
        overview="Crime organizado em uma familia mafiosa.",
        genres=["Crime", "Drama"],
        ratings={"IMDb": "9.2/10"},
        reviews=["One of the best scripts of twentieth century cinema."],
    )

    async def fake_fetch_reviews(_enriched):
        return ["One of the best scripts of twentieth century cinema."]

    async def fake_translate(reviews, *, title=None):
        assert title == "The Godfather"
        return ["Traducao 1"]

    pipeline.reviews.fetch_reviews = fake_fetch_reviews
    pipeline.openrouter.translate_reviews_to_pt_br = fake_translate

    messages = await pipeline.format_review_messages(enriched)

    assert messages == ["Review 1\nTraducao 1"]


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


def test_tmdb_client_accepts_single_exact_title_with_near_year_match() -> None:
    client = TMDbClient(build_settings())
    candidate = types.SimpleNamespace(detected_title="Coherence", year=2013)

    best = client._select_best_match(
        candidate,
        [
            (2.1, {"title": "Coherence", "release_date": "2014-08-06", "media_type": "movie", "id": 1}),
            (1.2, {"title": "Untitled Coherence Sequel", "release_date": "", "media_type": "movie", "id": 2}),
        ],
    )

    assert best["id"] == 1


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


@pytest.mark.asyncio
async def test_enrich_from_image_falls_back_to_omdb_when_tmdb_has_no_match() -> None:
    pipeline = PipelineService(build_settings())

    async def fake_fetch_media_bytes(channel: str, provider_message_id: str, media_url: str | None = None, media_file_id: str | None = None):
        return b"image"

    async def fake_identify_title(_image: bytes):
        return types.SimpleNamespace(detected_title="Virgin River", media_type="series", year=2019, confidence=0.94, visible_text=[], alt_titles=[])

    async def fake_tmdb_search(_candidate):
        raise HTTPException(status_code=404, detail="No TMDb match found.")

    async def fake_omdb_search(_candidate):
        return types.SimpleNamespace(
            title="Virgin River",
            original_title="Virgin River",
            localized_title="Virgin River",
            media_type="series",
            year=2019,
            imdb_id="tt9077530",
            tmdb_id=None,
            release_date="2019-12-06",
            overview="Mel se muda para uma cidade pequena.",
            genres=["Drama"],
            ratings={"IMDb": "7.4/10"},
            providers=[],
            reviews=[],
            confidence=0.94,
            payload={"source": "omdb"},
        )

    async def fake_attach_ratings(enriched):
        return enriched

    async def fake_attach_public_ratings(enriched):
        return enriched

    pipeline.fetch_media_bytes = fake_fetch_media_bytes
    pipeline.openrouter.identify_title = fake_identify_title
    pipeline.tmdb.search_and_enrich = fake_tmdb_search
    pipeline.omdb.search_and_enrich = fake_omdb_search
    pipeline.omdb.attach_ratings = fake_attach_ratings
    pipeline.trakt.attach_public_ratings = fake_attach_public_ratings

    enriched = await pipeline.enrich_from_image("msg-1")

    assert enriched.title == "Virgin River"
    assert enriched.imdb_id == "tt9077530"
    assert enriched.payload["source"] == "omdb"
