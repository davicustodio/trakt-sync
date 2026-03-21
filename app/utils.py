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


def normalize_phone(value: str | None) -> str:
    if not value:
        return "unknown"
    digits = "".join(PHONE_RE.findall(value))
    return digits or "unknown"


def normalize_chat_jid(value: str | None) -> str:
    return value or "unknown@g.us"


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
    provider_message_id: str
    chat_jid: str
    requester_phone: str
    sender_phone: str
    is_from_me: bool
    message_type: str
    text_body: str | None
    media_url: str | None
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
        provider_message_id=str(provider_message_id),
        chat_jid=chat_jid,
        requester_phone=requester_phone,
        sender_phone=sender_phone,
        is_from_me=is_from_me,
        message_type=message_type,
        text_body=text_body,
        media_url=media_url,
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
