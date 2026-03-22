from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(AsyncAttrs, DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class PhoneProfile(TimestampMixin, Base):
    __tablename__ = "phone_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phone_number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    whatsapp_jid: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    trakt_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    telegram_access_granted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    trakt_connection: Mapped["TraktConnection | None"] = relationship(back_populates="phone_profile", uselist=False)


class TraktConnection(TimestampMixin, Base):
    __tablename__ = "trakt_connections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phone_profile_id: Mapped[int] = mapped_column(ForeignKey("phone_profiles.id"), unique=True)
    client_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    client_secret: Mapped[str | None] = mapped_column(String(255), nullable=True)
    access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trakt_username: Mapped[str | None] = mapped_column(String(255), nullable=True)

    phone_profile: Mapped[PhoneProfile] = relationship(back_populates="trakt_connection")


class IncomingMessage(Base):
    __tablename__ = "incoming_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_message_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    event_name: Mapped[str] = mapped_column(String(100), index=True)
    chat_jid: Mapped[str] = mapped_column(String(255), index=True)
    requester_phone: Mapped[str] = mapped_column(String(32), index=True)
    sender_phone: Mapped[str] = mapped_column(String(32), index=True)
    is_from_me: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    message_type: Mapped[str] = mapped_column(String(64), index=True)
    text_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class IdentifiedMedia(Base):
    __tablename__ = "identified_media"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_message_id: Mapped[int] = mapped_column(ForeignKey("incoming_messages.id"), index=True)
    requester_phone: Mapped[str] = mapped_column(String(32), index=True)
    media_type: Mapped[str] = mapped_column(String(32))
    tmdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    imdb_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    title: Mapped[str] = mapped_column(String(255))
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    overview: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ChatState(Base):
    __tablename__ = "chat_states"
    __table_args__ = (UniqueConstraint("chat_jid", name="uq_chat_states_chat_jid"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_jid: Mapped[str] = mapped_column(String(255), index=True)
    requester_phone: Mapped[str] = mapped_column(String(32), index=True)
    last_image_message_id: Mapped[int | None] = mapped_column(ForeignKey("incoming_messages.id"), nullable=True)
    last_identified_media_id: Mapped[int | None] = mapped_column(ForeignKey("identified_media.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
