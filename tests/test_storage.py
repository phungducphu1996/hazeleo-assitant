from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.schemas import DailyMealUpdate, FridgeItemUpdate
from app.storage import FileStore, compute_next_daily_run


def test_profile_updates_are_deduplicated(tmp_path) -> None:
    store = FileStore(tmp_path)

    first = store.append_profile_updates(["Ngọc bị ốm nghén"])
    second = store.append_profile_updates(["ngoc bi om nghen"])

    assert first == ["Ngọc bị ốm nghén"]
    assert second == []
    assert store.read_profile().count("- Ngọc bị ốm nghén") == 1


def test_rules_updates_are_deduplicated(tmp_path) -> None:
    store = FileStore(tmp_path)

    first = store.append_rules_updates(["Gia mở đầu bằng vâng anh chị"])
    second = store.append_rules_updates(["gia mo dau bang vang anh chi"])

    assert first == ["Gia mở đầu bằng vâng anh chị"]
    assert second == []
    assert "Gia mở đầu bằng vâng anh chị" in store.read_rules()


def test_recent_updates_are_append_only(tmp_path) -> None:
    store = FileStore(tmp_path)
    now = datetime(2026, 4, 22, 9, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

    for index in range(25):
        store.append_recent_updates(
            [f"entry {index}"],
            now=now + timedelta(minutes=index),
            conversation_id="conv-1",
            from_uid="user-1",
        )

    recent = store.list_recent()
    assert len(recent) == 25
    assert recent[0].text == "entry 0"
    assert recent[-1].text == "entry 24"


def test_reminder_lifecycle_can_be_updated(tmp_path) -> None:
    store = FileStore(tmp_path)
    now = datetime(2026, 4, 22, 9, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
    record = store.add_reminder(
        text="mua sữa",
        reminder_time=now + timedelta(minutes=1),
        now=now,
        conversation_id="conv-1",
        conversation_type="user",
        thread_id="topic-1",
    )

    updated = store.update_reminder(record.id, status="sent", sent_at=(now + timedelta(minutes=1)).isoformat())

    assert updated is not None
    assert updated.status == "sent"
    assert updated.thread_id == "topic-1"
    assert store.list_reminders()[0].status == "sent"


def test_sent_reminder_can_be_marked_done_by_recent_match(tmp_path) -> None:
    store = FileStore(tmp_path)
    now = datetime(2026, 4, 22, 9, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
    record = store.add_reminder(
        text="mua sữa",
        reminder_time=now - timedelta(minutes=1),
        now=now - timedelta(hours=1),
        conversation_id="conv-1",
        conversation_type="user",
    )
    store.update_reminder(record.id, status="sent", sent_at=now.isoformat())

    updated = store.complete_matching_reminder(
        conversation_id="conv-1",
        target_text=None,
        completion_status="done",
        now=now + timedelta(minutes=5),
        completed_by="user-1",
        note="đã mua",
    )

    assert updated is not None
    assert updated.completion_status == "done"
    assert updated.completed_by == "user-1"
    assert updated.completion_note == "đã mua"


def test_completion_does_not_cross_conversation(tmp_path) -> None:
    store = FileStore(tmp_path)
    now = datetime(2026, 4, 22, 9, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
    record = store.add_reminder(
        text="mua sữa",
        reminder_time=now - timedelta(minutes=1),
        now=now - timedelta(hours=1),
        conversation_id="conv-1",
        conversation_type="user",
    )
    store.update_reminder(record.id, status="sent", sent_at=now.isoformat())

    updated = store.complete_matching_reminder(
        conversation_id="conv-2",
        target_text="mua sữa",
        completion_status="done",
        now=now + timedelta(minutes=5),
        completed_by="user-2",
        note=None,
    )

    assert updated is None
    assert store.list_reminders()[0].completion_status == "open"


def test_pending_reminder_can_be_canceled_by_target_text(tmp_path) -> None:
    store = FileStore(tmp_path)
    now = datetime(2026, 4, 22, 9, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
    store.add_reminder(
        text="mua sữa",
        reminder_time=now + timedelta(hours=1),
        now=now,
        conversation_id="conv-1",
        conversation_type="user",
    )

    updated = store.complete_matching_reminder(
        conversation_id="conv-1",
        target_text="nhắc mua sữa",
        completion_status="canceled",
        now=now + timedelta(minutes=5),
        completed_by="user-1",
        note="không cần nữa",
    )

    assert updated is not None
    assert updated.completion_status == "canceled"
    assert updated.status == "pending"


def test_agent_task_can_be_stored(tmp_path) -> None:
    store = FileStore(tmp_path)
    now = datetime(2026, 4, 22, 9, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

    record = store.add_agent_task(
        title="Gợi ý ăn trưa",
        prompt="9h gợi ý đồ ăn trưa",
        run_at=now + timedelta(hours=1),
        now=now,
        conversation_id="chat-1",
        conversation_type="user",
    )

    assert record.kind == "agent_task"
    assert record.prompt == "9h gợi ý đồ ăn trưa"
    assert store.list_reminders()[0].kind == "agent_task"


def test_recurring_agent_task_can_be_stored(tmp_path) -> None:
    store = FileStore(tmp_path)
    now = datetime(2026, 4, 22, 8, 30, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

    record = store.add_recurring_agent_task(
        title="Gợi ý ăn trưa",
        prompt="Gợi ý đồ ăn trưa đơn giản",
        local_time="09:00",
        timezone="Asia/Ho_Chi_Minh",
        now=now,
        conversation_id="chat-1",
        conversation_type="user",
    )

    assert record.frequency == "daily"
    assert record.next_run_at == "2026-04-22T09:00:00+07:00"
    assert store.list_recurring_tasks()[0].title == "Gợi ý ăn trưa"


def test_compute_next_daily_run_moves_to_tomorrow_when_time_passed() -> None:
    now = datetime(2026, 4, 22, 10, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

    assert compute_next_daily_run(local_time="09:00", now=now).isoformat() == "2026-04-23T09:00:00+07:00"


def test_fridge_updates_are_structured_and_replace_by_name(tmp_path) -> None:
    store = FileStore(tmp_path)
    now = datetime(2026, 4, 22, 9, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

    store.apply_fridge_updates(
        [
            FridgeItemUpdate(name="Trứng", quantity_note="5 quả", status="available", note=None),
            FridgeItemUpdate(name="rau cải", quantity_note="1 bó", status="available", note=None),
        ],
        now=now,
    )
    store.apply_fridge_updates(
        [FridgeItemUpdate(name="trung", quantity_note="3 quả", status="low", note="đã dùng bớt")],
        now=now + timedelta(hours=1),
    )

    items = store.list_fridge_items()
    assert len(items) == 2
    egg = next(item for item in items if item.name == "trung")
    assert egg.quantity_note == "3 quả"
    assert egg.status == "low"
    assert egg.category == "egg"
    assert egg.compartment == "cool"


def test_finished_fridge_item_is_removed(tmp_path) -> None:
    store = FileStore(tmp_path)
    now = datetime(2026, 4, 22, 9, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

    store.apply_fridge_updates([FridgeItemUpdate(name="sữa", quantity_note="1 hộp")], now=now)
    store.apply_fridge_updates([FridgeItemUpdate(name="sua", status="finished")], now=now + timedelta(hours=1))

    assert store.list_fridge_items() == []


def test_old_fridge_records_are_backward_compatible(tmp_path) -> None:
    store = FileStore(tmp_path)
    store.ensure_files()
    store.fridge_path.write_text(
        '[{"name":"sữa","quantity_note":"1 hộp","status":"available","note":null,"updated_at":"2026-04-22T09:00:00+07:00"}]',
        encoding="utf-8",
    )

    item = store.list_fridge_items()[0]

    assert item.name == "sữa"
    assert item.category == "other"
    assert item.compartment is None
    assert item.added_at is None
    assert item.expires_at is None
    assert item.expiry_source == "unknown"


def test_fridge_default_hsd_by_category_and_compartment(tmp_path) -> None:
    store = FileStore(tmp_path)
    now = datetime(2026, 4, 22, 9, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

    store.apply_fridge_updates(
        [
            FridgeItemUpdate(name="rau cải", category="vegetable", compartment="cool"),
            FridgeItemUpdate(name="thịt bò", category="meat", compartment="cool"),
            FridgeItemUpdate(name="tôm", category="seafood", compartment="cool"),
            FridgeItemUpdate(name="cá hồi", category="seafood", compartment="freezer"),
        ],
        now=now,
    )

    by_name = {item.name: item for item in store.list_fridge_items()}
    assert by_name["rau cải"].expires_at == (now + timedelta(days=7)).isoformat()
    assert by_name["thịt bò"].expires_at == (now + timedelta(days=2)).isoformat()
    assert by_name["tôm"].expires_at == (now + timedelta(days=1)).isoformat()
    assert by_name["cá hồi"].expires_at == (now + timedelta(days=30)).isoformat()
    assert by_name["rau cải"].expiry_source == "default"
    assert by_name["cá hồi"].expiry_source == "default"


def test_fridge_explicit_hsd_overrides_default(tmp_path) -> None:
    store = FileStore(tmp_path)
    now = datetime(2026, 4, 22, 9, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
    explicit_expiry = now + timedelta(days=5)

    store.apply_fridge_updates(
        [
            FridgeItemUpdate(
                name="thịt bò",
                category="meat",
                compartment="cool",
                expires_at=explicit_expiry.isoformat(),
                expiry_source="explicit",
            )
        ],
        now=now,
    )

    item = store.list_fridge_items()[0]
    assert item.expires_at == explicit_expiry.isoformat()
    assert item.expiry_source == "explicit"


def test_meat_and_seafood_without_compartment_are_not_saved(tmp_path) -> None:
    store = FileStore(tmp_path)
    now = datetime(2026, 4, 22, 9, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

    changed = store.apply_fridge_updates(
        [FridgeItemUpdate(name="thịt bò", quantity_note="500g", category="meat")],
        now=now,
    )

    assert changed == []
    assert store.list_fridge_items() == []


def test_fridge_warnings_include_expired_and_expiring_items(tmp_path) -> None:
    store = FileStore(tmp_path)
    now = datetime(2026, 4, 22, 9, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
    store.apply_fridge_updates(
        [
            FridgeItemUpdate(name="rau cũ", category="vegetable", compartment="cool", added_at=(now - timedelta(days=8)).isoformat()),
            FridgeItemUpdate(name="thịt bò", category="meat", compartment="cool", added_at=(now - timedelta(days=1)).isoformat()),
            FridgeItemUpdate(name="cá hồi", category="seafood", compartment="freezer"),
        ],
        now=now,
    )

    warnings = store.fridge_warnings(now=now)

    assert [item["name"] for item in warnings] == ["rau cũ", "thịt bò"]
    assert warnings[0]["level"] == "expired"
    assert warnings[1]["level"] == "expires_soon"


def test_daily_meal_update_is_saved_and_pruned(tmp_path) -> None:
    store = FileStore(tmp_path, daily_meal_retention_days=14)
    now = datetime(2026, 4, 23, 9, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

    store.apply_daily_meal_update(
        DailyMealUpdate(
            date="2026-04-09",
            meal_slot="lunch",
            suggestions=["món cũ"],
            selected=None,
            notes=None,
        ),
        now=now,
    )
    saved = store.apply_daily_meal_update(
        DailyMealUpdate(
            date="2026-04-23",
            meal_slot="dinner",
            suggestions=["cháo thịt bằm", "canh rau trứng"],
            selected="cháo thịt bằm",
            notes="nhẹ bụng",
        ),
        now=now,
    )

    assert saved.meals["dinner"].selected == "cháo thịt bằm"
    meals = store.list_daily_meals()
    assert [item.date for item in meals] == ["2026-04-23"]


def test_daily_meal_actual_items_are_saved_without_losing_suggestions(tmp_path) -> None:
    store = FileStore(tmp_path, daily_meal_retention_days=14)
    now = datetime(2026, 4, 25, 10, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

    store.apply_daily_meal_update(
        DailyMealUpdate(
            date="2026-04-25",
            meal_slot="lunch",
            suggestions=["trứng hấp thịt", "thịt kho trứng"],
            selected=None,
            notes="gợi ý cũ",
        ),
        now=now,
    )
    saved = store.apply_daily_meal_update(
        DailyMealUpdate(
            date="2026-04-25",
            meal_slot="lunch",
            suggestions=[],
            actual_items=["canh cải cúc", "cải ngồng xào tỏi", "thịt heo luộc"],
            selected=None,
            notes="thực đơn trưa đã lưu",
        ),
        now=now + timedelta(minutes=10),
    )

    slot = saved.meals["lunch"]
    assert slot.suggestions == ["trứng hấp thịt", "thịt kho trứng"]
    assert slot.actual_items == ["canh cải cúc", "cải ngồng xào tỏi", "thịt heo luộc"]
    assert slot.notes == "thực đơn trưa đã lưu"


def test_conversation_turns_keep_five_day_history_on_disk(tmp_path) -> None:
    store = FileStore(tmp_path, conversation_turn_retention_days=5)
    now = datetime(2026, 4, 22, 9, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

    store.append_conversation_turn(
        now=now - timedelta(days=6),
        conversation_id="chat-1",
        from_uid="user-1",
        role="user",
        text="old turn",
    )
    for index in range(12):
        store.append_conversation_turn(
            now=now - timedelta(hours=2) + timedelta(minutes=index),
            conversation_id="chat-1",
            from_uid="user-1",
            role="user" if index % 2 == 0 else "assistant",
            text=f"turn {index}",
        )

    turns = store.list_conversation_turns("chat-1", limit=30)
    assert len(turns) == 12
    assert turns[0].text == "turn 0"
    assert turns[-1].text == "turn 11"
    assert all(turn.text != "old turn" for turn in turns)


def test_conversation_turns_context_limit_is_applied_on_read(tmp_path) -> None:
    store = FileStore(tmp_path, conversation_turn_retention_days=5)
    now = datetime(2026, 4, 22, 9, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

    for index in range(35):
        store.append_conversation_turn(
            now=now + timedelta(minutes=index),
            conversation_id="chat-1",
            from_uid="user-1",
            role="user",
            text=f"turn {index}",
        )

    turns = store.list_conversation_turns("chat-1", limit=30)
    assert len(turns) == 30
    assert turns[0].text == "turn 5"
    assert turns[-1].text == "turn 34"
