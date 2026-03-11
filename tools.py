"""
Tool registry and implementations.

Every tool is an async function decorated with @register_tool. The decorator
collects name, description, and parameter schema so the agent can discover
tools at runtime and the LLM receives a clean function-calling spec.

Eight built-in tools ship out of the box:
  web_search, google_calendar, send_email, vault_search,
  vault_save, create_reminder, get_weather, summarize_url
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
import smtplib
import time
from collections import defaultdict
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Callable, Coroutine

import httpx

from config import settings

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

ToolFunc = Callable[..., Coroutine[Any, Any, str]]

_REGISTRY: dict[str, dict[str, Any]] = {}

# Global usage stats: tool_name -> list of timestamps
tool_usage_stats: dict[str, list[float]] = defaultdict(list)


def register_tool(
    name: str,
    description: str,
    parameters: dict[str, Any],
):
    """Decorator that registers an async tool function."""

    def decorator(func: ToolFunc) -> ToolFunc:
        _REGISTRY[name] = {
            "name": name,
            "description": description,
            "parameters": parameters,
            "func": func,
        }
        return func

    return decorator


def get_tool_schemas() -> list[dict[str, Any]]:
    """Return OpenAI-style function schemas for every registered tool."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            },
        }
        for t in _REGISTRY.values()
    ]


async def execute_tool(name: str, arguments: dict[str, Any]) -> str:
    """Look up a tool by name and run it. Returns the string result."""
    entry = _REGISTRY.get(name)
    if entry is None:
        return f"Error: unknown tool '{name}'"
    tool_usage_stats[name].append(time.time())
    try:
        return await entry["func"](**arguments)
    except Exception as exc:
        return f"Error running {name}: {exc}"


def get_usage_stats() -> dict[str, int]:
    """Return {tool_name: call_count} for the admin dashboard."""
    return {name: len(ts) for name, ts in tool_usage_stats.items()}


# ---------------------------------------------------------------------------
# Tool: web_search
# ---------------------------------------------------------------------------

@register_tool(
    name="web_search",
    description="Search the web for information. Returns top results with titles, snippets, and URLs.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
        },
        "required": ["query"],
    },
)
async def web_search(query: str) -> str:
    """
    Attempts a real search via SerpAPI / Serper if an API key is set,
    otherwise returns a structured mock response.
    """
    serper_key = os.getenv("SERPER_API_KEY", "")
    if serper_key:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": serper_key, "Content-Type": "application/json"},
                json={"q": query, "num": 5},
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("organic", [])[:5]
            lines = []
            for r in results:
                lines.append(f"- **{r['title']}**\n  {r.get('snippet', '')}\n  {r['link']}")
            return "\n".join(lines) or "No results found."

    # Mock fallback
    return (
        f"[Mock Search Results for: {query}]\n"
        f"1. {query} - Wikipedia — Comprehensive overview of the topic.\n"
        f"   https://en.wikipedia.org/wiki/{query.replace(' ', '_')}\n"
        f"2. Latest news on {query} — TechCrunch\n"
        f"   https://techcrunch.com/search/{query.replace(' ', '+')}\n"
        f"3. {query} explained simply — Medium\n"
        f"   https://medium.com/search?q={query.replace(' ', '+')}"
    )


# ---------------------------------------------------------------------------
# Tool: google_calendar
# ---------------------------------------------------------------------------

