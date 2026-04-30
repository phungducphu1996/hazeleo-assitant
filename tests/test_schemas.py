from __future__ import annotations

import json
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.config import Settings
from app.openai_client import AgentModelError, OpenAIAgentClient, _extract_response_text, _model_supports_temperature
from app.schemas import AgentOutput, ReminderRecord, ZaloIncomingRequest


def test_agent_output_accepts_chat_without_reminder() -> None:
    payload = {
        "reply": "tối nay ăn nhẹ nha",
        "memory": {"profile_updates": ["Ngọc đang ốm nghén"], "recent_updates": []},
        "reminder": None,
    }

    parsed = AgentOutput.model_validate(payload)

    assert parsed.reply == "tối nay ăn nhẹ nha"
    assert parsed.memory.profile_updates == ["Ngọc đang ốm nghén"]
    assert parsed.reminder is None


def test_incoming_request_accepts_long_telegram_text() -> None:
    payload = ZaloIncomingRequest(text="x" * 1500, source="telegram")

    assert len(payload.text) == 1500


def test_openai_client_wraps_http_errors(monkeypatch, tmp_path) -> None:
    async def raise_timeout(**_kwargs):
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr("app.openai_client._post_response", raise_timeout)
    settings = Settings(data_dir=tmp_path / "data", openai_api_key="test-key")
    client = OpenAIAgentClient(settings)

    with pytest.raises(AgentModelError) as exc_info:
        asyncio.run(
            client.run(
                agent_prompt="Return JSON only.",
                profile="",
                recent=[],
                payload=ZaloIncomingRequest(text="xin chào"),
                now=datetime(2026, 4, 29, 9, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh")),
            )
        )

    assert "ReadTimeout" in str(exc_info.value)


def test_openai_timeout_defaults_to_two_minutes() -> None:
    settings = Settings(openai_api_key="test-key")

    assert settings.openai_timeout_seconds == 120.0


def test_agent_output_accepts_reminder() -> None:
    payload = {
        "reply": "ok mai mình nhắc nha",
        "memory": {"profile_updates": [], "recent_updates": ["Cần mua sữa cho Ngọc"]},
        "reminder": {"text": "mua sữa cho Ngọc", "time": "2026-04-23T09:00:00+07:00"},
    }

    parsed = AgentOutput.model_validate(payload)

    assert parsed.reminder is not None
    assert parsed.reminder.text == "mua sữa cho Ngọc"


def test_agent_output_accepts_repeating_reminder() -> None:
    payload = {
        "reply": "ok 10h Gia nhắc, rồi cứ 30p nhắc lại tới khi anh báo xong nha",
        "memory": {"profile_updates": [], "recent_updates": []},
        "reminder": None,
        "repeating_reminder": {
            "text": "cất cơm vào tủ lạnh",
            "time": "2026-04-23T22:00:00+07:00",
            "repeat_interval_minutes": 30,
        },
    }

    parsed = AgentOutput.model_validate(payload)

    assert parsed.repeating_reminder is not None
    assert parsed.repeating_reminder.text == "cất cơm vào tủ lạnh"
    assert parsed.repeating_reminder.repeat_interval_minutes == 30


def test_agent_output_accepts_agent_task() -> None:
    payload = {
        "reply": "ok 22h mình tổng hợp cho nha",
        "memory": {"profile_updates": [], "recent_updates": []},
        "reminder": None,
        "agent_task": {
            "title": "Tổng hợp việc ngày mai",
            "prompt": "Cho mình 3 việc quan trọng nhất ngày mai",
            "time": "2026-04-23T22:00:00+07:00",
        },
    }

    parsed = AgentOutput.model_validate(payload)

    assert parsed.agent_task is not None
    assert parsed.agent_task.prompt == "Cho mình 3 việc quan trọng nhất ngày mai"


def test_agent_output_accepts_recurring_agent_task() -> None:
    payload = {
        "reply": "ok mỗi ngày 9h mình gợi ý ăn trưa nha",
        "memory": {"profile_updates": [], "recent_updates": []},
        "reminder": None,
        "agent_task": None,
        "recurring_agent_task": {
            "title": "Gợi ý ăn trưa",
            "prompt": "Gợi ý đồ ăn trưa đơn giản cho Ngọc",
            "frequency": "daily",
            "time": "09:00",
        },
    }

    parsed = AgentOutput.model_validate(payload)

    assert parsed.recurring_agent_task is not None
    assert parsed.recurring_agent_task.time == "09:00"


def test_agent_output_accepts_food_updates() -> None:
    payload = {
        "reply": "mình lưu tủ lạnh và gợi ý trưa rồi nha",
        "memory": {"profile_updates": [], "recent_updates": []},
        "reminder": None,
        "agent_task": None,
        "recurring_agent_task": None,
        "fridge_updates": [
            {
                "name": "trứng",
                "quantity_note": "5 quả",
                "status": "available",
                "note": None,
            }
        ],
        "daily_meal_update": {
            "date": "2026-04-23",
            "meal_slot": "lunch",
            "suggestions": ["cháo thịt bằm", "canh rau trứng"],
            "selected": None,
            "notes": "nhẹ bụng",
        },
    }

    parsed = AgentOutput.model_validate(payload)

    assert parsed.fridge_updates[0].name == "trứng"
    assert parsed.daily_meal_update is not None
    assert parsed.daily_meal_update.meal_slot == "lunch"


def test_agent_output_accepts_multiple_daily_meal_updates_with_actual_items() -> None:
    payload = {
        "reply": "mình lưu trưa và gợi ý tối rồi nha",
        "memory": {"profile_updates": [], "recent_updates": []},
        "reminder": None,
        "agent_task": None,
        "recurring_agent_task": None,
        "fridge_updates": [],
        "daily_meal_update": None,
        "daily_meal_updates": [
            {
                "date": "2026-04-25",
                "meal_slot": "lunch",
                "suggestions": [],
                "actual_items": ["canh cải cúc", "cải ngồng xào tỏi", "thịt heo luộc"],
                "selected": None,
                "notes": "thực đơn trưa đã lưu",
            },
            {
                "date": "2026-04-25",
                "meal_slot": "dinner",
                "suggestions": ["cá chiên", "đậu lăng hầm", "thịt heo xào"],
                "actual_items": [],
                "selected": None,
                "notes": "gợi ý dinner",
            },
        ],
    }

    parsed = AgentOutput.model_validate(payload)

    assert parsed.daily_meal_updates[0].actual_items == ["canh cải cúc", "cải ngồng xào tỏi", "thịt heo luộc"]
    assert parsed.daily_meal_updates[1].suggestions == ["cá chiên", "đậu lăng hầm", "thịt heo xào"]


def test_agent_output_accepts_food_place_updates() -> None:
    payload = {
        "reply": "Gia lưu quán A cho bữa trưa rồi nha",
        "memory": {"profile_updates": [], "recent_updates": []},
        "reminder": None,
        "agent_task": None,
        "recurring_agent_task": None,
        "fridge_updates": [],
        "food_place_updates": [
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
                "distance_note": "gần nhà",
                "price_note": "khoảng 80k",
                "status": "active",
                "event": "ordered",
                "notes": "trưa nay đặt về",
            }
        ],
        "daily_meal_update": None,
    }

    parsed = AgentOutput.model_validate(payload)

    assert parsed.food_place_updates[0].name == "Quán A"
    assert parsed.food_place_updates[0].event == "ordered"
    assert parsed.food_place_updates[0].favorite_items == ["cơm gà"]


