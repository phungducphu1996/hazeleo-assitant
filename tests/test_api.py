from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.agent_service import FamilyAssistantService
from app.config import Settings
from app.main import create_app
from app.schemas import AgentMemoryUpdates, AgentOutput, FoodPlaceUpdate, FridgeItemUpdate, ZaloDeliveryResult
from app.storage import FileStore
from app.thread_context import build_thread_key


class FakeModelClient:
    def __init__(self, output: AgentOutput) -> None:
        self.output = output
        self.calls: list[dict] = []

    async def run(self, **_kwargs) -> AgentOutput:
        self.calls.append(_kwargs)
        return self.output


class FakeSender:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[dict[str, str | None]] = []

    async def send_text(
        self,
        *,
        text: str,
        conversation_id: str | None = None,
        conversation_type: str = "user",
        thread_id: str | None = None,
    ) -> ZaloDeliveryResult:
        self.sent.append(
            {
                "text": text,
                "conversation_id": conversation_id,
                "conversation_type": conversation_type,
                "thread_id": thread_id,
            }
        )
        return ZaloDeliveryResult(ok=self.ok, error=None if self.ok else "boom")


def _settings(tmp_path, *, secret: str = "test-secret") -> Settings:
    prompt_path = tmp_path / "AGENT.md"
    prompt_path.write_text("Return JSON only.", encoding="utf-8")
    return Settings(
        data_dir=tmp_path / "data",
        agent_prompt_path=prompt_path,
        zalo_shared_secret=secret,
        telegram_webhook_secret="telegram-secret",
        reminder_poll_interval_seconds=3600,
        telegram_polling_enabled=False,
        openai_api_key="test-key",
    )


def test_zalo_incoming_saves_memory_reminder_and_sends_reply(tmp_path) -> None:
    settings = _settings(tmp_path)
    store = FileStore(settings.data_dir)
    future = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")) + timedelta(hours=1)
    output = AgentOutput(
        reply="ok, tối nay mình gợi ý món nhẹ nha",
        memory=AgentMemoryUpdates(
            profile_updates=["Ngọc đang ốm nghén"],
            recent_updates=["Tủ lạnh còn trứng và rau cải"],
        ),
        reminder={"text": "mua sữa cho Ngọc", "time": future.isoformat()},
    )
    fake_sender = FakeSender()
    service = FamilyAssistantService(
        settings=settings,
        store=store,
        model_client=FakeModelClient(output),
        sender=fake_sender,
    )
    app = create_app(settings)

    with TestClient(app) as client:
        app.state.zalo_assistant_service = service
        response = client.post(
            "/zalo/incoming",
            headers={"X-Internal-Secret": "test-secret"},
            json={
                "text": "mai 9h nhắc mình mua sữa cho Ngọc",
                "from_uid": "user-1",
                "conversation_id": "conv-1",
                "conversation_type": "user",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["reminder_saved"] is True
    assert fake_sender.sent[0]["text"] == "ok, tối nay mình gợi ý món nhẹ nha"
    assert fake_sender.sent[0]["conversation_id"] == "conv-1"
    assert "Ngọc đang ốm nghén" in store.read_profile()
    assert store.list_recent()[0].text == "Tủ lạnh còn trứng và rau cải"
    assert store.list_reminders()[0].text == "mua sữa cho Ngọc"


def test_schedule_reply_is_guarded_when_model_claims_saved_without_structured_task(tmp_path) -> None:
    settings = _settings(tmp_path, secret="")
    store = FileStore(settings.data_dir)
    fake_sender = FakeSender()
    service = FamilyAssistantService(
        settings=settings,
        store=store,
        model_client=FakeModelClient(
            AgentOutput(
                reply="ok Gia đã thêm task này rồi nha",
                memory=AgentMemoryUpdates(),
                reminder=None,
                agent_task=None,
                repeating_reminder=None,
                recurring_agent_task=None,
            )
        ),
        sender=fake_sender,
    )
    app = create_app(settings)

    with TestClient(app) as client:
        app.state.telegram_assistant_service = service
        app.state.telegram_poller.assistant_service = service
        response = client.post(
            "/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret"},
            json={
                "update_id": 115,
                "message": {
                    "message_id": 27,
                    "text": "mai 9h nhắc anh mua sữa",
                    "from": {"id": 999},
                    "chat": {"id": 12345, "type": "private"},
                },
            },
        )

    assert response.status_code == 200
    assert "chưa lưu được" in fake_sender.sent[0]["text"]
    assert store.list_reminders() == []


def test_schedule_reply_is_guarded_when_structured_task_save_fails(tmp_path) -> None:
    settings = _settings(tmp_path, secret="")
    store = FileStore(settings.data_dir)
    past = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")) - timedelta(hours=1)
    fake_sender = FakeSender()
    service = FamilyAssistantService(
        settings=settings,
        store=store,
        model_client=FakeModelClient(
            AgentOutput(
                reply="ok Gia đã đặt lịch nhắc rồi nha",
                memory=AgentMemoryUpdates(),
                reminder={"text": "mua sữa", "time": past.isoformat()},
            )
        ),
        sender=fake_sender,
    )
    app = create_app(settings)

    with TestClient(app) as client:
        app.state.telegram_assistant_service = service
        app.state.telegram_poller.assistant_service = service
        response = client.post(
            "/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret"},
            json={
                "update_id": 116,
                "message": {
                    "message_id": 28,
                    "text": "nhắc anh mua sữa lúc nãy",
                    "from": {"id": 999},
                    "chat": {"id": 12345, "type": "private"},
                },
            },
        )

    assert response.status_code == 200
    assert "thời gian này đã qua" in fake_sender.sent[0]["text"]
    assert store.list_reminders() == []


