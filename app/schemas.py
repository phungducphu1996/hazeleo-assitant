from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


FridgeCategory = Literal["vegetable", "fruit", "meat", "seafood", "egg", "dairy", "cooked_food", "other"]
FridgeCompartment = Literal["cool", "freezer"]
FridgeExpirySource = Literal["explicit", "default", "unknown"]
TaskCompletionStatus = Literal["open", "done", "skipped", "canceled"]


class ZaloIncomingRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=1000)
    from_uid: str | None = Field(default=None, max_length=255)
    conversation_id: str | None = Field(default=None, max_length=255)
    conversation_type: Literal["user", "group"] = "user"
    thread_id: str | None = Field(default=None, max_length=255)
    message_id: str | None = Field(default=None, max_length=255)
    reply_to_message_id: str | None = Field(default=None, max_length=255)
    reply_to_cli_message_id: str | None = Field(default=None, max_length=255)
    quoted_text: str | None = Field(default=None, max_length=1000)


class ZaloWorkerTarget(BaseModel):
    thread_id: str | None = None
    thread_type: str | None = None
    source: str | None = None


class ZaloDeliveryResult(BaseModel):
    ok: bool
    message_id: str | None = None
    fallback_used: bool | None = None
    target: ZaloWorkerTarget | None = None
    error: str | None = None


class ZaloIncomingResponse(BaseModel):
    reply: str
    memory: "AgentMemoryUpdates"
    reminder: "ReminderDraft | None" = None
    repeating_reminder: "RepeatingReminderDraft | None" = None
    agent_task: "AgentTaskDraft | None" = None
    recurring_agent_task: "RecurringAgentTaskDraft | None" = None
    delivery: ZaloDeliveryResult | None = None
    reminder_saved: bool = False
    reminder_error: str | None = None
    repeating_reminder_saved: bool = False
    repeating_reminder_error: str | None = None
    agent_task_saved: bool = False
    agent_task_error: str | None = None
    recurring_agent_task_saved: bool = False
    recurring_agent_task_error: str | None = None
    fridge_updates_saved: int = 0
    daily_meal_saved: bool = False
    rules_updates_saved: int = 0
    task_status_update: "TaskStatusUpdateDraft | None" = None
    task_status_updated: bool = False
    task_status_error: str | None = None


class TelegramWebhookResponse(BaseModel):
    ok: bool
    processed: bool = False
    reply: str | None = None
    error: str | None = None


class AgentMemoryUpdates(BaseModel):
    profile_updates: list[str] = Field(default_factory=list)
    recent_updates: list[str] = Field(default_factory=list)


class ReminderDraft(BaseModel):
    text: str = Field(..., min_length=1, max_length=500)
    time: str = Field(..., min_length=1, max_length=80)


class RepeatingReminderDraft(BaseModel):
    text: str = Field(..., min_length=1, max_length=500)
    time: str = Field(..., min_length=1, max_length=80)
    repeat_interval_minutes: int = Field(..., ge=5, le=1440)


class AgentTaskDraft(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)
    prompt: str = Field(..., min_length=1, max_length=1000)
    time: str = Field(..., min_length=1, max_length=80)


class RecurringAgentTaskDraft(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)
    prompt: str = Field(..., min_length=1, max_length=1000)
    frequency: Literal["daily"] = "daily"
    time: str = Field(..., pattern=r"^\d{2}:\d{2}$")


class FridgeItemUpdate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    quantity_note: str | None = Field(default=None, max_length=120)
    status: Literal["available", "low", "used", "finished"] = "available"
    note: str | None = Field(default=None, max_length=200)
    category: FridgeCategory = "other"
    compartment: FridgeCompartment | None = None
    added_at: str | None = Field(default=None, max_length=80)
    expires_at: str | None = Field(default=None, max_length=80)
    expiry_source: FridgeExpirySource = "unknown"


class DailyMealUpdate(BaseModel):
    date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    meal_slot: Literal["breakfast", "lunch", "dinner", "snack"]
    suggestions: list[str] = Field(default_factory=list, max_length=5)
    actual_items: list[str] = Field(default_factory=list, max_length=10)
    selected: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=300)


