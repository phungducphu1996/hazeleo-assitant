from __future__ import annotations

import re


def build_thread_key(
    *,
    source: str | None,
    conversation_id: str | None,
    conversation_type: str = "user",
    thread_id: str | None = None,
) -> str | None:
    conversation = _clean_part(conversation_id)
    if not conversation:
        return None
    channel = _clean_part(source) or "unknown"
    if conversation_type == "group":
        topic = _clean_part(thread_id)
        if topic:
            return f"{channel}:{conversation}:topic:{topic}"
        return f"{channel}:{conversation}:main"
    return f"{channel}:{conversation}:private"


def thread_dir_name(thread_key: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", thread_key.strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("._-")
    return cleaned or "unknown_thread"


def _clean_part(value: str | None) -> str:
    return str(value or "").strip()