def test_telegram_webhook_marks_recent_sent_reminder_done(tmp_path) -> None:
    settings = _settings(tmp_path, secret="")
    store = FileStore(settings.data_dir)
    now = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh"))
    reminder = store.add_reminder(
        text="mua sữa",
        reminder_time=now - timedelta(minutes=1),
        now=now - timedelta(hours=1),
        conversation_id="12345",
        conversation_type="user",
    )
    store.update_reminder(reminder.id, status="sent", sent_at=now.isoformat())
    model_client = FakeModelClient(
        AgentOutput(
            reply="vâng anh chị, Gia đánh dấu xong rồi nha",
            memory=AgentMemoryUpdates(),
            reminder=None,
            task_status_update={"target_text": None, "completion_status": "done", "note": None},
        )
    )
    service = FamilyAssistantService(
        settings=settings,
        store=store,
        model_client=model_client,
        sender=FakeSender(),
    )
    app = create_app(settings)

    with TestClient(app) as client:
        app.state.telegram_assistant_service = service
        app.state.telegram_poller.assistant_service = service
        response = client.post(
            "/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret"},
            json={
                "update_id": 110,
                "message": {
                    "message_id": 22,
                    "text": "xong rồi nha",
                    "from": {"id": 999},
                    "chat": {"id": 12345, "type": "private"},
                },
            },
        )

    assert response.status_code == 200
    saved = store.list_reminders()[0]
    assert saved.completion_status == "done"
    assert saved.completed_by == "999"
    assert model_client.calls[0]["open_tasks"][0]["text"] == "mua sữa"


