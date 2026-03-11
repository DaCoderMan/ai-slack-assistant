"""
FastAPI application — Slack event handler, health check, and admin dashboard.

Endpoints:
  POST /slack/events   — Slack Events API (message, app_mention)
  GET  /health         — Health check with uptime and stats
  GET  /admin          — Dark-themed admin dashboard
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.background import BackgroundTask
import httpx
from jinja2 import Environment, FileSystemLoader

from config import settings
from agent import run_agent
from memory import memory
from tools import get_usage_stats, tool_usage_stats

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("app")

# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

START_TIME = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("AI Slack Assistant starting on %s:%s", settings.host, settings.port)
    log.info("Agent max steps: %d | Memory window: %d", settings.agent_max_steps, settings.memory_window)
    log.info("AI endpoint: %s (model: %s)", settings.ai_api_url, settings.ai_model)
    if not settings.ai_api_key:
        log.warning("No AI_API_KEY set — running in DEMO mode (no real LLM calls)")
    if not settings.slack_bot_token:
        log.warning("No SLACK_BOT_TOKEN set — Slack replies will be skipped")
    yield
    log.info("Shutting down.")


app = FastAPI(
    title="AI Slack Assistant",
    version="1.0.0",
    lifespan=lifespan,
)

# Jinja2 for the admin template
jinja_env = Environment(loader=FileSystemLoader("templates"), autoescape=True)

# Track which message timestamps we've already processed (dedup)
_seen_events: set[str] = set()


# ---------------------------------------------------------------------------
# Slack signature verification
# ---------------------------------------------------------------------------

def verify_slack_signature(
    body: bytes,
    timestamp: str,
    signature: str,
) -> bool:
    """Verify the X-Slack-Signature header."""
    if not settings.slack_signing_secret:
        return True  # skip in dev
    basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
    computed = "v0=" + hmac.new(
        settings.slack_signing_secret.encode(),
        basestring.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(computed, signature)


# ---------------------------------------------------------------------------
# POST /slack/events
# ---------------------------------------------------------------------------

@app.post("/slack/events")
async def slack_events(request: Request):
    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    if not verify_slack_signature(body, timestamp, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = json.loads(body)

    # Handle Slack URL verification challenge
    if payload.get("type") == "url_verification":
        return JSONResponse({"challenge": payload["challenge"]})

    event = payload.get("event", {})
    event_type = event.get("type", "")

    # Only handle messages and app_mentions
    if event_type not in ("message", "app_mention"):
        return JSONResponse({"ok": True})

    # Ignore bot messages, edits, and deletes
    if event.get("bot_id") or event.get("subtype"):
        return JSONResponse({"ok": True})

    # Dedup — Slack can retry events
    event_id = payload.get("event_id", "")
    if event_id in _seen_events:
        return JSONResponse({"ok": True})
    _seen_events.add(event_id)
    # Keep set bounded
    if len(_seen_events) > 5000:
        _seen_events.clear()

    text = event.get("text", "").strip()
    channel = event.get("channel", "")
    user_id = event.get("user", "")
    thread_ts = event.get("thread_ts") or event.get("ts", "")

    if not text or not channel:
        return JSONResponse({"ok": True})

    # Strip bot mention if present (e.g., "<@U12345> what's the weather")
    import re
    text = re.sub(r"<@[\w]+>\s*", "", text).strip()

    # Process in background so Slack gets a 200 within 3 seconds
    task = BackgroundTask(_handle_message, text, channel, user_id, thread_ts)
    return JSONResponse({"ok": True}, background=task)


async def _handle_message(text: str, channel: str, user_id: str, thread_ts: str):
    """Run the agent and post the response back to Slack."""
    try:
        response = await run_agent(text, channel, user_id, thread_ts)
    except Exception as exc:
        log.exception("Agent error: %s", exc)
        response = f"Something went wrong: {exc}"

    await _post_to_slack(channel, response, thread_ts)


async def _post_to_slack(channel: str, text: str, thread_ts: str = ""):
    """Send a message (threaded) to Slack."""
    if not settings.slack_bot_token:
        log.info("Slack reply (no token, logged only):\n  channel=%s\n  %s", channel, text[:200])
        return

    payload = {
        "channel": channel,
        "text": text,
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {settings.slack_bot_token}"},
            json=payload,
        )
        data = resp.json()
        if not data.get("ok"):
            log.error("Slack API error: %s", data.get("error", "unknown"))


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    uptime_seconds = int(time.time() - START_TIME)
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    return {
        "status": "healthy",
        "uptime": f"{hours}h {minutes}m {seconds}s",
        "uptime_seconds": uptime_seconds,
        "ai_model": settings.ai_model,
        "demo_mode": not bool(settings.ai_api_key),
        "channels_active": len(await memory.list_channels()),
        "tool_calls_total": sum(get_usage_stats().values()),
        "tools_available": 8,
    }


# ---------------------------------------------------------------------------
# GET /admin
# ---------------------------------------------------------------------------

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard():
    channels = await memory.list_channels()
    messages_raw = await memory.recent_messages(limit=30)

    # Format timestamps as "Xm ago" / "Xh ago"
    now = time.time()
    messages = []
    for m in messages_raw:
        delta = int(now - m.get("ts", 0))
        if delta < 60:
            time_ago = f"{delta}s ago"
        elif delta < 3600:
            time_ago = f"{delta // 60}m ago"
        elif delta < 86400:
            time_ago = f"{delta // 3600}h ago"
        else:
            time_ago = f"{delta // 86400}d ago"
        m["time_ago"] = time_ago
        messages.append(m)

    stats = get_usage_stats()
    # Sort by count descending
    sorted_stats = dict(sorted(stats.items(), key=lambda x: x[1], reverse=True))
    max_count = max(stats.values()) if stats else 1
    total_calls = sum(stats.values())

    uptime_seconds = int(time.time() - START_TIME)
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    uptime_str = f"{hours}h {minutes}m"

    template = jinja_env.get_template("admin.html")
    html = template.render(
        channels=channels,
        messages=messages,
        tool_stats=sorted_stats,
        max_tool_count=max_count,
        total_tool_calls=total_calls,
        uptime=uptime_str,
    )
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )
