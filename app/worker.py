from __future__ import annotations

from arq import func
from fastapi import HTTPException
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal, init_db
from app.exceptions import AmbiguousTitleError, VisionIdentificationError
from app.models import PhoneProfile, TraktConnection
from app.queue import redis_settings
from app.services import MessageService, PipelineService
from app.utils import TELEGRAM_USER_KEY_PREFIX


async def startup(_: dict) -> None:
    await init_db()


async def process_x_info(_: dict, chat_jid: str, requester_phone: str, trigger_message_id: str | None = None) -> None:
    settings = get_settings()
    pipeline = PipelineService(settings)
    async with SessionLocal() as db:
        service = MessageService(settings, db)
        try:
            before_received_at = None
            if trigger_message_id:
                command_message = await service.get_message_by_provider_id(trigger_message_id)
                before_received_at = command_message.received_at
            message = await service.find_latest_image(chat_jid, requester_phone, before_received_at=before_received_at)
            enriched = await pipeline.enrich_from_image(message.provider_message_id, message.media_url)
            identified = await service.save_identified_media(message, enriched)
            await pipeline.evolution.send_text(chat_jid, await pipeline.format_whatsapp_reply(enriched))
            for review_message in await pipeline.format_review_messages(enriched):
                await pipeline.evolution.send_text(chat_jid, review_message)
            await service.store_pending_identification(
                chat_jid,
                requester_phone,
                pipeline.build_pending_watchlist_confirmation(
                    channel="whatsapp",
                    identified_media_id=getattr(identified, "id", None),
                ),
            )
            await pipeline.evolution.send_text(chat_jid, await pipeline.format_watchlist_question(enriched))
        except AmbiguousTitleError as exc:
            await service.store_pending_identification(
                chat_jid,
                requester_phone,
                pipeline.build_pending_options(
                    exc.options,
                    channel="whatsapp",
                    image_message_id=getattr(message, "id", None) if "message" in locals() else None,
                ),
            )
            await pipeline.evolution.send_text(chat_jid, await pipeline.format_ambiguous_reply(exc.options))
        except VisionIdentificationError as exc:
            await service.store_pending_identification(
                chat_jid,
                requester_phone,
                pipeline.build_pending_manual_input(
                    channel="whatsapp",
                    image_message_id=getattr(message, "id", None) if "message" in locals() else None,
                    attempts=exc.attempts,
                ),
            )
            await pipeline.evolution.send_text(chat_jid, await pipeline.format_manual_help_reply(exc.attempts))
        except Exception as exc:  # noqa: BLE001
            await service.store_pending_identification(
                chat_jid,
                requester_phone,
                pipeline.build_pending_manual_input(
                    channel="whatsapp",
                    image_message_id=getattr(message, "id", None) if "message" in locals() else None,
                ),
            )
            await pipeline.evolution.send_text(chat_jid, _format_x_info_failure(exc))


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
                raise ValueError(_build_trakt_connect_message(settings, requester_phone, "Telefone sem perfil cadastrado para Trakt."))
            connection_result = await db.execute(
                select(TraktConnection).where(TraktConnection.phone_profile_id == profile.id)
            )
            connection = connection_result.scalar_one_or_none()
            if not connection:
                raise ValueError(
                    _build_trakt_connect_message(settings, requester_phone, "Telefone sem conta Trakt vinculada.")
                )
            access_token, refresh_token, expires_at = await pipeline.trakt.ensure_fresh_tokens(connection)
            connection.access_token = access_token
            connection.refresh_token = refresh_token
            connection.expires_at = expires_at
            enriched = pipeline.build_watchlist_item(identified)
            if not enriched.tmdb_id and not enriched.imdb_id:
                raise ValueError("Titulo identificado sem IDs externos para salvar no Trakt.")
            await pipeline.trakt.add_to_watchlist(access_token, enriched)
            await service.clear_pending_identification(chat_jid, requester_phone)
            await db.commit()
            await pipeline.evolution.send_text(chat_jid, f"{identified.title} foi salvo na sua watchlist do Trakt.")
        except Exception as exc:  # noqa: BLE001
            await pipeline.evolution.send_text(chat_jid, f"Falha ao salvar no Trakt: {exc}")