def test_manual_completion_endpoint_updates_reminder(tmp_path) -> None:
    settings = _settings(tmp_path, secret="")
    store = FileStore(settings.data_dir)
    now = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh"))
    reminder = store.add_reminder(
        text="uống vitamin",
        reminder_time=now + timedelta(hours=1),
        now=now,
        conversation_id="12345",
        conversation_type="user",
    )
    service = FamilyAssistantService(
        settings=settings,
        store=store,
        model_client=FakeModelClient(AgentOutput(reply="ok", memory=AgentMemoryUpdates())),
        sender=FakeSender(),
    )
    app = create_app(settings)

    with TestClient(app) as client:
        app.state.telegram_assistant_service = service
        response = client.post(
            f"/api/reminders/{reminder.id}/completion",
            json={"completion_status": "canceled", "note": "không cần nữa", "completed_by": "debug-user"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["reminder"]["completion_status"] == "canceled"
    assert body["reminder"]["completion_note"] == "không cần nữa"


def test_thread_debug_endpoints_update_prompt_and_rules(tmp_path) -> None:
    settings = _settings(tmp_path, secret="")
    app = create_app(settings)
    thread_key = "telegram:-100123:topic:77"

    with TestClient(app) as client:
        prompt_response = client.put(
            f"/api/threads/{thread_key}/prompt",
            json={"prompt": "Thread ăn uống chuyên dinh dưỡng."},
        )
        rules_response = client.put(
            f"/api/threads/{thread_key}/rules",
            json={"rules": ["Ưu tiên món nhẹ bụng."]},
        )
        list_response = client.get("/api/threads")
        detail_response = client.get(f"/api/threads/{thread_key}")

    assert prompt_response.status_code == 200
    assert rules_response.status_code == 200
    assert list_response.status_code == 200
    assert detail_response.status_code == 200
    assert list_response.json()["threads"][0]["thread_key"] == thread_key
    assert "chuyên dinh dưỡng" in detail_response.json()["prompt"]
    assert "món nhẹ bụng" in detail_response.json()["rules"]


def test_test_message_defaults_to_not_sending(tmp_path) -> None:
    settings = _settings(tmp_path, secret="")
    store = FileStore(settings.data_dir)
    fake_sender = FakeSender()
    service = FamilyAssistantService(
        settings=settings,
        store=store,
        model_client=FakeModelClient(
            AgentOutput(
                reply="ăn cháo thịt bằm nhẹ nha",
                memory=AgentMemoryUpdates(),
                reminder=None,
            )
        ),
        sender=fake_sender,
    )
    app = create_app(settings)

    with TestClient(app) as client:
        app.state.telegram_assistant_service = service
        response = client.post("/api/agent/test-message", json={"text": "tối nay ăn gì"})

    assert response.status_code == 200
    assert response.json()["reply"] == "ăn cháo thịt bằm nhẹ nha"
    assert fake_sender.sent == []


def test_invalid_secret_is_rejected(tmp_path) -> None:
    settings = _settings(tmp_path, secret="test-secret")
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.post(
            "/zalo/incoming",
            headers={"X-Internal-Secret": "wrong"},
            json={"text": "xin chào", "conversation_id": "conv-1"},
        )

    assert response.status_code == 403


def test_telegram_webhook_processes_message_and_sends_reply(tmp_path) -> None:
    settings = _settings(tmp_path, secret="")
    store = FileStore(settings.data_dir)
    fake_sender = FakeSender()
    service = FamilyAssistantService(
        settings=settings,
        store=store,
        model_client=FakeModelClient(
            AgentOutput(
                reply="ăn canh rau với trứng hấp nhẹ nha",
                memory=AgentMemoryUpdates(recent_updates=["Telegram user hỏi tối nay ăn gì"]),
                reminder=None,
            )
        ),
        sender=fake_sender,
    )
    app = create_app(settings)

    with TestClient(app) as client:
        app.state.telegram_assistant_service = service
        app.state.telegram_poller.assistant_service = service
        response = client.post(
            "/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret"},
            json={
                "update_id": 100,
                "message": {
                    "message_id": 12,
                    "text": "tối nay ăn gì",
                    "from": {"id": 999},
                    "chat": {"id": 12345, "type": "private"},
                },
            },
        )

    assert response.status_code == 200
    assert response.json()["processed"] is True
    assert fake_sender.sent == [
        {
            "text": "ăn canh rau với trứng hấp nhẹ nha",
            "conversation_id": "12345",
            "conversation_type": "user",
            "thread_id": None,
        }
    ]


def test_telegram_webhook_replies_in_topic_thread(tmp_path) -> None:
    settings = _settings(tmp_path, secret="")
    store = FileStore(settings.data_dir)
    fake_sender = FakeSender()
    service = FamilyAssistantService(
        settings=settings,
        store=store,
        model_client=FakeModelClient(
            AgentOutput(
                reply="vâng anh chị, tối nay ăn nhẹ nha",
                memory=AgentMemoryUpdates(),
                reminder=None,
            )
        ),
        sender=fake_sender,
    )
    app = create_app(settings)

    with TestClient(app) as client:
        app.state.telegram_assistant_service = service
        app.state.telegram_poller.assistant_service = service
        response = client.post(
            "/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret"},
            json={
                "update_id": 111,
                "message": {
                    "message_id": 23,
                    "message_thread_id": 77,
                    "text": "tối nay ăn gì",
                    "from": {"id": 999},
                    "chat": {"id": -100123, "type": "supergroup"},
                },
            },
        )

    assert response.status_code == 200
    assert fake_sender.sent[0]["conversation_id"] == "-100123"
    assert fake_sender.sent[0]["conversation_type"] == "group"
    assert fake_sender.sent[0]["thread_id"] == "77"


def test_telegram_topic_context_uses_thread_prompt_rules_and_history(tmp_path) -> None:
    settings = _settings(tmp_path, secret="")
    store = FileStore(settings.data_dir)
    now = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh"))
    food_key = build_thread_key(
        source="telegram",
        conversation_id="-100123",
        conversation_type="group",
        thread_id="77",
    )
    chores_key = build_thread_key(
        source="telegram",
        conversation_id="-100123",
        conversation_type="group",
        thread_id="88",
    )
    assert food_key is not None
    assert chores_key is not None
    store.set_thread_prompt(food_key, "Thread ăn uống: trả lời như chuyên gia dinh dưỡng gia đình.")
    store.append_thread_rules_updates(food_key, ["Ưu tiên món nhẹ cho Ngọc."])
    store.append_conversation_turn(
        now=now - timedelta(minutes=4),
        conversation_id="-100123",
        thread_key=food_key,
        from_uid="user-1",
        role="assistant",
        text="History ăn uống",
    )
    store.append_conversation_turn(
        now=now - timedelta(minutes=3),
        conversation_id="-100123",
        thread_key=chores_key,
        from_uid="user-1",
        role="assistant",
        text="History việc nhà",
    )
    model_client = FakeModelClient(
        AgentOutput(
            reply="vâng anh chị, Gia gợi ý món nhẹ nha",
            memory=AgentMemoryUpdates(),
            reminder=None,
        )
    )
    service = FamilyAssistantService(
        settings=settings,
        store=store,
        model_client=model_client,
        sender=FakeSender(),
    )
    app = create_app(settings)

    with TestClient(app) as client:
        app.state.telegram_assistant_service = service
        app.state.telegram_poller.assistant_service = service
        response = client.post(
            "/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret"},
            json={
                "update_id": 113,
                "message": {
                    "message_id": 25,
                    "message_thread_id": 77,
                    "text": "tối nay ăn gì",
                    "from": {"id": 999},
                    "chat": {"id": -100123, "type": "supergroup"},
                },
            },
        )

    assert response.status_code == 200
    call = model_client.calls[0]
    assert call["thread_key"] == food_key
    assert "chuyên gia dinh dưỡng" in call["thread_prompt"]
    assert "món nhẹ" in call["thread_rules"]
    assert [turn["text"] for turn in call["conversation_turns"]] == ["History ăn uống"]


