from __future__ import annotations

from arq import func
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal, init_db
from app.exceptions import AmbiguousTitleError
from app.models import PhoneProfile, TraktConnection
from app.queue import redis_settings
from app.services import MessageService, PipelineService


async def startup(_: dict) -> None:
    await init_db()


async def process_x_info(_: dict, chat_jid: str, requester_phone: str) -> None:
    settings = get_settings()
    pipeline = PipelineService(settings)
    async with SessionLocal() as db:
        service = MessageService(settings, db)
        try:
            message = await service.find_latest_image(chat_jid)
            if not message.media_url:
                await pipeline.evolution.send_text(
                    chat_jid,
                    "Nao encontrei uma URL de midia reutilizavel nessa imagem. Ajuste o Evolution para enviar mediaUrl ou webhook_base64.",
                )
                return
            enriched = await pipeline.enrich_from_image(message.media_url)
            await service.save_identified_media(message, enriched)
            await pipeline.evolution.send_text(chat_jid, await pipeline.format_whatsapp_reply(enriched))
        except AmbiguousTitleError as exc:
            await pipeline.evolution.send_text(chat_jid, await pipeline.format_ambiguous_reply(exc.options))
        except Exception as exc:  # noqa: BLE001
            await pipeline.evolution.send_text(chat_jid, f"Falha ao analisar a imagem: {exc}")


async def process_x_save(_: dict, chat_jid: str, requester_phone: str) -> None:
    settings = get_settings()
    pipeline = PipelineService(settings)
    async with SessionLocal() as db:
        service = MessageService(settings, db)
        try:
            identified = await service.get_latest_identified_media(requester_phone)
            profile_result = await db.execute(select(PhoneProfile).where(PhoneProfile.phone_number == requester_phone))
            profile = profile_result.scalar_one_or_none()
            if not profile:
                raise ValueError("Telefone sem perfil cadastrado para Trakt.")
            connection_result = await db.execute(
                select(TraktConnection).where(TraktConnection.phone_profile_id == profile.id)
            )
            connection = connection_result.scalar_one_or_none()
            if not connection:
                raise ValueError("Telefone sem conta Trakt vinculada. Use /admin/trakt para conectar.")
            access_token, refresh_token, expires_at = await pipeline.trakt.ensure_fresh_tokens(connection)
            connection.access_token = access_token
            connection.refresh_token = refresh_token
            connection.expires_at = expires_at
            enriched = pipeline.build_watchlist_item(identified)
            if not enriched.tmdb_id and not enriched.imdb_id:
                raise ValueError("Titulo identificado sem IDs externos para salvar no Trakt.")
            await pipeline.trakt.add_to_watchlist(access_token, enriched)
            await db.commit()
            await pipeline.evolution.send_text(chat_jid, f"{identified.title} foi salvo na sua watchlist do Trakt.")
        except Exception as exc:  # noqa: BLE001
            await pipeline.evolution.send_text(chat_jid, f"Falha ao salvar no Trakt: {exc}")


class WorkerSettings:
    functions = [
        func(process_x_info, max_tries=3, timeout=180),
        func(process_x_save, max_tries=3, timeout=120),
    ]
    redis_settings = redis_settings()
    on_startup = startup