@register_tool(
    name="google_calendar",
    description=(
        "Interact with Google Calendar. "
        "Actions: 'list' returns upcoming events; 'create' adds a new event."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "create"],
                "description": "'list' or 'create'",
            },
            "days": {
                "type": "integer",
                "description": "For list: how many days ahead to look (default 7)",
            },
            "title": {"type": "string", "description": "For create: event title"},
            "start": {
                "type": "string",
                "description": "For create: ISO datetime start (e.g. 2026-03-15T10:00:00)",
            },
            "end": {
                "type": "string",
                "description": "For create: ISO datetime end",
            },
        },
        "required": ["action"],
    },
)
async def google_calendar(
    action: str,
    days: int = 7,
    title: str = "",
    start: str = "",
    end: str = "",
) -> str:
    creds_path = settings.google_calendar_credentials
    if not creds_path or not Path(creds_path).exists():
        # Demo / mock mode
        if action == "list":
            now = dt.datetime.now()
            events = []
            for i in range(3):
                d = now + dt.timedelta(days=i + 1, hours=10)
                events.append(f"- {d.strftime('%a %b %d %H:%M')} — Sample Meeting #{i+1}")
            return "Upcoming events (demo mode):\n" + "\n".join(events)
        if action == "create":
            return f"Event created (demo mode): '{title}' from {start} to {end}"
        return f"Unknown calendar action: {action}"

    # Real Google Calendar via service account
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    SCOPES = ["https://www.googleapis.com/auth/calendar"]
    creds = service_account.Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    service = await asyncio.to_thread(build, "calendar", "v3", credentials=creds)

    if action == "list":
        now = dt.datetime.utcnow().isoformat() + "Z"
        until = (dt.datetime.utcnow() + dt.timedelta(days=days)).isoformat() + "Z"
        result = await asyncio.to_thread(
            lambda: service.events()
            .list(calendarId="primary", timeMin=now, timeMax=until, singleEvents=True, orderBy="startTime")
            .execute()
        )
        items = result.get("items", [])
        if not items:
            return "No upcoming events."
        lines = []
        for ev in items[:10]:
            s = ev["start"].get("dateTime", ev["start"].get("date"))
            lines.append(f"- {s} — {ev.get('summary', '(no title)')}")
        return "\n".join(lines)

    if action == "create":
        event_body = {
            "summary": title,
            "start": {"dateTime": start, "timeZone": "UTC"},
            "end": {"dateTime": end, "timeZone": "UTC"},
        }
        created = await asyncio.to_thread(
            lambda: service.events().insert(calendarId="primary", body=event_body).execute()
        )
        return f"Event created: {created.get('htmlLink', title)}"

    return f"Unknown calendar action: {action}"


# ---------------------------------------------------------------------------
# Tool: send_email
# ---------------------------------------------------------------------------

@register_tool(
    name="send_email",
    description="Send an email via SMTP.",
    parameters={
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "Recipient email address"},
            "subject": {"type": "string", "description": "Email subject line"},
            "body": {"type": "string", "description": "Plain-text email body"},
        },
        "required": ["to", "subject", "body"],
    },
)
async def send_email(to: str, subject: str, body: str) -> str:
    if not settings.smtp_user or not settings.smtp_password:
        return f"Email sent (demo mode) to {to}: {subject}"

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = settings.smtp_user
    msg["To"] = to

    def _send():
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
            server.starttls()
            server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(msg)

    await asyncio.to_thread(_send)
    return f"Email sent to {to}: {subject}"


# ---------------------------------------------------------------------------
# Tool: vault_search
# ---------------------------------------------------------------------------

@register_tool(
    name="vault_search",
    description="Search the local knowledge base (vault) for notes and documents matching a query.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search terms"},
        },
        "required": ["query"],
    },
)
async def vault_search(query: str) -> str:
    vault = Path(settings.vault_dir)
    if not vault.exists():
        return "Vault directory not found."

    query_lower = query.lower()
    terms = query_lower.split()
    results: list[tuple[int, str, str]] = []

    for fpath in vault.rglob("*"):
        if not fpath.is_file():
            continue
        if fpath.suffix not in (".md", ".txt", ".json", ".yml", ".yaml"):
            continue
        try:
            text = await asyncio.to_thread(fpath.read_text, encoding="utf-8")
        except Exception:
            continue
        score = sum(1 for t in terms if t in text.lower())
        if score > 0:
            # Grab a relevant snippet
            lines = text.splitlines()
            best_line = ""
            best_score = 0
            for line in lines:
                ls = sum(1 for t in terms if t in line.lower())
                if ls > best_score:
                    best_score = ls
                    best_line = line.strip()
            results.append((score, str(fpath.relative_to(vault)), best_line[:200]))

    if not results:
        return f"No vault results for: {query}"

    results.sort(key=lambda x: x[0], reverse=True)
    lines = []
    for score, path, snippet in results[:5]:
        lines.append(f"- **{path}** (relevance {score})\n  {snippet}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool: vault_save
# ---------------------------------------------------------------------------

@register_tool(
    name="vault_save",
    description="Save a note or document to the local knowledge base (vault).",
    parameters={
        "type": "object",
        "properties": {
            "filename": {"type": "string", "description": "File name (e.g. 'meeting-notes.md')"},
            "content": {"type": "string", "description": "Content to save"},
        },
        "required": ["filename", "content"],
    },
)
async def vault_save(filename: str, content: str) -> str:
    vault = Path(settings.vault_dir)
    vault.mkdir(parents=True, exist_ok=True)
    # Sanitize filename
    safe_name = re.sub(r"[^\w\-.]", "_", filename)
    path = vault / safe_name
    await asyncio.to_thread(path.write_text, content, encoding="utf-8")
    return f"Saved to vault: {safe_name} ({len(content)} chars)"


# ---------------------------------------------------------------------------
# Tool: create_reminder
# ---------------------------------------------------------------------------

# In-memory reminder store (in production you'd use a DB or scheduler)
_reminders: list[dict[str, Any]] = []


@register_tool(
    name="create_reminder",
    description="Create a scheduled reminder. The bot will post the reminder in the channel at the specified time.",
    parameters={
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "Reminder text"},
            "when": {
                "type": "string",
                "description": "ISO datetime or relative like 'in 30 minutes', 'tomorrow 9am'",
            },
            "channel": {
                "type": "string",
                "description": "Slack channel ID to post the reminder in (optional, defaults to current)",
            },
        },
        "required": ["message", "when"],
    },
)
async def create_reminder(message: str, when: str, channel: str = "") -> str:
    reminder = {
        "message": message,
        "when": when,
        "channel": channel,
        "created_at": time.time(),
        "fired": False,
    }
    _reminders.append(reminder)
    return f"Reminder set: '{message}' at {when}"


