from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import canonical_owner_phone, is_authorized_self_chat
from app.clients import EvolutionClient, OpenRouterClient, TelegramClient, TraktClient
from app.config import Settings, get_settings
from app.db import SessionLocal, get_db_session, init_db
from app.models import PhoneProfile, TraktConnection
from app.schemas import NormalizedMessage
from app.services import MessageService, PipelineService
from app.utils import (
    build_telegram_user_key,
    canonical_command,
    decode_state,
    encode_state,
    extract_message_from_evolution,
    extract_message_from_telegram,
    normalize_requester_key,
    normalize_phone,
)
from app.worker import process_telegram_x_info, process_telegram_x_save, process_x_info, process_x_save

templates = Jinja2Templates(directory="app/templates")
poller_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    global poller_task
    settings = get_settings()
    if settings.evolution_polling_enabled:
        poller_task = asyncio.create_task(run_evolution_reconciler(settings))
    try:
        yield
    finally:
        if poller_task is not None:
            poller_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poller_task


app = FastAPI(title="trakt-sync", lifespan=lifespan)


async def settings_dep() -> Settings:
    return get_settings()


async def db_dep() -> AsyncSession:
    async for session in get_db_session():
        yield session


async def require_admin(
    request: Request,
    settings: Annotated[Settings, Depends(settings_dep)],
    x_admin_secret: Annotated[str | None, Header(alias="X-Admin-Secret")] = None,
) -> None:
    if not settings.admin_shared_secret:
        return
    token = x_admin_secret or request.query_params.get("token")
    if token != settings.admin_shared_secret:
        raise HTTPException(status_code=401, detail="Invalid admin secret.")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
async def ready() -> dict[str, str]:
    return {"status": "ready"}


async def dispatch_command(
    command: str,
    chat_jid: str,
    requester_phone: str,
    background_tasks: BackgroundTasks,
    trigger_message_id: str | None = None,
) -> None:
    if command == "x-info":
        background_tasks.add_task(process_x_info, {}, chat_jid, requester_phone, trigger_message_id)
    elif command == "x-save":
        background_tasks.add_task(process_x_save, {}, chat_jid, requester_phone)


async def dispatch_command_inline(
    command: str, chat_jid: str, requester_phone: str, trigger_message_id: str | None = None
) -> None:
    if command == "x-info":
        await process_x_info({}, chat_jid, requester_phone, trigger_message_id)
    elif command == "x-save":
        await process_x_save({}, chat_jid, requester_phone)


async def dispatch_telegram_command(
    command: str,
    chat_id: str,
    requester_key: str,
    background_tasks: BackgroundTasks,
    trigger_message_id: str | None = None,
    status_message_id: int | None = None,
) -> None:
    if command == "x-info":
        background_tasks.add_task(process_telegram_x_info, {}, chat_id, requester_key, trigger_message_id, status_message_id)
    elif command == "x-save":
        background_tasks.add_task(process_telegram_x_save, {}, chat_id, requester_key, status_message_id)


