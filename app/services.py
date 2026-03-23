from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from dataclasses import dataclass
import re

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import EvolutionClient, OMDbClient, OpenRouterClient, ReviewSourceClient, TMDbClient, TelegramClient, TraktClient
from app.config import Settings
from app.models import ChatState, IdentifiedMedia, IncomingMessage, PhoneProfile, TraktConnection
from app.schemas import EnrichedMedia, NormalizedMessage, PendingIdentificationState, VisionCandidate


@dataclass(slots=True)
class PersistMessageResult:
    message: IncomingMessage
    created: bool


class MessageService:
    def __init__(self, settings: Settings, db: AsyncSession) -> None:
        self.settings = settings
        self.db = db

    async def upsert_phone_profile(self, phone_number: str, whatsapp_jid: str | None = None) -> PhoneProfile:
        result = await self.db.execute(select(PhoneProfile).where(PhoneProfile.phone_number == phone_number))
        profile = result.scalar_one_or_none()
        if profile is None:
            profile = PhoneProfile(phone_number=phone_number, whatsapp_jid=whatsapp_jid)
            self.db.add(profile)
            await self.db.flush()
        elif whatsapp_jid and not profile.whatsapp_jid:
            profile.whatsapp_jid = whatsapp_jid
        return profile

    async def persist_message(self, normalized: NormalizedMessage) -> PersistMessageResult:
        existing = await self.db.execute(
            select(IncomingMessage).where(IncomingMessage.provider_message_id == normalized.provider_message_id)
        )
        message = existing.scalar_one_or_none()
        if message:
            return PersistMessageResult(message=message, created=False)
        await self.upsert_phone_profile(
            normalized.requester_phone,
            normalized.chat_jid if normalized.channel == "whatsapp" else None,
        )
        message = IncomingMessage(
            provider_message_id=normalized.provider_message_id,
            event_name=normalized.event_name,
            chat_jid=normalized.chat_jid,
            requester_phone=normalized.requester_phone,
            sender_phone=normalized.sender_phone,
            is_from_me=normalized.is_from_me,
            message_type=normalized.message_type,
            text_body=normalized.text_body,
            media_url=normalized.media_url,
            media_mime_type=normalized.media_mime_type,
            raw_payload=normalized.raw_payload,
        )
        self.db.add(message)
        await self.db.flush()

        result = await self.db.execute(select(ChatState).where(ChatState.chat_jid == normalized.chat_jid))
        state = result.scalar_one_or_none()
        if state is None:
            state = ChatState(chat_jid=normalized.chat_jid, requester_phone=normalized.requester_phone)
            self.db.add(state)
            await self.db.flush()
        state.requester_phone = normalized.requester_phone
        if normalized.message_type == "image":
            state.last_image_message_id = message.id
        await self.db.commit()
        return PersistMessageResult(message=message, created=True)

    async def get_message_by_provider_id(self, provider_message_id: str) -> IncomingMessage:
        result = await self.db.execute(
            select(IncomingMessage).where(IncomingMessage.provider_message_id == provider_message_id).limit(1)
        )
        message = result.scalar_one_or_none()
        if not message:
            raise HTTPException(status_code=404, detail="Command message not found.")
        return message

    async def get_message_by_id(self, message_id: int) -> IncomingMessage:
        message = await self.db.get(IncomingMessage, message_id)
        if not message:
            raise HTTPException(status_code=404, detail="Source image message not found.")
        return message

    async def find_latest_image(
        self,
        chat_jid: str,
        requester_phone: str | None = None,
        *,
        before_received_at: datetime | None = None,
    ) -> IncomingMessage:
        min_time = datetime.now(UTC) - timedelta(minutes=self.settings.image_command_ttl_minutes)
        query = (
            select(IncomingMessage)
            .where(IncomingMessage.chat_jid == chat_jid)
            .where(IncomingMessage.message_type == "image")
            .where(IncomingMessage.received_at >= min_time)
        )
        if before_received_at is not None:
            query = query.where(IncomingMessage.received_at <= before_received_at)
        result = await self.db.execute(query.order_by(IncomingMessage.id.desc()).limit(1))
        message = result.scalar_one_or_none()
        if not message and requester_phone:
            query = (
                select(IncomingMessage)
                .where(IncomingMessage.requester_phone == requester_phone)
                .where(IncomingMessage.message_type == "image")
                .where(IncomingMessage.received_at >= min_time)
            )
            if before_received_at is not None:
                query = query.where(IncomingMessage.received_at <= before_received_at)
            result = await self.db.execute(query.order_by(IncomingMessage.id.desc()).limit(1))
            message = result.scalar_one_or_none()
        if not message:
            raise HTTPException(status_code=404, detail="No recent image found in this chat.")
        return message

    async def save_identified_media(self, message: IncomingMessage, enriched: EnrichedMedia) -> IdentifiedMedia:
        identified = IdentifiedMedia(
            source_message_id=message.id,
            requester_phone=message.requester_phone,
            media_type=enriched.media_type,
            tmdb_id=enriched.tmdb_id,
            imdb_id=enriched.imdb_id,
            title=enriched.title,
            year=enriched.year,
            confidence=enriched.confidence,
            overview=enriched.overview,
            payload={
                **(enriched.payload or {}),
                "original_title": enriched.original_title,
                "localized_title": enriched.localized_title,
            },
        )
        self.db.add(identified)
        await self.db.flush()
        result = await self.db.execute(select(ChatState).where(ChatState.chat_jid == message.chat_jid))
        state = result.scalar_one()
        state.last_identified_media_id = identified.id
        state.pending_identification = None
        await self.db.commit()
        return identified

    async def get_latest_identified_media(self, requester_phone: str) -> IdentifiedMedia:
        min_time = datetime.now(UTC) - timedelta(hours=self.settings.save_command_ttl_hours)
        result = await self.db.execute(
            select(IdentifiedMedia)
            .where(IdentifiedMedia.requester_phone == requester_phone)
            .where(IdentifiedMedia.created_at >= min_time)
            .order_by(IdentifiedMedia.id.desc())
            .limit(1)
        )
        identified = result.scalar_one_or_none()
        if not identified:
            raise HTTPException(status_code=404, detail="No identified title is available yet.")
        return identified

    async def store_pending_identification(
        self,
        chat_jid: str,
        requester_phone: str,
        pending: PendingIdentificationState,
    ) -> None:
        result = await self.db.execute(select(ChatState).where(ChatState.chat_jid == chat_jid))
        state = result.scalar_one_or_none()
        if state is None:
            state = ChatState(chat_jid=chat_jid, requester_phone=requester_phone)
            self.db.add(state)
            await self.db.flush()
        state.requester_phone = requester_phone
        state.pending_identification = pending.model_dump()
        await self.db.commit()

    async def get_pending_identification(
        self,
        chat_jid: str,
        requester_phone: str | None = None,
    ) -> PendingIdentificationState | None:
        if not hasattr(self.db, "execute"):
            return None
        try:
            result = await self.db.execute(select(ChatState).where(ChatState.chat_jid == chat_jid))
            state = result.scalar_one_or_none()
            pending = getattr(state, "pending_identification", None) if state is not None else None
            if pending:
                return PendingIdentificationState.model_validate(pending)
            if requester_phone:
                result = await self.db.execute(select(ChatState).where(ChatState.requester_phone == requester_phone))
                state = result.scalar_one_or_none()
                pending = getattr(state, "pending_identification", None) if state is not None else None
                if pending:
                    return PendingIdentificationState.model_validate(pending)
        except Exception:
            return None
        return None

    async def clear_pending_identification(self, chat_jid: str, requester_phone: str | None = None) -> None:
        if not hasattr(self.db, "execute"):
            return
        result = await self.db.execute(select(ChatState).where(ChatState.chat_jid == chat_jid))
        state = result.scalar_one_or_none()
        if state is None and requester_phone:
            result = await self.db.execute(select(ChatState).where(ChatState.requester_phone == requester_phone))
            state = result.scalar_one_or_none()
        if state is None:
            return
        state.pending_identification = None
        await self.db.commit()


