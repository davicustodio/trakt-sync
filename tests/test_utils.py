from app.utils import decode_state, encode_state, extract_message_from_evolution, normalize_phone


def test_normalize_phone_keeps_digits_only() -> None:
    assert normalize_phone("5511-99888-7766@s.whatsapp.net") == "5511998887766"


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
