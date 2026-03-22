from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class NormalizedMessage(BaseModel):
    channel: str = "whatsapp"
    event_name: str
    provider_message_id: str
    chat_jid: str
    requester_phone: str
    sender_phone: str
    provider_update_id: str | None = None
    provider_chat_id: str | None = None
    provider_user_id: str | None = None
    chat_message_id: str | None = None
    is_from_me: bool = False
    message_type: str
    text_body: str | None = None
    media_url: str | None = None
    media_file_id: str | None = None
    media_mime_type: str | None = None
    raw_payload: dict[str, Any]


class VisionCandidate(BaseModel):
    detected_title: str | None = None
    media_type: str = "unknown"
    year: int | None = None
    confidence: float = 0.0
    alt_titles: list[str] = Field(default_factory=list)
    visible_text: list[str] = Field(default_factory=list)
    need_clarification: bool = False


class EnrichedMedia(BaseModel):
    title: str
    media_type: str
    year: int | None = None
    tmdb_id: int | None = None
    imdb_id: str | None = None
    release_date: str | None = None
    overview: str | None = None
    genres: list[str] = Field(default_factory=list)
    ratings: dict[str, str] = Field(default_factory=dict)
    providers: list[str] = Field(default_factory=list)
    reviews: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    payload: dict[str, Any] = Field(default_factory=dict)


class TraktLinkState(BaseModel):
    phone: str
    next_url: str
    generated_at: datetime
