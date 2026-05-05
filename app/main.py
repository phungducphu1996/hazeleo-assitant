from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request, status

from app.agent_service import FamilyAssistantService
from app.config import Settings, get_settings
from app.openai_client import OpenAIAgentClient
from app.reminders import ReminderPoller
from app.schemas import TelegramWebhookResponse, ZaloIncomingRequest, ZaloIncomingResponse
from app.skylight_client import SKYLIGHT_ALLOWED_TOOLS, SkylightMCPClient, SkylightMCPError
from app.storage import FileStore
from app.telegram_poller import TelegramUpdatePoller
from app.telegram_sender import TelegramSender
from app.zalo_sender import ZaloSender


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    store = FileStore(
        resolved_settings.data_dir,
        conversation_turn_retention_days=resolved_settings.conversation_turn_retention_days,
    )
    zalo_sender = ZaloSender(resolved_settings)
    telegram_sender = TelegramSender(resolved_settings)
    model_client = OpenAIAgentClient(resolved_settings)
    skylight_client = SkylightMCPClient(resolved_settings)
    zalo_assistant_service = FamilyAssistantService(
        settings=resolved_settings,
        store=store,
        model_client=model_client,
        sender=zalo_sender,
        skylight_client=skylight_client,
    )
    telegram_assistant_service = FamilyAssistantService(
        settings=resolved_settings,
        store=store,
        model_client=model_client,
        sender=telegram_sender,
        skylight_client=skylight_client,
    )
    poller = ReminderPoller(
        settings=resolved_settings,
        store=store,
        sender=telegram_sender,
        agent_task_runner=telegram_assistant_service,
    )
    telegram_poller = TelegramUpdatePoller(
        settings=resolved_settings,
        sender=telegram_sender,
        assistant_service=telegram_assistant_service,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store.ensure_files()
        app.state.settings = resolved_settings
        app.state.store = store
        app.state.zalo_sender = zalo_sender
        app.state.telegram_sender = telegram_sender
        app.state.model_client = model_client
        app.state.skylight_client = skylight_client
        app.state.assistant_service = telegram_assistant_service
        app.state.zalo_assistant_service = zalo_assistant_service
        app.state.telegram_assistant_service = telegram_assistant_service
        app.state.reminder_poller = poller
        app.state.telegram_poller = telegram_poller
        poller.start()
        telegram_poller.start()
        try:
            yield
        finally:
            await telegram_poller.stop()
            await poller.stop()

    app = FastAPI(title="Family Assistant Ver 2", lifespan=lifespan)
    app.state.settings = resolved_settings
    app.state.store = store
    app.state.zalo_sender = zalo_sender
    app.state.telegram_sender = telegram_sender
    app.state.model_client = model_client
    app.state.skylight_client = skylight_client
    app.state.assistant_service = telegram_assistant_service
    app.state.zalo_assistant_service = zalo_assistant_service
    app.state.telegram_assistant_service = telegram_assistant_service
    app.state.reminder_poller = poller
    app.state.telegram_poller = telegram_poller

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "service": "family-assistant-ver2",
            "telegram_configured": telegram_sender.configured,
            "telegram_polling_enabled": resolved_settings.telegram_polling_enabled,
            "skylight_configured": skylight_client.configured,
        }

    @app.post("/zalo/incoming", response_model=ZaloIncomingResponse)
    async def zalo_incoming(
        request: Request,
        payload: ZaloIncomingRequest,
        internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
        service: FamilyAssistantService = Depends(get_zalo_assistant_service),
    ) -> ZaloIncomingResponse:
        _require_internal_secret(request.app.state.settings, internal_secret)
        return await service.handle_incoming(payload, send_reply=True)

    @app.post("/telegram/webhook", response_model=TelegramWebhookResponse)
    async def telegram_webhook(
        request: Request,
        payload: dict = Body(...),
        telegram_secret: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
    ) -> TelegramWebhookResponse:
        _require_telegram_secret(request.app.state.settings, telegram_secret)
        return await request.app.state.telegram_poller.process_update(payload)

    @app.post("/api/agent/test-message", response_model=ZaloIncomingResponse)
    async def test_message(
        request: Request,
        payload: dict = Body(...),
        internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
        service: FamilyAssistantService = Depends(get_telegram_assistant_service),
    ) -> ZaloIncomingResponse:
        _require_internal_secret(request.app.state.settings, internal_secret)
        text = str(payload.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="text is required")
        incoming = ZaloIncomingRequest(
            text=text,
            source=str(payload.get("source") or "debug"),
            from_uid=str(payload.get("from_uid") or "debug-user"),
            conversation_id=str(payload.get("conversation_id") or "debug-conversation"),
            conversation_type="group" if payload.get("conversation_type") == "group" else "user",
            thread_id=str(payload.get("thread_id") or "").strip() or None,
        )
        send_reply = bool(payload.get("send", False))
        return await service.handle_incoming(incoming, send_reply=send_reply)

    @app.get("/api/memory")
    async def memory(
        request: Request,
        internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    ) -> dict[str, object]:
        _require_internal_secret(request.app.state.settings, internal_secret)
        return request.app.state.store.snapshot()

    @app.get("/api/food")
    async def food(
        request: Request,
        internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    ) -> dict[str, object]:
        _require_internal_secret(request.app.state.settings, internal_secret)
        return {
            "fridge": [item.model_dump() for item in request.app.state.store.list_fridge_items()],
            "fridge_warnings": request.app.state.store.fridge_warnings(now=datetime.now(request.app.state.settings.timezone)),
            "daily_meals": [item.model_dump() for item in request.app.state.store.list_daily_meals()],
            "food_places": [item.model_dump() for item in request.app.state.store.list_food_places()],
        }

    @app.get("/api/reminders")
    async def reminders(
        request: Request,
        internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    ) -> dict[str, object]:
        _require_internal_secret(request.app.state.settings, internal_secret)
        return {
            "reminders": [item.model_dump() for item in request.app.state.store.list_reminders()],
            "recurring_tasks": [item.model_dump() for item in request.app.state.store.list_recurring_tasks()],
        }

    @app.get("/api/skylight/health")
    async def skylight_health(
        request: Request,
        internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    ) -> dict[str, object]:
        _require_internal_secret(request.app.state.settings, internal_secret)
        try:
            return await request.app.state.skylight_client.health()
        except Exception as exc:
            return {"ok": False, "configured": request.app.state.skylight_client.configured, "error": str(exc)}

    @app.post("/api/skylight/tool")
    async def skylight_tool(
        request: Request,
        payload: dict = Body(...),
        internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    ) -> dict[str, object]:
        _require_internal_secret(request.app.state.settings, internal_secret)
        tool = str(payload.get("tool") or "").strip()
        if tool not in SKYLIGHT_ALLOWED_TOOLS:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Skylight tool.")
        arguments = payload.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="arguments must be an object.")
        try:
            result = await request.app.state.skylight_client.call_tool(tool, arguments)
        except SkylightMCPError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        return {"ok": not bool(result.get("isError")), "tool": tool, "arguments": arguments, "result": result}

    @app.get("/api/threads")
    async def threads(
        request: Request,
        internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    ) -> dict[str, object]:
        _require_internal_secret(request.app.state.settings, internal_secret)
        return {"threads": request.app.state.store.list_threads()}

    @app.get("/api/threads/{thread_key}")
    async def thread_detail(
        thread_key: str,
        request: Request,
        internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    ) -> dict[str, object]:
        _require_internal_secret(request.app.state.settings, internal_secret)
        return request.app.state.store.thread_snapshot(thread_key)

    @app.put("/api/threads/{thread_key}/prompt")
    async def update_thread_prompt(
        thread_key: str,
        request: Request,
        payload: dict = Body(...),
        internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    ) -> dict[str, object]:
        _require_internal_secret(request.app.state.settings, internal_secret)
        saved = request.app.state.store.set_thread_prompt(thread_key, str(payload.get("prompt") or ""))
        if not saved:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="prompt is required.")
        return {"ok": True, "thread": request.app.state.store.thread_snapshot(thread_key)}

    @app.put("/api/threads/{thread_key}/rules")
    async def update_thread_rules(
        thread_key: str,
        request: Request,
        payload: dict = Body(...),
        internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    ) -> dict[str, object]:
        _require_internal_secret(request.app.state.settings, internal_secret)
        raw_rules = payload.get("rules") or payload.get("rule") or payload.get("text")
        rules = raw_rules if isinstance(raw_rules, list) else [str(raw_rules or "")]
        saved = request.app.state.store.append_thread_rules_updates(thread_key, [str(item) for item in rules])
        return {"ok": True, "saved": saved, "thread": request.app.state.store.thread_snapshot(thread_key)}

    @app.post("/api/reminders/{reminder_id}/completion")
    async def update_reminder_completion(
        reminder_id: str,
        request: Request,
        payload: dict = Body(...),
        internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
    ) -> dict[str, object]:
        _require_internal_secret(request.app.state.settings, internal_secret)
        completion_status = str(payload.get("completion_status") or "").strip()
        if completion_status not in {"open", "done", "skipped", "canceled"}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="completion_status must be one of: open, done, skipped, canceled",
            )
        updated = request.app.state.store.update_reminder_completion(
            reminder_id,
            completion_status=completion_status,
            now=datetime.now(request.app.state.settings.timezone),
            completed_by=str(payload.get("completed_by") or "api"),
            note=str(payload.get("note") or "").strip() or None,
        )
        if updated is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reminder not found.")
        return {"ok": True, "reminder": updated.model_dump()}

    return app


def get_zalo_assistant_service(request: Request) -> FamilyAssistantService:
    return request.app.state.zalo_assistant_service


def get_telegram_assistant_service(request: Request) -> FamilyAssistantService:
    return request.app.state.telegram_assistant_service


def _require_internal_secret(settings: Settings, got: str | None) -> None:
    expected = (settings.zalo_shared_secret or "").strip()
    if expected and (got or "").strip() != expected:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid internal secret.")


def _require_telegram_secret(settings: Settings, got: str | None) -> None:
    expected = (settings.telegram_webhook_secret or "").strip()
    if expected and (got or "").strip() != expected:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid Telegram secret.")


app = create_app()