def test_agent_output_accepts_fridge_hsd_fields() -> None:
    payload = {
        "reply": "Gia lưu cá hồi ngăn đông rồi nha",
        "memory": {"profile_updates": [], "recent_updates": []},
        "reminder": None,
        "agent_task": None,
        "recurring_agent_task": None,
        "rules_updates": [],
        "fridge_updates": [
            {
                "name": "cá hồi",
                "quantity_note": "500g",
                "status": "available",
                "note": None,
                "category": "seafood",
                "compartment": "freezer",
                "added_at": None,
                "expires_at": "2026-05-23T09:00:00+07:00",
                "expiry_source": "explicit",
            }
        ],
        "daily_meal_update": None,
    }

    parsed = AgentOutput.model_validate(payload)

    assert parsed.fridge_updates[0].category == "seafood"
    assert parsed.fridge_updates[0].compartment == "freezer"
    assert parsed.fridge_updates[0].expiry_source == "explicit"


def test_agent_output_accepts_rules_updates() -> None:
    payload = {
        "reply": "vâng anh chị, Gia nhớ rồi nha",
        "memory": {"profile_updates": [], "recent_updates": []},
        "reminder": None,
        "agent_task": None,
        "recurring_agent_task": None,
        "rules_updates": ["Gia mở đầu câu trả lời bằng 'vâng anh chị' khi phù hợp."],
        "fridge_updates": [],
        "daily_meal_update": None,
    }

    parsed = AgentOutput.model_validate(payload)

    assert parsed.rules_updates == ["Gia mở đầu câu trả lời bằng 'vâng anh chị' khi phù hợp."]


