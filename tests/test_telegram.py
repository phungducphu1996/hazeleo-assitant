from __future__ import annotations

from app.telegram_poller import telegram_update_to_incoming


def test_telegram_update_to_incoming_private_message() -> None:
    incoming = telegram_update_to_incoming(
        {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "text": "Ngọc đang nghén",
                "from": {"id": 99},
                "chat": {"id": 123, "type": "private"},
            },
        }
    )

    assert incoming is not None
    assert incoming.text == "Ngọc đang nghén"
    assert incoming.from_uid == "99"
    assert incoming.conversation_id == "123"
    assert incoming.conversation_type == "user"


def test_telegram_update_to_incoming_filters_chat_id() -> None:
    incoming = telegram_update_to_incoming(
        {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "text": "hello",
                "from": {"id": 99},
                "chat": {"id": 123, "type": "private"},
            },
        },
        allowed_chat_ids={"999"},
    )

    assert incoming is None
