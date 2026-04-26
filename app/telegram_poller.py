from __future__ import annotations

import asyncio
from typing import Any

from app.agent_service import FamilyAssistantService
from app.config import Settings
from app.schemas import TelegramWebhookResponse, ZaloIncomingRequest, ZaloIncomingResponse
from app.telegram_sender import TelegramSender


class TelegramUpdatePoller:
    def __init__(
        self,
        *,
        settings: Settings,
        sender: TelegramSender,
        assistant_service: FamilyAssistantService,
    ) -> None:
        self.settings = settings
        self.sender = sender
        self.assistant_service = assistant_service
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self._offset: int | None = None

    def start(self) -> None:
        if not self.settings.telegram_polling_enabled or not self.sender.configured:
            return
        if self._task is None or self._task.done():
            self._stopped.clear()
            self._task = asyncio.create_task(self._run(), name="telegram-update-poller")

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def process_update(self, update: dict[str, Any]) -> TelegramWebhookResponse:
        incoming = telegram_update_to_incoming(update, allowed_chat_ids=self.settings.telegram_allowed_chat_id_set)
        if incoming is None:
            return TelegramWebhookResponse(ok=True, processed=False)
        result = await self.assistant_service.handle_incoming(incoming, send_reply=True)
        return TelegramWebhookResponse(ok=True, processed=True, reply=result.reply)

    async def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                updates = await self.sender.get_updates(offset=self._offset)
                for update in updates:
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        self._offset = update_id + 1
                    try:
                        await self.process_update(update)
                    except Exception as exc:
                        print("TELEGRAM_UPDATE_PROCESS_ERROR", str(exc))
            except Exception as exc:  # pragma: no cover - keeps background task alive.
                print("TELEGRAM_POLLER_ERROR", str(exc))

            try:
                await asyncio.wait_for(
                    self._stopped.wait(),
                    timeout=max(0.1, self.settings.telegram_poll_interval_seconds),
                )
            except asyncio.TimeoutError:
                continue


def telegram_update_to_incoming(
    update: dict[str, Any],
    *,
    allowed_chat_ids: set[str] | None = None,
) -> ZaloIncomingRequest | None:
    message = update.get("message")
    if not isinstance(message, dict):
        return None

    text = str(message.get("text") or "").strip()
    if not text:
        return None

    chat = message.get("chat")
    if not isinstance(chat, dict):
        return None
    chat_id = str(chat.get("id") or "").strip()
    if not chat_id:
        return None
    if allowed_chat_ids and chat_id not in allowed_chat_ids:
        return None

    sender = message.get("from") if isinstance(message.get("from"), dict) else {}
    chat_type = str(chat.get("type") or "private").strip().lower()
    conversation_type = "group" if chat_type in {"group", "supergroup"} else "user"

    return ZaloIncomingRequest(
        text=text,
        from_uid=str(sender.get("id") or "").strip() or None,
        conversation_id=chat_id,
        conversation_type=conversation_type,
        thread_id=str(message.get("message_thread_id") or "").strip() or None,
        message_id=str(message.get("message_id") or "").strip() or None,
    )