async def process_x_info_confirmation(_: dict, chat_jid: str, requester_phone: str, selection: str) -> None:
    settings = get_settings()
    pipeline = PipelineService(settings)
    async with SessionLocal() as db:
        service = MessageService(settings, db)
        pending = await service.get_pending_identification(chat_jid, requester_phone)
        if pending is None:
            await pipeline.evolution.send_text(chat_jid, "Nao ha nenhuma identificacao pendente para confirmar.")
            return
        try:
            enriched = await pipeline.enrich_from_user_confirmation(selection, pending)
            source_message = None
            if pending.image_message_id:
                source_message = await service.get_message_by_id(pending.image_message_id)
            if source_message is not None:
                identified = await service.save_identified_media(source_message, enriched)
            else:
                identified = None
                await service.clear_pending_identification(chat_jid, requester_phone)
            await pipeline.evolution.send_text(chat_jid, await pipeline.format_whatsapp_reply(enriched))
            for review_message in await pipeline.format_review_messages(enriched):
                await pipeline.evolution.send_text(chat_jid, review_message)
            await service.store_pending_identification(
                chat_jid,
                requester_phone,
                pipeline.build_pending_watchlist_confirmation(
                    channel="whatsapp",
                    identified_media_id=getattr(identified, "id", None),
                ),
            )
            await pipeline.evolution.send_text(chat_jid, await pipeline.format_watchlist_question(enriched))
        except (AmbiguousTitleError, HTTPException, ValueError):
            await pipeline.evolution.send_text(chat_jid, await pipeline.format_confirmation_retry_reply())
        except Exception as exc:  # noqa: BLE001
            await pipeline.evolution.send_text(chat_jid, _format_x_info_failure(exc))


async def process_x_watchlist_reply(_: dict, chat_jid: str, requester_phone: str, selection: str) -> None:
    settings = get_settings()
    pipeline = PipelineService(settings)
    async with SessionLocal() as db:
        service = MessageService(settings, db)
        pending = await service.get_pending_identification(chat_jid, requester_phone)
        if pending is None or pending.mode != "watchlist-confirmation":
            await pipeline.evolution.send_text(chat_jid, "Nao ha nenhuma pergunta pendente sobre a watchlist.")
            return
        decision = selection.strip().casefold()
        if decision in {"sim", "s", "yes", "y", "save", "salvar"}:
            await process_x_save({}, chat_jid, requester_phone)
            return
        if decision in {"nao", "não", "n", "no", "cancelar", "ignorar"}:
            await service.clear_pending_identification(chat_jid, requester_phone)
            await pipeline.evolution.send_text(chat_jid, await pipeline.format_watchlist_declined())
            return
        await pipeline.evolution.send_text(chat_jid, await pipeline.format_watchlist_retry())


async def _telegram_update_status(pipeline: PipelineService, chat_id: str, status_message_id: int | None, text: str) -> None:
    if pipeline.telegram is None:
        return
    if status_message_id is None:
        await pipeline.telegram.send_text(chat_id, text)
        return
    await pipeline.telegram.edit_text(chat_id, status_message_id, text)


async def _telegram_send_stage(
    pipeline: PipelineService,
    chat_id: str,
    status_message_id: int | None,
    text: str,
    *,
    action: str | None = None,
) -> None:
    if pipeline.telegram is None:
        return
    if action and pipeline.settings.telegram_enable_chat_actions:
        await pipeline.telegram.send_chat_action(chat_id, action)
    if pipeline.settings.telegram_enable_progress_messages:
        await _telegram_update_status(pipeline, chat_id, status_message_id, text)


