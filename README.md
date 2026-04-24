# Family Assistant Ver 2

FastAPI backend for a file-based family assistant. The current test-first channel is Telegram; the old Zalo endpoint is still available.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env
```

Set `OPENAI_API_KEY` and make sure the Zalo worker has:

```env
BACKEND_URL=http://127.0.0.1:8030
```

For Telegram testing, create a bot with BotFather and set:

```env
TELEGRAM_BOT_TOKEN=123456:your-token
TELEGRAM_POLLING_ENABLED=true
```

Then run the backend and send a message to the bot. Long polling is built in, so local testing does not need a public webhook URL.

## Run

```bash
.venv/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port 8030
```

Run only one uvicorn worker in production so the in-process reminder poller does not send duplicate reminders.

## Test

```bash
.venv/bin/pytest
```

## Endpoints

- `GET /health`
- `POST /zalo/incoming`
- `POST /telegram/webhook`
- `POST /api/agent/test-message`
- `GET /api/memory`
- `GET /api/reminders`
# hazeleo-assitant
