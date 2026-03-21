from __future__ import annotations

from app.config import Settings
from app.schemas import NormalizedMessage
from app.utils import normalize_phone


def is_authorized_self_chat(settings: Settings, message: NormalizedMessage) -> bool:
    owner_phone = normalize_phone(settings.evolution_owner_phone)
    is_owner_chat = normalize_phone(message.chat_jid) == owner_phone
    is_owner_sender = normalize_phone(message.sender_phone) == owner_phone
    is_owner_requester = normalize_phone(message.requester_phone) == owner_phone
    return message.is_from_me and is_owner_chat and is_owner_sender and is_owner_requester
