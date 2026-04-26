from __future__ import annotations

from app.config import Settings
from app.telegram_poller import telegram_update_to_incoming
from app.telegram_sender import TelegramSender


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


def test_telegram_update_to_incoming_supergroup_topic() -> None:
    incoming = telegram_update_to_incoming(
        {
            "update_id": 2,
            "message": {
                "message_id": 11,
                "message_thread_id": 77,
                "text": "tối nay ăn gì",
                "from": {"id": 99},
                "chat": {"id": -100123, "type": "supergroup"},
            },
        }
    )

    assert incoming is not None
    assert incoming.conversation_id == "-100123"
    assert incoming.conversation_type == "group"
    assert incoming.thread_id == "77"


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


def test_telegram_sender_includes_message_thread_id(monkeypatch, tmp_path) -> None:
    captured: dict = {}

    class FakeResponse:
        content = b"{}"
        status_code = 200

        def json(self):
            return {"ok": True, "result": {"message_id": 42}}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, json):
            captured["url"] = url
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr("app.telegram_sender.httpx.AsyncClient", FakeAsyncClient)
    settings = Settings(
        data_dir=tmp_path / "data",
        agent_prompt_path=tmp_path / "AGENT.md",
        telegram_bot_token="test-token",
    )
    sender = TelegramSender(settings)

    import asyncio

    result = asyncio.run(sender.send_text(text="hello", conversation_id="-100123", conversation_type="group", thread_id="77"))

    assert result.ok is True
    assert captured["json"]["chat_id"] == "-100123"
    assert captured["json"]["message_thread_id"] == 77
