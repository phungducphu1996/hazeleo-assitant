from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Protocol

from app.config import Settings
from app.schemas import RecurringAgentTaskRecord, ReminderRecord
from app.storage import FileStore, compute_next_daily_run


class MessageSender(Protocol):
    async def send_text(
        self,
        *,
        text: str,
        conversation_id: str | None = None,
        conversation_type: str = "user",
    ):
        ...


class AgentTaskRunner(Protocol):
    async def run_agent_task(self, record: ReminderRecord | RecurringAgentTaskRecord):
        ...

    async def render_static_reminder(self, record: ReminderRecord) -> str:
        ...


class ReminderPoller:
    def __init__(
        self,
        *,
        settings: Settings,
        store: FileStore,
        sender: MessageSender,
        agent_task_runner: AgentTaskRunner | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.sender = sender
        self.agent_task_runner = agent_task_runner
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopped.clear()
            self._task = asyncio.create_task(self._run(), name="reminder-poller")

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def process_due_once(self) -> list[ReminderRecord]:
        now = datetime.now(self.settings.timezone)
        due_records = self.store.due_pending_reminders(
            now=now,
            max_attempts=self.settings.reminder_max_attempts,
        )
        handled: list[ReminderRecord] = []
        for record in due_records:
            attempts = record.attempts + 1
            self.store.update_reminder(record.id, attempts=attempts, last_error=None)
            if record.kind == "agent_task":
                if self.agent_task_runner is None:
                    delivery = await self.sender.send_text(
                        text=f"Mình chưa có runner cho task hẹn giờ: {record.text}",
                        conversation_id=record.conversation_id,
                        conversation_type=record.conversation_type,
                    )
                else:
                    delivery = await self.agent_task_runner.run_agent_task(record)
            else:
                reminder_text = f"Nhắc nè: {record.text}"
                if self.agent_task_runner is not None:
                    reminder_text = await self.agent_task_runner.render_static_reminder(record)
                delivery = await self.sender.send_text(
                    text=reminder_text,
                    conversation_id=record.conversation_id,
                    conversation_type=record.conversation_type,
                )
            if delivery.ok:
                updated = self.store.update_reminder(
                    record.id,
                    status="sent",
                    sent_at=now.isoformat(),
                    last_error=None,
                )
            else:
                next_status = "failed" if attempts >= self.settings.reminder_max_attempts else "pending"
                updated = self.store.update_reminder(
                    record.id,
                    status=next_status,
                    last_error=delivery.error or "send_failed",
                )
            if updated is not None:
                handled.append(updated)
        await self._process_due_recurring_tasks(now=now)
        return handled

    async def _process_due_recurring_tasks(self, *, now: datetime) -> None:
        due_tasks = self.store.due_recurring_tasks(now=now)
        for task in due_tasks:
            attempts = task.attempts + 1
            self.store.update_recurring_task(task.id, attempts=attempts, last_error=None)
            if self.agent_task_runner is None:
                delivery = await self.sender.send_text(
                    text=f"Mình chưa có runner cho daily task: {task.title}",
                    conversation_id=task.conversation_id,
                    conversation_type=task.conversation_type,
                )
            else:
                delivery = await self.agent_task_runner.run_agent_task(task)

            if delivery.ok:
                next_run_at = compute_next_daily_run(local_time=task.time, now=now)
                self.store.update_recurring_task(
                    task.id,
                    attempts=0,
                    last_run_at=now.isoformat(),
                    last_completion_status="open",
                    last_completed_at=None,
                    last_completion_note=None,
                    next_run_at=next_run_at.isoformat(),
                    last_error=None,
                )
            else:
                self.store.update_recurring_task(
                    task.id,
                    last_error=delivery.error or "send_failed",
                    status="failed" if attempts >= self.settings.reminder_max_attempts else "active",
                )

    async def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.process_due_once()
            except Exception as exc:  # pragma: no cover - keeps background task alive.
                print("REMINDER_POLLER_ERROR", str(exc))

            try:
                await asyncio.wait_for(
                    self._stopped.wait(),
                    timeout=max(1, self.settings.reminder_poll_interval_seconds),
                )
            except asyncio.TimeoutError:
                continue