def test_telegram_webhook_saves_agent_task(tmp_path) -> None:
    settings = _settings(tmp_path, secret="")
    store = FileStore(settings.data_dir)
    future = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")) + timedelta(hours=1)
    fake_sender = FakeSender()
    service = FamilyAssistantService(
        settings=settings,
        store=store,
        model_client=FakeModelClient(
            AgentOutput(
                reply="ok 22h mình gửi 3 việc quan trọng nha",
                memory=AgentMemoryUpdates(),
                reminder=None,
                agent_task={
                    "title": "3 việc ngày mai",
                    "prompt": "Cho mình 3 việc quan trọng nhất ngày mai",
                    "time": future.isoformat(),
                },
            )
        ),
        sender=fake_sender,
    )
    app = create_app(settings)

    with TestClient(app) as client:
        app.state.telegram_assistant_service = service
        app.state.telegram_poller.assistant_service = service
        response = client.post(
            "/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret"},
            json={
                "update_id": 101,
                "message": {
                    "message_id": 13,
                    "text": "10h tối cho mình 3 việc quan trọng nhất ngày mai",
                    "from": {"id": 999},
                    "chat": {"id": 12345, "type": "private"},
                },
            },
        )

    assert response.status_code == 200
    records = store.list_reminders()
    assert len(records) == 1
    assert records[0].kind == "agent_task"
    assert records[0].prompt == "Cho mình 3 việc quan trọng nhất ngày mai"
    assert records[0].thread_key == "telegram:12345:private"


def test_run_agent_task_uses_saved_thread_context(tmp_path) -> None:
    settings = _settings(tmp_path, secret="")
    store = FileStore(settings.data_dir)
    thread_key = "telegram:-100123:topic:77"
    store.set_thread_prompt(thread_key, "Thread ăn uống chuyên dinh dưỡng.")
    store.append_thread_rules_updates(thread_key, ["Ưu tiên tủ lạnh và HSD."])
    record = store.add_agent_task(
        title="Gợi ý dinner",
        prompt="Gợi ý 3 món tối",
        run_at=datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")) + timedelta(hours=1),
        now=datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")),
        conversation_id="-100123",
        conversation_type="group",
        thread_id="77",
        thread_key=thread_key,
    )
    model_client = FakeModelClient(
        AgentOutput(
            reply="vâng anh chị, tối nay ăn món nhẹ nha",
            memory=AgentMemoryUpdates(),
            reminder=None,
        )
    )
    sender = FakeSender()
    service = FamilyAssistantService(
        settings=settings,
        store=store,
        model_client=model_client,
        sender=sender,
    )

    asyncio.run(service.run_agent_task(record))

    call = model_client.calls[0]
    assert call["thread_key"] == thread_key
    assert "chuyên dinh dưỡng" in call["thread_prompt"]
    assert "HSD" in call["thread_rules"]
    assert sender.sent[0]["thread_id"] == "77"


def test_telegram_webhook_saves_repeating_reminder(tmp_path) -> None:
    settings = _settings(tmp_path, secret="")
    store = FileStore(settings.data_dir)
    future = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")) + timedelta(hours=1)
    fake_sender = FakeSender()
    service = FamilyAssistantService(
        settings=settings,
        store=store,
        model_client=FakeModelClient(
            AgentOutput(
                reply="ok tới giờ Gia nhắc, rồi cứ 30p nhắc lại tới khi anh báo xong nha",
                memory=AgentMemoryUpdates(),
                reminder=None,
                repeating_reminder={
                    "text": "cất cơm vào tủ lạnh",
                    "time": future.isoformat(),
                    "repeat_interval_minutes": 30,
                },
            )
        ),
        sender=fake_sender,
    )
    app = create_app(settings)

    with TestClient(app) as client:
        app.state.telegram_assistant_service = service
        app.state.telegram_poller.assistant_service = service
        response = client.post(
            "/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret"},
            json={
                "update_id": 112,
                "message": {
                    "message_id": 24,
                    "message_thread_id": 77,
                    "text": "10h nhắc anh cất cơm, cứ 30p nhắc tới khi xong",
                    "from": {"id": 999},
                    "chat": {"id": -100123, "type": "supergroup"},
                },
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["processed"] is True
    records = store.list_reminders()
    assert records[0].kind == "repeating_reminder"
    assert records[0].text == "cất cơm vào tủ lạnh"
    assert records[0].repeat_interval_minutes == 30
    assert records[0].next_run_at == future.isoformat()
    assert records[0].thread_id == "77"
    assert records[0].thread_key == "telegram:-100123:topic:77"


def test_telegram_webhook_saves_recurring_agent_task(tmp_path) -> None:
    settings = _settings(tmp_path, secret="")
    store = FileStore(settings.data_dir)
    fake_sender = FakeSender()
    service = FamilyAssistantService(
        settings=settings,
        store=store,
        model_client=FakeModelClient(
            AgentOutput(
                reply="ok mỗi ngày 9h mình gợi ý ăn trưa nha",
                memory=AgentMemoryUpdates(),
                reminder=None,
                agent_task=None,
                recurring_agent_task={
                    "title": "Gợi ý ăn trưa",
                    "prompt": "Gợi ý đồ ăn trưa đơn giản cho Ngọc",
                    "frequency": "daily",
                    "time": "09:00",
                },
            )
        ),
        sender=fake_sender,
    )
    app = create_app(settings)

    with TestClient(app) as client:
        app.state.telegram_assistant_service = service
        app.state.telegram_poller.assistant_service = service
        response = client.post(
            "/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret"},
            json={
                "update_id": 102,
                "message": {
                    "message_id": 14,
                    "text": "mỗi ngày 9h sáng gợi ý đồ ăn trưa",
                    "from": {"id": 999},
                    "chat": {"id": 12345, "type": "private"},
                },
            },
        )

    assert response.status_code == 200
    records = store.list_recurring_tasks()
    assert len(records) == 1
    assert records[0].time == "09:00"
    assert records[0].prompt == "Gợi ý đồ ăn trưa đơn giản cho Ngọc"
    assert records[0].thread_key == "telegram:12345:private"


def test_telegram_webhook_saves_fridge_and_daily_meal(tmp_path) -> None:
    settings = _settings(tmp_path, secret="")
    store = FileStore(settings.data_dir)
    fake_sender = FakeSender()
    service = FamilyAssistantService(
        settings=settings,
        store=store,
        model_client=FakeModelClient(
            AgentOutput(
                reply="mình lưu tủ lạnh và gợi ý bữa trưa rồi nha",
                memory=AgentMemoryUpdates(),
                reminder=None,
                agent_task=None,
                recurring_agent_task=None,
                fridge_updates=[
                    {
                        "name": "trứng",
                        "quantity_note": "5 quả",
                        "status": "available",
                        "note": None,
                        "category": "egg",
                        "compartment": "cool",
                        "added_at": None,
                        "expires_at": None,
                        "expiry_source": "unknown",
                    }
                ],
                daily_meal_update={
                    "date": "2026-04-23",
                    "meal_slot": "lunch",
                    "suggestions": ["cháo thịt bằm", "canh rau trứng"],
                    "selected": None,
                    "notes": "nhẹ bụng cho Ngọc",
                },
            )
        ),
        sender=fake_sender,
    )
    app = create_app(settings)

    with TestClient(app) as client:
        app.state.telegram_assistant_service = service
        app.state.telegram_poller.assistant_service = service
        response = client.post(
            "/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret"},
            json={
                "update_id": 103,
                "message": {
                    "message_id": 15,
                    "text": "tủ lạnh còn 5 quả trứng, gợi ý ăn trưa",
                    "from": {"id": 999},
                    "chat": {"id": 12345, "type": "private"},
                },
            },
        )

    assert response.status_code == 200
    assert store.list_fridge_items()[0].name == "trứng"
    assert store.list_daily_meals()[0].meals["lunch"].suggestions == ["cháo thịt bằm", "canh rau trứng"]