async def _handle_telegram_utility_command(
    normalized: NormalizedMessage,
    settings: Settings,
    db: AsyncSession,
) -> dict[str, str | None]:
    telegram = TelegramClient(settings)
    command = canonical_command(normalized.text_body)
    service = MessageService(settings, db)

    if command == "/start":
        profile = await service.upsert_phone_profile(normalized.requester_phone)
        profile.display_name = normalized.raw_payload.get("message", {}).get("from", {}).get("first_name")
        await db.commit()
        await telegram.send_text(
            normalized.chat_jid,
            "Bot ativo.\nEnvie uma foto com `x-info` na legenda ou envie a foto e depois `x-info`.\nUse `/trakt-connect` para ligar sua conta Trakt.",
        )
        return {"status": "accepted", "command": command}

    if command == "/help":
        await telegram.send_text(
            normalized.chat_jid,
            "/start\n/help\n/whoami\n/trakt-connect\n/trakt-status\n/admin-help\nx-info\nx-save",
        )
        return {"status": "accepted", "command": command}

    if command == "/whoami":
        await telegram.send_text(
            normalized.chat_jid,
            (
                f"user_key: {normalized.requester_phone}\n"
                f"user_id: {normalized.provider_user_id or 'unknown'}\n"
                f"chat_id: {normalized.chat_jid}"
            ),
        )
        return {"status": "accepted", "command": command}

    if command == "/trakt-connect":
        url = f"{settings.app_base_url.rstrip('/')}/admin/trakt/connect/{normalized.requester_phone}"
        if settings.admin_shared_secret:
            url = f"{url}?token={settings.admin_shared_secret}"
        await telegram.send_text(normalized.chat_jid, f"Abra este link para conectar sua conta Trakt:\n{url}")
        return {"status": "accepted", "command": command}

    if command == "/trakt-status":
        result = await db.execute(select(PhoneProfile).where(PhoneProfile.phone_number == normalized.requester_phone))
        profile = result.scalar_one_or_none()
        if not profile:
            await telegram.send_text(normalized.chat_jid, "Nenhum perfil encontrado ainda. Use /start primeiro.")
            return {"status": "accepted", "command": command}
        connection_result = await db.execute(
            select(TraktConnection).where(TraktConnection.phone_profile_id == profile.id)
        )
        connection = connection_result.scalar_one_or_none()
        if not connection or not connection.access_token:
            await telegram.send_text(normalized.chat_jid, "Sua conta Trakt ainda nao esta conectada. Use /trakt-connect.")
            return {"status": "accepted", "command": command}
        await telegram.send_text(
            normalized.chat_jid,
            f"Conta Trakt conectada. Usuario: {connection.trakt_username or 'desconhecido'}.",
        )
        return {"status": "accepted", "command": command}

    if command == "/admin-help":
        admin_url = f"{settings.app_base_url.rstrip('/')}/admin/trakt"
        if settings.admin_shared_secret:
            admin_url = f"{admin_url}?token={settings.admin_shared_secret}"
        await telegram.send_text(
            normalized.chat_jid,
            (
                "Controle de acesso do bot\n"
                "1. No Dokploy, abra a app e edite as env vars.\n"
                "2. Defina TELEGRAM_REQUIRE_APPROVAL=true.\n"
                f"3. Defina TELEGRAM_AUTO_APPROVED_USER_KEYS={normalized.requester_phone} para manter seu acesso.\n"
                "4. Compartilhe o bot com seus amigos e peca para enviarem /start.\n"
                f"5. Abra o painel admin: {admin_url}\n"
                "6. Na coluna Acesso Telegram, clique em Aprovar ou Revogar para cada usuario."
            ),
        )
        return {"status": "accepted", "command": command}

    return {"status": "ignored", "reason": "unsupported-command"}


async def _check_telegram_access(
    normalized: NormalizedMessage,
    settings: Settings,
    db: AsyncSession,
) -> bool:
    if not settings.telegram_require_approval:
        return True
    result = await db.execute(select(PhoneProfile).where(PhoneProfile.phone_number == normalized.requester_phone))
    profile = result.scalar_one_or_none()
    if profile is None:
        return False
    first_name = normalized.raw_payload.get("message", {}).get("from", {}).get("first_name")
    username = normalized.raw_payload.get("message", {}).get("from", {}).get("username")
    display_name = first_name or (f"@{username}" if username else None)
    should_commit = False
    if display_name and getattr(profile, "display_name", None) != display_name:
        profile.display_name = display_name
        should_commit = True
    if normalized.requester_phone in set(settings.telegram_auto_approved_user_keys):
        if not profile.telegram_access_granted:
            profile.telegram_access_granted = True
            should_commit = True
        if should_commit:
            await db.commit()
        return True
    if should_commit:
        await db.commit()
    return bool(profile.telegram_access_granted)


async def _handle_blocked_telegram_user(
    normalized: NormalizedMessage,
    settings: Settings,
) -> dict[str, str]:
    telegram = TelegramClient(settings)
    await telegram.send_text(
        normalized.chat_jid,
        (
            "Seu acesso ao bot ainda nao foi liberado.\n"
            "Seu cadastro foi recebido e o administrador precisa aprovar seu usuario antes do uso."
        ),
    )
    return {"status": "ignored", "reason": "telegram-access-pending"}