# ---------------------------------------------------------------------------
# Tool: get_weather
# ---------------------------------------------------------------------------

@register_tool(
    name="get_weather",
    description="Get current weather for a city.",
    parameters={
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "City name (e.g. 'Tel Aviv')"},
        },
        "required": ["city"],
    },
)
async def get_weather(city: str) -> str:
    api_key = settings.weather_api_key
    if not api_key:
        # Demo mode with realistic structure
        return (
            f"Weather for {city} (demo mode):\n"
            f"  Temperature: 22C / 72F\n"
            f"  Condition: Partly cloudy\n"
            f"  Humidity: 55%\n"
            f"  Wind: 12 km/h NW"
        )

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            "https://api.openweathermap.org/data/2.5/weather",
            params={"q": city, "appid": api_key, "units": "metric"},
        )
        resp.raise_for_status()
        data = resp.json()

    main = data["main"]
    weather = data["weather"][0]
    wind = data.get("wind", {})
    return (
        f"Weather for {city}:\n"
        f"  Temperature: {main['temp']}C (feels like {main['feels_like']}C)\n"
        f"  Condition: {weather['description'].title()}\n"
        f"  Humidity: {main['humidity']}%\n"
        f"  Wind: {wind.get('speed', '?')} m/s"
    )


# ---------------------------------------------------------------------------
# Tool: summarize_url
# ---------------------------------------------------------------------------

@register_tool(
    name="summarize_url",
    description="Fetch a web page and produce an AI-generated summary of its content.",
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch and summarize"},
        },
        "required": ["url"],
    },
)
async def summarize_url(url: str) -> str:
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        try:
            resp = await client.get(url, headers={"User-Agent": "SlackAssistantBot/1.0"})
            resp.raise_for_status()
            raw_html = resp.text
        except Exception as exc:
            return f"Failed to fetch {url}: {exc}"

    # Strip HTML tags for a rough text extraction
    text = re.sub(r"<script[^>]*>.*?</script>", "", raw_html, flags=re.DOTALL)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text[:6000]  # limit context

    if not settings.ai_api_key:
        # Summarize locally — just return first 500 chars
        return f"Summary of {url} (demo — no AI key):\n{text[:500]}..."

    # Call AI for summary
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            settings.ai_api_url,
            headers={
                "Authorization": f"Bearer {settings.ai_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.ai_model,
                "max_tokens": 600,
                "messages": [
                    {"role": "system", "content": "Summarize the following web page text concisely."},
                    {"role": "user", "content": text},
                ],
            },
        )
        resp.raise_for_status()
        data = resp.json()
        summary = data["choices"][0]["message"]["content"]
    return f"Summary of {url}:\n{summary}"