def test_telegram_webhook_saves_actual_lunch_and_dinner_suggestions(tmp_path) -> None:
    settings = _settings(tmp_path, secret="")
    store = FileStore(settings.data_dir)
    service = FamilyAssistantService(
        settings=settings,
        store=store,
        model_client=FakeModelClient(
            AgentOutput(
                reply="vâng anh chị, Gia lưu trưa và gợi ý dinner rồi nha",
                memory=AgentMemoryUpdates(),
                reminder=None,
                agent_task=None,
                recurring_agent_task=None,
                daily_meal_updates=[
                    {
                        "date": "2026-04-25",
                        "meal_slot": "lunch",
                        "suggestions": [],
                        "actual_items": ["canh cải cúc", "cải ngồng xào tỏi", "thịt heo luộc", "mắm đủ đủ tôm"],
                        "selected": None,
                        "notes": "tôm còn khoảng 150g, đã nấu bớt cho bé",
                    },
                    {
                        "date": "2026-04-25",
                        "meal_slot": "dinner",
                        "suggestions": ["thịt heo xào sả ớt", "cá chiên giòn", "đậu lăng hầm cà chua"],
                        "actual_items": [],
                        "selected": None,
                        "notes": "gợi ý dinner khác",
                    },
                ],
            )
        ),
        sender=FakeSender(),
    )
    app = create_app(settings)

    with TestClient(app) as client:
        app.state.telegram_assistant_service = service
        app.state.telegram_poller.assistant_service = service
        response = client.post(
            "/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret"},
            json={
                "update_id": 109,
                "message": {
                    "message_id": 21,
                    "text": "Em lưu thực đơn trưa nay rồi gợi ý dinner khác nha",
                    "from": {"id": 999},
                    "chat": {"id": 12345, "type": "private"},
                },
            },
        )

    assert response.status_code == 200
    meals = store.list_daily_meals()[0].meals
    assert meals["lunch"].actual_items == ["canh cải cúc", "cải ngồng xào tỏi", "thịt heo luộc", "mắm đủ đủ tôm"]
    assert meals["dinner"].suggestions == ["thịt heo xào sả ớt", "cá chiên giòn", "đậu lăng hầm cà chua"]


def test_telegram_webhook_saves_food_place_and_daily_meal(tmp_path) -> None:
    settings = _settings(tmp_path, secret="")
    store = FileStore(settings.data_dir)
    service = FamilyAssistantService(
        settings=settings,
        store=store,
        model_client=FakeModelClient(
            AgentOutput(
                reply="vâng anh chị, Gia lưu bữa trưa và quán A rồi nha",
                memory=AgentMemoryUpdates(),
                reminder=None,
                agent_task=None,
                recurring_agent_task=None,
                food_place_updates=[
                    {
                        "name": "Quán A",
                        "place_type": "delivery",
                        "cuisine": "Vietnamese",
                        "meal_slots": ["lunch"],
                        "favorite_items": ["cơm gà"],
                        "avoid_items": [],
                        "health_notes": "Ngọc ăn ổn",
                        "delivery_apps": ["Grab"],
                        "address_note": None,
                        "distance_note": None,
                        "price_note": None,
                        "status": "active",
                        "event": "ordered",
                        "notes": "trưa nay đặt về",
                    }
                ],
                daily_meal_updates=[
                    {
                        "date": "2026-04-25",
                        "meal_slot": "lunch",
                        "suggestions": [],
                        "actual_items": ["cơm gà Quán A"],
                        "selected": None,
                        "notes": "đặt về, Ngọc ăn ổn",
                    }
                ],
            )
        ),
        sender=FakeSender(),
    )
    app = create_app(settings)

    with TestClient(app) as client:
        app.state.telegram_assistant_service = service
        app.state.telegram_poller.assistant_service = service
        response = client.post(
            "/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret"},
            json={
                "update_id": 117,
                "message": {
                    "message_id": 29,
                    "text": "trưa nay đặt cơm gà quán A, Ngọc ăn ổn",
                    "from": {"id": 999},
                    "chat": {"id": 12345, "type": "private"},
                },
            },
        )

    assert response.status_code == 200
    place = store.list_food_places()[0]
    assert place.name == "Quán A"
    assert place.order_count == 1
    assert place.favorite_items == ["cơm gà"]
    assert place.health_notes == "Ngọc ăn ổn"
    assert store.list_daily_meals()[0].meals["lunch"].actual_items == ["cơm gà Quán A"]


def test_food_places_are_sent_to_model_context(tmp_path) -> None:
    settings = _settings(tmp_path, secret="")
    store = FileStore(settings.data_dir)
    now = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh"))
    store.apply_food_place_updates(
        [
            FoodPlaceUpdate(
                name="Quán A",
                place_type="delivery",
                cuisine="Vietnamese",
                meal_slots=["lunch"],
                favorite_items=["cơm gà"],
                event="ordered",
            )
        ],
        now=now,
    )
    model_client = FakeModelClient(
        AgentOutput(
            reply="Gia nhớ quán A để cân nhắc bữa trưa nha",
            memory=AgentMemoryUpdates(),
            reminder=None,
        )
    )
    service = FamilyAssistantService(
        settings=settings,
        store=store,
        model_client=model_client,
        sender=FakeSender(),
    )
    app = create_app(settings)

    with TestClient(app) as client:
        app.state.telegram_assistant_service = service
        app.state.telegram_poller.assistant_service = service
        response = client.post(
            "/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret"},
            json={
                "update_id": 118,
                "message": {
                    "message_id": 30,
                    "text": "trưa nay ăn ngoài gì được ta",
                    "from": {"id": 999},
                    "chat": {"id": 12345, "type": "private"},
                },
            },
        )

    assert response.status_code == 200
    assert model_client.calls[0]["food_places"][0]["name"] == "Quán A"