async def handle_normalized_message(
    normalized: NormalizedMessage,
    settings: Settings,
    db: AsyncSession,
    *,
    background_tasks: BackgroundTasks | None = None,
    force_inline_dispatch: bool = False,
) -> dict[str, str | None]:
    if normalized.channel == "whatsapp" and settings.self_chat_only_mode and not is_authorized_self_chat(settings, normalized):
        return {"status": "ignored", "reason": "self-chat-only"}
    if normalized.channel == "whatsapp" and settings.self_chat_only_mode:
        normalized.requester_phone = canonical_owner_phone(settings)
        normalized.sender_phone = canonical_owner_phone(settings)

    service = MessageService(settings, db)
    persisted = await service.persist_message(normalized)
    if not persisted.created:
        return {"status": "ignored", "reason": "duplicate-event"}

    command = canonical_command(normalized.text_body)
    if normalized.channel == "telegram":
        access_granted = await _check_telegram_access(normalized, settings, db)
        if not access_granted:
            return await _handle_blocked_telegram_user(normalized, settings)
    if normalized.channel == "telegram" and command in {
        "/start",
        "/help",
        "/whoami",
        "/trakt-connect",
        "/trakt-status",
        "/admin-help",
    }:
        return await _handle_telegram_utility_command(normalized, settings, db)
    if command in {"x-info", "x-save"}:
        if normalized.channel == "telegram":
            telegram = TelegramClient(settings)
            total_steps = 6 if command == "x-info" else 5
            await telegram.send_text(normalized.chat_jid, f"Recebi sua solicitacao. O {command} esta em processamento.")
            status_message_id = await telegram.send_text(
                normalized.chat_jid,
                f"[{command}] Etapa 1/{total_steps}: preparando o processamento.",
            )
            if background_tasks is None or force_inline_dispatch:
                if command == "x-info":
                    await process_telegram_x_info(
                        {}, normalized.chat_jid, normalized.requester_phone, normalized.provider_message_id, status_message_id
                    )
                else:
                    await process_telegram_x_save({}, normalized.chat_jid, normalized.requester_phone, status_message_id)
            else:
                await dispatch_telegram_command(
                    command,
                    normalized.chat_jid,
                    normalized.requester_phone,
                    background_tasks,
                    normalized.provider_message_id,
                    status_message_id,
                )
            return {"status": "accepted", "command": command}
        if force_inline_dispatch or background_tasks is None:
            await dispatch_command_inline(
                command,
                normalized.chat_jid,
                normalized.requester_phone,
                normalized.provider_message_id,
            )
        else:
            await dispatch_command(
                command,
                normalized.chat_jid,
                normalized.requester_phone,
                background_tasks,
                normalized.provider_message_id,
            )
    return {"status": "accepted", "command": command or None}


async def reconcile_recent_owner_messages(settings: Settings) -> None:
    evolution = EvolutionClient(settings)
    owner_jids = [f"{canonical_owner_phone(settings)}@s.whatsapp.net"]
    if settings.evolution_owner_lid:
        owner_jids.append(settings.evolution_owner_lid)

    payloads: list[dict] = []
    for remote_jid in dict.fromkeys(owner_jids):
        for record in await evolution.fetch_recent_messages(remote_jid, settings.evolution_polling_limit):
            payloads.append({"event": "MESSAGES_UPSERT", "data": record})

    unique_payloads: dict[str, dict] = {}
    for payload in payloads:
        extracted = extract_message_from_evolution(payload)
        if extracted is None:
            continue
        unique_payloads.setdefault(extracted.provider_message_id, payload)

    for payload in sorted(unique_payloads.values(), key=lambda item: int((item.get("data") or {}).get("messageTimestamp") or 0)):
        extracted = extract_message_from_evolution(payload)
        if extracted is None:
            continue
        normalized = NormalizedMessage(
            channel=extracted.channel,
            event_name=str(payload.get("event") or "MESSAGES_UPSERT"),
            provider_message_id=extracted.provider_message_id,
            chat_jid=extracted.chat_jid,
            requester_phone=extracted.requester_phone,
            sender_phone=extracted.sender_phone,
            provider_update_id=extracted.provider_update_id,
            provider_chat_id=extracted.provider_chat_id,
            provider_user_id=extracted.provider_user_id,
            chat_message_id=extracted.chat_message_id,
            is_from_me=extracted.is_from_me,
            message_type=extracted.message_type,
            text_body=extracted.text_body,
            media_url=extracted.media_url,
            media_file_id=extracted.media_file_id,
            media_mime_type=extracted.media_mime_type,
            raw_payload=payload,
        )
        async with SessionLocal() as db:
            await handle_normalized_message(normalized, settings, db, force_inline_dispatch=True)


