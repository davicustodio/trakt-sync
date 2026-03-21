from __future__ import annotations

from app.config import Settings
from app.schemas import NormalizedMessage
from app.utils import normalize_phone


def canonical_owner_phone(settings: Settings) -> str:
    return normalize_phone(settings.evolution_owner_phone)


def owner_identifiers(settings: Settings) -> set[str]:
    identifiers = {canonical_owner_phone(settings)}
    owner_lid = normalize_phone(settings.evolution_owner_lid)
    if owner_lid != "unknown":
        identifiers.add(owner_lid)
    return identifiers


def is_authorized_self_chat(settings: Settings, message: NormalizedMessage) -> bool:
    identifiers = owner_identifiers(settings)
    is_owner_chat = normalize_phone(message.chat_jid) in identifiers
    is_owner_sender = normalize_phone(message.sender_phone) in identifiers
    is_owner_requester = normalize_phone(message.requester_phone) in identifiers
    return message.is_from_me and is_owner_chat and is_owner_sender and is_owner_requester
