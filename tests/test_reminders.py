from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import Settings
from app.reminders import ReminderPoller
from app.schemas import ZaloDeliveryResult
from app.storage import FileStore


class FakeSender:
    def __init__(self, *, ok: bool) -> None:
        self.ok = ok
        self.sent: list[str] = []

    async def send_text(self, *, text: str, conversation_id: str | None = None, conversation_type: str = "user") -> ZaloDeliveryResult:
        self.sent.append(text)
        return ZaloDeliveryResult(ok=self.ok, error=None if self.ok else "send failed")


class FakeAgentTaskRunner:
    def __init__(self) -> None:
        self.ran: list[str] = []
        self.rendered: list[str] = []

    async def run_agent_task(self, record) -> ZaloDeliveryResult:
        self.ran.append(record.prompt or record.text)
        return ZaloDeliveryResult(ok=True)

    async def render_static_reminder(self, record) -> str:
        self.rendered.append(record.text)
        return f"Tới giờ rồi nè: {record.text}"


def _settings(tmp_path, *, max_attempts: int = 2) -> Settings:
    prompt_path = tmp_path / "AGENT.md"
    prompt_path.write_text("prompt", encoding="utf-8")
    return Settings(
        data_dir=tmp_path / "data",
        agent_prompt_path=prompt_path,
        openai_api_key="test-key",
        reminder_max_attempts=max_attempts,
        reminder_poll_interval_seconds=3600,
    )


def test_process_due_once_marks_sent(tmp_path) -> None:
    settings = _settings(tmp_path)
    store = FileStore(settings.data_dir)
    now = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh"))
    store.add_reminder(
        text="mua sữa",
        reminder_time=now - timedelta(minutes=1),
        now=now - timedelta(hours=1),
        conversation_id="conv-1",
        conversation_type="user",
    )
    sender = FakeSender(ok=True)
    poller = ReminderPoller(settings=settings, store=store, sender=sender)

    handled = asyncio.run(poller.process_due_once())

    assert handled[0].status == "sent"
    assert sender.sent == ["Nhắc nè: mua sữa"]


def test_process_due_once_renders_static_reminder_with_agent(tmp_path) -> None:
    settings = _settings(tmp_path)
    store = FileStore(settings.data_dir)
    now = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh"))
    store.add_reminder(
        text="uống vitamin",
        reminder_time=now - timedelta(minutes=1),
        now=now - timedelta(hours=1),
        conversation_id="conv-1",
        conversation_type="user",
    )
    sender = FakeSender(ok=True)
    runner = FakeAgentTaskRunner()
    poller = ReminderPoller(settings=settings, store=store, sender=sender, agent_task_runner=runner)

    handled = asyncio.run(poller.process_due_once())

    assert handled[0].status == "sent"
    assert runner.rendered == ["uống vitamin"]
    assert sender.sent == ["Tới giờ rồi nè: uống vitamin"]


def test_process_due_once_marks_failed_after_max_attempts(tmp_path) -> None:
    settings = _settings(tmp_path, max_attempts=1)
    store = FileStore(settings.data_dir)
    now = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh"))
    store.add_reminder(
        text="uống vitamin",
        reminder_time=now - timedelta(minutes=1),
        now=now - timedelta(hours=1),
        conversation_id="conv-1",
        conversation_type="user",
    )
    poller = ReminderPoller(settings=settings, store=store, sender=FakeSender(ok=False))

    handled = asyncio.run(poller.process_due_once())

    assert handled[0].status == "failed"
    assert handled[0].attempts == 1


def test_process_due_once_skips_canceled_pending_reminder(tmp_path) -> None:
    settings = _settings(tmp_path)
    store = FileStore(settings.data_dir)
    now = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh"))
    reminder = store.add_reminder(
        text="mua sữa",
        reminder_time=now - timedelta(minutes=1),
        now=now - timedelta(hours=1),
        conversation_id="conv-1",
        conversation_type="user",
    )
    store.update_reminder_completion(
        reminder.id,
        completion_status="canceled",
        now=now - timedelta(minutes=5),
        completed_by="user-1",
        note="không cần nữa",
    )
    sender = FakeSender(ok=True)
    poller = ReminderPoller(settings=settings, store=store, sender=sender)

    handled = asyncio.run(poller.process_due_once())

    assert handled == []
    assert sender.sent == []


def test_process_due_once_runs_agent_task(tmp_path) -> None:
    settings = _settings(tmp_path)
    store = FileStore(settings.data_dir)
    now = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh"))
    store.add_agent_task(
        title="3 việc ngày mai",
        prompt="Cho mình 3 việc quan trọng nhất ngày mai",
        run_at=now - timedelta(minutes=1),
        now=now - timedelta(hours=1),
        conversation_id="conv-1",
        conversation_type="user",
    )
    runner = FakeAgentTaskRunner()
    sender = FakeSender(ok=True)
    poller = ReminderPoller(settings=settings, store=store, sender=sender, agent_task_runner=runner)

    handled = asyncio.run(poller.process_due_once())

    assert handled[0].status == "sent"
    assert runner.ran == ["Cho mình 3 việc quan trọng nhất ngày mai"]
    assert sender.sent == []


def test_process_due_once_runs_recurring_agent_task_and_reschedules(tmp_path) -> None:
    settings = _settings(tmp_path)
    store = FileStore(settings.data_dir)
    now = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh"))
    store.add_recurring_agent_task(
        title="Gợi ý ăn trưa",
        prompt="Gợi ý đồ ăn trưa đơn giản",
        local_time=(now - timedelta(minutes=1)).strftime("%H:%M"),
        timezone="Asia/Ho_Chi_Minh",
        now=now - timedelta(hours=1),
        conversation_id="conv-1",
        conversation_type="user",
    )
    runner = FakeAgentTaskRunner()
    sender = FakeSender(ok=True)
    poller = ReminderPoller(settings=settings, store=store, sender=sender, agent_task_runner=runner)

    asyncio.run(poller.process_due_once())

    task = store.list_recurring_tasks()[0]
    assert task.status == "active"
    assert task.last_run_at is not None
    assert task.attempts == 0
    assert task.next_due_at() > now
    assert runner.ran == ["Gợi ý đồ ăn trưa đơn giản"]