def test_meat_without_compartment_is_not_saved_from_model_output(tmp_path) -> None:
    settings = _settings(tmp_path, secret="")
    store = FileStore(settings.data_dir)
    service = FamilyAssistantService(
        settings=settings,
        store=store,
        model_client=FakeModelClient(
            AgentOutput(
                reply="Anh chị để ngăn đông hay ngăn mát để Gia lưu HSD cho đúng nha?",
                memory=AgentMemoryUpdates(),
                reminder=None,
                agent_task=None,
                recurring_agent_task=None,
                fridge_updates=[
                    {
                        "name": "thịt bò",
                        "quantity_note": "500g",
                        "status": "available",
                        "note": None,
                        "category": "meat",
                        "compartment": None,
                        "added_at": None,
                        "expires_at": None,
                        "expiry_source": "unknown",
                    }
                ],
            )
        ),
        sender=FakeSender(),
    )
    app = create_app(settings)

    with TestClient(app) as client:
        app.state.telegram_assistant_service = service
        app.state.telegram_poller.assistant_service = service
        response = client.post(
            "/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret"},
            json={
                "update_id": 106,
                "message": {
                    "message_id": 18,
                    "text": "thịt bò 500g",
                    "from": {"id": 999},
                    "chat": {"id": 12345, "type": "private"},
                },
            },
    )

    assert response.status_code == 200
    assert response.json()["processed"] is True
    assert store.list_fridge_items() == []


def test_followup_compartment_can_save_pending_meat_item(tmp_path) -> None:
    settings = _settings(tmp_path, secret="")
    store = FileStore(settings.data_dir)
    now = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh"))
    store.append_conversation_turn(
        now=now - timedelta(minutes=2),
        conversation_id="12345",
        from_uid=None,
        role="assistant",
        text="Anh chị để ngăn đông hay ngăn mát để Gia lưu HSD cho thịt bò 500g đúng nha?",
    )
    model_client = FakeModelClient(
        AgentOutput(
            reply="Gia lưu thịt bò vào ngăn đông rồi nha",
            memory=AgentMemoryUpdates(),
            reminder=None,
            fridge_updates=[
                {
                    "name": "thịt bò",
                    "quantity_note": "500g",
                    "status": "available",
                    "note": None,
                    "category": "meat",
                    "compartment": "freezer",
                    "added_at": None,
                    "expires_at": None,
                    "expiry_source": "unknown",
                }
            ],
        )
    )
    service = FamilyAssistantService(
        settings=settings,
        store=store,
        model_client=model_client,
        sender=FakeSender(),
    )
    app = create_app(settings)

    with TestClient(app) as client:
        app.state.telegram_assistant_service = service
        app.state.telegram_poller.assistant_service = service
        response = client.post(
            "/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret"},
            json={
                "update_id": 107,
                "message": {
                    "message_id": 19,
                    "text": "ngăn đông nha",
                    "from": {"id": 999},
                    "chat": {"id": 12345, "type": "private"},
                },
            },
        )

    assert response.status_code == 200
    assert model_client.calls[0]["conversation_turns"][-1]["text"].startswith("Anh chị để ngăn đông")
    item = store.list_fridge_items()[0]
    assert item.name == "thịt bò"
    assert item.compartment == "freezer"
    assert item.expires_at is not None


def test_fridge_warnings_are_sent_to_model_context(tmp_path) -> None:
    settings = _settings(tmp_path, secret="")
    store = FileStore(settings.data_dir)
    now = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh"))
    store.apply_fridge_updates(
        [FridgeItemUpdate(name="tôm", category="seafood", compartment="cool", added_at=(now - timedelta(days=1)).isoformat())],
        now=now,
    )
    model_client = FakeModelClient(
        AgentOutput(
            reply="Tôm nên dùng sớm nha",
            memory=AgentMemoryUpdates(),
            reminder=None,
        )
    )
    service = FamilyAssistantService(
        settings=settings,
        store=store,
        model_client=model_client,
        sender=FakeSender(),
    )
    app = create_app(settings)

    with TestClient(app) as client:
        app.state.telegram_assistant_service = service
        app.state.telegram_poller.assistant_service = service
        response = client.post(
            "/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret"},
            json={
                "update_id": 108,
                "message": {
                    "message_id": 20,
                    "text": "tủ lạnh có gì sắp hư không",
                    "from": {"id": 999},
                    "chat": {"id": 12345, "type": "private"},
                },
            },
        )

    assert response.status_code == 200
    assert model_client.calls[0]["fridge_warnings"][0]["name"] == "tôm"