class TaskStatusUpdateDraft(BaseModel):
    target_text: str | None = Field(default=None, max_length=500)
    completion_status: TaskCompletionStatus
    note: str | None = Field(default=None, max_length=300)


class AgentOutput(BaseModel):
    reply: str = Field(..., min_length=1, max_length=2000)
    memory: AgentMemoryUpdates = Field(default_factory=AgentMemoryUpdates)
    reminder: ReminderDraft | None = None
    repeating_reminder: RepeatingReminderDraft | None = None
    agent_task: AgentTaskDraft | None = None
    recurring_agent_task: RecurringAgentTaskDraft | None = None
    rules_updates: list[str] = Field(default_factory=list)
    fridge_updates: list[FridgeItemUpdate] = Field(default_factory=list)
    daily_meal_update: DailyMealUpdate | None = None
    daily_meal_updates: list[DailyMealUpdate] = Field(default_factory=list, max_length=5)
    task_status_update: TaskStatusUpdateDraft | None = None


class RecentMemoryEntry(BaseModel):
    ts: str
    conversation_id: str | None = None
    from_uid: str | None = None
    text: str


class ConversationTurn(BaseModel):
    ts: str
    conversation_id: str | None = None
    from_uid: str | None = None
    role: Literal["user", "assistant"] = "user"
    text: str


class ReminderRecord(BaseModel):
    id: str
    kind: Literal["reminder", "agent_task", "repeating_reminder"] = "reminder"
    text: str
    prompt: str | None = None
    time: str
    repeat_interval_minutes: int | None = None
    next_run_at: str | None = None
    conversation_id: str | None = None
    conversation_type: Literal["user", "group"] = "user"
    thread_id: str | None = None
    status: Literal["pending", "sent", "failed"] = "pending"
    attempts: int = 0
    created_at: str
    sent_at: str | None = None
    completion_status: TaskCompletionStatus = "open"
    completed_at: str | None = None
    completed_by: str | None = None
    completion_note: str | None = None
    last_error: str | None = None

    def due_at(self) -> datetime:
        return datetime.fromisoformat(self.next_run_at or self.time)


class RecurringAgentTaskRecord(BaseModel):
    id: str
    title: str
    prompt: str
    frequency: Literal["daily"] = "daily"
    time: str = Field(..., pattern=r"^\d{2}:\d{2}$")
    timezone: str = "Asia/Ho_Chi_Minh"
    conversation_id: str | None = None
    conversation_type: Literal["user", "group"] = "user"
    thread_id: str | None = None
    status: Literal["active", "paused", "failed"] = "active"
    attempts: int = 0
    created_at: str
    last_run_at: str | None = None
    last_completion_status: TaskCompletionStatus = "open"
    last_completed_at: str | None = None
    last_completion_note: str | None = None
    next_run_at: str
    last_error: str | None = None

    def next_due_at(self) -> datetime:
        return datetime.fromisoformat(self.next_run_at)


class FridgeItemRecord(BaseModel):
    name: str
    quantity_note: str | None = None
    status: Literal["available", "low", "used", "finished"] = "available"
    note: str | None = None
    category: FridgeCategory = "other"
    compartment: FridgeCompartment | None = None
    added_at: str | None = None
    expires_at: str | None = None
    expiry_source: FridgeExpirySource = "unknown"
    updated_at: str


class DailyMealSlotRecord(BaseModel):
    suggestions: list[str] = Field(default_factory=list)
    actual_items: list[str] = Field(default_factory=list)
    selected: str | None = None
    notes: str | None = None
    updated_at: str


class DailyMealRecord(BaseModel):
    date: str
    meals: dict[str, DailyMealSlotRecord] = Field(default_factory=dict)