async def process_telegram_x_info(
    _: dict,
    chat_id: str,
    requester_key: str,
    trigger_message_id: str | None = None,
    status_message_id: int | None = None,
) -> None:
    settings = get_settings()
    pipeline = PipelineService(settings)
    async with SessionLocal() as db:
        service = MessageService(settings, db)
        try:
            before_received_at = None
            if trigger_message_id:
                command_message = await service.get_message_by_provider_id(trigger_message_id)
                before_received_at = command_message.received_at
            await _telegram_send_stage(
                pipeline,
                chat_id,
                status_message_id,
                "[x-info] Etapa 2/6: localizando a ultima imagem valida.",
                action="typing",
            )
            message = await service.find_latest_image(chat_id, requester_key, before_received_at=before_received_at)
            await _telegram_send_stage(
                pipeline,
                chat_id,
                status_message_id,
                "[x-info] Etapa 3/6: baixando a imagem do Telegram.",
                action="upload_photo",
            )
            await _telegram_send_stage(
                pipeline,
                chat_id,
                status_message_id,
                "[x-info] Etapa 4/6: analisando a imagem com o modelo de visao.",
                action="typing",
            )
            enriched = await pipeline.enrich_from_image(
                message.provider_message_id,
                message.media_url,
                channel="telegram",
                media_file_id=getattr(message, "media_file_id", None) or message.media_url,
            )
            await _telegram_send_stage(
                pipeline,
                chat_id,
                status_message_id,
                "[x-info] Etapa 5/6: consolidando catalogo, ratings e provedores.",
                action="typing",
            )
            identified = await service.save_identified_media(message, enriched)
            await _telegram_send_stage(
                pipeline,
                chat_id,
                status_message_id,
                "[x-info] Etapa 6/6: montando a resposta final.",
                action="typing",
            )
            if pipeline.telegram is not None:
                await pipeline.telegram.send_text(chat_id, await pipeline.format_media_reply(enriched))
                for review_message in await pipeline.format_review_messages(enriched):
                    await pipeline.telegram.send_text(chat_id, review_message)
                await service.store_pending_identification(
                    chat_id,
                    requester_key,
                    pipeline.build_pending_watchlist_confirmation(
                        channel="telegram",
                        identified_media_id=getattr(identified, "id", None),
                    ),
                )
                await pipeline.telegram.send_text(chat_id, await pipeline.format_watchlist_question(enriched))
                await _telegram_update_status(pipeline, chat_id, status_message_id, "[x-info] Concluido com sucesso.")
        except AmbiguousTitleError as exc:
            await service.store_pending_identification(
                chat_id,
                requester_key,
                pipeline.build_pending_options(
                    exc.options,
                    channel="telegram",
                    image_message_id=getattr(message, "id", None) if "message" in locals() else None,
                ),
            )
            await _telegram_update_status(
                pipeline,
                chat_id,
                status_message_id,
                "[x-info] Ambiguidade detectada. Preciso de confirmacao para continuar.",
            )
            if pipeline.telegram is not None:
                await pipeline.telegram.send_text(chat_id, await pipeline.format_ambiguous_reply(exc.options))
        except VisionIdentificationError as exc:
            await service.store_pending_identification(
                chat_id,
                requester_key,
                pipeline.build_pending_manual_input(
                    channel="telegram",
                    image_message_id=getattr(message, "id", None) if "message" in locals() else None,
                    attempts=exc.attempts,
                ),
            )
            await _telegram_update_status(
                pipeline,
                chat_id,
                status_message_id,
                "[x-info] Falha durante o processamento.",
            )
            if pipeline.telegram is not None:
                await pipeline.telegram.send_text(chat_id, await pipeline.format_manual_help_reply(exc.attempts))
        except Exception as exc:  # noqa: BLE001
            await _telegram_update_status(
                pipeline,
                chat_id,
                status_message_id,
                "[x-info] Falha durante o processamento.",
            )
            if pipeline.telegram is not None:
                await pipeline.telegram.send_text(chat_id, _format_x_info_failure(exc))