async def run_evolution_reconciler(settings: Settings) -> None:
    while True:
        try:
            await reconcile_recent_owner_messages(settings)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(max(settings.evolution_polling_interval_seconds, 5))


@app.post("/webhooks/evolution/messages")
async def evolution_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Annotated[Settings, Depends(settings_dep)],
    db: Annotated[AsyncSession, Depends(db_dep)],
) -> JSONResponse:
    payload = await request.json()
    background_tasks.add_task(OpenRouterClient(settings).refresh_free_text_models_if_due)
    if settings.evolution_webhook_secret:
        provided = request.headers.get("X-Evolution-Secret")
        if provided != settings.evolution_webhook_secret:
            raise HTTPException(status_code=401, detail="Invalid Evolution webhook secret.")

    extracted = extract_message_from_evolution(payload)
    if extracted is None:
        return JSONResponse({"status": "ignored"})

    normalized = NormalizedMessage(
        channel=extracted.channel,
        event_name=str(payload.get("event") or "MESSAGES_UPSERT"),
        provider_message_id=extracted.provider_message_id,
        chat_jid=extracted.chat_jid,
        requester_phone=extracted.requester_phone,
        sender_phone=extracted.sender_phone,
        provider_update_id=extracted.provider_update_id,
        provider_chat_id=extracted.provider_chat_id,
        provider_user_id=extracted.provider_user_id,
        chat_message_id=extracted.chat_message_id,
        is_from_me=extracted.is_from_me,
        message_type=extracted.message_type,
        text_body=extracted.text_body,
        media_url=extracted.media_url,
        media_file_id=extracted.media_file_id,
        media_mime_type=extracted.media_mime_type,
        raw_payload=payload,
    )

    return JSONResponse(await handle_normalized_message(normalized, settings, db, background_tasks=background_tasks))


@app.post("/webhooks/telegram")
async def telegram_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Annotated[Settings, Depends(settings_dep)],
    db: Annotated[AsyncSession, Depends(db_dep)],
) -> JSONResponse:
    payload = await request.json()
    background_tasks.add_task(OpenRouterClient(settings).refresh_free_text_models_if_due)
    if settings.telegram_webhook_secret:
        provided = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if provided != settings.telegram_webhook_secret:
            raise HTTPException(status_code=401, detail="Invalid Telegram webhook secret.")

    extracted = extract_message_from_telegram(payload)
    if extracted is None:
        return JSONResponse({"status": "ignored"})

    normalized = NormalizedMessage(
        channel=extracted.channel,
        event_name="telegram.message",
        provider_message_id=extracted.provider_message_id,
        chat_jid=extracted.chat_jid,
        requester_phone=extracted.requester_phone,
        sender_phone=extracted.sender_phone,
        provider_update_id=extracted.provider_update_id,
        provider_chat_id=extracted.provider_chat_id,
        provider_user_id=extracted.provider_user_id,
        chat_message_id=extracted.chat_message_id,
        is_from_me=extracted.is_from_me,
        message_type=extracted.message_type,
        text_body=extracted.text_body,
        media_url=extracted.media_url,
        media_file_id=extracted.media_file_id,
        media_mime_type=extracted.media_mime_type,
        raw_payload=payload,
    )
    return JSONResponse(await handle_normalized_message(normalized, settings, db, background_tasks=background_tasks))