class PipelineService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.evolution = EvolutionClient(settings)
        self.telegram = TelegramClient(settings) if getattr(settings, "telegram_bot_token", None) else None
        self.openrouter = OpenRouterClient(settings)
        self.tmdb = TMDbClient(settings)
        self.omdb = OMDbClient(settings)
        self.reviews = ReviewSourceClient(settings)
        self.trakt = TraktClient(settings)

    def messaging_for(self, channel: str):
        if channel == "telegram":
            if self.telegram is None:
                raise RuntimeError("Telegram client is not configured.")
            return self.telegram
        return self.evolution

    async def fetch_media_bytes(
        self,
        channel: str,
        provider_message_id: str,
        media_url: str | None = None,
        media_file_id: str | None = None,
    ) -> bytes:
        if channel == "telegram":
            if not media_file_id:
                raise HTTPException(status_code=404, detail="No Telegram file_id found for this message.")
            if self.telegram is None:
                raise RuntimeError("Telegram client is not configured.")
            return await self.telegram.fetch_media_bytes(media_file_id)
        return await self.evolution.fetch_media_bytes(provider_message_id, media_url)

    async def enrich_from_image(
        self,
        provider_message_id: str,
        media_url: str | None = None,
        *,
        channel: str = "whatsapp",
        media_file_id: str | None = None,
    ) -> EnrichedMedia:
        image = await self.fetch_media_bytes(channel, provider_message_id, media_url, media_file_id)
        candidate = await self.openrouter.identify_title(image)
        try:
            enriched = await self.tmdb.search_and_enrich(candidate)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            try:
                enriched = await self.omdb.search_and_enrich(candidate)
            except HTTPException:
                enriched = EnrichedMedia(
                    title=candidate.detected_title or "Titulo nao confirmado",
                    original_title=candidate.detected_title,
                    localized_title=candidate.detected_title,
                    media_type="series" if candidate.media_type == "series" else "movie",
                    year=candidate.year,
                    confidence=candidate.confidence,
                    overview="Identificacao parcial: nao consegui confirmar o catalogo nem no TMDb nem no IMDb/OMDb.",
                    payload={
                        "fallback": "vision-only",
                        "visible_text": candidate.visible_text,
                        "alt_titles": candidate.alt_titles,
                    },
                )
        return await self.omdb.attach_ratings(enriched)

    async def enrich_from_user_confirmation(
        self,
        selection: str,
        pending: PendingIdentificationState,
    ) -> EnrichedMedia:
        refined = await self.openrouter.refine_title_from_user_feedback(selection, pending.options)
        option = self._match_pending_option(selection, pending.options)
        if refined.detected_title:
            candidate = VisionCandidate(
                detected_title=refined.detected_title,
                media_type=refined.media_type,
                year=refined.year,
                confidence=max(refined.confidence, 0.96),
                alt_titles=refined.alt_titles or [entry.get("label", "") for entry in pending.options if entry.get("label")],
                visible_text=[f"user_selection={selection.strip()}", *(refined.visible_text or [])],
            )
        elif option is not None:
            candidate = VisionCandidate(
                detected_title=option["title"],
                media_type=option["media_type"],
                year=option.get("year"),
                confidence=0.99,
                alt_titles=[entry.get("label", "") for entry in pending.options if entry.get("label")],
                visible_text=[f"user_selection={selection.strip()}"],
            )
        else:
            parsed = self._parse_user_title_hint(selection)
            candidate = VisionCandidate(
                detected_title=parsed["title"],
                media_type=parsed["media_type"],
                year=parsed.get("year"),
                confidence=0.95,
                visible_text=[f"user_hint={selection.strip()}"],
            )
        enriched = await self.tmdb.search_and_enrich(candidate)
        return await self.omdb.attach_ratings(enriched)

    async def format_media_reply(self, enriched: EnrichedMedia) -> str:
        ratings_lines = [f"- {label}: {value}" for label, value in enriched.ratings.items()]
        providers_lines = [f"- {provider}" for provider in enriched.providers[:6]] or [
            "- Nenhuma disponibilidade confirmada no Brasil no momento."
        ]
        genres = ", ".join(enriched.genres[:4]) if enriched.genres else "Nao informado"
        release_date = self._format_date(enriched.release_date)
        return "\n".join(
            [
                f"{(enriched.localized_title or enriched.title)} ({enriched.year or 'N/A'})",
                f"Titulo original: {enriched.original_title or enriched.title}",
                f"Titulo em portugues: {enriched.localized_title or enriched.title}",
                f"Tipo: {'Serie' if enriched.media_type == 'series' else 'Filme'}",
                f"Lancamento: {release_date}",
                f"Generos: {genres}",
                "",
                "Notas",
                *(ratings_lines or ["- Sem ratings disponiveis."]),
                "",
                "Onde assistir no Brasil",
                *providers_lines,
                "",
                "Resumo",
                enriched.overview or "Sem resumo disponivel.",
            ]
        )

    async def format_whatsapp_reply(self, enriched: EnrichedMedia) -> str:
        return await self.format_media_reply(enriched)

    async def format_review_messages(self, enriched: EnrichedMedia) -> list[str]:
        reviews = [str(review).strip() for review in await self.reviews.fetch_reviews(enriched) if str(review).strip()]
        if not reviews:
            return ["Reviews\nNao encontrei reviews publicas integrais disponiveis na API oficial do TMDb para este titulo."]
        localized = await self.openrouter.translate_reviews_to_pt_br(reviews, title=getattr(enriched, "title", None))
        final_reviews = localized[: len(reviews)] if localized else reviews[:]
        return [f"Review {index + 1}\n{review}" for index, review in enumerate(final_reviews)]

    async def format_ambiguous_reply(self, options: list[str]) -> str:
        lines = [f"{index + 1}. {option}" for index, option in enumerate(options[:3])]
        return "\n".join(
            [
                "Nao consegui confirmar com seguranca qual titulo esta na imagem.",
                "As melhores opcoes foram:",
                *lines,
                "",
                "Responda com o numero da opcao mais adequada ou escreva o titulo correto com ano.",
            ]
        )

    async def format_confirmation_retry_reply(self) -> str:
        return (
            "Ainda nao consegui confirmar o titulo com sua resposta.\n"
            "Responda com o numero da opcao sugerida ou escreva algo como `Coherence (2013)`."
        )

    async def format_manual_help_reply(self, attempts: list[str] | None = None) -> str:
        lines = [
            "Nao consegui identificar o titulo com confianca suficiente, mesmo apos tentar OCR e modelos de visao mais fortes.",
            "Se voce souber o titulo, responda nesta conversa com algo como `The Gift (2015)` ou `Coherence (2013)`.",
            "Se preferir, envie outra imagem ou mais contexto e eu tento de novo.",
        ]
        if attempts:
            lines.extend(["", "Modelos e etapas testados:"])
            lines.extend(f"- {attempt}" for attempt in attempts[:8])
        return "\n".join(lines)

    async def format_watchlist_question(self, enriched: EnrichedMedia) -> str:
        media_label = "serie" if enriched.media_type == "series" else "filme"
        return f"Voce quer salvar este {media_label} na sua watchlist do Trakt? Responda aqui com sim ou nao."

    async def format_watchlist_declined(self) -> str:
        return "Certo, nao vou salvar este titulo na watchlist do Trakt."

    async def format_watchlist_retry(self) -> str:
        return "Responda com `sim` para salvar na watchlist do Trakt ou `nao` para ignorar."

    def build_watchlist_item(self, identified: IdentifiedMedia) -> EnrichedMedia:
        return EnrichedMedia(
            title=identified.title,
            original_title=identified.payload.get("original_title") if isinstance(identified.payload, dict) else None,
            localized_title=identified.payload.get("localized_title") if isinstance(identified.payload, dict) else None,
            media_type=identified.media_type,
            year=identified.year,
            tmdb_id=identified.tmdb_id,
            imdb_id=identified.imdb_id,
            overview=identified.overview,
            confidence=identified.confidence,
            payload=identified.payload,
        )

    def _format_date(self, value: str | None) -> str:
        if not value:
            return "Nao informado"
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d")
            return parsed.strftime("%d/%m/%Y")
        except ValueError:
            return value

    def build_pending_options(self, options: list[str], *, channel: str, image_message_id: int | None = None) -> PendingIdentificationState:
        parsed_options = [self._parse_option_label(option) for option in options[:3]]
        return PendingIdentificationState(
            mode="ambiguity",
            channel=channel,
            image_message_id=image_message_id,
            options=parsed_options,
        )

    def build_pending_manual_input(
        self,
        *,
        channel: str,
        image_message_id: int | None = None,
        attempts: list[str] | None = None,
    ) -> PendingIdentificationState:
        return PendingIdentificationState(
            mode="manual-input",
            channel=channel,
            image_message_id=image_message_id,
            attempts=attempts or [],
        )

    def build_pending_watchlist_confirmation(
        self,
        *,
        channel: str,
        identified_media_id: int | None = None,
    ) -> PendingIdentificationState:
        return PendingIdentificationState(
            mode="watchlist-confirmation",
            channel=channel,
            identified_media_id=identified_media_id,
        )

    def _match_pending_option(self, selection: str, options: list[dict[str, Any]]) -> dict[str, Any] | None:
        trimmed = selection.strip()
        if not trimmed:
            return None
        if trimmed.isdigit():
            index = int(trimmed) - 1
            if 0 <= index < len(options):
                return options[index]
        lowered = trimmed.casefold()
        for option in options:
            label = str(option.get("label") or "")
            title = str(option.get("title") or "")
            if lowered in {label.casefold(), title.casefold()}:
                return option
        return None

    def _parse_option_label(self, label: str) -> dict[str, Any]:
        match = self._parse_title_pattern(label)
        if match is None:
            return {"label": label, "title": label.strip(), "year": None, "media_type": "unknown"}
        return {
            "label": label,
            "title": match["title"],
            "year": match.get("year"),
            "media_type": match["media_type"],
        }

    def _parse_user_title_hint(self, selection: str) -> dict[str, Any]:
        parsed = self._parse_title_pattern(selection)
        if parsed is not None:
            return parsed
        title = " ".join(selection.split()).strip(" -")
        if len(title) < 2:
            raise HTTPException(status_code=400, detail="Titulo manual invalido.")
        return {"title": title, "year": None, "media_type": "unknown"}

    def _parse_title_pattern(self, text: str) -> dict[str, Any] | None:
        cleaned = " ".join(text.split()).strip()
        pattern = re.compile(
            r"^(?P<title>.+?)(?:\s*\((?P<year>\d{4}|N/A)\))?(?:\s*-\s*(?P<media>Filme|Serie|S[eé]rie|Movie|TV|Show))?$",
            re.IGNORECASE,
        )
        match = pattern.match(cleaned)
        if match is None:
            return None
        title = match.group("title").strip(" -")
        if not title:
            return None
        year_value = match.group("year")
        media_value = (match.group("media") or "").casefold()
        media_type = "unknown"
        if media_value in {"filme", "movie"}:
            media_type = "movie"
        elif media_value in {"serie", "série", "tv", "show"}:
            media_type = "series"
        year = int(year_value) if year_value and year_value.isdigit() else None
        return {"title": title, "year": year, "media_type": media_type}

    async def persist_trakt_callback(
        self, db: AsyncSession, phone_number: str, token_payload: dict[str, Any]
    ) -> TraktConnection:
        service = MessageService(self.settings, db)
        profile = await service.upsert_phone_profile(phone_number)
        profile.trakt_enabled = True
        result = await db.execute(select(TraktConnection).where(TraktConnection.phone_profile_id == profile.id))
        connection = result.scalar_one_or_none()
        expires_at = datetime.now(UTC) + timedelta(seconds=token_payload["expires_in"])
        if connection is None:
            connection = TraktConnection(
                phone_profile_id=profile.id,
                client_id=self.settings.trakt_client_id,
                client_secret=self.settings.trakt_client_secret,
                access_token=token_payload["access_token"],
                refresh_token=token_payload.get("refresh_token"),
                expires_at=expires_at,
            )
            db.add(connection)
        else:
            connection.client_id = self.settings.trakt_client_id
            connection.client_secret = self.settings.trakt_client_secret
            connection.access_token = token_payload["access_token"]
            connection.refresh_token = token_payload.get("refresh_token")
            connection.expires_at = expires_at
        await db.commit()
        await db.refresh(connection)
        return connection
