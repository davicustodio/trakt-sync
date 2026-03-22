from app.utils import (
    build_telegram_user_key,
    canonical_command,
    decode_state,
    encode_state,
    extract_message_from_evolution,
    extract_message_from_telegram,
    normalize_phone,
    normalize_requester_key,
)


def test_normalize_phone_keeps_digits_only() -> None:
    assert normalize_phone("5511-99888-7766@s.whatsapp.net") == "5511998887766"


def test_normalize_requester_key_preserves_telegram_key() -> None:
    assert normalize_requester_key("telegram_321") == "telegram_321"
    assert normalize_requester_key("5511-99888-7766@s.whatsapp.net") == "5511998887766"


def test_encode_decode_state_roundtrip() -> None:
    payload = {"phone_number": "5511999999999"}
    token = encode_state(payload, "secret")
    assert decode_state(token, "secret") == payload


def test_extract_message_from_evolution_image_payload() -> None:
    payload = {
        "event": "MESSAGES_UPSERT",
        "data": {
            "key": {
                "id": "abc123",
                "remoteJid": "5511999999999@s.whatsapp.net",
                "fromMe": True,
            },
            "message": {
                "imageMessage": {
                    "caption": "poster",
                    "url": "https://example.com/image.jpg",
                    "mimetype": "image/jpeg",
                }
            },
        },
    }
    extracted = extract_message_from_evolution(payload)
    assert extracted is not None
    assert extracted.provider_message_id == "abc123"
    assert extracted.message_type == "image"
    assert extracted.media_url == "https://example.com/image.jpg"
    assert extracted.requester_phone == "5511999999999"


def test_extract_message_from_evolution_unwraps_view_once_image() -> None:
    payload = {
        "event": "MESSAGES_UPSERT",
        "data": {
            "key": {
                "id": "wrapped-1",
                "remoteJid": "5511999999999@s.whatsapp.net",
                "fromMe": True,
            },
            "message": {
                "viewOnceMessageV2": {
                    "message": {
                        "imageMessage": {
                            "caption": "print",
                            "url": "https://example.com/pasted-image.jpg",
                            "mimetype": "image/jpeg",
                        }
                    }
                }
            },
        },
    }

    extracted = extract_message_from_evolution(payload)

    assert extracted is not None
    assert extracted.provider_message_id == "wrapped-1"
    assert extracted.message_type == "image"
    assert extracted.media_url == "https://example.com/pasted-image.jpg"
    assert extracted.text_body == "print"


def test_extract_message_from_telegram_photo_caption_payload() -> None:
    payload = {
        "update_id": 1001,
        "message": {
            "message_id": 88,
            "from": {"id": 321, "first_name": "Davi", "username": "davi"},
            "chat": {"id": 321, "type": "private"},
            "caption": "x-info",
            "photo": [
                {"file_id": "small-photo"},
                {"file_id": "large-photo"},
            ],
        },
    }

    extracted = extract_message_from_telegram(payload)

    assert extracted is not None
    assert extracted.channel == "telegram"
    assert extracted.provider_message_id == "88"
    assert extracted.provider_update_id == "1001"
    assert extracted.chat_jid == "321"
    assert extracted.requester_phone == "telegram_321"
    assert extracted.message_type == "image"
    assert extracted.media_file_id == "large-photo"
    assert extracted.media_url == "large-photo"
    assert extracted.text_body == "x-info"


def test_build_telegram_user_key_and_canonical_command() -> None:
    assert build_telegram_user_key(123) == "telegram_123"
    assert canonical_command("  X-INFO  ") == "x-info"
