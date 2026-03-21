from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import is_authorized_self_chat
from app.clients import TraktClient
from app.config import Settings, get_settings
from app.db import SessionLocal, get_db_session, init_db
from app.models import PhoneProfile, TraktConnection
from app.queue import get_redis_pool
from app.schemas import NormalizedMessage
from app.services import MessageService, PipelineService
from app.utils import decode_state, encode_state, extract_message_from_evolution, normalize_phone
from app.worker import process_x_info, process_x_save

templates = Jinja2Templates(directory="app/templates")


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    yield


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


@app.post("/webhooks/evolution/messages")
async def evolution_webhook(
    request: Request,
    settings: Annotated[Settings, Depends(settings_dep)],
    db: Annotated[AsyncSession, Depends(db_dep)],
) -> JSONResponse:
    payload = await request.json()
    if settings.evolution_webhook_secret:
        provided = request.headers.get("X-Evolution-Secret")
        if provided != settings.evolution_webhook_secret:
            raise HTTPException(status_code=401, detail="Invalid Evolution webhook secret.")

    extracted = extract_message_from_evolution(payload)
    if extracted is None:
        return JSONResponse({"status": "ignored"})

    normalized = NormalizedMessage(
        event_name=str(payload.get("event") or "MESSAGES_UPSERT"),
        provider_message_id=extracted.provider_message_id,
        chat_jid=extracted.chat_jid,
        requester_phone=extracted.requester_phone,
        sender_phone=extracted.sender_phone,
        is_from_me=extracted.is_from_me,
        message_type=extracted.message_type,
        text_body=extracted.text_body,
        media_url=extracted.media_url,
        media_mime_type=extracted.media_mime_type,
        raw_payload=payload,
    )

    if settings.self_chat_only_mode and not is_authorized_self_chat(settings, normalized):
        return JSONResponse({"status": "ignored", "reason": "self-chat-only"})

    service = MessageService(settings, db)
    persisted = await service.persist_message(normalized)

    if not persisted.created:
        return JSONResponse({"status": "ignored", "reason": "duplicate-event"})

    command = (normalized.text_body or "").strip().lower()
    try:
        redis = await get_redis_pool()
    except Exception:
        redis = None

    try:
        if command == "x-info":
            if redis is not None:
                await redis.enqueue_job("process_x_info", normalized.chat_jid, normalized.requester_phone)
            else:
                asyncio.create_task(process_x_info({}, normalized.chat_jid, normalized.requester_phone))
        elif command == "x-save":
            if redis is not None:
                await redis.enqueue_job("process_x_save", normalized.chat_jid, normalized.requester_phone)
            else:
                asyncio.create_task(process_x_save({}, normalized.chat_jid, normalized.requester_phone))
    finally:
        if redis is not None:
            await redis.close(close_connection_pool=True)

    return JSONResponse({"status": "accepted", "command": command or None})


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
                "has_token": bool(connection and connection.access_token),
                "connect_url": str(request.url_for("connect_trakt", phone_number=profile.phone_number)),
            }
        )
    return templates.TemplateResponse(
        "trakt_admin.html",
        {
            "request": request,
            "profiles": profile_rows,
            "redirect_uri": settings.computed_trakt_redirect_uri,
            "admin_secret": settings.admin_shared_secret or "",
            "register_url": str(request.url_for("register_phone")),
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
    profile = await service.upsert_phone_profile(normalize_phone(phone_number))
    profile.display_name = display_name
    await db.commit()
    target = str(request.url_for("trakt_admin"))
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
        {"phone_number": normalize_phone(phone_number), "generated_at": datetime.now(UTC).isoformat()},
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
    phone_number = normalize_phone(payload["phone_number"])
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
