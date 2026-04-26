from __future__ import annotations

import httpx

from app.config import Settings
from app.schemas import ZaloDeliveryResult


class ZaloSender:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def send_text(
        self,
        *,
        text: str,
        conversation_id: str | None = None,
        conversation_type: str = "user",
        thread_id: str | None = None,
    ) -> ZaloDeliveryResult:
        del thread_id
        headers = {"Content-Type": "application/json"}
        if self.settings.zalo_shared_secret:
            headers["X-Internal-Secret"] = self.settings.zalo_shared_secret

        body = {
            "text": text,
            "conversation_type": conversation_type,
            "event_type": "family_assistant",
        }
        if conversation_id:
            body["conversation_id"] = conversation_id

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(
                    f"{self.settings.normalized_zalo_worker_url}/api/send-text",
                    headers=headers,
                    json=body,
                )
            data = response.json() if response.content else {}
            if response.status_code >= 400:
                return ZaloDeliveryResult(ok=False, error=str(data))
            return ZaloDeliveryResult.model_validate({"ok": True, **data})
        except Exception as exc:  # pragma: no cover - network failure shape varies.
            return ZaloDeliveryResult(ok=False, error=str(exc))
