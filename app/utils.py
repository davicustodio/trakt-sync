from __future__ import annotations

import json
import hashlib
import hmac
import re
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from typing import Any


PHONE_RE = re.compile(r"(\d+)")
MESSAGE_WRAPPER_KEYS = {
    "ephemeralMessage",
    "viewOnceMessage",
    "viewOnceMessageV2",
    "viewOnceMessageV2Extension",
}
TELEGRAM_USER_KEY_PREFIX = "telegram_"


def normalize_phone(value: str | None) -> str:
    if not value:
        return "unknown"
    digits = "".join(PHONE_RE.findall(value))
    return digits or "unknown"


def normalize_requester_key(value: str | None) -> str:
    if not value:
        return "unknown"
    text = str(value).strip()
    if text.startswith(TELEGRAM_USER_KEY_PREFIX):
        return text
    return normalize_phone(text)


def normalize_chat_jid(value: str | None) -> str:
    return value or "unknown@g.us"


def build_telegram_user_key(user_id: str | int | None) -> str:
    if user_id in (None, ""):
        return "telegram_unknown"
    return f"{TELEGRAM_USER_KEY_PREFIX}{user_id}"


def canonical_command(text: str | None) -> str | None:
    if not text:
        return None
    command = " ".join(str(text).strip().split()).lower()
    if not command:
        return None
    return command


def parse_json_response(value: str) -> dict[str, Any]:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        start = value.find("{")
        end = value.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(value[start : end + 1])
        raise


def first_not_empty(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def unwrap_message_content(message: dict[str, Any]) -> dict[str, Any]:
    current = message
    while isinstance(current, dict) and len(current) == 1:
        wrapper_key = next(iter(current.keys()))
        if wrapper_key not in MESSAGE_WRAPPER_KEYS:
            break
        wrapped = (current.get(wrapper_key) or {}).get("message")
        if not isinstance(wrapped, dict) or not wrapped:
            break
        current = wrapped
    return current


@dataclass(slots=True)
class ExtractedMessage:
    channel: str
    provider_message_id: str
    chat_jid: str
    requester_phone: str
    sender_phone: str
    provider_update_id: str | None
    provider_chat_id: str | None
    provider_user_id: str | None
    chat_message_id: str | None
    is_from_me: bool
    message_type: str
    text_body: str | None
    media_url: str | None
    media_file_id: str | None
    media_mime_type: str | None


def extract_message_from_evolution(payload: dict[str, Any]) -> ExtractedMessage | None:
    event = payload.get("event") or payload.get("type") or "UNKNOWN"
    if str(event).upper() not in {"MESSAGES_UPSERT", "MESSAGE_UPSERT", "messages.upsert"}:
        return None

    data = payload.get("data") or {}
    key = data.get("key") or {}
    message = unwrap_message_content(data.get("message") or {})
    provider_message_id = first_not_empty(key.get("id"), data.get("id"))
    if not provider_message_id:
        return None

    chat_jid = normalize_chat_jid(first_not_empty(key.get("remoteJid"), data.get("remoteJid"), payload.get("sender")))
    participant = first_not_empty(key.get("participant"), data.get("participant"))
    sender_candidate = participant or chat_jid
    sender_phone = normalize_phone(sender_candidate)
    requester_phone = normalize_phone(sender_candidate if participant else chat_jid)
    is_from_me = bool(key.get("fromMe") or data.get("fromMe"))

    if "conversation" in message:
        message_type = "text"
        text_body = message.get("conversation")
        media_url = None
        mime_type = None
    elif "extendedTextMessage" in message:
        message_type = "text"
        text_body = (message.get("extendedTextMessage") or {}).get("text")
        media_url = None
        mime_type = None
    elif "imageMessage" in message:
        image = message.get("imageMessage") or {}
        message_type = "image"
        text_body = image.get("caption")
        media_url = first_not_empty(image.get("url"), data.get("mediaUrl"), data.get("url"))
        mime_type = image.get("mimetype")
    else:
        message_type = next(iter(message.keys()), "unknown")
        text_body = None
        media_url = first_not_empty(data.get("mediaUrl"), data.get("url"))
        mime_type = None

    return ExtractedMessage(
        channel="whatsapp",
        provider_message_id=str(provider_message_id),
        chat_jid=chat_jid,
        requester_phone=requester_phone,
        sender_phone=sender_phone,
        provider_update_id=str(payload.get("id")) if payload.get("id") else None,
        provider_chat_id=chat_jid,
        provider_user_id=sender_phone,
        chat_message_id=str(provider_message_id),
        is_from_me=is_from_me,
        message_type=message_type,
        text_body=text_body,
        media_url=media_url,
        media_file_id=None,
        media_mime_type=mime_type,
    )


def extract_message_from_telegram(payload: dict[str, Any]) -> ExtractedMessage | None:
    message = payload.get("message")
    if not isinstance(message, dict):
        return None

    chat = message.get("chat") or {}
    from_user = message.get("from") or {}
    chat_id = chat.get("id")
    message_id = message.get("message_id")
    if chat_id in (None, "") or message_id in (None, ""):
        return None

    text = first_not_empty(message.get("text"), message.get("caption"))
    photo_entries = message.get("photo") or []
    photo = photo_entries[-1] if isinstance(photo_entries, list) and photo_entries else None
    document = message.get("document") if isinstance(message.get("document"), dict) else None
    document_mime_type = str((document or {}).get("mime_type") or "").strip().lower()
    image_document = document if document and document_mime_type.startswith("image/") else None
    media_payload = photo if isinstance(photo, dict) else image_document
    message_type = "image" if isinstance(media_payload, dict) else "text"
    media_file_id = media_payload.get("file_id") if isinstance(media_payload, dict) else None

    requester_user_id = str(from_user.get("id") or chat_id)
    user_key = build_telegram_user_key(requester_user_id)
    if isinstance(photo, dict):
        mime_type = "image/jpeg"
    elif image_document is not None:
        mime_type = document_mime_type or "image/jpeg"
    else:
        mime_type = None

    return ExtractedMessage(
        channel="telegram",
        provider_message_id=str(message_id),
        chat_jid=str(chat_id),
        requester_phone=user_key,
        sender_phone=user_key,
        provider_update_id=str(payload.get("update_id")) if payload.get("update_id") is not None else None,
        provider_chat_id=str(chat_id),
        provider_user_id=requester_user_id,
        chat_message_id=str(message_id),
        is_from_me=False,
        message_type=message_type,
        text_body=str(text) if text is not None else None,
        media_url=media_file_id,
        media_file_id=media_file_id,
        media_mime_type=mime_type,
    )


def compact_text(text: str | None, limit: int) -> str | None:
    if not text:
        return None
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def encode_state(payload: dict[str, Any], secret: str) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest().encode("ascii")
    return urlsafe_b64encode(raw + b"." + signature).decode("ascii")


def decode_state(token: str, secret: str) -> dict[str, Any]:
    decoded = urlsafe_b64decode(token.encode("ascii"))
    raw, signature = decoded.rsplit(b".", 1)
    expected = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest().encode("ascii")
    if not hmac.compare_digest(signature, expected):
        raise ValueError("Invalid state signature")
    return json.loads(raw.decode("utf-8"))
