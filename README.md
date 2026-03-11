# AI Slack Assistant

A production-grade AI assistant that lives in Slack. Receives messages via webhook, reasons through a multi-step agent loop, calls tools, and responds in threads.

Built as a showcase of real-world AI agent architecture — the same patterns powering a 57-tool production system.

---

## Architecture

```
Slack (Events API)
        |
        v
+------------------+     +-------------------+
|   app.py         |     |   agent.py        |
|   FastAPI        |---->|   Agent Loop      |
|   - /slack/events|     |   (max 8 steps)   |
|   - /health      |     |                   |
|   - /admin       |     |  1. System prompt |
+------------------+     |  2. Load context  |
        ^                 |  3. Call LLM      |
        |                 |  4. Tool call?    |---+
+------------------+     |  5. Execute tool  |   |
|   memory.py      |     |  6. Loop or reply |   |
|   Per-channel    |<----|                   |   |
|   JSON history   |     +-------------------+   |
|   Sliding window |                             |
+------------------+     +-------------------+   |
                         |   tools.py        |<--+
                         |   8 tools         |
                         |   Registry pattern|
                         |                   |
                         |   web_search      |
                         |   google_calendar |
                         |   send_email      |
                         |   vault_search    |
                         |   vault_save      |
                         |   create_reminder |
                         |   get_weather     |
                         |   summarize_url   |
                         +-------------------+
```

## Features

- **Multi-step agent loop** — the LLM can chain up to 8 tool calls per request, gathering information before composing a final answer
- **8 built-in tools** — web search, calendar, email, knowledge base, reminders, weather, URL summarization
- **Conversation memory** — per-channel history persisted to JSON, sliding window for LLM context
- **Threaded replies** — all responses go in the message thread
- **Slack signature verification** — validates `X-Slack-Signature` headers
- **Event deduplication** — handles Slack retries gracefully
- **Admin dashboard** — dark-themed UI showing recent conversations and tool usage stats
- **Demo mode** — runs fully offline without API keys (mock tool responses, simulated routing)
- **Async throughout** — FastAPI + httpx + asyncio for high throughput

## Quick Start

```bash
# Clone and install
cd ai-slack-assistant
pip install -r requirements.txt

# Run in demo mode (no API keys needed)
python app.py
```

The server starts on `http://localhost:8000`:
- `GET /health` — JSON health check
- `GET /admin` — admin dashboard
- `POST /slack/events` — Slack webhook endpoint

## Configuration

All settings come from environment variables. Create a `.env` file or export them:

| Variable | Required | Description |
|---|---|---|
| `SLACK_BOT_TOKEN` | For Slack replies | `xoxb-...` bot token |
| `SLACK_SIGNING_SECRET` | For signature verification | From Slack app settings |
| `AI_API_KEY` | For real LLM calls | OpenAI / compatible API key |
| `AI_API_URL` | No | LLM endpoint (default: OpenAI) |
| `AI_MODEL` | No | Model name (default: `gpt-4o`) |
| `WEATHER_API_KEY` | For real weather | OpenWeatherMap API key |
| `SERPER_API_KEY` | For real web search | serper.dev API key |
| `SMTP_USER` / `SMTP_PASSWORD` | For real email | Gmail app password |
| `GOOGLE_CALENDAR_CREDENTIALS` | For real calendar | Path to service account JSON |
| `AGENT_MAX_STEPS` | No | Max tool calls per request (default: 8) |
| `MEMORY_WINDOW` | No | Messages in LLM context (default: 20) |

## Slack App Setup

1. Create a Slack app at [api.slack.com/apps](https://api.slack.com/apps)
2. Enable **Event Subscriptions** with request URL: `https://your-domain.com/slack/events`
3. Subscribe to bot events: `message.channels`, `message.groups`, `message.im`, `app_mention`
4. Add OAuth scopes: `chat:write`, `app_mentions:read`, `channels:history`, `groups:history`, `im:history`
5. Install to workspace, copy the bot token

## Tool Details

| Tool | Description | API Required |
|---|---|---|
| `web_search` | Search the web via Serper.dev | Optional (has mock) |
| `google_calendar` | List/create calendar events | Optional (has mock) |
| `send_email` | Send via SMTP | Optional (has mock) |
| `vault_search` | Search local knowledge base files | No |
| `vault_save` | Save notes to knowledge base | No |
| `create_reminder` | Schedule a reminder message | No |
| `get_weather` | Current weather for a city | Optional (has mock) |
| `summarize_url` | Fetch URL + AI summarize | Partial (fetches without key) |

## Project Structure

```
ai-slack-assistant/
  app.py              FastAPI app, Slack handler, admin dashboard
  agent.py            Agent loop: plan -> tool calls -> respond
  tools.py            8 tools with registry pattern
  memory.py           Per-channel conversation persistence
  config.py           Settings from environment variables
  templates/
    admin.html        Dark-themed admin dashboard
  data/
    conversations/    Per-channel JSON history files
    vault/            Knowledge base files
  requirements.txt
  README.md
```

## Production Deployment

```bash
# With environment variables set:
uvicorn app:app --host 0.0.0.0 --port 8000 --workers 1

# Or with PM2:
pm2 start "uvicorn app:app --host 0.0.0.0 --port 8000" --name ai-slack-assistant

# Behind nginx:
# proxy_pass http://127.0.0.1:8000;
```

## Design Decisions

- **Registry pattern for tools** — each tool is a decorated async function; adding a new tool is one function + one decorator
- **Sliding window memory** — keeps full history on disk but only sends the last N messages to the LLM, balancing context quality with token cost
- **Background processing** — Slack requires a 200 response within 3 seconds, so agent work runs as a background task
- **Demo mode** — every external dependency has a mock fallback, so the project runs and demonstrates its architecture with zero configuration
- **Single-file-per-concern** — five focused Python files instead of one monolith; each under 300 lines

---

Built by [Workitu Tech](https://workitu.com) — the same architecture powering a production AI Brain with 57 tools, 12 background threads, and multi-LLM routing.
