from __future__ import annotations

import base64
import binascii
import contextlib
import os
import re
import tempfile
from io import BytesIO
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException

from app.config import Settings
from app.exceptions import AmbiguousTitleError, VisionIdentificationError
from app.schemas import EnrichedMedia, VisionCandidate
from app.utils import compact_text, normalize_phone, parse_json_response


class EvolutionClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _headers(self) -> dict[str, str]:
        return {"apikey": self.settings.evolution_api_key, "Content-Type": "application/json"}

    def _destination_number(self, chat_jid: str) -> str:
        normalized_chat = normalize_phone(chat_jid)
        owner_lid = normalize_phone(self.settings.evolution_owner_lid)
        if owner_lid != "unknown" and normalized_chat == owner_lid:
            return self.settings.evolution_owner_phone
        owner_phone = normalize_phone(self.settings.evolution_owner_phone)
        if normalized_chat == owner_phone:
            return self.settings.evolution_owner_phone
        return normalized_chat if normalized_chat != "unknown" else chat_jid

    async def send_text(self, chat_jid: str, text: str) -> None:
        payload = {
            "number": self._destination_number(chat_jid),
            "text": text,
            "textMessage": {"text": text},
            "options": {"delay": 0},
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

    async def fetch_recent_messages(self, remote_jid: str, limit: int = 10) -> list[dict[str, Any]]:
        payload = {"where": {"key.remoteJid": remote_jid}, "limit": limit}
        async with httpx.AsyncClient(base_url=self.settings.evolution_base_url, timeout=30.0) as client:
            response = await client.post(
                f"/chat/findMessages/{self.settings.evolution_instance}",
                headers=self._headers(),
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        records = ((data.get("messages") or {}).get("records") or []) if isinstance(data, dict) else []
        return [record for record in records if isinstance(record, dict)]


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

    def _build_legacy_ocr_engine(self):
        from rapidocr_onnxruntime import RapidOCR

        self._ocr_backend = "rapidocr_onnxruntime"
        self._ocr_engine = RapidOCR()
        return self._ocr_engine

    async def identify_title(self, image_bytes: bytes) -> VisionCandidate:
        ocr_candidate = self._identify_title_from_ocr(image_bytes)
        ocr_hint: VisionCandidate | None = None
        if ocr_candidate is not None and self._should_return_ocr_candidate(ocr_candidate):
            return ocr_candidate
        if ocr_candidate is not None:
            ocr_hint = ocr_candidate

        image_b64 = base64.b64encode(image_bytes).decode("ascii")
        attempts = ["ocr: no confident local text match"]
        ocr_hint_text = ""
        if ocr_hint and ocr_hint.detected_title:
            ocr_hint_text = (
                f" Visible text may read as '{ocr_hint.detected_title}'. "
                "If spacing is collapsed, restore the natural title spacing before answering."
            )
        prompt = (
            "Identify the movie or TV series shown in the image. "
            "Return JSON only with keys: detected_title, media_type, year, confidence, alt_titles, "
            "visible_text, need_clarification. "
            "media_type must be one of movie, series, unknown. "
            "confidence must be between 0 and 1."
            + ocr_hint_text
        )
        parsed, query_attempts = await self._query_candidate(image_b64, prompt, use_json_mode=True)
        attempts.extend(query_attempts)
        if parsed.detected_title and parsed.confidence >= self.settings.openrouter_confidence_threshold:
            return parsed

        ocr_prompt = (
            "Read the visible text in this image and use it to infer the movie or TV series title. "
            "Return JSON only with keys: detected_title, media_type, year, confidence, alt_titles, "
            "visible_text, need_clarification. "
            "If the title is explicitly written in the image, copy it to detected_title and use confidence >= 0.9. "
            "visible_text must contain the main readable text lines from the image."
        )
        parsed, query_attempts = await self._query_candidate(image_b64, ocr_prompt, use_json_mode=False)
        attempts.extend(query_attempts)
        if parsed.detected_title and parsed.confidence >= 0.75:
            return parsed
        raise VisionIdentificationError("Nao consegui identificar o titulo com confianca suficiente.", attempts)

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

        for variant_name, candidate_bytes in self._build_ocr_variants(image_bytes):
            entries = self._run_ocr(candidate_bytes)
            lines = [entry["text"] for entry in entries]
            if not lines:
                continue

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
                        visible_text=[*lines, f"ocr_variant={variant_name}"],
                    )
            title = self._guess_title_line(entries)
            if title:
                return VisionCandidate(
                    detected_title=title,
                    media_type="unknown",
                    confidence=0.9,
                    visible_text=[*lines, f"ocr_variant={variant_name}"],
                )
        return None

    def _should_return_ocr_candidate(self, candidate: VisionCandidate) -> bool:
        title = (candidate.detected_title or "").strip()
        if not title:
            return False
        if candidate.year:
            return True
        if candidate.media_type != "unknown":
            return True
        words = title.split()
        if len(words) >= 2:
            return True
        if not title.isalpha():
            return True
        return False

    def _build_ocr_variants(self, image_bytes: bytes) -> list[tuple[str, bytes]]:
        variants = [("original", image_bytes)]
        try:
            from PIL import Image, ImageEnhance, ImageFilter, ImageOps
        except Exception:
            return variants

        try:
            with Image.open(BytesIO(image_bytes)) as source:
                image = source.convert("RGB")
                width, height = image.size

                grayscale = ImageOps.autocontrast(ImageOps.grayscale(image))
                high_contrast = ImageEnhance.Contrast(grayscale).enhance(2.4)
                sharpened = high_contrast.filter(ImageFilter.SHARPEN)
                variants.append(("grayscale-contrast", self._encode_image_variant(sharpened)))

                lower_crop = image.crop((0, max(int(height * 0.58), 0), width, height))
                lower_gray = ImageOps.autocontrast(ImageOps.grayscale(lower_crop))
                lower_large = lower_gray.resize((max(lower_crop.width * 2, 1), max(lower_crop.height * 2, 1)))
                lower_sharp = ImageEnhance.Contrast(lower_large).enhance(2.8).filter(ImageFilter.SHARPEN)
                variants.append(("bottom-crop-contrast", self._encode_image_variant(lower_sharp)))
        except Exception:
            return variants
        return variants

    def _encode_image_variant(self, image) -> bytes:
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    def _run_ocr(self, image_bytes: bytes) -> list[dict[str, Any]]:
        suffix = ".png"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            handle.write(image_bytes)
            temp_path = handle.name
        try:
            try:
                if self._ocr_backend == "rapidocr":
                    result = self._ocr_engine(temp_path)
                    return [
                        {
                            "text": text.strip(),
                            "bbox": box.tolist() if hasattr(box, "tolist") else box,
                            "score": score,
                        }
                        for text, box, score in zip(
                            getattr(result, "txts", ()),
                            getattr(result, "boxes", ()),
                            getattr(result, "scores", ()),
                        )
                        if text and text.strip()
                    ]
                result, _ = self._ocr_engine(temp_path)
                return [
                    {"text": entry[1].strip(), "bbox": entry[0], "score": entry[2] if len(entry) >= 3 else 0.0}
                    for entry in (result or [])
                    if len(entry) >= 2 and entry[1].strip()
                ]
            except Exception:
                if self._ocr_backend != "rapidocr":
                    return []
                try:
                    self._build_legacy_ocr_engine()
                    result, _ = self._ocr_engine(temp_path)
                    return [
                        {"text": entry[1].strip(), "bbox": entry[0], "score": entry[2] if len(entry) >= 3 else 0.0}
                        for entry in (result or [])
                        if len(entry) >= 2 and entry[1].strip()
                    ]
                except Exception:
                    return []
        finally:
            with contextlib.suppress(OSError):
                os.unlink(temp_path)

    def _guess_title_line(self, entries: list[dict[str, Any]]) -> str | None:
        stopwords = {
            "SORTED CINEMA",
            "STREAMING",
            "APPLETV",
            "DISNEY PLUS",
            "VOTAR",
            "RESPOSTAS",
            "SEGUIR",
            "MOVIFIED",
            "MOVIFIEDBOLLYWOOD",
            "COMMENT",
            "COMENTARIO",
            "COMENTÁRIO",
        }
        best_line: str | None = None
        best_score = 0.0
        for entry in entries:
            raw_text = str(entry.get("text") or "").strip()
            if not raw_text:
                continue
            if "www." in raw_text.lower() or "http" in raw_text.lower() or "@" in raw_text:
                continue
            cleaned = re.sub(r"[^A-Za-z0-9&' -]", " ", raw_text)
            cleaned = " ".join(cleaned.split()).strip(" -")
            if not cleaned:
                continue
            upper_cleaned = cleaned.upper()
            if any(token in upper_cleaned for token in stopwords):
                continue
            words = [word for word in cleaned.split() if word]
            if not 1 <= len(words) <= 4:
                continue
            if len(cleaned) < 4 or len(cleaned) > 32:
                continue
            letters = [char for char in cleaned if char.isalpha()]
            if not letters:
                continue
            non_space = cleaned.replace(" ", "")
            alpha_ratio = len(letters) / max(len(non_space), 1)
            uppercase_ratio = sum(char.isupper() for char in letters) / len(letters)
            if alpha_ratio < 0.65:
                continue
            if uppercase_ratio < 0.7 and not cleaned.istitle():
                continue
            bbox = entry.get("bbox") or []
            y_center = 0.0
            if bbox:
                ys = [float(point[1]) for point in bbox if isinstance(point, (list, tuple)) and len(point) >= 2]
                if ys:
                    y_center = sum(ys) / len(ys)
            score = float(entry.get("score") or 0.0)
            score += uppercase_ratio
            score += 0.2 if len(words) in {2, 3} else 0.0
            score += 0.2 if y_center >= 350 else 0.0
            score += min(len(cleaned) / 20, 0.4)
            if score <= best_score:
                continue
            best_score = score
            best_line = cleaned.title() if cleaned.isupper() else cleaned
        return best_line

    async def _query_candidate(self, image_b64: str, prompt: str, *, use_json_mode: bool) -> tuple[VisionCandidate, list[str]]:
        attempts: list[str] = []
        model_sequence = [*self.settings.openrouter_vision_models]
        if self.settings.openrouter_enable_paid_fallback:
            model_sequence.extend(self.settings.openrouter_paid_vision_models)
        model_sequence.append(self.settings.openrouter_emergency_router)
        async with httpx.AsyncClient(base_url="https://openrouter.ai/api/v1", timeout=90.0) as client:
            for model in model_sequence:
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
                        attempts.append(
                            f"{model}: {parsed.detected_title} (confidence={parsed.confidence:.2f}, type={parsed.media_type})"
                        )
                        return parsed, attempts
                    attempts.append(f"{model}: empty title")
                except Exception as exc:
                    attempts.append(f"{model}: {exc.__class__.__name__}")
                    continue
        return VisionCandidate(), attempts


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
            best = self._select_best_match(candidate, scored)
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
                content = " ".join(str(review.get("content") or "").split())
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

    def _select_best_match(self, candidate: VisionCandidate, scored: list[tuple[float, dict[str, Any]]]) -> dict[str, Any]:
        exact_matches = [
            result
            for _, result in scored
            if self._is_exact_title_match(candidate.detected_title, result) and self._is_exact_year_match(candidate.year, result)
        ]
        if len(exact_matches) == 1:
            return exact_matches[0]
        self._raise_for_ambiguity(scored)
        return scored[0][1]

    def _is_exact_title_match(self, detected_title: str | None, result: dict[str, Any]) -> bool:
        if not detected_title:
            return False
        detected_title = detected_title.strip().lower()
        titles = {
            (result.get("title") or "").strip().lower(),
            (result.get("name") or "").strip().lower(),
            (result.get("original_title") or "").strip().lower(),
            (result.get("original_name") or "").strip().lower(),
        }
        return detected_title in titles

    def _is_exact_year_match(self, candidate_year: int | None, result: dict[str, Any]) -> bool:
        if not candidate_year:
            return False
        result_year = (result.get("release_date") or result.get("first_air_date") or "")[:4]
        return result_year == str(candidate_year)

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