async def process_telegram_x_info_confirmation(_: dict, chat_id: str, requester_key: str, selection: str) -> None:
    settings = get_settings()
    pipeline = PipelineService(settings)
    async with SessionLocal() as db:
        service = MessageService(settings, db)
        pending = await service.get_pending_identification(chat_id, requester_key)
        if pending is None:
            if pipeline.telegram is not None:
                await pipeline.telegram.send_text(chat_id, "Nao ha nenhuma identificacao pendente para confirmar.")
            return
        try:
            enriched = await pipeline.enrich_from_user_confirmation(selection, pending)
            source_message = None
            if pending.image_message_id:
                source_message = await service.get_message_by_id(pending.image_message_id)
            if source_message is not None:
                identified = await service.save_identified_media(source_message, enriched)
            else:
                identified = None
                await service.clear_pending_identification(chat_id, requester_key)
            if pipeline.telegram is not None:
                await pipeline.telegram.send_text(chat_id, await pipeline.format_media_reply(enriched))
                for review_message in await pipeline.format_review_messages(enriched):
                    await pipeline.telegram.send_text(chat_id, review_message)
                await service.store_pending_identification(
                    chat_id,
                    requester_key,
                    pipeline.build_pending_watchlist_confirmation(
                        channel="telegram",
                        identified_media_id=getattr(identified, "id", None),
                    ),
                )
                await pipeline.telegram.send_text(chat_id, await pipeline.format_watchlist_question(enriched))
        except (AmbiguousTitleError, HTTPException, ValueError):
            if pipeline.telegram is not None:
                await pipeline.telegram.send_text(chat_id, await pipeline.format_confirmation_retry_reply())
        except Exception as exc:  # noqa: BLE001
            if pipeline.telegram is not None:
                await pipeline.telegram.send_text(chat_id, _format_x_info_failure(exc))


async def process_telegram_watchlist_reply(_: dict, chat_id: str, requester_key: str, selection: str) -> None:
    settings = get_settings()
    pipeline = PipelineService(settings)
    async with SessionLocal() as db:
        service = MessageService(settings, db)
        pending = await service.get_pending_identification(chat_id, requester_key)
        if pending is None or pending.mode != "watchlist-confirmation":
            if pipeline.telegram is not None:
                await pipeline.telegram.send_text(chat_id, "Nao ha nenhuma pergunta pendente sobre a watchlist.")
            return
        decision = selection.strip().casefold()
        if decision in {"sim", "s", "yes", "y", "save", "salvar"}:
            await process_telegram_x_save({}, chat_id, requester_key)
            return
        if decision in {"nao", "não", "n", "no", "cancelar", "ignorar"}:
            await service.clear_pending_identification(chat_id, requester_key)
            if pipeline.telegram is not None:
                await pipeline.telegram.send_text(chat_id, await pipeline.format_watchlist_declined())
            return
        if pipeline.telegram is not None:
            await pipeline.telegram.send_text(chat_id, await pipeline.format_watchlist_retry())


