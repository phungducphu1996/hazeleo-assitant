from __future__ import annotations

from typing import Any

import httpx

from app.config import Settings
from app.schemas import ZaloDeliveryResult


class TelegramSender:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def configured(self) -> bool:
        return bool((self.settings.telegram_bot_token or "").strip())

    def _method_url(self, method: str) -> str:
        token = (self.settings.telegram_bot_token or "").strip()
        return f"{self.settings.normalized_telegram_api_base_url}/bot{token}/{method}"

    async def send_text(
        self,
        *,
        text: str,
        conversation_id: str | None = None,
        conversation_type: str = "user",
    ) -> ZaloDeliveryResult:
        del conversation_type
        if not self.configured:
            return ZaloDeliveryResult(ok=False, error="missing_telegram_bot_token")
        if not conversation_id:
            return ZaloDeliveryResult(ok=False, error="missing_telegram_chat_id")

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(
                    self._method_url("sendMessage"),
                    json={
                        "chat_id": conversation_id,
                        "text": text,
                        "disable_web_page_preview": True,
                    },
                )
            data = response.json() if response.content else {}
            if response.status_code >= 400 or not data.get("ok", False):
                return ZaloDeliveryResult(ok=False, error=str(data))
            result = data.get("result") or {}
            return ZaloDeliveryResult(ok=True, message_id=str(result.get("message_id") or ""))
        except Exception as exc:  # pragma: no cover - network failure shape varies.
            return ZaloDeliveryResult(ok=False, error=str(exc))

    async def get_updates(self, *, offset: int | None) -> list[dict[str, Any]]:
        if not self.configured:
            return []

        payload: dict[str, Any] = {
            "timeout": max(1, self.settings.telegram_poll_timeout_seconds),
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset

        async with httpx.AsyncClient(timeout=self.settings.telegram_poll_timeout_seconds + 10.0) as client:
            response = await client.post(self._method_url("getUpdates"), json=payload)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            return []
        result = data.get("result") or []
        return result if isinstance(result, list) else []