def test_agent_output_accepts_thread_rules_and_prompt_update() -> None:
    payload = {
        "reply": "vâng anh chị, Gia chỉnh thread này rồi nha",
        "memory": {"profile_updates": [], "recent_updates": []},
        "reminder": None,
        "agent_task": None,
        "recurring_agent_task": None,
        "rules_updates": [],
        "thread_rules_updates": ["Thread này trả lời như chuyên gia dinh dưỡng mẹ bầu."],
        "thread_prompt_update": "Chuyên gia ăn uống gia đình, ưu tiên mẹ bầu và HSD tủ lạnh.",
        "fridge_updates": [],
        "daily_meal_update": None,
    }

    parsed = AgentOutput.model_validate(payload)

    assert parsed.thread_rules_updates == ["Thread này trả lời như chuyên gia dinh dưỡng mẹ bầu."]
    assert parsed.thread_prompt_update == "Chuyên gia ăn uống gia đình, ưu tiên mẹ bầu và HSD tủ lạnh."


def test_agent_output_accepts_task_status_update() -> None:
    payload = {
        "reply": "vâng anh chị, Gia đánh dấu xong rồi nha",
        "memory": {"profile_updates": [], "recent_updates": []},
        "reminder": None,
        "agent_task": None,
        "recurring_agent_task": None,
        "rules_updates": [],
        "fridge_updates": [],
        "daily_meal_update": None,
        "daily_meal_updates": [],
        "task_status_update": {
            "target_text": None,
            "completion_status": "done",
            "note": None,
        },
    }

    parsed = AgentOutput.model_validate(payload)

    assert parsed.task_status_update is not None
    assert parsed.task_status_update.completion_status == "done"


def test_old_reminder_record_defaults_completion_status() -> None:
    parsed = ReminderRecord.model_validate(
        {
            "id": "rem-1",
            "kind": "reminder",
            "text": "mua sữa",
            "time": "2026-04-23T09:00:00+07:00",
            "conversation_id": "chat-1",
            "conversation_type": "user",
            "status": "sent",
            "attempts": 1,
            "created_at": "2026-04-23T08:00:00+07:00",
            "sent_at": "2026-04-23T09:00:00+07:00",
            "last_error": None,
        }
    )

    assert parsed.completion_status == "open"
    assert parsed.completed_at is None
    assert parsed.repeat_interval_minutes is None
    assert parsed.next_run_at is None
    assert parsed.thread_key is None


def test_extract_response_text_from_responses_payload() -> None:
    expected = {
        "reply": "ok",
        "memory": {"profile_updates": [], "recent_updates": []},
        "reminder": None,
    }
    payload = {
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(expected, ensure_ascii=False),
                    }
                ],
            }
        ]
    }

    assert json.loads(_extract_response_text(payload)) == expected


def test_gpt5_models_skip_temperature() -> None:
    assert _model_supports_temperature("gpt-5-mini") is False
    assert _model_supports_temperature("gpt-4.1-mini") is True