async def process_telegram_x_save(
    _: dict,
    chat_id: str,
    requester_key: str,
    status_message_id: int | None = None,
) -> None:
    settings = get_settings()
    pipeline = PipelineService(settings)
    async with SessionLocal() as db:
        service = MessageService(settings, db)
        try:
            await _telegram_send_stage(
                pipeline,
                chat_id,
                status_message_id,
                "[x-save] Etapa 2/5: validando o ultimo titulo identificado.",
                action="typing",
            )
            identified = await service.get_latest_identified_media(requester_key)
            profile_result = await db.execute(select(PhoneProfile).where(PhoneProfile.phone_number == requester_key))
            profile = profile_result.scalar_one_or_none()
            if not profile:
                raise ValueError(_build_trakt_connect_message(settings, requester_key, "Usuario sem perfil cadastrado para Trakt."))
            await _telegram_send_stage(
                pipeline,
                chat_id,
                status_message_id,
                "[x-save] Etapa 3/5: validando sua conexao com o Trakt.",
                action="typing",
            )
            connection_result = await db.execute(
                select(TraktConnection).where(TraktConnection.phone_profile_id == profile.id)
            )
            connection = connection_result.scalar_one_or_none()
            if not connection:
                raise ValueError(_build_trakt_connect_message(settings, requester_key, "Conta Trakt ainda nao vinculada."))
            access_token, refresh_token, expires_at = await pipeline.trakt.ensure_fresh_tokens(connection)
            connection.access_token = access_token
            connection.refresh_token = refresh_token
            connection.expires_at = expires_at
            enriched = pipeline.build_watchlist_item(identified)
            if not enriched.tmdb_id and not enriched.imdb_id:
                raise ValueError("Titulo identificado sem IDs externos para salvar no Trakt.")
            await _telegram_send_stage(
                pipeline,
                chat_id,
                status_message_id,
                "[x-save] Etapa 4/5: enviando o titulo para a watchlist do Trakt.",
                action="typing",
            )
            await pipeline.trakt.add_to_watchlist(access_token, enriched)
            await service.clear_pending_identification(chat_id, requester_key)
            await db.commit()
            if pipeline.telegram is not None:
                await pipeline.telegram.send_text(chat_id, f"{identified.title} foi salvo na sua watchlist do Trakt.")
            await _telegram_update_status(pipeline, chat_id, status_message_id, "[x-save] Concluido com sucesso.")
        except Exception as exc:  # noqa: BLE001
            await _telegram_update_status(pipeline, chat_id, status_message_id, "[x-save] Falha durante o processamento.")
            if pipeline.telegram is not None:
                await pipeline.telegram.send_text(chat_id, f"Falha ao salvar no Trakt: {exc}")


def _format_x_info_failure(exc: Exception) -> str:
    if isinstance(exc, VisionIdentificationError):
        attempts = exc.attempts[:6]
        lines = [
            "Falha ao analisar a imagem.",
            f"Motivo: {exc.reason}",
        ]
        if attempts:
            lines.extend(["Modelos e etapas testados:", *[f"- {attempt}" for attempt in attempts]])
        lines.extend(
            [
                "",
                "Se esta imagem for uma captura do Telegram ou Instagram, envie um recorte mais fechado no poster ou no frame principal.",
            ]
        )
        return "\n".join(lines)
    if isinstance(exc, HTTPException):
        return f"Falha ao analisar a imagem. Motivo: {exc.detail}"
    return f"Falha ao analisar a imagem. Motivo: {exc}"


def _build_trakt_connect_message(settings, requester_phone: str, reason: str) -> str:
    if requester_phone.startswith(TELEGRAM_USER_KEY_PREFIX):
        return f"{reason} Envie /trakt-connect, conclua a autorizacao da sua conta Trakt e depois envie x-save novamente."
    base = settings.app_base_url.rstrip("/")
    if settings.admin_shared_secret:
        return (
            f"{reason} Abra {base}/admin/trakt/connect/{requester_phone}?token={settings.admin_shared_secret} "
            "para conectar sua conta Trakt e depois envie x-save novamente."
        )
    return f"{reason} Abra {base}/admin/trakt para conectar sua conta Trakt e depois envie x-save novamente."


class WorkerSettings:
    functions = [
        func(process_x_info, max_tries=3, timeout=180),
        func(process_x_save, max_tries=3, timeout=120),
        func(process_telegram_x_info, max_tries=3, timeout=180),
        func(process_telegram_x_save, max_tries=3, timeout=120),
    ]
    redis_settings = redis_settings()
    on_startup = startup
    health_check_interval = 30