@app.get("/admin/trakt", response_class=HTMLResponse)
async def trakt_admin(
    request: Request,
    _: Annotated[None, Depends(require_admin)],
    settings: Annotated[Settings, Depends(settings_dep)],
    db: Annotated[AsyncSession, Depends(db_dep)],
) -> HTMLResponse:
    result = await db.execute(select(PhoneProfile).order_by(PhoneProfile.phone_number.asc()))
    profiles = result.scalars().all()
    profile_rows = []
    for profile in profiles:
        connection_result = await db.execute(
            select(TraktConnection).where(TraktConnection.phone_profile_id == profile.id)
        )
        connection = connection_result.scalar_one_or_none()
        profile_rows.append(
            {
                "phone_number": profile.phone_number,
                "display_name": profile.display_name,
                "trakt_enabled": profile.trakt_enabled,
                "telegram_access_granted": profile.telegram_access_granted,
                "is_telegram_user": profile.phone_number.startswith("telegram_"),
                "has_token": bool(connection and connection.access_token),
                "connect_url": f"trakt/connect/{profile.phone_number}",
            }
        )
    return templates.TemplateResponse(
        "trakt_admin.html",
        {
            "request": request,
            "profiles": profile_rows,
            "redirect_uri": settings.computed_trakt_redirect_uri,
            "admin_secret": settings.admin_shared_secret or "",
            "register_url": "trakt/register",
            "access_url": "telegram/access",
        },
    )


@app.post("/admin/trakt/register")
async def register_phone(
    request: Request,
    _: Annotated[None, Depends(require_admin)],
    settings: Annotated[Settings, Depends(settings_dep)],
    db: Annotated[AsyncSession, Depends(db_dep)],
    phone_number: Annotated[str, Form()],
    display_name: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    service = MessageService(settings, db)
    profile = await service.upsert_phone_profile(normalize_requester_key(phone_number))
    profile.display_name = display_name
    await db.commit()
    target = "../trakt"
    if settings.admin_shared_secret:
        target += f"?token={settings.admin_shared_secret}"
    return RedirectResponse(target, status_code=303)


@app.post("/admin/telegram/access")
async def update_telegram_access(
    _: Annotated[None, Depends(require_admin)],
    settings: Annotated[Settings, Depends(settings_dep)],
    db: Annotated[AsyncSession, Depends(db_dep)],
    phone_number: Annotated[str, Form()],
    granted: Annotated[bool, Form()],
) -> RedirectResponse:
    normalized_phone = normalize_requester_key(phone_number)
    service = MessageService(settings, db)
    profile = await service.upsert_phone_profile(normalized_phone)
    profile.telegram_access_granted = granted
    await db.commit()
    target = "../trakt"
    if settings.admin_shared_secret:
        target += f"?token={settings.admin_shared_secret}"
    return RedirectResponse(target, status_code=303)


@app.get("/admin/trakt/connect/{phone_number}")
async def connect_trakt(
    phone_number: str,
    _: Annotated[None, Depends(require_admin)],
    settings: Annotated[Settings, Depends(settings_dep)],
) -> RedirectResponse:
    client = TraktClient(settings)
    state = encode_state(
        {"phone_number": normalize_requester_key(phone_number), "generated_at": datetime.now(UTC).isoformat()},
        settings.trakt_client_secret,
    )
    return RedirectResponse(client.build_authorize_url(state), status_code=302)


@app.get("/auth/trakt/callback", response_class=HTMLResponse)
async def trakt_callback(
    request: Request,
    code: str,
    state: str,
    settings: Annotated[Settings, Depends(settings_dep)],
) -> HTMLResponse:
    payload = decode_state(state, settings.trakt_client_secret)
    phone_number = normalize_requester_key(payload["phone_number"])
    pipeline = PipelineService(settings)
    token_payload = await pipeline.trakt.exchange_code(code)
    async with SessionLocal() as db:
        connection = await pipeline.persist_trakt_callback(db, phone_number, token_payload)
        profile = await pipeline.trakt.get_profile(connection.access_token or token_payload["access_token"])
        connection.trakt_username = profile.get("user", {}).get("username")
        await db.commit()
    return HTMLResponse(
        f"<html><body><h1>Conta Trakt vinculada</h1><p>Telefone: {phone_number}</p>"
        f"<p>Usuario Trakt: {connection.trakt_username or 'desconhecido'}</p></body></html>"
    )
