from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import EvolutionClient, OMDbClient, OpenRouterClient, TMDbClient, TraktClient
from app.config import Settings
from app.models import ChatState, IdentifiedMedia, IncomingMessage, PhoneProfile, TraktConnection
from app.schemas import EnrichedMedia, NormalizedMessage


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

    async def persist_message(self, normalized: NormalizedMessage) -> IncomingMessage:
        existing = await self.db.execute(
            select(IncomingMessage).where(IncomingMessage.provider_message_id == normalized.provider_message_id)
        )
        message = existing.scalar_one_or_none()
        if message:
            return message
        await self.upsert_phone_profile(normalized.requester_phone, normalized.chat_jid)
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
        return message

    async def find_latest_image(self, chat_jid: str) -> IncomingMessage:
        min_time = datetime.now(UTC) - timedelta(minutes=self.settings.image_command_ttl_minutes)
        result = await self.db.execute(
            select(IncomingMessage)
            .where(IncomingMessage.chat_jid == chat_jid)
            .where(IncomingMessage.message_type == "image")
            .where(IncomingMessage.received_at >= min_time)
            .order_by(IncomingMessage.id.desc())
            .limit(1)
        )
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
            payload=enriched.payload,
        )
        self.db.add(identified)
        await self.db.flush()
        result = await self.db.execute(select(ChatState).where(ChatState.chat_jid == message.chat_jid))
        state = result.scalar_one()
        state.last_identified_media_id = identified.id
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


class PipelineService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.evolution = EvolutionClient(settings)
        self.openrouter = OpenRouterClient(settings)
        self.tmdb = TMDbClient(settings)
        self.omdb = OMDbClient(settings)
        self.trakt = TraktClient(settings)

    async def enrich_from_image(self, media_url: str) -> EnrichedMedia:
        image = await self.evolution.fetch_media_bytes(media_url)
        candidate = await self.openrouter.identify_title(image)
        enriched = await self.tmdb.search_and_enrich(candidate)
        return await self.omdb.attach_ratings(enriched)

    async def format_whatsapp_reply(self, enriched: EnrichedMedia) -> str:
        ratings_lines = [f"- {label}: {value}" for label, value in enriched.ratings.items()]
        providers_lines = [f"- {provider}" for provider in enriched.providers[:6]] or [
            "- Nenhuma disponibilidade confirmada no Brasil no momento."
        ]
        review_lines = [f"{index + 1}. {review}" for index, review in enumerate(enriched.reviews[:3])]
        genres = ", ".join(enriched.genres[:4]) if enriched.genres else "Nao informado"
        return "\n".join(
            [
                f"{enriched.title} ({enriched.year or 'N/A'})",
                f"Tipo: {'Serie' if enriched.media_type == 'series' else 'Filme'}",
                f"Lancamento: {enriched.release_date or 'Nao informado'}",
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
                "",
                "Reviews",
                *(review_lines or ["1. Nenhuma review disponivel."]),
                "",
                "Comando",
                "- x-save",
            ]
        )

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
