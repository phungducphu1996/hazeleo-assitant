from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
import fcntl
import json
import os
from pathlib import Path
import re
import unicodedata
import uuid

from app.schemas import ConversationTurn, DailyMealRecord, DailyMealSlotRecord, DailyMealUpdate, FoodPlaceRecord, FoodPlaceUpdate, FridgeItemRecord, FridgeItemUpdate, RecentMemoryEntry, RecurringAgentTaskRecord, ReminderRecord, TaskCompletionStatus
from app.thread_context import thread_dir_name


class FileStore:
    def __init__(
        self,
        data_dir: Path,
        *,
        daily_meal_retention_days: int = 14,
        conversation_turn_retention_days: int = 5,
    ) -> None:
        self.data_dir = data_dir
        self.profile_path = data_dir / "profile.md"
        self.rules_path = data_dir / "rules.md"
        self.recent_path = data_dir / "recent.json"
        self.conversation_turns_path = data_dir / "conversation_turns.json"
        self.threads_dir = data_dir / "threads"
        self.reminders_path = data_dir / "reminders.json"
        self.recurring_tasks_path = data_dir / "recurring_tasks.json"
        self.fridge_path = data_dir / "fridge.json"
        self.daily_meals_path = data_dir / "daily_meals.json"
        self.food_places_path = data_dir / "food_places.json"
        self.lock_path = data_dir / ".store.lock"
        self.daily_meal_retention_days = daily_meal_retention_days
        self.conversation_turn_retention_days = conversation_turn_retention_days

    def ensure_files(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.threads_dir.mkdir(parents=True, exist_ok=True)
        if not self.profile_path.exists():
            self._atomic_write_text_unlocked(self.profile_path, "# Profile Memory\n")
        if not self.rules_path.exists():
            self._atomic_write_text_unlocked(self.rules_path, "# Behavior Rules\n")
        if not self.recent_path.exists():
            self._atomic_write_text_unlocked(self.recent_path, "[]")
        if not self.conversation_turns_path.exists():
            self._atomic_write_text_unlocked(self.conversation_turns_path, "[]")
        if not self.reminders_path.exists():
            self._atomic_write_text_unlocked(self.reminders_path, "[]")
        if not self.recurring_tasks_path.exists():
            self._atomic_write_text_unlocked(self.recurring_tasks_path, "[]")
        if not self.fridge_path.exists():
            self._atomic_write_text_unlocked(self.fridge_path, "[]")
        if not self.daily_meals_path.exists():
            self._atomic_write_text_unlocked(self.daily_meals_path, "[]")
        if not self.food_places_path.exists():
            self._atomic_write_text_unlocked(self.food_places_path, "[]")

    def read_profile(self) -> str:
        self.ensure_files()
        with self._locked():
            return self.profile_path.read_text(encoding="utf-8")

    def read_rules(self) -> str:
        self.ensure_files()
        with self._locked():
            return self.rules_path.read_text(encoding="utf-8")

    def read_thread_prompt(self, thread_key: str | None) -> str:
        if not thread_key:
            return ""
        self.ensure_files()
        with self._locked():
            paths = self._ensure_thread_files_unlocked(thread_key)
            return paths["prompt"].read_text(encoding="utf-8")

    def read_thread_rules(self, thread_key: str | None) -> str:
        if not thread_key:
            return ""
        self.ensure_files()
        with self._locked():
            paths = self._ensure_thread_files_unlocked(thread_key)
            return paths["rules"].read_text(encoding="utf-8")

    def list_recent(self) -> list[RecentMemoryEntry]:
        self.ensure_files()
        with self._locked():
            return self._read_recent_unlocked()

    def list_conversation_turns(
        self,
        conversation_id: str | None,
        *,
        thread_key: str | None = None,
        limit: int = 30,
    ) -> list[ConversationTurn]:
        self.ensure_files()
        if not conversation_id and not thread_key:
            return []
        with self._locked():
            if thread_key:
                turns = self._prune_and_persist_thread_conversation_turns_unlocked(thread_key)
                if not turns and conversation_id:
                    global_turns = self._prune_and_persist_conversation_turns_unlocked()
                    turns = [
                        turn
                        for turn in global_turns
                        if turn.conversation_id == conversation_id
                        and (turn.thread_key is None or turn.thread_key == thread_key)
                    ]
            else:
                turns = self._prune_and_persist_conversation_turns_unlocked()
                turns = [
                    turn
                    for turn in turns
                    if turn.conversation_id == conversation_id
                ]
            return turns[-limit:]

    def list_threads(self) -> list[dict[str, object]]:
        self.ensure_files()
        with self._locked():
            return self._list_threads_unlocked()

    def list_reminders(self) -> list[ReminderRecord]:
        self.ensure_files()
        with self._locked():
            return self._read_reminders_unlocked()

    def list_recurring_tasks(self) -> list[RecurringAgentTaskRecord]:
        self.ensure_files()
        with self._locked():
            return self._read_recurring_tasks_unlocked()

    def list_fridge_items(self) -> list[FridgeItemRecord]:
        self.ensure_files()
        with self._locked():
            return self._read_fridge_unlocked()

    def fridge_warnings(self, *, now: datetime) -> list[dict[str, object]]:
        self.ensure_files()
        with self._locked():
            return _build_fridge_warnings(self._read_fridge_unlocked(), now=now)

    def list_daily_meals(self) -> list[DailyMealRecord]:
        self.ensure_files()
        with self._locked():
            return self._read_daily_meals_unlocked()

    def list_food_places(self) -> list[FoodPlaceRecord]:
        self.ensure_files()
        with self._locked():
            return self._read_food_places_unlocked()

    def snapshot(self) -> dict[str, object]:
        self.ensure_files()
        with self._locked():
            conversation_turns = self._prune_and_persist_conversation_turns_unlocked()
            return {
                "profile": self.profile_path.read_text(encoding="utf-8"),
                "rules": self.rules_path.read_text(encoding="utf-8"),
                "recent": [entry.model_dump() for entry in self._read_recent_unlocked()],
                "conversation_turns": [entry.model_dump() for entry in conversation_turns[-30:]],
                "fridge": [item.model_dump() for item in self._read_fridge_unlocked()],
                "fridge_warnings": _build_fridge_warnings(self._read_fridge_unlocked(), now=datetime.now().astimezone()),
                "daily_meals": [item.model_dump() for item in self._read_daily_meals_unlocked()],
                "food_places": [item.model_dump() for item in self._read_food_places_unlocked()],
                "threads": self._list_threads_unlocked(),
            }

    def thread_snapshot(self, thread_key: str) -> dict[str, object]:
        self.ensure_files()
        with self._locked():
            paths = self._ensure_thread_files_unlocked(thread_key)
            turns = self._prune_and_persist_thread_conversation_turns_unlocked(thread_key)
            return {
                "thread_key": thread_key,
                "dir_name": paths["dir"].name,
                "prompt": paths["prompt"].read_text(encoding="utf-8"),
                "rules": paths["rules"].read_text(encoding="utf-8"),
                "settings": self._read_json_object_unlocked(paths["settings"]),
                "conversation_turns": [entry.model_dump() for entry in turns[-30:]],
            }

    def append_profile_updates(self, updates: list[str]) -> list[str]:
        return self._append_markdown_updates(self.profile_path, updates)

    def append_rules_updates(self, updates: list[str]) -> list[str]:
        return self._append_markdown_updates(self.rules_path, updates)

    def append_thread_rules_updates(self, thread_key: str | None, updates: list[str]) -> list[str]:
        if not thread_key:
            return []
        self.ensure_files()
        with self._locked():
            paths = self._ensure_thread_files_unlocked(thread_key)
        return self._append_markdown_updates(paths["rules"], updates)

    def set_thread_prompt(self, thread_key: str | None, prompt: str | None) -> bool:
        clean_prompt = _clean_multiline(prompt or "")
        if not thread_key or not clean_prompt:
            return False
        self.ensure_files()
        with self._locked():
            paths = self._ensure_thread_files_unlocked(thread_key)
            self._atomic_write_text_unlocked(paths["prompt"], "# Thread Prompt\n\n" + clean_prompt + "\n")
            return True

    def _append_markdown_updates(self, path: Path, updates: list[str]) -> list[str]:
        cleaned = [_clean_update(item) for item in updates]
        cleaned = [item for item in cleaned if item]
        if not cleaned:
            return []

        self.ensure_files()
        with self._locked():
            file_text = path.read_text(encoding="utf-8")
            existing_keys = {_normalize_for_dedupe(line) for line in file_text.splitlines() if line.strip()}
            accepted: list[str] = []
            for item in cleaned:
                bullet = f"- {item}"
                key = _normalize_for_dedupe(bullet)
                if not key or key in existing_keys:
                    continue
                accepted.append(item)
                existing_keys.add(key)

            if accepted:
                suffix = "" if file_text.endswith("\n") else "\n"
                next_text = file_text + suffix + "\n".join(f"- {item}" for item in accepted) + "\n"
                self._atomic_write_text_unlocked(path, next_text)
            return accepted

    def append_recent_updates(
        self,
        updates: list[str],
        *,
        now: datetime,
        conversation_id: str | None,
        from_uid: str | None,
    ) -> list[RecentMemoryEntry]:
        cleaned = [_clean_update(item) for item in updates]
        cleaned = [item for item in cleaned if item]
        if not cleaned:
            return []

        self.ensure_files()
        with self._locked():
            items = self._read_recent_unlocked()
            added = [
                RecentMemoryEntry(
                    ts=now.isoformat(),
                    conversation_id=conversation_id,
                    from_uid=from_uid,
                    text=item,
                )
                for item in cleaned
            ]
            items.extend(added)
            self._atomic_write_json_unlocked(self.recent_path, [item.model_dump() for item in items])
            return added

    def append_conversation_turn(
        self,
        *,
        now: datetime,
        conversation_id: str | None,
        from_uid: str | None,
        role: str,
        text: str,
        thread_key: str | None = None,
    ) -> ConversationTurn | None:
        clean_text = _clean_update(text)
        if not clean_text or role not in {"user", "assistant"}:
            return None
        self.ensure_files()
        turn = ConversationTurn(
            ts=now.isoformat(),
            conversation_id=conversation_id,
            thread_key=thread_key,
            from_uid=from_uid,
            role=role,
            text=clean_text,
        )
        with self._locked():
            turns = self._read_conversation_turns_unlocked()
            turns.append(turn)
            turns = _prune_conversation_turns(
                turns,
                now=now,
                retention_days=self.conversation_turn_retention_days,
            )
            self._atomic_write_json_unlocked(self.conversation_turns_path, [item.model_dump() for item in turns])
            if thread_key:
                paths = self._ensure_thread_files_unlocked(thread_key)
                thread_turns = self._read_thread_conversation_turns_unlocked(thread_key)
                thread_turns.append(turn)
                thread_turns = _prune_conversation_turns(
                    thread_turns,
                    now=now,
                    retention_days=self.conversation_turn_retention_days,
                )
                self._atomic_write_json_unlocked(paths["conversation_turns"], [item.model_dump() for item in thread_turns])
            return turn

    def apply_fridge_updates(self, updates: list[FridgeItemUpdate], *, now: datetime) -> list[FridgeItemRecord]:
        if not updates:
            return []
        self.ensure_files()
        with self._locked():
            items = self._read_fridge_unlocked()
            by_key = {_normalize_for_dedupe(item.name): item for item in items}
            changed: list[FridgeItemRecord] = []
            for update in updates:
                name = _clean_update(update.name)
                key = _normalize_for_dedupe(name)
                if not key:
                    continue
                existing = by_key.get(key)
                if update.status == "finished":
                    removed = by_key.pop(key, None)
                    if removed is not None:
                        changed.append(removed.model_copy(update={"status": "finished", "updated_at": now.isoformat()}))
                    continue
                category = _resolve_fridge_category(name, update.category, existing)
                compartment = _resolve_fridge_compartment(category, update.compartment, existing)
                if category in {"meat", "seafood"} and compartment is None:
                    continue
                added_at = _resolve_added_at(update.added_at, existing, now)
                expires_at, expiry_source = _resolve_expiry(
                    update=update,
                    existing=existing,
                    category=category,
                    compartment=compartment,
                    added_at=added_at,
                    now=now,
                )
                record = FridgeItemRecord(
                    name=name,
                    quantity_note=_clean_optional(update.quantity_note),
                    status=update.status,
                    note=_clean_optional(update.note),
                    category=category,
                    compartment=compartment,
                    added_at=added_at.isoformat(),
                    expires_at=expires_at,
                    expiry_source=expiry_source,
                    updated_at=now.isoformat(),
                )
                by_key[key] = record
                changed.append(record)
            next_items = sorted(by_key.values(), key=lambda item: item.name.lower())
            self._atomic_write_json_unlocked(self.fridge_path, [item.model_dump() for item in next_items])
            return changed

    def apply_daily_meal_update(self, update: DailyMealUpdate, *, now: datetime) -> DailyMealRecord:
        self.ensure_files()
        with self._locked():
            meals = self._read_daily_meals_unlocked()
            by_date = {item.date: item for item in meals}
            day = by_date.get(update.date) or DailyMealRecord(date=update.date)
            existing_slot = day.meals.get(update.meal_slot)
            suggestions = [_clean_update(item) for item in update.suggestions if _clean_update(item)]
            actual_items = [_clean_update(item) for item in update.actual_items if _clean_update(item)]
            next_slot = DailyMealSlotRecord(
                suggestions=suggestions or (existing_slot.suggestions if existing_slot else []),
                actual_items=actual_items or (existing_slot.actual_items if existing_slot else []),
                selected=_clean_optional(update.selected) or (existing_slot.selected if existing_slot else None),
                notes=_clean_optional(update.notes) or (existing_slot.notes if existing_slot else None),
                updated_at=now.isoformat(),
            )
            day.meals[update.meal_slot] = next_slot
            by_date[update.date] = day
            next_meals = _prune_daily_meals(
                list(by_date.values()),
                now=now,
                retention_days=self.daily_meal_retention_days,
            )
            self._atomic_write_json_unlocked(self.daily_meals_path, [item.model_dump() for item in next_meals])
            return day

    def apply_food_place_updates(self, updates: list[FoodPlaceUpdate], *, now: datetime) -> list[FoodPlaceRecord]:
        if not updates:
            return []
        self.ensure_files()
        with self._locked():
            places = self._read_food_places_unlocked()
            by_key = {_normalize_for_dedupe(place.name): place for place in places}
            changed: list[FoodPlaceRecord] = []
            for update in updates:
                name = _clean_update(update.name)
                key = _normalize_for_dedupe(name)
                if not key:
                    continue
                existing = by_key.get(key)
                created_at = existing.created_at if existing else now.isoformat()
                event = update.event
                record = FoodPlaceRecord(
                    id=existing.id if existing else str(uuid.uuid4()),
                    name=existing.name if existing else name,
                    place_type=_resolve_food_place_type(update.place_type, existing),
                    cuisine=update.cuisine if update.cuisine is not None else (existing.cuisine if existing else None),
                    meal_slots=_merge_clean_lists(existing.meal_slots if existing else [], update.meal_slots),
                    favorite_items=_merge_clean_lists(existing.favorite_items if existing else [], update.favorite_items),
                    avoid_items=_merge_clean_lists(existing.avoid_items if existing else [], update.avoid_items),
                    health_notes=_clean_optional(update.health_notes) or (existing.health_notes if existing else None),
                    delivery_apps=_merge_clean_lists(existing.delivery_apps if existing else [], update.delivery_apps),
                    address_note=_clean_optional(update.address_note) or (existing.address_note if existing else None),
                    distance_note=_clean_optional(update.distance_note) or (existing.distance_note if existing else None),
                    price_note=_clean_optional(update.price_note) or (existing.price_note if existing else None),
                    status=_resolve_food_place_status(update.status, event, existing),
                    last_ordered_at=now.isoformat() if event == "ordered" else (existing.last_ordered_at if existing else None),
                    last_visited_at=now.isoformat() if event == "visited" else (existing.last_visited_at if existing else None),
                    order_count=(existing.order_count if existing else 0) + (1 if event == "ordered" else 0),
                    visit_count=(existing.visit_count if existing else 0) + (1 if event == "visited" else 0),
                    notes=_clean_optional(update.notes) or (existing.notes if existing else None),
                    created_at=created_at,
                    updated_at=now.isoformat(),
                )
                by_key[key] = record
                changed.append(record)
            next_places = sorted(by_key.values(), key=lambda item: item.name.lower())
            self._atomic_write_json_unlocked(self.food_places_path, [item.model_dump() for item in next_places])
            return changed

    def add_reminder(
        self,
        *,
        text: str,
        reminder_time: datetime,
        now: datetime,
        conversation_id: str | None,
        conversation_type: str,
        thread_id: str | None = None,
        thread_key: str | None = None,
    ) -> ReminderRecord:
        self.ensure_files()
        record = ReminderRecord(
            id=str(uuid.uuid4()),
            kind="reminder",
            text=text.strip(),
            time=reminder_time.isoformat(),
            conversation_id=conversation_id,
            conversation_type="group" if conversation_type == "group" else "user",
            thread_id=thread_id,
            thread_key=thread_key,
            created_at=now.isoformat(),
        )
        with self._locked():
            reminders = self._read_reminders_unlocked()
            reminders.append(record)
            self._atomic_write_json_unlocked(self.reminders_path, [item.model_dump() for item in reminders])
            return record

    def add_agent_task(
        self,
        *,
        title: str,
        prompt: str,
        run_at: datetime,
        now: datetime,
        conversation_id: str | None,
        conversation_type: str,
        thread_id: str | None = None,
        thread_key: str | None = None,
    ) -> ReminderRecord:
        self.ensure_files()
        record = ReminderRecord(
            id=str(uuid.uuid4()),
            kind="agent_task",
            text=title.strip(),
            prompt=prompt.strip(),
            time=run_at.isoformat(),
            conversation_id=conversation_id,
            conversation_type="group" if conversation_type == "group" else "user",
            thread_id=thread_id,
            thread_key=thread_key,
            created_at=now.isoformat(),
        )
        with self._locked():
            reminders = self._read_reminders_unlocked()
            reminders.append(record)
            self._atomic_write_json_unlocked(self.reminders_path, [item.model_dump() for item in reminders])
            return record

    def add_repeating_reminder(
        self,
        *,
        text: str,
        first_run_at: datetime,
        repeat_interval_minutes: int,
        now: datetime,
        conversation_id: str | None,
        conversation_type: str,
        thread_id: str | None = None,
        thread_key: str | None = None,
    ) -> ReminderRecord:
        self.ensure_files()
        record = ReminderRecord(
            id=str(uuid.uuid4()),
            kind="repeating_reminder",
            text=text.strip(),
            time=first_run_at.isoformat(),
            repeat_interval_minutes=max(5, min(1440, repeat_interval_minutes)),
            next_run_at=first_run_at.isoformat(),
            conversation_id=conversation_id,
            conversation_type="group" if conversation_type == "group" else "user",
            thread_id=thread_id,
            thread_key=thread_key,
            created_at=now.isoformat(),
        )
        with self._locked():
            reminders = self._read_reminders_unlocked()
            reminders.append(record)
            self._atomic_write_json_unlocked(self.reminders_path, [item.model_dump() for item in reminders])
            return record

    def add_recurring_agent_task(
        self,
        *,
        title: str,
        prompt: str,
        local_time: str,
        timezone: str,
        now: datetime,
        conversation_id: str | None,
        conversation_type: str,
        thread_id: str | None = None,
        thread_key: str | None = None,
    ) -> RecurringAgentTaskRecord:
        self.ensure_files()
        next_run_at = compute_next_daily_run(local_time=local_time, now=now)
        record = RecurringAgentTaskRecord(
            id=str(uuid.uuid4()),
            title=title.strip(),
            prompt=prompt.strip(),
            frequency="daily",
            time=local_time,
            timezone=timezone,
            conversation_id=conversation_id,
            conversation_type="group" if conversation_type == "group" else "user",
            thread_id=thread_id,
            thread_key=thread_key,
            created_at=now.isoformat(),
            next_run_at=next_run_at.isoformat(),
        )
        with self._locked():
            tasks = self._read_recurring_tasks_unlocked()
            tasks.append(record)
            self._atomic_write_json_unlocked(self.recurring_tasks_path, [item.model_dump() for item in tasks])
            return record

    def update_reminder(self, reminder_id: str, **updates: object) -> ReminderRecord | None:
        self.ensure_files()
        with self._locked():
            reminders = self._read_reminders_unlocked()
            updated_record = None
            next_records: list[ReminderRecord] = []
            for record in reminders:
                if record.id == reminder_id:
                    data = record.model_dump()
                    data.update(updates)
                    record = ReminderRecord.model_validate(data)
                    updated_record = record
                next_records.append(record)
            if updated_record is not None:
                self._atomic_write_json_unlocked(self.reminders_path, [item.model_dump() for item in next_records])
            return updated_record

    def update_reminder_completion(
        self,
        reminder_id: str,
        *,
        completion_status: TaskCompletionStatus,
        now: datetime,
        completed_by: str | None,
        note: str | None,
    ) -> ReminderRecord | None:
        return self.update_reminder(
            reminder_id,
            completion_status=completion_status,
            completed_at=now.isoformat() if completion_status != "open" else None,
            completed_by=completed_by if completion_status != "open" else None,
            completion_note=_clean_optional(note) if completion_status != "open" else None,
        )

    def complete_matching_reminder(
        self,
        *,
        conversation_id: str | None,
        target_text: str | None,
        completion_status: TaskCompletionStatus,
        now: datetime,
        completed_by: str | None,
        note: str | None,
        thread_key: str | None = None,
    ) -> ReminderRecord | None:
        self.ensure_files()
        with self._locked():
            reminders = self._read_reminders_unlocked()
            match = _find_matching_reminder(
                reminders,
                conversation_id=conversation_id,
                thread_key=thread_key,
                target_text=target_text,
                completion_status=completion_status,
                now=now,
            )
            if match is None:
                return None
            next_records: list[ReminderRecord] = []
            updated_record: ReminderRecord | None = None
            for record in reminders:
                if record.id == match.id:
                    data = record.model_dump()
                    data.update(
                        {
                            "completion_status": completion_status,
                            "completed_at": now.isoformat() if completion_status != "open" else None,
                            "completed_by": completed_by if completion_status != "open" else None,
                            "completion_note": _clean_optional(note) if completion_status != "open" else None,
                        }
                    )
                    record = ReminderRecord.model_validate(data)
                    updated_record = record
                next_records.append(record)
            self._atomic_write_json_unlocked(self.reminders_path, [item.model_dump() for item in next_records])
            return updated_record

    def due_pending_reminders(self, *, now: datetime, max_attempts: int) -> list[ReminderRecord]:
        self.ensure_files()
        with self._locked():
            reminders = self._read_reminders_unlocked()
            return [
                record
                for record in reminders
                if record.status == "pending"
                and record.completion_status == "open"
                and record.attempts < max_attempts
                and _as_aware(record.due_at(), now.tzinfo) <= now
            ]

    def due_recurring_tasks(self, *, now: datetime) -> list[RecurringAgentTaskRecord]:
        self.ensure_files()
        with self._locked():
            tasks = self._read_recurring_tasks_unlocked()
            return [
                task
                for task in tasks
                if task.status == "active"
                and _as_aware(task.next_due_at(), now.tzinfo) <= now
            ]

    def update_recurring_task(self, task_id: str, **updates: object) -> RecurringAgentTaskRecord | None:
        self.ensure_files()
        with self._locked():
            tasks = self._read_recurring_tasks_unlocked()
            updated_task = None
            next_tasks: list[RecurringAgentTaskRecord] = []
            for task in tasks:
                if task.id == task_id:
                    data = task.model_dump()
                    data.update(updates)
                    task = RecurringAgentTaskRecord.model_validate(data)
                    updated_task = task
                next_tasks.append(task)
            if updated_task is not None:
                self._atomic_write_json_unlocked(self.recurring_tasks_path, [item.model_dump() for item in next_tasks])
            return updated_task

    def complete_matching_recurring_task(
        self,
        *,
        conversation_id: str | None,
        target_text: str | None,
        completion_status: TaskCompletionStatus,
        now: datetime,
        note: str | None,
        thread_key: str | None = None,
    ) -> RecurringAgentTaskRecord | None:
        self.ensure_files()
        with self._locked():
            tasks = self._read_recurring_tasks_unlocked()
            match = _find_matching_recurring_task(
                tasks,
                conversation_id=conversation_id,
                thread_key=thread_key,
                target_text=target_text,
            )
            if match is None:
                return None
            next_tasks: list[RecurringAgentTaskRecord] = []
            updated_task: RecurringAgentTaskRecord | None = None
            for task in tasks:
                if task.id == match.id:
                    data = task.model_dump()
                    data.update(
                        {
                            "last_completion_status": completion_status,
                            "last_completed_at": now.isoformat() if completion_status != "open" else None,
                            "last_completion_note": _clean_optional(note) if completion_status != "open" else None,
                        }
                    )
                    task = RecurringAgentTaskRecord.model_validate(data)
                    updated_task = task
                next_tasks.append(task)
            self._atomic_write_json_unlocked(self.recurring_tasks_path, [item.model_dump() for item in next_tasks])
            return updated_task

    def _list_threads_unlocked(self) -> list[dict[str, object]]:
        threads: list[dict[str, object]] = []
        if not self.threads_dir.exists():
            return threads
        for path in sorted(self.threads_dir.iterdir()):
            if not path.is_dir():
                continue
            settings = self._read_json_object_unlocked(path / "settings.json")
            thread_key = str(settings.get("thread_key") or path.name)
            turns = self._read_json_list_unlocked(path / "conversation_turns.json")
            threads.append(
                {
                    "thread_key": thread_key,
                    "dir_name": path.name,
                    "conversation_turns_count": len(turns),
                }
            )
        return threads

    def _thread_paths_unlocked(self, thread_key: str) -> dict[str, Path]:
        thread_dir = self.threads_dir / thread_dir_name(thread_key)
        return {
            "dir": thread_dir,
            "prompt": thread_dir / "prompt.md",
            "rules": thread_dir / "rules.md",
            "conversation_turns": thread_dir / "conversation_turns.json",
            "settings": thread_dir / "settings.json",
        }

    def _ensure_thread_files_unlocked(self, thread_key: str) -> dict[str, Path]:
        paths = self._thread_paths_unlocked(thread_key)
        paths["dir"].mkdir(parents=True, exist_ok=True)
        if not paths["prompt"].exists():
            self._atomic_write_text_unlocked(paths["prompt"], "# Thread Prompt\n")
        if not paths["rules"].exists():
            self._atomic_write_text_unlocked(paths["rules"], "# Thread Rules\n")
        if not paths["conversation_turns"].exists():
            self._atomic_write_text_unlocked(paths["conversation_turns"], "[]")
        if not paths["settings"].exists():
            self._atomic_write_json_unlocked(
                paths["settings"],
                {
                    "thread_key": thread_key,
                    "dir_name": paths["dir"].name,
                    "created_at": datetime.now().astimezone().isoformat(),
                },
            )
        return paths

    def _read_recent_unlocked(self) -> list[RecentMemoryEntry]:
        return [RecentMemoryEntry.model_validate(item) for item in self._read_json_list_unlocked(self.recent_path)]

    def _read_conversation_turns_unlocked(self) -> list[ConversationTurn]:
        return [
            ConversationTurn.model_validate(item)
            for item in self._read_json_list_unlocked(self.conversation_turns_path)
        ]

    def _read_thread_conversation_turns_unlocked(self, thread_key: str) -> list[ConversationTurn]:
        paths = self._ensure_thread_files_unlocked(thread_key)
        return [
            ConversationTurn.model_validate(item)
            for item in self._read_json_list_unlocked(paths["conversation_turns"])
        ]

    def _prune_and_persist_conversation_turns_unlocked(self) -> list[ConversationTurn]:
        turns = self._read_conversation_turns_unlocked()
        pruned = _prune_conversation_turns(
            turns,
            now=datetime.now().astimezone(),
            retention_days=self.conversation_turn_retention_days,
        )
        if len(pruned) != len(turns):
            self._atomic_write_json_unlocked(self.conversation_turns_path, [item.model_dump() for item in pruned])
        return pruned

    def _prune_and_persist_thread_conversation_turns_unlocked(self, thread_key: str) -> list[ConversationTurn]:
        paths = self._ensure_thread_files_unlocked(thread_key)
        turns = self._read_thread_conversation_turns_unlocked(thread_key)
        pruned = _prune_conversation_turns(
            turns,
            now=datetime.now().astimezone(),
            retention_days=self.conversation_turn_retention_days,
        )
        if len(pruned) != len(turns):
            self._atomic_write_json_unlocked(paths["conversation_turns"], [item.model_dump() for item in pruned])
        return pruned

    def _read_reminders_unlocked(self) -> list[ReminderRecord]:
        return [ReminderRecord.model_validate(item) for item in self._read_json_list_unlocked(self.reminders_path)]

    def _read_recurring_tasks_unlocked(self) -> list[RecurringAgentTaskRecord]:
        return [
            RecurringAgentTaskRecord.model_validate(item)
            for item in self._read_json_list_unlocked(self.recurring_tasks_path)
        ]

    def _read_fridge_unlocked(self) -> list[FridgeItemRecord]:
        return [FridgeItemRecord.model_validate(item) for item in self._read_json_list_unlocked(self.fridge_path)]

    def _read_daily_meals_unlocked(self) -> list[DailyMealRecord]:
        return [
            DailyMealRecord.model_validate(item)
            for item in self._read_json_list_unlocked(self.daily_meals_path)
        ]

    def _read_food_places_unlocked(self) -> list[FoodPlaceRecord]:
        return [
            FoodPlaceRecord.model_validate(item)
            for item in self._read_json_list_unlocked(self.food_places_path)
        ]

    def _read_json_list_unlocked(self, path: Path) -> list[object]:
        try:
            raw = path.read_text(encoding="utf-8").strip()
            if not raw:
                return []
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def _read_json_object_unlocked(self, path: Path) -> dict[str, object]:
        try:
            raw = path.read_text(encoding="utf-8").strip()
            if not raw:
                return {}
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    @contextmanager
    def _locked(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _atomic_write_json_unlocked(self, path: Path, value: object) -> None:
        self._atomic_write_text_unlocked(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def _atomic_write_text_unlocked(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        tmp_path.write_text(text, encoding="utf-8")
        os.replace(tmp_path, path)


def _clean_update(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip(" -\n\t"))


def _clean_optional(value: str | None) -> str | None:
    cleaned = _clean_update(value or "")
    return cleaned or None


def _clean_multiline(value: str) -> str:
    lines = [_clean_update(line) for line in str(value or "").splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _merge_clean_lists(existing: list[str], updates: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for value in [*existing, *updates]:
        cleaned = _clean_update(value)
        key = _normalize_for_dedupe(cleaned)
        if not cleaned or not key or key in seen:
            continue
        merged.append(cleaned)
        seen.add(key)
    return merged


def _normalize_for_dedupe(value: str) -> str:
    text = unicodedata.normalize("NFKD", value.lower().replace("đ", "d"))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _as_aware(value: datetime, tzinfo) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=tzinfo)
    return value.astimezone(tzinfo)


def _parse_optional_datetime(value: str | None, now: datetime) -> datetime | None:
    clean_value = _clean_update(value or "")
    if not clean_value:
        return None
    if clean_value.endswith("Z"):
        clean_value = clean_value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(clean_value)
    except ValueError:
        return None
    return _as_aware(parsed, now.tzinfo)


def _resolve_food_place_type(place_type: str, existing: FoodPlaceRecord | None) -> str:
    if place_type != "other":
        return place_type
    if existing is not None and existing.place_type != "other":
        return existing.place_type
    return place_type


def _resolve_food_place_status(status: str, event: str, existing: FoodPlaceRecord | None) -> str:
    if event == "disliked":
        return "disliked"
    if status != "unknown":
        return status
    if existing is not None and existing.status != "unknown":
        return existing.status
    if event in {"ordered", "visited", "mentioned", "updated"}:
        return "active"
    return "unknown"


def _resolve_fridge_category(
    name: str,
    category: str,
    existing: FridgeItemRecord | None,
) -> str:
    if category != "other":
        return category
    if existing is not None and existing.category != "other":
        return existing.category
    return _infer_fridge_category(name)


def _infer_fridge_category(name: str) -> str:
    raw = name.lower()
    normalized = _normalize_for_dedupe(name)
    if any(keyword in normalized.split() for keyword in {"trung", "egg"}):
        return "egg"
    if any(keyword in raw for keyword in ["tôm", "mực", "nghêu", "sò", "hải sản", "cua", "ghẹ"]):
        return "seafood"
    if any(keyword in normalized for keyword in ["tom", "muc", "ngheu", "hai san", "ghe"]):
        return "seafood"
    if raw.startswith("cá ") or " cá " in raw or normalized.startswith("ca ") or any(
        keyword in normalized for keyword in ["ca hoi", "ca thu", "ca basa"]
    ):
        return "seafood"
    if any(keyword in raw for keyword in ["thịt", "sườn"]):
        return "meat"
    if any(keyword in raw for keyword in ["bò", "heo", "gà", "vịt"]):
        return "meat"
    if any(keyword in normalized.split() for keyword in {"heo", "ga", "suon", "vit"}):
        return "meat"
    if any(keyword in raw for keyword in ["rau", "củ", "cải"]):
        return "vegetable"
    if any(keyword in normalized for keyword in ["rau", "cu ", "cai", "bap cai", "ca chua", "dua leo"]):
        return "vegetable"
    if any(keyword in raw for keyword in ["trái", "quả"]):
        return "fruit"
    if any(keyword in normalized.split() for keyword in {"chuoi", "cam", "tao", "nho", "dua"}):
        return "fruit"
    if "pho mai" in normalized or any(keyword in normalized.split() for keyword in {"sua", "cheese", "yogurt", "yaourt"}):
        return "dairy"
    return "other"


def _resolve_fridge_compartment(
    category: str,
    update_compartment: str | None,
    existing: FridgeItemRecord | None,
) -> str | None:
    if update_compartment is not None:
        return update_compartment
    if existing is not None and existing.compartment is not None:
        return existing.compartment
    if category in {"vegetable", "fruit", "egg", "dairy", "cooked_food"}:
        return "cool"
    return None


def _resolve_added_at(
    update_added_at: str | None,
    existing: FridgeItemRecord | None,
    now: datetime,
) -> datetime:
    explicit_added_at = _parse_optional_datetime(update_added_at, now)
    if explicit_added_at is not None:
        return explicit_added_at
    if existing is not None:
        existing_added_at = _parse_optional_datetime(existing.added_at, now)
        if existing_added_at is not None:
            return existing_added_at
    return now


def _resolve_expiry(
    *,
    update: FridgeItemUpdate,
    existing: FridgeItemRecord | None,
    category: str,
    compartment: str | None,
    added_at: datetime,
    now: datetime,
) -> tuple[str | None, str]:
    explicit_expires_at = _parse_optional_datetime(update.expires_at, now)
    if explicit_expires_at is not None:
        return explicit_expires_at.isoformat(), "explicit"
    if existing is not None and existing.expiry_source == "explicit" and existing.expires_at:
        return existing.expires_at, "explicit"
    default_expires_at = _default_fridge_expiry(added_at=added_at, category=category, compartment=compartment)
    if default_expires_at is not None:
        return default_expires_at.isoformat(), "default"
    if existing is not None and existing.expires_at:
        return existing.expires_at, existing.expiry_source
    return None, "unknown"


def _default_fridge_expiry(
    *,
    added_at: datetime,
    category: str,
    compartment: str | None,
) -> datetime | None:
    if category in {"vegetable", "fruit"} and compartment == "cool":
        return added_at + timedelta(days=7)
    if category == "meat" and compartment == "cool":
        return added_at + timedelta(days=2)
    if category == "seafood" and compartment == "cool":
        return added_at + timedelta(days=1)
    if category in {"meat", "seafood"} and compartment == "freezer":
        return added_at + timedelta(days=30)
    return None


def _build_fridge_warnings(items: list[FridgeItemRecord], *, now: datetime) -> list[dict[str, object]]:
    warnings: list[dict[str, object]] = []
    for item in items:
        if item.status == "finished":
            continue
        expires_at = _parse_optional_datetime(item.expires_at, now)
        if expires_at is None:
            continue
        days_left = (expires_at.date() - now.date()).days
        if days_left < 0:
            level = "expired"
        elif days_left == 0:
            level = "expires_today"
        elif days_left <= 2:
            level = "expires_soon"
        else:
            continue
        warnings.append(
            {
                "name": item.name,
                "quantity_note": item.quantity_note,
                "category": item.category,
                "compartment": item.compartment,
                "status": item.status,
                "added_at": item.added_at,
                "expires_at": item.expires_at,
                "expiry_source": item.expiry_source,
                "days_left": days_left,
                "level": level,
            }
        )
    return sorted(warnings, key=lambda item: (int(item["days_left"]), str(item["name"]).lower()))


def _find_matching_reminder(
    reminders: list[ReminderRecord],
    *,
    conversation_id: str | None,
    thread_key: str | None,
    target_text: str | None,
    completion_status: TaskCompletionStatus,
    now: datetime,
) -> ReminderRecord | None:
    target_key = _normalize_for_dedupe(target_text or "")
    candidates: list[ReminderRecord] = []
    for record in reminders:
        if conversation_id and record.conversation_id != conversation_id:
            continue
        if thread_key and record.thread_key and record.thread_key != thread_key:
            continue
        if record.completion_status != "open":
            continue
        if record.status == "failed":
            continue
        if (
            completion_status != "canceled"
            and record.status != "sent"
            and not (record.kind == "repeating_reminder" and record.sent_at)
        ):
            continue
        if target_key and not _record_matches_target(record, target_key):
            continue
        candidates.append(record)
    if not candidates:
        return None
    return sorted(candidates, key=lambda record: _reminder_sort_time(record, now))[-1]


def _find_matching_recurring_task(
    tasks: list[RecurringAgentTaskRecord],
    *,
    conversation_id: str | None,
    thread_key: str | None,
    target_text: str | None,
) -> RecurringAgentTaskRecord | None:
    target_key = _normalize_for_dedupe(target_text or "")
    candidates: list[RecurringAgentTaskRecord] = []
    for task in tasks:
        if conversation_id and task.conversation_id != conversation_id:
            continue
        if thread_key and task.thread_key and task.thread_key != thread_key:
            continue
        if task.status != "active":
            continue
        if task.last_completion_status != "open":
            continue
        if target_key and not _recurring_task_matches_target(task, target_key):
            continue
        candidates.append(task)
    if not candidates:
        return None
    return sorted(candidates, key=lambda task: task.last_run_at or task.created_at)[-1]


def _record_matches_target(record: ReminderRecord, target_key: str) -> bool:
    fields = [record.id, record.text, record.prompt or ""]
    for field in fields:
        key = _normalize_for_dedupe(field)
        if key and (target_key in key or key in target_key):
            return True
    return False


def _recurring_task_matches_target(task: RecurringAgentTaskRecord, target_key: str) -> bool:
    fields = [task.id, task.title, task.prompt]
    for field in fields:
        key = _normalize_for_dedupe(field)
        if key and (target_key in key or key in target_key):
            return True
    return False


def _reminder_sort_time(record: ReminderRecord, now: datetime) -> datetime:
    for value in (record.sent_at, record.time, record.created_at):
        parsed = _parse_optional_datetime(value, now)
        if parsed is not None:
            return parsed
    return now


def compute_next_daily_run(*, local_time: str, now: datetime) -> datetime:
    hour_text, minute_text = local_time.split(":", maxsplit=1)
    candidate = now.replace(hour=int(hour_text), minute=int(minute_text), second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def _prune_daily_meals(
    meals: list[DailyMealRecord],
    *,
    now: datetime,
    retention_days: int,
) -> list[DailyMealRecord]:
    cutoff = (now - timedelta(days=retention_days - 1)).date().isoformat()
    return sorted([item for item in meals if item.date >= cutoff], key=lambda item: item.date)


def _prune_conversation_turns(
    turns: list[ConversationTurn],
    *,
    now: datetime,
    retention_days: int,
) -> list[ConversationTurn]:
    cutoff = now - timedelta(days=max(1, retention_days))
    kept: list[ConversationTurn] = []
    for turn in turns:
        try:
            turn_ts = datetime.fromisoformat(turn.ts)
        except ValueError:
            continue
        if turn_ts.tzinfo is None and now.tzinfo is not None:
            turn_ts = turn_ts.replace(tzinfo=now.tzinfo)
        elif turn_ts.tzinfo is not None and now.tzinfo is not None:
            turn_ts = turn_ts.astimezone(now.tzinfo)
        if turn_ts >= cutoff:
            kept.append(turn)
    return sorted(kept, key=lambda item: item.ts)
