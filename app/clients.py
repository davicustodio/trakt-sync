from __future__ import annotations

import base64
import binascii
import contextlib
import os
import re
import tempfile
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException

from app.config import Settings
from app.exceptions import AmbiguousTitleError
from app.schemas import EnrichedMedia, VisionCandidate
from app.utils import compact_text, parse_json_response


class EvolutionClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _headers(self) -> dict[str, str]:
        return {"apikey": self.settings.evolution_api_key, "Content-Type": "application/json"}

    async def send_text(self, chat_jid: str, text: str) -> None:
        payload = {
            "number": chat_jid,
            "text": text,
            "textMessage": {"text": text},
            "options": {"delay": 0, "presence": "composing"},
        }
        async with httpx.AsyncClient(base_url=self.settings.evolution_base_url, timeout=30.0) as client:
            response = await client.post(
                f"/message/sendText/{self.settings.evolution_instance}",
                headers=self._headers(),
                json=payload,
            )
            response.raise_for_status()

    async def fetch_media_bytes(self, provider_message_id: str, media_url: str | None = None) -> bytes:
        async with httpx.AsyncClient(base_url=self.settings.evolution_base_url, timeout=60.0) as client:
            response = await client.post(
                f"/chat/getBase64FromMediaMessage/{self.settings.evolution_instance}",
                headers=self._headers(),
                json={"message": {"key": {"id": provider_message_id}}, "convertToMp4": False},
            )
            if response.status_code < 400:
                payload = response.json()
                media_b64 = payload.get("base64")
                if media_b64:
                    if "," in media_b64 and media_b64.split(",", 1)[0].startswith("data:"):
                        media_b64 = media_b64.split(",", 1)[1]
                    try:
                        return base64.b64decode(media_b64)
                    except (binascii.Error, ValueError):
                        pass

        if not media_url:
            raise HTTPException(status_code=404, detail="No reusable media payload found for this message.")

        # WhatsApp CDN URLs occasionally fail certificate validation inside minimal containers.
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True, verify=False) as client:
            response = await client.get(media_url)
            response.raise_for_status()
            return response.content


class OpenRouterClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._ocr_engine = None
        self._ocr_backend: str | None = None

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.settings.openrouter_api_key}",
            "Content-Type": "application/json",
            "X-Title": self.settings.openrouter_app_name,
        }
        if self.settings.openrouter_site_url:
            headers["HTTP-Referer"] = self.settings.openrouter_site_url
        return headers

    async def identify_title(self, image_bytes: bytes) -> VisionCandidate:
        ocr_candidate = self._identify_title_from_ocr(image_bytes)
        if ocr_candidate is not None:
            return ocr_candidate

        image_b64 = base64.b64encode(image_bytes).decode("ascii")
        prompt = (
            "Identify the movie or TV series shown in the image. "
            "Return JSON only with keys: detected_title, media_type, year, confidence, alt_titles, "
            "visible_text, need_clarification. "
            "media_type must be one of movie, series, unknown. "
            "confidence must be between 0 and 1."
        )
        parsed = await self._query_candidate(image_b64, prompt, use_json_mode=True)
        if parsed.detected_title and parsed.confidence >= self.settings.openrouter_confidence_threshold:
            return parsed

        ocr_prompt = (
            "Read the visible text in this image and use it to infer the movie or TV series title. "
            "Return JSON only with keys: detected_title, media_type, year, confidence, alt_titles, "
            "visible_text, need_clarification. "
            "If the title is explicitly written in the image, copy it to detected_title and use confidence >= 0.9. "
            "visible_text must contain the main readable text lines from the image."
        )
        parsed = await self._query_candidate(image_b64, ocr_prompt, use_json_mode=False)
        if parsed.detected_title and parsed.confidence >= 0.75:
            return parsed
        raise HTTPException(status_code=422, detail="Could not identify a movie or series from the image.")

    def _identify_title_from_ocr(self, image_bytes: bytes) -> VisionCandidate | None:
        if self._ocr_engine is None:
            try:
                from rapidocr import RapidOCR

                self._ocr_backend = "rapidocr"
            except Exception:
                try:
                    from rapidocr_onnxruntime import RapidOCR

                    self._ocr_backend = "rapidocr_onnxruntime"
                except Exception:
                    return None
            self._ocr_engine = RapidOCR()

        if self._ocr_backend == "rapidocr":
            result = self._ocr_engine(image_bytes)
            lines = [text.strip() for text in getattr(result, "txts", ()) if text and text.strip()]
        else:
            suffix = ".jpg"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
                handle.write(image_bytes)
                temp_path = handle.name
            try:
                result, _ = self._ocr_engine(temp_path)
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(temp_path)
            lines = [entry[1].strip() for entry in (result or []) if len(entry) >= 2 and entry[1].strip()]
        if not lines:
            return None

        for line in lines:
            match = re.search(r"(?P<title>[A-Za-z0-9' .,:!?-]{2,})\((?P<year>\d{4})\)", line)
            if not match:
                continue
            title = match.group("title").strip(" -")
            year = int(match.group("year"))
            if title:
                return VisionCandidate(
                    detected_title=title,
                    media_type="movie",
                    year=year,
                    confidence=0.98,
                    visible_text=lines,
                )
        return None

    async def _query_candidate(self, image_b64: str, prompt: str, *, use_json_mode: bool) -> VisionCandidate:
        async with httpx.AsyncClient(base_url="https://openrouter.ai/api/v1", timeout=90.0) as client:
            for model in [*self.settings.openrouter_vision_models, self.settings.openrouter_emergency_router]:
                payload = {
                    "model": model,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:image/jpeg;base64,{image_b64}",
                                    },
                                },
                            ],
                        }
                    ],
                }
                if use_json_mode:
                    payload["response_format"] = {"type": "json_object"}
                try:
                    response = await client.post("/chat/completions", headers=self._headers(), json=payload)
                    response.raise_for_status()
                    data = response.json()
                    content = data["choices"][0]["message"]["content"]
                    parsed = VisionCandidate.model_validate(parse_json_response(content))
                    if parsed.detected_title:
                        return parsed
                except Exception:
                    continue
        return VisionCandidate()


class TMDbClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.tmdb_api_token}",
            "accept": "application/json",
        }

    async def search_and_enrich(self, candidate: VisionCandidate) -> EnrichedMedia:
        async with httpx.AsyncClient(base_url="https://api.themoviedb.org/3", timeout=60.0) as client:
            endpoint = "/search/multi"
            seen: set[tuple[str, int]] = set()
            results: list[dict[str, Any]] = []
            for language in dict.fromkeys([self.settings.tmdb_language, "en-US"]):
                params = {
                    "query": candidate.detected_title,
                    "language": language,
                    "include_adult": "false",
                }
                response = await client.get(endpoint, headers=self._headers(), params=params)
                response.raise_for_status()
                for result in response.json().get("results", []):
                    media_type = result.get("media_type")
                    item_id = result.get("id")
                    if media_type not in {"movie", "tv"} or not item_id:
                        continue
                    key = (media_type, int(item_id))
                    if key in seen:
                        continue
                    seen.add(key)
                    results.append(result)
            scored = []
            for result in results:
                media_type = result.get("media_type")
                score = 0.0
                title = result.get("title") or result.get("name") or ""
                original_title = result.get("original_title") or result.get("original_name") or ""
                detected_title = (candidate.detected_title or "").lower()
                if title.lower() == detected_title:
                    score += 1.0
                elif detected_title in title.lower():
                    score += 0.5
                if original_title.lower() == detected_title:
                    score += 1.0
                elif detected_title and detected_title in original_title.lower():
                    score += 0.5
                if candidate.media_type == "series" and media_type == "tv":
                    score += 0.5
                if candidate.media_type == "movie" and media_type == "movie":
                    score += 0.5
                result_year = (result.get("release_date") or result.get("first_air_date") or "")[:4]
                if candidate.year and result_year and str(candidate.year) == result_year:
                    score += 1.0
                score += min((result.get("popularity") or 0) / 100, 0.5)
                scored.append((score, result))
            if not scored:
                raise HTTPException(status_code=404, detail="No TMDb match found.")
            scored.sort(key=lambda item: item[0], reverse=True)
            self._raise_for_ambiguity(scored)
            best = scored[0][1]
            media_type = "series" if best["media_type"] == "tv" else "movie"
            item_id = best["id"]
            detail_endpoint = f"/{'tv' if media_type == 'series' else 'movie'}/{item_id}"
            details = await client.get(
                detail_endpoint,
                headers=self._headers(),
                params={"language": self.settings.tmdb_language, "append_to_response": "external_ids,reviews,watch/providers"},
            )
            details.raise_for_status()
            payload = details.json()
            providers = (
                (((payload.get("watch/providers") or {}).get("results") or {}).get(self.settings.tmdb_region) or {})
            )
            provider_lines = []
            for key, label in (("flatrate", "assinatura"), ("rent", "aluguel"), ("buy", "compra")):
                for entry in providers.get(key, [])[:3]:
                    provider_lines.append(f"{entry['provider_name']} ({label})")
            reviews = []
            for review in (payload.get("reviews") or {}).get("results", [])[:3]:
                content = compact_text(review.get("content"), 220)
                if content:
                    reviews.append(content)
            return EnrichedMedia(
                title=payload.get("title") or payload.get("name") or candidate.detected_title or "Unknown",
                media_type=media_type,
                year=int((payload.get("release_date") or payload.get("first_air_date") or "0000")[:4])
                if (payload.get("release_date") or payload.get("first_air_date"))
                else None,
                tmdb_id=payload.get("id"),
                imdb_id=(payload.get("external_ids") or {}).get("imdb_id"),
                release_date=payload.get("release_date") or payload.get("first_air_date"),
                overview=compact_text(payload.get("overview"), 420),
                genres=[genre["name"] for genre in payload.get("genres", [])],
                ratings={"TMDb": f"{payload.get('vote_average', 0):.1f}/10"},
                providers=provider_lines,
                reviews=reviews,
                confidence=candidate.confidence,
                payload=payload,
            )

    def _raise_for_ambiguity(self, scored: list[tuple[float, dict[str, Any]]]) -> None:
        if len(scored) < 2:
            return
        top_score, top_result = scored[0]
        second_score, second_result = scored[1]
        title_a = top_result.get("title") or top_result.get("name") or "Desconhecido"
        title_b = second_result.get("title") or second_result.get("name") or "Desconhecido"
        year_a = (top_result.get("release_date") or top_result.get("first_air_date") or "")[:4]
        year_b = (second_result.get("release_date") or second_result.get("first_air_date") or "")[:4]
        if title_a == title_b and year_a == year_b:
            return
        if top_score - second_score >= 0.35:
            return
        options: list[str] = []
        for _, result in scored[:3]:
            title = result.get("title") or result.get("name") or "Desconhecido"
            year = (result.get("release_date") or result.get("first_air_date") or "")[:4]
            media_type = "Serie" if result.get("media_type") == "tv" else "Filme"
            label = f"{title} ({year or 'N/A'}) - {media_type}"
            if label not in options:
                options.append(label)
        raise AmbiguousTitleError(options)


class OMDbClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def attach_ratings(self, enriched: EnrichedMedia) -> EnrichedMedia:
        if not enriched.imdb_id:
            return enriched
        async with httpx.AsyncClient(base_url="https://www.omdbapi.com", timeout=30.0) as client:
            response = await client.get("/", params={"i": enriched.imdb_id, "apikey": self.settings.omdb_api_key})
            response.raise_for_status()
            payload = response.json()
        ratings = dict(enriched.ratings)
        for rating in payload.get("Ratings", []):
            source = rating.get("Source")
            value = rating.get("Value")
            if source == "Internet Movie Database":
                ratings["IMDb"] = value
            elif source == "Rotten Tomatoes":
                ratings["Rotten Tomatoes"] = value
            elif source == "Metacritic":
                ratings["Metacritic"] = value
        enriched.ratings = ratings
        return enriched


class TraktClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def exchange_code(self, code: str) -> dict[str, Any]:
        payload = {
            "code": code,
            "client_id": self.settings.trakt_client_id,
            "client_secret": self.settings.trakt_client_secret,
            "redirect_uri": self.settings.computed_trakt_redirect_uri,
            "grant_type": "authorization_code",
        }
        async with httpx.AsyncClient(base_url="https://api.trakt.tv", timeout=30.0) as client:
            response = await client.post("/oauth/token", json=payload)
            response.raise_for_status()
            return response.json()

    def build_authorize_url(self, state: str) -> str:
        params = urlencode(
            {
                "response_type": "code",
                "client_id": self.settings.trakt_client_id,
                "redirect_uri": self.settings.computed_trakt_redirect_uri,
                "state": state,
            }
        )
        return f"https://trakt.tv/oauth/authorize?{params}"

    async def refresh_token(self, refresh_token: str) -> dict[str, Any]:
        payload = {
            "refresh_token": refresh_token,
            "client_id": self.settings.trakt_client_id,
            "client_secret": self.settings.trakt_client_secret,
            "redirect_uri": self.settings.computed_trakt_redirect_uri,
            "grant_type": "refresh_token",
        }
        async with httpx.AsyncClient(base_url="https://api.trakt.tv", timeout=30.0) as client:
            response = await client.post("/oauth/token", json=payload)
            response.raise_for_status()
            return response.json()

    async def get_profile(self, access_token: str) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url="https://api.trakt.tv", timeout=30.0) as client:
            response = await client.get("/users/settings", headers=self._headers(access_token))
            response.raise_for_status()
            return response.json()

    async def ensure_fresh_tokens(self, connection: Any) -> tuple[str, str | None, datetime | None]:
        if connection.expires_at and connection.expires_at > datetime.now(UTC) + timedelta(minutes=5):
            return connection.access_token, connection.refresh_token, connection.expires_at
        if not connection.refresh_token:
            raise HTTPException(status_code=400, detail="Trakt account is not linked.")
        refreshed = await self.refresh_token(connection.refresh_token)
        expires_at = datetime.now(UTC) + timedelta(seconds=refreshed["expires_in"])
        return refreshed["access_token"], refreshed.get("refresh_token"), expires_at

    async def add_to_watchlist(self, access_token: str, enriched: EnrichedMedia) -> None:
        item_key = "shows" if enriched.media_type == "series" else "movies"
        payload = {item_key: [{"ids": {"tmdb": enriched.tmdb_id, "imdb": enriched.imdb_id}}]}
        async with httpx.AsyncClient(base_url="https://api.trakt.tv", timeout=30.0) as client:
            response = await client.post("/sync/watchlist", headers=self._headers(access_token), json=payload)
            response.raise_for_status()

    def _headers(self, access_token: str) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
            "trakt-api-version": "2",
            "trakt-api-key": self.settings.trakt_client_id,
        }
