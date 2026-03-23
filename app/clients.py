from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import os
import re
import tempfile
from html import unescape
from io import BytesIO
from datetime import UTC, datetime, timedelta
from pathlib import Path
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


class TelegramClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        if not self.settings.telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured.")

    @property
    def _base_url(self) -> str:
        return f"https://api.telegram.org/bot{self.settings.telegram_bot_token}"

    @property
    def _file_base_url(self) -> str:
        return f"https://api.telegram.org/file/bot{self.settings.telegram_bot_token}"

    async def send_text(self, chat_id: str, text: str, *, parse_mode: str | None = None) -> int | None:
        payload: dict[str, Any] = {
            "chat_id": str(chat_id),
            "text": text,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        async with httpx.AsyncClient(base_url=self._base_url, timeout=30.0) as client:
            response = await client.post("/sendMessage", json=payload)
            response.raise_for_status()
            result = response.json().get("result") or {}
            message_id = result.get("message_id")
            return int(message_id) if isinstance(message_id, int) else None

    async def edit_text(self, chat_id: str, message_id: int, text: str, *, parse_mode: str | None = None) -> None:
        payload: dict[str, Any] = {
            "chat_id": str(chat_id),
            "message_id": int(message_id),
            "text": text,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        async with httpx.AsyncClient(base_url=self._base_url, timeout=30.0) as client:
            response = await client.post("/editMessageText", json=payload)
            response.raise_for_status()

    async def send_chat_action(self, chat_id: str, action: str = "typing") -> None:
        async with httpx.AsyncClient(base_url=self._base_url, timeout=15.0) as client:
            response = await client.post("/sendChatAction", json={"chat_id": str(chat_id), "action": action})
            response.raise_for_status()

    async def fetch_media_bytes(self, file_id: str) -> bytes:
        async with httpx.AsyncClient(base_url=self._base_url, timeout=30.0) as client:
            response = await client.post("/getFile", json={"file_id": file_id})
            response.raise_for_status()
            payload = response.json()
        file_path = ((payload.get("result") or {}).get("file_path")) if isinstance(payload, dict) else None
        if not file_path:
            raise HTTPException(status_code=404, detail="Telegram file_path not found.")
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(f"{self._file_base_url}/{file_path}")
            response.raise_for_status()
            return response.content


class OpenRouterClient:
    _free_text_models_cache: list[str] | None = None
    _free_text_models_updated_at: datetime | None = None
    _free_text_models_lock: asyncio.Lock | None = None

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._ocr_engine = None
        self._ocr_backend: str | None = None
        self._hydrate_free_text_models_cache()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.settings.openrouter_api_key}",
            "Content-Type": "application/json",
            "X-Title": self.settings.openrouter_app_name,
        }
        if self.settings.openrouter_site_url:
            headers["HTTP-Referer"] = self.settings.openrouter_site_url
        return headers

    @classmethod
    def _cache_lock(cls) -> asyncio.Lock:
        if cls._free_text_models_lock is None:
            cls._free_text_models_lock = asyncio.Lock()
        return cls._free_text_models_lock

    def _free_models_cache_path(self) -> Path:
        return Path(self.settings.openrouter_free_models_cache_file)

    def _hydrate_free_text_models_cache(self) -> None:
        cls = self.__class__
        if cls._free_text_models_cache is not None:
            return
        cls._free_text_models_cache = list(self.settings.openrouter_free_text_models)
        cache_path = self._free_models_cache_path()
        if not cache_path.exists():
            return
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            return
        models = payload.get("models") if isinstance(payload, dict) else None
        if isinstance(models, list):
            cleaned = [str(model).strip() for model in models if str(model).strip()]
            if cleaned:
                cls._free_text_models_cache = cleaned
        updated_at = payload.get("updated_at") if isinstance(payload, dict) else None
        if isinstance(updated_at, str):
            with contextlib.suppress(ValueError):
                cls._free_text_models_updated_at = datetime.fromisoformat(updated_at)

    def _persist_free_text_models_cache(self, models: list[str], updated_at: datetime) -> None:
        cache_path = self._free_models_cache_path()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({"updated_at": updated_at.isoformat(), "models": models}, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )

    def _text_task_model_sequence(self) -> list[str]:
        available = list(self.__class__._free_text_models_cache or self.settings.openrouter_free_text_models)
        preferred = [str(model).strip() for model in self.settings.openrouter_free_text_models if str(model).strip()]
        preferred_set = set(available)
        ordered: list[str] = []
        for model in preferred:
            if model in preferred_set and model not in ordered:
                ordered.append(model)
        for model in available:
            if model not in ordered:
                ordered.append(model)
        if self.settings.openrouter_emergency_router not in ordered:
            ordered.append(self.settings.openrouter_emergency_router)
        return ordered

    async def refresh_free_text_models_if_due(self) -> None:
        self._hydrate_free_text_models_cache()
        now = datetime.now(UTC)
        updated_at = self.__class__._free_text_models_updated_at
        if updated_at and now - updated_at < timedelta(seconds=self.settings.openrouter_free_models_refresh_interval_seconds):
            return
        async with self._cache_lock():
            updated_at = self.__class__._free_text_models_updated_at
            now = datetime.now(UTC)
            if updated_at and now - updated_at < timedelta(
                seconds=self.settings.openrouter_free_models_refresh_interval_seconds
            ):
                return
            models = await self._fetch_free_text_models()
            if not models:
                return
            self.__class__._free_text_models_cache = models
            self.__class__._free_text_models_updated_at = now
            with contextlib.suppress(OSError):
                self._persist_free_text_models_cache(models, now)

    async def _fetch_free_text_models(self) -> list[str]:
        async with httpx.AsyncClient(base_url="https://openrouter.ai/api/v1", timeout=30.0) as client:
            response = await client.get("/models", headers=self._headers())
            response.raise_for_status()
            payload = response.json()

        preferred_rank = {
            model: index for index, model in enumerate(self.settings.openrouter_free_text_models)
        }
        collected: list[tuple[int, int, str]] = []
        for item in payload.get("data", []) if isinstance(payload, dict) else []:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("id") or "").strip()
            if not model_id:
                continue
            pricing = item.get("pricing") or {}
            if str(pricing.get("prompt")) != "0" or str(pricing.get("completion")) != "0":
                continue
            architecture = item.get("architecture") or {}
            input_modalities = {str(modality).lower() for modality in architecture.get("input_modalities") or []}
            output_modalities = {str(modality).lower() for modality in architecture.get("output_modalities") or []}
            if input_modalities and "text" not in input_modalities:
                continue
            if output_modalities and "text" not in output_modalities:
                continue
            context_length = int(item.get("context_length") or 0)
            collected.append((preferred_rank.get(model_id, 999), -context_length, model_id))

        collected.sort(key=lambda item: (item[0], item[1], item[2]))
        models = [model_id for _, _, model_id in collected]
        return models or list(self.settings.openrouter_free_text_models)

    async def _run_text_json_task(self, prompt: str) -> dict[str, Any] | None:
        payload_base = {
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
        }
        async with httpx.AsyncClient(base_url="https://openrouter.ai/api/v1", timeout=45.0) as client:
            for model in self._text_task_model_sequence():
                payload = {"model": model, **payload_base}
                try:
                    response = await client.post("/chat/completions", headers=self._headers(), json=payload)
                    response.raise_for_status()
                    body = response.json()
                    message = (((body.get("choices") or [{}])[0]).get("message") or {}).get("content", "")
                    if not isinstance(message, str) or not message.strip():
                        continue
                    data = parse_json_response(message)
                    if isinstance(data, dict):
                        return data
                except Exception:
                    continue
        return None

    def _build_legacy_ocr_engine(self):
        from rapidocr_onnxruntime import RapidOCR

        self._ocr_backend = "rapidocr_onnxruntime"
        self._ocr_engine = RapidOCR()
        return self._ocr_engine

    async def identify_title(self, image_bytes: bytes) -> VisionCandidate:
        attempts: list[str] = []
        try:
            async with asyncio.timeout(self.settings.openrouter_vision_total_timeout_seconds):
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
                assertive_first = self._should_prioritize_assertive_title_resolution(ocr_hint)
                if assertive_first:
                    assertive_candidate, assertive_attempts = await self._query_assertive_text_candidate(image_bytes, ocr_hint)
                    attempts.extend(assertive_attempts)
                    if assertive_candidate.detected_title and self._candidate_matches_visible_text(assertive_candidate, ocr_hint):
                        return assertive_candidate

                prompt = (
                    "A imagem em anexo se refere a algum filme ou serie. "
                    "Seu objetivo e descobrir o titulo ORIGINAL desse filme ou serie. "
                    "Use o contexto visual da imagem, personagens, frame, poster, screenshot de rede social e qualquer texto visivel. "
                    "Se houver texto, cruze o texto com o contexto visual para encontrar o titulo original mais provavel. "
                    "Se houver varias possibilidades plausiveis para o mesmo contexto, nao invente certeza: coloque a melhor em detected_title "
                    "e preencha alt_titles com as outras opcoes mais provaveis para eu poder pedir confirmacao ao usuario. "
                    "Retorne JSON apenas com as chaves: detected_title, media_type, year, confidence, alt_titles, visible_text, need_clarification. "
                    "media_type deve ser movie, series ou unknown. "
                    "confidence deve ficar entre 0 e 1."
                    + ocr_hint_text
                )
                parsed, query_attempts = await self._query_candidate(image_b64, prompt, use_json_mode=True)
                attempts.extend(query_attempts)
                if parsed.detected_title and parsed.confidence >= self.settings.openrouter_confidence_threshold:
                    if self._candidate_matches_visible_text(parsed, ocr_hint):
                        return parsed
                    attempts.append("free-candidate-rejected-by-visible-text")

                assertive_candidate, assertive_attempts = await self._query_assertive_text_candidate(image_bytes, ocr_hint)
                attempts.extend(assertive_attempts)
                if assertive_candidate.detected_title and assertive_candidate.confidence >= 0.7:
                    if not self._candidate_matches_visible_text(assertive_candidate, ocr_hint):
                        attempts.append("assertive-candidate-rejected-by-visible-text")
                    else:
                        return assertive_candidate

                ocr_prompt = (
                    "Leia o texto visivel na imagem e use esse texto junto com o contexto visual para inferir o titulo ORIGINAL do filme ou da serie. "
                    "Se o titulo estiver escrito na imagem, copie o titulo original em detected_title. "
                    "Se houver mais de uma opcao plausivel, preencha alt_titles com as melhores alternativas. "
                    "Retorne JSON apenas com as chaves: detected_title, media_type, year, confidence, alt_titles, visible_text, need_clarification. "
                    "visible_text deve conter as principais linhas legiveis da imagem."
                )
                parsed, query_attempts = await self._query_candidate(image_b64, ocr_prompt, use_json_mode=False)
                attempts.extend(query_attempts)
                if parsed.detected_title and parsed.confidence >= 0.75:
                    if self._candidate_matches_visible_text(parsed, ocr_hint):
                        return parsed
                    attempts.append("ocr-prompt-candidate-rejected-by-visible-text")

                rescue_candidate, rescue_attempts = await self._query_scene_rescue_candidate(image_bytes, ocr_hint)
                attempts.extend(rescue_attempts)
                if rescue_candidate.detected_title and rescue_candidate.confidence >= 0.55:
                    if self._candidate_matches_visible_text(rescue_candidate, ocr_hint):
                        return rescue_candidate
                    attempts.append("rescue-candidate-rejected-by-visible-text")
        except TimeoutError:
            attempts.append("vision-time-budget-exceeded")
        raise VisionIdentificationError("Nao consegui identificar o titulo com confianca suficiente.", attempts)

    async def generate_review_blurbs(self, enriched: EnrichedMedia) -> list[str]:
        ratings_summary = ", ".join(f"{label}: {value}" for label, value in enriched.ratings.items()) or "sem ratings"
        prompt = (
            "Voce e um assistente que resume recepcao critica de filmes e series. "
            "Com base apenas nos metadados fornecidos, gere exatamente 3 mini reviews em portugues do Brasil. "
            "Cada review deve ter no maximo 240 caracteres, tom informativo e deixar claro quando a informacao e inferida. "
            "Retorne JSON puro no formato {\"reviews\":[\"...\",\"...\",\"...\"]}.\n\n"
            f"Titulo: {enriched.title}\n"
            f"Ano: {enriched.year or 'desconhecido'}\n"
            f"Tipo: {'serie' if enriched.media_type == 'series' else 'filme'}\n"
            f"Resumo: {enriched.overview or 'sem resumo'}\n"
            f"Generos: {', '.join(enriched.genres) or 'sem generos'}\n"
            f"Ratings: {ratings_summary}\n"
        )
        data = await self._run_text_json_task(prompt) or {}
        reviews = data.get("reviews") if isinstance(data, dict) else []
        if not isinstance(reviews, list):
            return []
        return [compact_text(str(review), 260) for review in reviews if str(review).strip()][:3]

    async def translate_reviews_to_pt_br(self, reviews: list[str], *, title: str | None = None) -> list[str]:
        cleaned = [" ".join(str(review).split()) for review in reviews if str(review).strip()]
        if not cleaned:
            return []
        prompt = (
            "Converta as reviews abaixo para portugues brasileiro natural. "
            "Se ja estiverem em portugues, normalize para pt-BR. "
            "Preserve o sentido, remova markdown desnecessario e retorne JSON puro no formato "
            "{\"reviews\":[\"...\",\"...\"]}. Mantenha a mesma quantidade de reviews recebida e nao resuma nem corte o texto.\n\n"
            f"Titulo: {title or 'desconhecido'}\n"
            f"Reviews: {cleaned}"
        )
        data = await self._run_text_json_task(prompt) or {}
        translated = data.get("reviews") if isinstance(data, dict) else []
        if not isinstance(translated, list):
            return cleaned
        normalized = [" ".join(str(review).split()) for review in translated if str(review).strip()]
        return normalized[: len(cleaned)] or cleaned

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

            explicit_title = self._extract_explicit_title_from_lines(lines)
            if explicit_title:
                return VisionCandidate(
                    detected_title=explicit_title,
                    media_type="unknown",
                    confidence=0.99,
                    visible_text=[*lines, f"ocr_variant={variant_name}"],
                )

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
            contextual_title = self._extract_title_from_context_lines(lines)
            if contextual_title:
                return VisionCandidate(
                    detected_title=contextual_title,
                    media_type="unknown",
                    confidence=0.84,
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

    def _should_prioritize_assertive_title_resolution(self, candidate: VisionCandidate | None) -> bool:
        if candidate is None:
            return False
        if candidate.year:
            return True
        visible_text = " ".join(candidate.visible_text or []).casefold()
        if "titulo original" in visible_text or "original title" in visible_text:
            return True
        title = (candidate.detected_title or "").strip()
        return len(title.split()) >= 2

    def _extract_explicit_title_from_lines(self, lines: list[str]) -> str | None:
        patterns = [
            r"titulo\s+original\s*[:\-]\s*(?P<title>[A-Za-z0-9'& .,:!?-]{2,})",
            r"original\s+title\s*[:\-]\s*(?P<title>[A-Za-z0-9'& .,:!?-]{2,})",
        ]
        for raw_line in lines:
            line = " ".join(str(raw_line).split())
            for pattern in patterns:
                match = re.search(pattern, line, flags=re.IGNORECASE)
                if not match:
                    continue
                title = match.group("title").strip(" -")
                if title:
                    return title
        return None

    def _candidate_matches_visible_text(
        self,
        candidate: VisionCandidate,
        ocr_hint: VisionCandidate | None,
    ) -> bool:
        if ocr_hint is None:
            return True
        hint_title = (ocr_hint.detected_title or "").strip()
        visible_text = " ".join(ocr_hint.visible_text or [])
        reference = " ".join(part for part in [hint_title, visible_text] if part).casefold()
        if not reference:
            return True
        candidate_tokens = self._title_tokens(candidate.detected_title or "")
        if not candidate_tokens:
            return True
        reference_tokens = self._title_tokens(reference)
        if not reference_tokens:
            compact_reference = re.sub(r"[^a-z0-9]+", "", reference)
            compact_candidate = re.sub(r"[^a-z0-9]+", "", (candidate.detected_title or "").casefold())
            return bool(compact_reference and compact_candidate and compact_candidate in compact_reference)
        overlap = candidate_tokens & reference_tokens
        if overlap:
            return True
        compact_reference = re.sub(r"[^a-z0-9]+", "", reference)
        compact_candidate = re.sub(r"[^a-z0-9]+", "", (candidate.detected_title or "").casefold())
        return bool(compact_reference and compact_candidate and compact_candidate in compact_reference)

    def _title_tokens(self, value: str) -> set[str]:
        stopwords = {"the", "a", "an", "of", "and", "os", "as", "o", "a", "de", "da", "do", "der", "die"}
        tokens = {
            token
            for token in re.findall(r"[a-z0-9]+", value.casefold())
            if len(token) >= 3 and token not in stopwords
        }
        return tokens

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

                top_title_crop = image.crop((int(width * 0.08), 0, int(width * 0.92), int(height * 0.34)))
                top_title_gray = ImageOps.autocontrast(ImageOps.grayscale(top_title_crop))
                top_title_large = top_title_gray.resize((max(top_title_crop.width * 3, 1), max(top_title_crop.height * 3, 1)))
                top_title_sharp = ImageEnhance.Contrast(top_title_large).enhance(3.2).filter(ImageFilter.SHARPEN)
                variants.append(("top-title-crop", self._encode_image_variant(top_title_sharp)))

                metadata_crop = image.crop((int(width * 0.16), int(height * 0.08), int(width * 0.86), int(height * 0.26)))
                metadata_gray = ImageOps.autocontrast(ImageOps.grayscale(metadata_crop))
                metadata_large = metadata_gray.resize((max(metadata_crop.width * 4, 1), max(metadata_crop.height * 4, 1)))
                metadata_sharp = ImageEnhance.Contrast(metadata_large).enhance(3.4).filter(ImageFilter.SHARPEN)
                variants.append(("metadata-crop", self._encode_image_variant(metadata_sharp)))

                lower_crop = image.crop((0, max(int(height * 0.58), 0), width, height))
                lower_gray = ImageOps.autocontrast(ImageOps.grayscale(lower_crop))
                lower_large = lower_gray.resize((max(lower_crop.width * 2, 1), max(lower_crop.height * 2, 1)))
                lower_sharp = ImageEnhance.Contrast(lower_large).enhance(2.8).filter(ImageFilter.SHARPEN)
                variants.append(("bottom-crop-contrast", self._encode_image_variant(lower_sharp)))

                center_crop = image.crop((int(width * 0.18), int(height * 0.10), int(width * 0.82), int(height * 0.86)))
                center_gray = ImageOps.autocontrast(ImageOps.grayscale(center_crop))
                center_large = center_gray.resize((max(center_crop.width * 2, 1), max(center_crop.height * 2, 1)))
                center_sharp = ImageEnhance.Contrast(center_large).enhance(2.6).filter(ImageFilter.SHARPEN)
                variants.append(("center-crop-contrast", self._encode_image_variant(center_sharp)))

                poster_title_crop = image.crop((int(width * 0.22), int(height * 0.46), int(width * 0.78), int(height * 0.80)))
                poster_title_gray = ImageOps.autocontrast(ImageOps.grayscale(poster_title_crop))
                poster_title_large = poster_title_gray.resize(
                    (max(poster_title_crop.width * 3, 1), max(poster_title_crop.height * 3, 1))
                )
                poster_title_sharp = ImageEnhance.Contrast(poster_title_large).enhance(3.0).filter(ImageFilter.SHARPEN)
                variants.append(("poster-title-crop", self._encode_image_variant(poster_title_sharp)))
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
            "ASSINAR",
            "CURTIDAS",
            "COMENTARIOS",
            "COMENTÁRIOS",
            "FINAL DE SEMANA",
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
            score += 0.25 if 180 <= y_center <= 780 else 0.0
            score -= 0.35 if y_center >= 900 else 0.0
            score += min(len(cleaned) / 20, 0.4)
            if score <= best_score:
                continue
            best_score = score
            best_line = cleaned.title() if cleaned.isupper() else cleaned
        return best_line

    def _extract_title_from_context_lines(self, lines: list[str]) -> str | None:
        removable_trailing_words = {"main", "official", "audio", "trailer", "teaser", "scene", "clip", "edit"}
        best_candidate: str | None = None
        best_score = 0.0
        for raw_line in lines:
            cleaned_line = " ".join(str(raw_line).replace("|", "-").split())
            if not cleaned_line:
                continue
            for fragment_index, fragment in enumerate(re.split(r"[-:/]", cleaned_line)):
                normalized = " ".join(fragment.split()).strip(" -")
                if not normalized or len(normalized) < 4:
                    continue
                words = normalized.split()
                while words and words[-1].casefold() in removable_trailing_words:
                    words.pop()
                if not 1 <= len(words) <= 4:
                    continue
                candidate = " ".join(words)
                letters = [char for char in candidate if char.isalpha()]
                if len(letters) < 4:
                    continue
                if candidate.isupper():
                    score = 1.2
                elif candidate.istitle():
                    score = 1.0
                else:
                    continue
                score += fragment_index * 0.18
                if len(words) in {2, 3}:
                    score += 0.2
                if score <= best_score:
                    continue
                best_score = score
                best_candidate = candidate.title() if candidate.isupper() else candidate
        return best_candidate

    def _build_llm_vision_variants(self, image_bytes: bytes) -> list[tuple[str, bytes]]:
        variants = [("original", image_bytes)]
        try:
            from PIL import Image, ImageOps
        except Exception:
            return variants

        try:
            with Image.open(BytesIO(image_bytes)) as source:
                image = source.convert("RGB")
                width, height = image.size

                frame_top = int(height * 0.12)
                frame_bottom = int(height * 0.86)
                frame_left = int(width * 0.06)
                frame_right = int(width * 0.94)
                if frame_right > frame_left and frame_bottom > frame_top:
                    frame_crop = image.crop((frame_left, frame_top, frame_right, frame_bottom))
                    variants.append(("frame-crop", self._encode_image_variant(frame_crop)))

                subtitle_top = int(height * 0.54)
                subtitle_bottom = int(height * 0.82)
                if subtitle_bottom > subtitle_top:
                    subtitle_crop = ImageOps.autocontrast(image.crop((frame_left, subtitle_top, frame_right, subtitle_bottom)))
                    variants.append(("subtitle-crop", self._encode_image_variant(subtitle_crop)))
        except Exception:
            return variants
        return variants

    async def _query_scene_rescue_candidate(
        self,
        image_bytes: bytes,
        ocr_hint: VisionCandidate | None,
    ) -> tuple[VisionCandidate, list[str]]:
        visible_text = ", ".join(ocr_hint.visible_text[:8]) if ocr_hint and ocr_hint.visible_text else "sem OCR util"
        hint_title = ocr_hint.detected_title if ocr_hint and ocr_hint.detected_title else "sem palpite"
        prompt = (
            "Voce esta analisando um frame dificil de filme ou serie, possivelmente uma foto de tela com legenda ou interface social. "
            "Use rosto, figurino, cinematografia, nomes escritos, titulo sobreposto e qualquer texto visivel para inferir o TITULO ORIGINAL mais provavel. "
            "Retorne JSON apenas com: detected_title, media_type, year, confidence, alt_titles, visible_text, need_clarification. "
            "Se houver varias opcoes plausiveis, coloque as alternativas em alt_titles e marque need_clarification=true. "
            "Se houver um palpite plausivel, retorne-o mesmo com confianca moderada; nao invente certeza.\n\n"
            f"Palpite OCR: {hint_title}\n"
            f"Texto OCR visivel: {visible_text}\n"
        )
        attempts: list[str] = []
        paid_models = list(dict.fromkeys(self.settings.openrouter_paid_vision_models))
        if not paid_models:
            return VisionCandidate(), attempts
        for variant_name, variant_bytes in self._build_llm_vision_variants(image_bytes):
            variant_b64 = base64.b64encode(variant_bytes).decode("ascii")
            candidate, variant_attempts = await self._query_candidate(
                variant_b64,
                prompt,
                use_json_mode=True,
                models=paid_models,
            )
            attempts.extend([f"{variant_name}::{attempt}" for attempt in variant_attempts])
            if candidate.detected_title:
                return candidate, attempts
        return VisionCandidate(), attempts

    async def _query_assertive_text_candidate(
        self,
        image_bytes: bytes,
        ocr_hint: VisionCandidate | None,
    ) -> tuple[VisionCandidate, list[str]]:
        preferred_models = list(
            dict.fromkeys(
                [
                    model
                    for model in [
                        "google/gemini-3-flash-preview",
                        "google/gemini-2.5-pro",
                        "google/gemini-2.5-flash",
                        "openai/gpt-4.1-mini",
                        *self.settings.openrouter_paid_vision_models,
                    ]
                    if model
                ]
            )
        )
        if not preferred_models:
            return VisionCandidate(), []
        hint_title = ocr_hint.detected_title if ocr_hint and ocr_hint.detected_title else "sem palpite"
        hint_text = ", ".join(ocr_hint.visible_text[:8]) if ocr_hint and ocr_hint.visible_text else "sem OCR util"
        prompt = (
            "A imagem em anexo representa algum filme ou serie e provavelmente contem pistas textuais fortes. "
            "Leia o texto visivel com precisao, normalize espacos quebrados e cruze esse texto com o contexto visual para encontrar o TITULO ORIGINAL. "
            "Priorize texto explicito e contexto visual acima de popularidade. "
            "Se houver mais de uma opcao plausivel, use alt_titles para listar as melhores opcoes e marque need_clarification=true. "
            "Retorne JSON apenas com: detected_title, media_type, year, confidence, alt_titles, visible_text, need_clarification.\n\n"
            f"Palpite OCR: {hint_title}\n"
            f"Texto OCR visivel: {hint_text}\n"
        )
        attempts: list[str] = []
        for variant_name, variant_bytes in self._build_llm_vision_variants(image_bytes):
            variant_b64 = base64.b64encode(variant_bytes).decode("ascii")
            candidate, variant_attempts = await self._query_candidate(
                variant_b64,
                prompt,
                use_json_mode=True,
                models=preferred_models,
            )
            attempts.extend([f"assertive::{variant_name}::{attempt}" for attempt in variant_attempts])
            if candidate.detected_title:
                return candidate, attempts
        return VisionCandidate(), attempts

    async def refine_title_from_user_feedback(
        self,
        selection: str,
        options: list[dict[str, Any]],
    ) -> VisionCandidate:
        options_payload = json.dumps(options, ensure_ascii=True)
        prompt = (
            "O usuario respondeu para desambiguar o titulo de um filme ou serie. "
            "Sua tarefa e descobrir o TITULO ORIGINAL exato a partir da resposta do usuario e das opcoes candidatas. "
            "Se o usuario escolheu um numero, mapeie para a opcao correta. "
            "Se o usuario escreveu um nome parcial ou traduzido, normalize para o titulo original mais provavel. "
            "Retorne JSON apenas com: detected_title, media_type, year, confidence, alt_titles, visible_text, need_clarification.\n\n"
            f"Resposta do usuario: {selection}\n"
            f"Opcoes candidatas: {options_payload}\n"
        )
        data = await self._run_text_json_task(prompt) or {}
        if not data:
            return VisionCandidate()
        try:
            return VisionCandidate.model_validate(data)
        except Exception:
            return VisionCandidate()

    async def _query_candidate(
        self,
        image_b64: str,
        prompt: str,
        *,
        use_json_mode: bool,
        models: list[str] | None = None,
    ) -> tuple[VisionCandidate, list[str]]:
        attempts: list[str] = []
        model_sequence = [*(models or self.settings.openrouter_vision_models)]
        async with httpx.AsyncClient(
            base_url="https://openrouter.ai/api/v1",
            timeout=self.settings.openrouter_vision_request_timeout_seconds,
        ) as client:
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
            review_results = (payload.get("reviews") or {}).get("results", []) or []
            scored_reviews: list[tuple[float, str]] = []
            for review in review_results:
                content = " ".join(str(review.get("content") or "").split())
                if not content:
                    continue
                author_rating = ((review.get("author_details") or {}).get("rating")) or 0
                score = float(author_rating) * 10
                score += min(len(content), 2400) / 400
                scored_reviews.append((score, content))
            scored_reviews.sort(key=lambda item: item[0], reverse=True)
            reviews = [content for _, content in scored_reviews[:3]]
            return EnrichedMedia(
                title=payload.get("title") or payload.get("name") or candidate.detected_title or "Unknown",
                original_title=payload.get("original_title") or payload.get("original_name") or payload.get("title") or payload.get("name"),
                localized_title=payload.get("title") or payload.get("name") or candidate.detected_title or "Unknown",
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
        near_year_matches = [
            result
            for _, result in scored
            if self._is_exact_title_match(candidate.detected_title, result) and self._is_near_year_match(candidate.year, result)
        ]
        if len(near_year_matches) == 1:
            return near_year_matches[0]
        exact_title_matches = [result for _, result in scored if self._is_exact_title_match(candidate.detected_title, result)]
        if len(exact_title_matches) == 1:
            return exact_title_matches[0]
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

    def _is_near_year_match(self, candidate_year: int | None, result: dict[str, Any]) -> bool:
        if not candidate_year:
            return False
        result_year = (result.get("release_date") or result.get("first_air_date") or "")[:4]
        if not result_year.isdigit():
            return False
        return abs(int(result_year) - candidate_year) <= 1

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

    async def search_and_enrich(self, candidate: VisionCandidate) -> EnrichedMedia:
        params: dict[str, str] = {
            "apikey": self.settings.omdb_api_key,
            "plot": "full",
            "t": candidate.detected_title or "",
        }
        if candidate.year:
            params["y"] = str(candidate.year)
        if candidate.media_type == "movie":
            params["type"] = "movie"
        elif candidate.media_type == "series":
            params["type"] = "series"
        async with httpx.AsyncClient(base_url="https://www.omdbapi.com", timeout=30.0) as client:
            response = await client.get("/", params=params)
            response.raise_for_status()
            payload = response.json()
        if payload.get("Response") != "True":
            raise HTTPException(status_code=404, detail="No OMDb match found.")
        ratings: dict[str, str] = {}
        imdb_rating = str(payload.get("imdbRating") or "").strip()
        if imdb_rating and imdb_rating != "N/A":
            ratings["IMDb"] = f"{imdb_rating}/10"
        for rating in payload.get("Ratings", []) if isinstance(payload.get("Ratings"), list) else []:
            source = str(rating.get("Source") or "").strip()
            value = str(rating.get("Value") or "").strip()
            if source and value:
                ratings[source] = value
        media_type = "series" if str(payload.get("Type") or "").lower() == "series" else "movie"
        genres = [part.strip() for part in str(payload.get("Genre") or "").split(",") if part.strip() and part.strip() != "N/A"]
        return EnrichedMedia(
            title=str(payload.get("Title") or candidate.detected_title or "Unknown"),
            original_title=str(payload.get("Title") or candidate.detected_title or "Unknown"),
            localized_title=str(payload.get("Title") or candidate.detected_title or "Unknown"),
            media_type=media_type,
            year=int(str(payload.get("Year") or "0")[:4]) if str(payload.get("Year") or "")[:4].isdigit() else candidate.year,
            imdb_id=str(payload.get("imdbID") or "") or None,
            release_date=str(payload.get("Released") or "") or None,
            overview=compact_text(str(payload.get("Plot") or ""), 1200),
            genres=genres,
            ratings=ratings,
            providers=[],
            reviews=[],
            confidence=candidate.confidence,
            payload={"source": "omdb", "omdb": payload},
        )

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


class TMDbReviewClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def fetch_reviews(self, enriched: EnrichedMedia) -> list[str]:
        if not enriched.tmdb_id:
            return []
        endpoint = f"/tv/{enriched.tmdb_id}/reviews" if enriched.media_type == "series" else f"/movie/{enriched.tmdb_id}/reviews"
        async with httpx.AsyncClient(base_url="https://api.themoviedb.org/3", timeout=30.0) as client:
            collected = await self._fetch_review_candidates(client, endpoint)
        ranked = sorted(collected, key=self._review_sort_key, reverse=True)
        reviews: list[str] = []
        for _, text in ranked:
            if text not in reviews:
                reviews.append(text)
            if len(reviews) == 3:
                break
        return reviews

    async def _fetch_review_candidates(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
    ) -> list[tuple[tuple[int, float, int], str]]:
        collected: list[tuple[tuple[int, float, int], str]] = []
        for page in (1, 2):
            response = await client.get(endpoint, headers=self._headers(), params={"language": "en-US", "page": page})
            response.raise_for_status()
            payload = response.json()
            page_results = payload.get("results", []) if isinstance(payload, dict) else []
            if not page_results:
                break
            for item in page_results:
                text = self._normalize_review_text(item.get("content"))
                if not text:
                    continue
                if self._looks_like_transcript_or_chat(text):
                    continue
                collected.append((self._build_review_rank(item, text), text))
            total_pages = int(payload.get("total_pages") or 1) if isinstance(payload, dict) else 1
            if page >= total_pages:
                break
        return collected

    def _build_review_rank(self, item: dict[str, Any], text: str) -> tuple[int, float, int]:
        author_details = item.get("author_details") if isinstance(item, dict) else {}
        raw_rating = (author_details or {}).get("rating")
        rating = self._parse_rating(raw_rating)
        has_rating = 1 if rating is not None else 0
        normalized_rating = rating if rating is not None else -1.0
        return (has_rating, normalized_rating, len(text))

    def _review_sort_key(self, item: tuple[tuple[int, float, int], str]) -> tuple[int, float, int]:
        return item[0]

    def _parse_rating(self, value: Any) -> float | None:
        if value in (None, ""):
            return None
        try:
            rating = float(value)
        except (TypeError, ValueError):
            return None
        if rating < 0:
            return None
        return rating

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.tmdb_api_token}",
            "accept": "application/json",
        }

    def _normalize_review_text(self, value: Any) -> str:
        text = " ".join(str(value or "").split())
        if len(text) < 180:
            return ""
        return text[:6000]

    def _looks_like_transcript_or_chat(self, text: str) -> bool:
        lowered = text.casefold()
        if re.search(r"\b\d{1,2}:\d{2}:\d{2}\b", text):
            return True
        if text.count('"') >= 8 or text.count("“") + text.count("”") >= 8:
            return True
        if re.search(r"\b(epa|cara|quer um cafe|pizza\?)\b", lowered):
            return True
        dialogue_markers = sum(lowered.count(token) for token in [" eu ", " voce ", " sim ", " nao ", " quando? ", " por que? "])
        return dialogue_markers >= 8


class ReviewSourceClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.tmdb = TMDbReviewClient(settings)

    async def fetch_reviews(self, enriched: EnrichedMedia) -> list[str]:
        return await self.tmdb.fetch_reviews(enriched)


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
