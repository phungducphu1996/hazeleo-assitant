from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Protocol

from app.config import Settings
from app.openai_client import AgentModelError, OpenAIAgentClient
from app.schemas import AgentMemoryUpdates, AgentOutput, AgentTaskDraft, DailyMealUpdate, RecurringAgentTaskDraft, RecurringAgentTaskRecord, ReminderDraft, ReminderRecord, RepeatingReminderDraft, TaskStatusUpdateDraft, ZaloIncomingRequest, ZaloIncomingResponse
from app.skylight_client import SkylightMCPClient
from app.storage import FileStore
from app.thread_context import build_thread_key


class MessageSender(Protocol):
    async def send_text(
        self,
        *,
        text: str,
        conversation_id: str | None = None,
        conversation_type: str = "user",
        thread_id: str | None = None,
    ):
        ...


class FamilyAssistantService:
    def __init__(
        self,
        *,
        settings: Settings,
        store: FileStore,
        model_client: OpenAIAgentClient,
        sender: MessageSender,
        skylight_client: SkylightMCPClient | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.model_client = model_client
        self.sender = sender
        self.skylight_client = skylight_client

    @property
    def conversation_turn_context_limit(self) -> int:
        return max(1, self.settings.conversation_turn_context_limit)

    async def handle_incoming(self, payload: ZaloIncomingRequest, *, send_reply: bool = True) -> ZaloIncomingResponse:
        now = datetime.now(self.settings.timezone)
        thread_key = _payload_thread_key(payload)
        agent_prompt = _read_agent_prompt(self.settings.agent_prompt_path)
        recent = self.store.list_recent()
        conversation_turns = [
            item.model_dump()
            for item in self.store.list_conversation_turns(
                payload.conversation_id,
                thread_key=thread_key,
                limit=self.conversation_turn_context_limit,
            )
        ]
        profile = self.store.read_profile()
        rules_text = self.store.read_rules()
        thread_prompt = self.store.read_thread_prompt(thread_key)
        thread_rules = self.store.read_thread_rules(thread_key)
        fridge = [item.model_dump() for item in self.store.list_fridge_items()]
        fridge_warnings = self.store.fridge_warnings(now=now)
        daily_meals = [item.model_dump() for item in self.store.list_daily_meals()]
        food_places = [item.model_dump() for item in self.store.list_food_places()]
        open_tasks = self._open_task_context(payload.conversation_id, thread_key)

        try:
            output = await self.model_client.run(
                agent_prompt=agent_prompt,
                profile=profile,
                rules_text=rules_text,
                thread_key=thread_key,
                thread_prompt=thread_prompt,
                thread_rules=thread_rules,
                recent=recent,
                conversation_turns=conversation_turns,
                fridge=fridge,
                fridge_warnings=fridge_warnings,
                daily_meals=daily_meals,
                food_places=food_places,
                open_tasks=open_tasks,
                payload=payload,
                now=now,
            )
        except AgentModelError as exc:
            print("AGENT_MODEL_ERROR", str(exc))
            output = AgentOutput(
                reply="Mình chưa gọi được AI lúc này, bạn thử lại xíu nha.",
                memory=AgentMemoryUpdates(),
                reminder=None,
                repeating_reminder=None,
                agent_task=None,
                recurring_agent_task=None,
            )

        skylight_actions = list(output.skylight_actions)
        skylight_results: list[dict] = []
        skylight_error = None
        if skylight_actions:
            if self.skylight_client is None:
                skylight_error = "skylight_client_not_configured"
                skylight_results = [
                    {
                        "tool": action.tool,
                        "arguments": action.arguments,
                        "ok": False,
                        "error": skylight_error,
                    }
                    for action in skylight_actions
                ]
            else:
                skylight_results = await self.skylight_client.execute_actions(skylight_actions)

            try:
                output = await self.model_client.run(
                    agent_prompt=agent_prompt,
                    profile=profile,
                    rules_text=rules_text,
                    thread_key=thread_key,
                    thread_prompt=thread_prompt,
                    thread_rules=thread_rules,
                    recent=recent,
                    conversation_turns=conversation_turns,
                    fridge=fridge,
                    fridge_warnings=fridge_warnings,
                    daily_meals=daily_meals,
                    food_places=food_places,
                    open_tasks=open_tasks,
                    skylight_results=skylight_results,
                    payload=payload,
                    now=now,
                )
            except AgentModelError as exc:
                skylight_error = str(exc)
                print("SKYLIGHT_FINAL_MODEL_ERROR", str(exc))
                output = AgentOutput(
                    reply="Gia đã gọi Skylight rồi nhưng chưa tổng hợp được câu trả lời. Anh chị thử lại giúp Gia nha.",
                    memory=AgentMemoryUpdates(),
                    reminder=None,
                    repeating_reminder=None,
                    agent_task=None,
                    recurring_agent_task=None,
                )

        accepted_profile = self.store.append_profile_updates(output.memory.profile_updates)
        accepted_rules = self.store.append_rules_updates(output.rules_updates)
        accepted_thread_rules = self.store.append_thread_rules_updates(thread_key, output.thread_rules_updates)
        thread_prompt_saved = self.store.set_thread_prompt(thread_key, output.thread_prompt_update)
        self.store.append_conversation_turn(
            now=now,
            conversation_id=payload.conversation_id,
            thread_key=thread_key,
            from_uid=payload.from_uid,
            role="user",
            text=payload.text,
        )
        accepted_recent = self.store.append_recent_updates(
            output.memory.recent_updates,
            now=now,
            conversation_id=payload.conversation_id,
            from_uid=payload.from_uid,
        )
        fridge_updates = self.store.apply_fridge_updates(output.fridge_updates, now=now)
        food_place_updates = self.store.apply_food_place_updates(output.food_place_updates, now=now)
        daily_meal_saved = bool(self._apply_daily_meal_updates(output, now=now))

        saved_reminder = False
        reminder_error = None
        if output.reminder is not None:
            saved_reminder, reminder_error = self._try_save_reminder(output.reminder, payload, now, thread_key)

        saved_repeating_reminder = False
        repeating_reminder_error = None
        if output.repeating_reminder is not None:
            saved_repeating_reminder, repeating_reminder_error = self._try_save_repeating_reminder(
                output.repeating_reminder,
                payload,
                now,
                thread_key,
            )

        saved_agent_task = False
        agent_task_error = None
        if output.agent_task is not None:
            saved_agent_task, agent_task_error = self._try_save_agent_task(output.agent_task, payload, now, thread_key)

        saved_recurring_task = False
        recurring_task_error = None
        if output.recurring_agent_task is not None:
            saved_recurring_task, recurring_task_error = self._try_save_recurring_agent_task(
                output.recurring_agent_task,
                payload,
                now,
                thread_key,
            )

        task_status_updated = False
        task_status_error = None
        if output.task_status_update is not None:
            task_status_updated, task_status_error = self._try_apply_task_status_update(
                output.task_status_update,
                payload,
                now,
                thread_key,
            )

        delivery = None
        if send_reply and payload.conversation_id:
            delivery = await self.sender.send_text(
                text=output.reply,
                conversation_id=payload.conversation_id,
                conversation_type=payload.conversation_type,
                thread_id=payload.thread_id,
            )
        self.store.append_conversation_turn(
            now=now,
            conversation_id=payload.conversation_id,
            thread_key=thread_key,
            from_uid=None,
            role="assistant",
            text=output.reply,
        )

        return ZaloIncomingResponse(
            reply=output.reply,
            thread_key=thread_key,
            memory=AgentMemoryUpdates(
                profile_updates=accepted_profile,
                recent_updates=[item.text for item in accepted_recent],
            ),
            reminder=output.reminder,
            repeating_reminder=output.repeating_reminder,
            agent_task=output.agent_task,
            recurring_agent_task=output.recurring_agent_task,
            delivery=delivery,
            reminder_saved=saved_reminder,
            reminder_error=reminder_error,
            repeating_reminder_saved=saved_repeating_reminder,
            repeating_reminder_error=repeating_reminder_error,
            agent_task_saved=saved_agent_task,
            agent_task_error=agent_task_error,
            recurring_agent_task_saved=saved_recurring_task,
            recurring_agent_task_error=recurring_task_error,
            fridge_updates_saved=len(fridge_updates),
            food_place_updates_saved=len(food_place_updates),
            daily_meal_saved=daily_meal_saved,
            rules_updates_saved=len(accepted_rules),
            thread_rules_updates_saved=len(accepted_thread_rules),
            thread_prompt_saved=thread_prompt_saved,
            task_status_update=output.task_status_update,
            task_status_updated=task_status_updated,
            task_status_error=task_status_error,
            skylight_actions=skylight_actions,
            skylight_results=skylight_results,
            skylight_error=skylight_error,
        )

    async def run_agent_task(self, record: ReminderRecord | RecurringAgentTaskRecord):
        now = datetime.now(self.settings.timezone)
        thread_key = getattr(record, "thread_key", None) or build_thread_key(
            source="telegram",
            conversation_id=record.conversation_id,
            conversation_type=record.conversation_type,
            thread_id=getattr(record, "thread_id", None),
        )
        prompt = (record.prompt or getattr(record, "text", "")).strip()
        title = getattr(record, "title", None) or getattr(record, "text", "task hẹn giờ")
        is_recurring = isinstance(record, RecurringAgentTaskRecord)
        no_schedule_text = (
            "Do not create a reminder. Do not create agent_task. Do not create recurring_agent_task. "
            "Set reminder, agent_task, and recurring_agent_task to null."
        )
        task_prompt = (
            "Scheduled agent task is due now. Do the requested work now and send the final useful result.\n"
            + no_schedule_text
            + ("\nThis is a recurring daily task.\n\n" if is_recurring else "\n\n")
            + f"Task: {prompt}"
        )
        payload = ZaloIncomingRequest(
            text=task_prompt,
            source="telegram",
            from_uid="scheduled-agent-task",
            conversation_id=record.conversation_id,
            conversation_type=record.conversation_type,
            thread_id=getattr(record, "thread_id", None),
        )
        agent_prompt = _read_agent_prompt(self.settings.agent_prompt_path)
        recent = self.store.list_recent()
        profile = self.store.read_profile()

        try:
            output = await self.model_client.run(
                agent_prompt=agent_prompt,
                profile=profile,
                rules_text=self.store.read_rules(),
                thread_key=thread_key,
                thread_prompt=self.store.read_thread_prompt(thread_key),
                thread_rules=self.store.read_thread_rules(thread_key),
                recent=recent,
                conversation_turns=[
                    item.model_dump()
                    for item in self.store.list_conversation_turns(
                        record.conversation_id,
                        thread_key=thread_key,
                        limit=self.conversation_turn_context_limit,
                    )
                ],
                fridge=[item.model_dump() for item in self.store.list_fridge_items()],
                fridge_warnings=self.store.fridge_warnings(now=now),
                daily_meals=[item.model_dump() for item in self.store.list_daily_meals()],
                food_places=[item.model_dump() for item in self.store.list_food_places()],
                open_tasks=self._open_task_context(record.conversation_id, thread_key),
                payload=payload,
                now=now,
            )
        except AgentModelError as exc:
            print("AGENT_TASK_MODEL_ERROR", str(exc))
            return await self.sender.send_text(
                text=f"Mình chưa chạy được task hẹn giờ: {title}",
                conversation_id=record.conversation_id,
                conversation_type=record.conversation_type,
                thread_id=getattr(record, "thread_id", None),
            )

        self.store.append_profile_updates(output.memory.profile_updates)
        self.store.append_rules_updates(output.rules_updates)
        self.store.append_thread_rules_updates(thread_key, output.thread_rules_updates)
        self.store.set_thread_prompt(thread_key, output.thread_prompt_update)
        self.store.append_recent_updates(
            output.memory.recent_updates,
            now=now,
            conversation_id=record.conversation_id,
            from_uid="scheduled-agent-task",
        )
        self.store.apply_fridge_updates(output.fridge_updates, now=now)
        self.store.apply_food_place_updates(output.food_place_updates, now=now)
        self._apply_daily_meal_updates(output, now=now)
        return await self.sender.send_text(
            text=output.reply,
            conversation_id=record.conversation_id,
            conversation_type=record.conversation_type,
            thread_id=getattr(record, "thread_id", None),
        )

    async def render_static_reminder(self, record: ReminderRecord) -> str:
        now = datetime.now(self.settings.timezone)
        thread_key = record.thread_key or build_thread_key(
            source="telegram",
            conversation_id=record.conversation_id,
            conversation_type=record.conversation_type,
            thread_id=record.thread_id,
        )
        payload = ZaloIncomingRequest(
            text=(
                "A simple reminder is due now. Write only the final reminder message to the user.\n"
                "Make it warm, short, natural Vietnamese, like a close family assistant on Telegram.\n"
                "Do not create reminder, agent_task, or recurring_agent_task. Set all schedule fields to null.\n\n"
                f"Reminder: {record.text}"
            ),
            source="telegram",
            from_uid="scheduled-static-reminder",
            conversation_id=record.conversation_id,
            conversation_type=record.conversation_type,
            thread_id=record.thread_id,
        )
        try:
            output = await self.model_client.run(
                agent_prompt=_read_agent_prompt(self.settings.agent_prompt_path),
                profile=self.store.read_profile(),
                rules_text=self.store.read_rules(),
                thread_key=thread_key,
                thread_prompt=self.store.read_thread_prompt(thread_key),
                thread_rules=self.store.read_thread_rules(thread_key),
                recent=self.store.list_recent(),
                conversation_turns=[
                    item.model_dump()
                    for item in self.store.list_conversation_turns(
                        record.conversation_id,
                        thread_key=thread_key,
                        limit=self.conversation_turn_context_limit,
                    )
                ],
                fridge=[item.model_dump() for item in self.store.list_fridge_items()],
                fridge_warnings=self.store.fridge_warnings(now=now),
                daily_meals=[item.model_dump() for item in self.store.list_daily_meals()],
                food_places=[item.model_dump() for item in self.store.list_food_places()],
                open_tasks=self._open_task_context(record.conversation_id, thread_key),
                payload=payload,
                now=now,
            )
            return output.reply.strip() or f"Nhắc nè: {record.text}"
        except AgentModelError as exc:
            print("REMINDER_RENDER_MODEL_ERROR", str(exc))
            return f"Nhắc nè: {record.text}"

    def _try_save_reminder(
        self,
        reminder: ReminderDraft,
        payload: ZaloIncomingRequest,
        now: datetime,
        thread_key: str | None,
    ) -> tuple[bool, str | None]:
        try:
            reminder_time = _parse_agent_datetime(reminder.time, now)
        except ValueError:
            return False, "invalid_reminder_time"

        reminder_time = _coerce_past_schedule_datetime(reminder_time, now)

        self.store.add_reminder(
            text=reminder.text,
            reminder_time=reminder_time,
            now=now,
            conversation_id=payload.conversation_id,
            conversation_type=payload.conversation_type,
            thread_id=payload.thread_id,
            thread_key=thread_key,
        )
        return True, None

    def _try_save_recurring_agent_task(
        self,
        recurring_task: RecurringAgentTaskDraft,
        payload: ZaloIncomingRequest,
        now: datetime,
        thread_key: str | None,
    ) -> tuple[bool, str | None]:
        try:
            self.store.add_recurring_agent_task(
                title=recurring_task.title,
                prompt=recurring_task.prompt,
                local_time=recurring_task.time,
                timezone=self.settings.app_timezone,
                now=now,
                conversation_id=payload.conversation_id,
                conversation_type=payload.conversation_type,
                thread_id=payload.thread_id,
                thread_key=thread_key,
            )
        except ValueError:
            return False, "invalid_recurring_agent_task"
        return True, None

    def _try_save_repeating_reminder(
        self,
        repeating_reminder: RepeatingReminderDraft,
        payload: ZaloIncomingRequest,
        now: datetime,
        thread_key: str | None,
    ) -> tuple[bool, str | None]:
        try:
            first_run_at = _parse_agent_datetime(repeating_reminder.time, now)
        except ValueError:
            return False, "invalid_repeating_reminder_time"

        first_run_at = _coerce_past_schedule_datetime(first_run_at, now)

        self.store.add_repeating_reminder(
            text=repeating_reminder.text,
            first_run_at=first_run_at,
            repeat_interval_minutes=repeating_reminder.repeat_interval_minutes,
            now=now,
            conversation_id=payload.conversation_id,
            conversation_type=payload.conversation_type,
            thread_id=payload.thread_id,
            thread_key=thread_key,
        )
        return True, None

    def _try_save_agent_task(
        self,
        agent_task: AgentTaskDraft,
        payload: ZaloIncomingRequest,
        now: datetime,
        thread_key: str | None,
    ) -> tuple[bool, str | None]:
        try:
            run_at = _parse_agent_datetime(agent_task.time, now)
        except ValueError:
            return False, "invalid_agent_task_time"

        run_at = _coerce_past_schedule_datetime(run_at, now)

        self.store.add_agent_task(
            title=agent_task.title,
            prompt=agent_task.prompt,
            run_at=run_at,
            now=now,
            conversation_id=payload.conversation_id,
            conversation_type=payload.conversation_type,
            thread_id=payload.thread_id,
            thread_key=thread_key,
        )
        return True, None

    def _apply_daily_meal_updates(self, output: AgentOutput, *, now: datetime) -> list[DailyMealUpdate]:
        updates = _collect_daily_meal_updates(output)
        for update in updates:
            self.store.apply_daily_meal_update(update, now=now)
        return updates

    def _try_apply_task_status_update(
        self,
        status_update: TaskStatusUpdateDraft,
        payload: ZaloIncomingRequest,
        now: datetime,
        thread_key: str | None,
    ) -> tuple[bool, str | None]:
        updated = self.store.complete_matching_reminder(
            conversation_id=payload.conversation_id,
            target_text=status_update.target_text,
            completion_status=status_update.completion_status,
            now=now,
            completed_by=payload.from_uid,
            note=status_update.note,
            thread_key=thread_key,
        )
        if updated is not None:
            return True, None

        recurring_updated = self.store.complete_matching_recurring_task(
            conversation_id=payload.conversation_id,
            target_text=status_update.target_text,
            completion_status=status_update.completion_status,
            now=now,
            note=status_update.note,
            thread_key=thread_key,
        )
        if recurring_updated is not None:
            return True, None
        return False, "task_not_found"

    def _open_task_context(self, conversation_id: str | None, thread_key: str | None) -> list[dict[str, object]]:
        if not conversation_id:
            return []
        reminders = [
            {
                "id": record.id,
                "kind": record.kind,
                "text": record.text,
                "prompt": record.prompt,
                "time": record.time,
                "thread_key": record.thread_key,
                "same_thread": bool(thread_key and record.thread_key == thread_key),
                "status": record.status,
                "completion_status": record.completion_status,
                "sent_at": record.sent_at,
            }
            for record in self.store.list_reminders()
            if record.conversation_id == conversation_id
            and record.completion_status == "open"
            and record.status in {"pending", "sent"}
        ]
        recurring_tasks = [
            {
                "id": task.id,
                "kind": "recurring_agent_task",
                "text": task.title,
                "prompt": task.prompt,
                "time": task.time,
                "thread_key": task.thread_key,
                "same_thread": bool(thread_key and task.thread_key == thread_key),
                "status": task.status,
                "completion_status": task.last_completion_status,
                "last_run_at": task.last_run_at,
            }
            for task in self.store.list_recurring_tasks()
            if task.conversation_id == conversation_id
            and task.status == "active"
            and task.last_completion_status == "open"
        ]
        return (reminders + recurring_tasks)[-10:]


def _read_agent_prompt(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return "You are a short, warm Vietnamese family assistant. Return valid JSON only."


def _payload_thread_key(payload: ZaloIncomingRequest) -> str | None:
    return build_thread_key(
        source=payload.source,
        conversation_id=payload.conversation_id,
        conversation_type=payload.conversation_type,
        thread_id=payload.thread_id,
    )


def _parse_agent_datetime(value: str, now: datetime) -> datetime:
    clean_value = value.strip()
    if clean_value.endswith("Z"):
        clean_value = clean_value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(clean_value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=now.tzinfo)
    return parsed.astimezone(now.tzinfo)


def _coerce_past_schedule_datetime(value: datetime, now: datetime) -> datetime:
    if value > now:
        return value
    return now


def _collect_daily_meal_updates(output: AgentOutput) -> list[DailyMealUpdate]:
    updates: list[DailyMealUpdate] = []
    if output.daily_meal_update is not None:
        updates.append(output.daily_meal_update)
    updates.extend(output.daily_meal_updates)

    seen: set[tuple] = set()
    unique: list[DailyMealUpdate] = []
    for update in updates:
        key = (
            update.date,
            update.meal_slot,
            tuple(update.suggestions),
            tuple(update.actual_items),
            update.selected,
            update.notes,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(update)
    return unique
