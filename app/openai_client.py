from __future__ import annotations

from datetime import datetime
import json
from typing import Any

import httpx
from pydantic import ValidationError

from app.config import Settings
from app.schemas import AGENT_OUTPUT_JSON_SCHEMA, AgentOutput, RecentMemoryEntry, ZaloIncomingRequest


class AgentModelError(RuntimeError):
    """Raised when the model cannot return a valid assistant payload."""


class OpenAIAgentClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def run(
        self,
        *,
        agent_prompt: str,
        profile: str,
        recent: list[RecentMemoryEntry],
        rules_text: str = "",
        thread_key: str | None = None,
        thread_prompt: str = "",
        thread_rules: str = "",
        conversation_turns: list[dict[str, Any]] | None = None,
        fridge: list[dict[str, Any]] | None = None,
        fridge_warnings: list[dict[str, Any]] | None = None,
        daily_meals: list[dict[str, Any]] | None = None,
        food_places: list[dict[str, Any]] | None = None,
        open_tasks: list[dict[str, Any]] | None = None,
        skylight_results: list[dict[str, Any]] | None = None,
        payload: ZaloIncomingRequest,
        now: datetime,
    ) -> AgentOutput:
        api_key = (self.settings.openai_api_key or "").strip()
        if not api_key:
            raise AgentModelError("missing_openai_api_key")

        context = {
            "current_time": now.isoformat(),
            "timezone": self.settings.app_timezone,
            "profile_md": profile,
            "rules_md": rules_text,
            "thread_key": thread_key,
            "thread_prompt_md": thread_prompt,
            "thread_rules_md": thread_rules,
            "recent_memory": [item.model_dump() for item in recent[-20:]],
            "conversation_turns": conversation_turns or [],
            "fridge": fridge or [],
            "fridge_warnings": fridge_warnings or [],
            "daily_meals": daily_meals or [],
            "food_places": food_places or [],
            "open_tasks": open_tasks or [],
            "skylight_results": skylight_results or [],
            "incoming": payload.model_dump(),
            "rules": [
                "Return JSON only and follow the provided schema.",
                "Use profile_md, rules_md, thread_prompt_md, thread_rules_md, recent_memory, conversation_turns, fridge, daily_meals, food_places, and open_tasks as context.",
                "Global profile/rules/fridge/meals/food_places/tasks are shared family context; thread_prompt_md and thread_rules_md customize the current thread.",
                "Use open_tasks to verify currently active tasks; use conversation_turns to understand pending requests, short follow-ups, quoted messages, and recent choices.",
                "Do not invent memory facts, active tasks, fridge items, food places, or meal history.",
                "When reminder time or frequency is missing, obey rules_md/thread_rules_md defaults before asking a follow-up.",
                "Use structured fields whenever the user asks to remember, schedule, update fridge/meal/food place, or mark tasks done.",
                "Use skylight_actions only when the user wants to read or change Skylight Calendar tasks, events, or meals. If skylight_results are present, answer from those results and normally return skylight_actions as an empty array.",
            ],
        }

        request_payload = {
            "model": self.settings.openai_model,
            "input": [
                {"role": "system", "content": agent_prompt},
                {
                    "role": "system",
                    "content": "Runtime context JSON:\n" + json.dumps(context, ensure_ascii=False),
                },
                {"role": "user", "content": payload.text},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "family_assistant_response",
                    "strict": True,
                    "schema": AGENT_OUTPUT_JSON_SCHEMA,
                }
            },
        }
        if _model_supports_temperature(self.settings.openai_model):
            request_payload["temperature"] = self.settings.openai_temperature

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=max(1.0, self.settings.openai_timeout_seconds)) as client:
                response = await _post_response(
                    client=client,
                    url=f"{self.settings.normalized_openai_base_url}/responses",
                    headers=headers,
                    payload=request_payload,
                )
                if response.status_code >= 400 and "temperature" in request_payload and _mentions_temperature(response.text):
                    retry_payload = dict(request_payload)
                    retry_payload.pop("temperature", None)
                    print("OPENAI_RETRY_WITHOUT_TEMPERATURE", {"model": self.settings.openai_model})
                    response = await _post_response(
                        client=client,
                        url=f"{self.settings.normalized_openai_base_url}/responses",
                        headers=headers,
                        payload=retry_payload,
                    )
        except httpx.HTTPError as exc:
            raise AgentModelError(f"openai_request_error: {type(exc).__name__}: {exc}") from exc

        if response.status_code >= 400:
            raise AgentModelError(f"openai_http_{response.status_code}: {response.text[:500]}")

        try:
            raw_payload = response.json()
        except ValueError as exc:
            raise AgentModelError(f"invalid_openai_response_json: {exc}") from exc
        text = _extract_response_text(raw_payload)
        if not text:
            raise AgentModelError("empty_openai_response")

        try:
            parsed = json.loads(text)
            return AgentOutput.model_validate(parsed)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise AgentModelError(f"invalid_agent_json: {exc}") from exc


def _extract_response_text(payload: dict[str, Any]) -> str:
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    parts: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts).strip()


async def _post_response(
    *,
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> httpx.Response:
    print(
        "OPENAI_REQUEST",
        {
            "model": payload.get("model"),
            "has_temperature": "temperature" in payload,
            "endpoint": url,
        },
    )
    response = await client.post(url, headers=headers, json=payload)
    print("OPENAI_RESPONSE", {"status_code": response.status_code})
    return response


def _model_supports_temperature(model: str) -> bool:
    normalized = model.strip().lower()
    unsupported_prefixes = ("gpt-5", "o1", "o3", "o4")
    return not normalized.startswith(unsupported_prefixes)


def _mentions_temperature(text: str) -> bool:
    return "temperature" in text.lower()