AGENT_OUTPUT_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "reply": {
            "type": "string",
            "description": "Short friendly Vietnamese reply to show to the user.",
        },
        "memory": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "profile_updates": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Important long-term facts only.",
                },
                "recent_updates": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Short-term facts such as recent meals, fridge items, or daily activities.",
                },
            },
            "required": ["profile_updates", "recent_updates"],
        },
        "reminder": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "text": {"type": "string"},
                        "time": {
                            "type": "string",
                            "format": "date-time",
                            "description": "ISO datetime with timezone when possible.",
                        },
                    },
                    "required": ["text", "time"],
                },
            ]
        },
        "repeating_reminder": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "text": {"type": "string"},
                        "time": {
                            "type": "string",
                            "format": "date-time",
                            "description": "ISO datetime for the first reminder occurrence.",
                        },
                        "repeat_interval_minutes": {
                            "type": "integer",
                            "minimum": 5,
                            "maximum": 1440,
                        },
                    },
                    "required": ["text", "time", "repeat_interval_minutes"],
                },
            ]
        },
        "agent_task": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "title": {"type": "string"},
                        "prompt": {
                            "type": "string",
                            "description": "The exact work the assistant should perform when the scheduled time arrives.",
                        },
                        "time": {
                            "type": "string",
                            "format": "date-time",
                            "description": "ISO datetime with timezone when possible.",
                        },
                    },
                    "required": ["title", "prompt", "time"],
                },
            ]
        },
        "recurring_agent_task": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "title": {"type": "string"},
                        "prompt": {
                            "type": "string",
                            "description": "The work to perform every day at the configured local time.",
                        },
                        "frequency": {"type": "string", "enum": ["daily"]},
                        "time": {
                            "type": "string",
                            "pattern": "^\\d{2}:\\d{2}$",
                            "description": "Local 24-hour HH:MM time, for example 09:00 or 22:00.",
                        },
                    },
                    "required": ["title", "prompt", "frequency", "time"],
                },
            ]
        },
        "rules_updates": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Durable assistant behavior rules requested by the user.",
        },
        "fridge_updates": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "quantity_note": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "status": {"type": "string", "enum": ["available", "low", "used", "finished"]},
                    "note": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "category": {
                        "type": "string",
                        "enum": ["vegetable", "fruit", "meat", "seafood", "egg", "dairy", "cooked_food", "other"],
                    },
                    "compartment": {"anyOf": [{"type": "string", "enum": ["cool", "freezer"]}, {"type": "null"}]},
                    "added_at": {
                        "anyOf": [{"type": "string"}, {"type": "null"}],
                        "description": "ISO date-time when user explicitly says when the item was put into the fridge; otherwise null.",
                    },
                    "expires_at": {
                        "anyOf": [{"type": "string"}, {"type": "null"}],
                        "description": "ISO date-time only when user explicitly gives HSD/expiry; otherwise null.",
                    },
                    "expiry_source": {"type": "string", "enum": ["explicit", "default", "unknown"]},
                },
                "required": [
                    "name",
                    "quantity_note",
                    "status",
                    "note",
                    "category",
                    "compartment",
                    "added_at",
                    "expires_at",
                    "expiry_source",
                ],
            },
        },
        "daily_meal_update": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "date": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
                        "meal_slot": {"type": "string", "enum": ["breakfast", "lunch", "dinner", "snack"]},
                        "suggestions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": 5,
                        },
                        "actual_items": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": 10,
                            "description": "Foods actually eaten, cooked, or explicitly saved for this meal.",
                        },
                        "selected": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "notes": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    },
                    "required": ["date", "meal_slot", "suggestions", "actual_items", "selected", "notes"],
                },
            ]
        },
        "daily_meal_updates": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "date": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
                    "meal_slot": {"type": "string", "enum": ["breakfast", "lunch", "dinner", "snack"]},
                    "suggestions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 5,
                    },
                    "actual_items": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 10,
                        "description": "Foods actually eaten, cooked, or explicitly saved for this meal.",
                    },
                    "selected": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "notes": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["date", "meal_slot", "suggestions", "actual_items", "selected", "notes"],
            },
            "maxItems": 5,
        },
        "task_status_update": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "target_text": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "description": "The task/reminder text the user is referring to, or null for the most recent open sent task.",
                        },
                        "completion_status": {
                            "type": "string",
                            "enum": ["open", "done", "skipped", "canceled"],
                        },
                        "note": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    },
                    "required": ["target_text", "completion_status", "note"],
                },
            ]
        },
    },
    "required": [
        "reply",
        "memory",
        "reminder",
        "repeating_reminder",
        "agent_task",
        "recurring_agent_task",
        "rules_updates",
        "fridge_updates",
        "daily_meal_update",
        "daily_meal_updates",
        "task_status_update",
    ],
}