def test_followup_message_gets_previous_conversation_turns(tmp_path) -> None:
    settings = _settings(tmp_path, secret="")
    store = FileStore(settings.data_dir)
    now = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh"))
    store.append_conversation_turn(
        now=now - timedelta(minutes=2),
        conversation_id="12345",
        from_uid=None,
        role="assistant",
        text="Anh chị muốn Gia nhắc gấp quần áo vào mấy giờ buổi sáng?",
    )
    model_client = FakeModelClient(
        AgentOutput(
            reply="ok Gia đặt 10h sáng và 7h tối nha",
            memory=AgentMemoryUpdates(),
            reminder=None,
        )
    )
    service = FamilyAssistantService(
        settings=settings,
        store=store,
        model_client=model_client,
        sender=FakeSender(),
    )
    app = create_app(settings)

    with TestClient(app) as client:
        app.state.telegram_assistant_service = service
        app.state.telegram_poller.assistant_service = service
        response = client.post(
            "/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret"},
            json={
                "update_id": 104,
                "message": {
                    "message_id": 16,
                    "text": "10h sáng với 7h tối nha",
                    "from": {"id": 999},
                    "chat": {"id": 12345, "type": "private"},
                },
            },
        )

    assert response.status_code == 200
    context_turns = model_client.calls[0]["conversation_turns"]
    assert context_turns[-1]["text"] == "Anh chị muốn Gia nhắc gấp quần áo vào mấy giờ buổi sáng?"
    saved_turns = store.list_conversation_turns("12345", limit=30)
    assert saved_turns[-2].text == "10h sáng với 7h tối nha"
    assert saved_turns[-1].text == "ok Gia đặt 10h sáng và 7h tối nha"


def test_telegram_webhook_saves_rules_update(tmp_path) -> None:
    settings = _settings(tmp_path, secret="")
    store = FileStore(settings.data_dir)
    service = FamilyAssistantService(
        settings=settings,
        store=store,
        model_client=FakeModelClient(
            AgentOutput(
                reply="vâng anh chị, Gia nhớ quy định này rồi nha",
                memory=AgentMemoryUpdates(),
                reminder=None,
                agent_task=None,
                recurring_agent_task=None,
                rules_updates=["Gia mở đầu câu trả lời bằng 'vâng anh chị' khi phù hợp."],
            )
        ),
        sender=FakeSender(),
    )
    app = create_app(settings)

    with TestClient(app) as client:
        app.state.telegram_assistant_service = service
        app.state.telegram_poller.assistant_service = service
        response = client.post(
            "/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret"},
            json={
                "update_id": 105,
                "message": {
                    "message_id": 17,
                    "text": "quy định từ giờ mở đầu bằng vâng anh chị",
                    "from": {"id": 999},
                    "chat": {"id": 12345, "type": "private"},
                },
            },
        )

    assert response.status_code == 200
    assert "vâng anh chị" in store.read_rules()


def test_telegram_webhook_saves_thread_rules_and_prompt_update(tmp_path) -> None:
    settings = _settings(tmp_path, secret="")
    store = FileStore(settings.data_dir)
    service = FamilyAssistantService(
        settings=settings,
        store=store,
        model_client=FakeModelClient(
            AgentOutput(
                reply="vâng anh chị, Gia chỉnh prompt thread này rồi nha",
                memory=AgentMemoryUpdates(),
                reminder=None,
                agent_task=None,
                recurring_agent_task=None,
                rules_updates=[],
                thread_rules_updates=["Thread này ưu tiên checklist việc nhà."],
                thread_prompt_update="Thread việc nhà: Gia là trợ lý sắp xếp công việc, ưu tiên checklist ngắn.",
            )
        ),
        sender=FakeSender(),
    )
    app = create_app(settings)

    with TestClient(app) as client:
        app.state.telegram_assistant_service = service
        app.state.telegram_poller.assistant_service = service
        response = client.post(
            "/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret"},
            json={
                "update_id": 114,
                "message": {
                    "message_id": 26,
                    "message_thread_id": 88,
                    "text": "đổi prompt thread này thành chuyên gia việc nhà",
                    "from": {"id": 999},
                    "chat": {"id": -100123, "type": "supergroup"},
                },
            },
        )

    assert response.status_code == 200
    thread_key = "telegram:-100123:topic:88"
    assert "checklist việc nhà" in store.read_thread_rules(thread_key)
    assert "trợ lý sắp xếp công việc" in store.read_thread_prompt(thread_key)
    assert "checklist việc nhà" not in store.read_rules()


def test_telegram_webhook_rejects_invalid_secret(tmp_path) -> None:
    settings = _settings(tmp_path, secret="")
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.post(
            "/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
            json={"update_id": 1},
        )

    assert response.status_code == 403
