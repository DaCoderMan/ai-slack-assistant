"""
Agent loop — the core reasoning engine.

Flow:
  1. Build system prompt with available tools
  2. Load conversation context from memory
  3. Send to LLM with function-calling enabled
  4. If LLM calls a tool -> execute it -> append result -> loop (up to N steps)
  5. When LLM returns a text response -> deliver to Slack

Supports multi-step reasoning: the agent can chain multiple tool calls
to gather information before composing a final answer.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from config import settings
from memory import Message, memory
from tools import execute_tool, get_tool_schemas

log = logging.getLogger("agent")

SYSTEM_PROMPT = """\
You are an intelligent Slack assistant with access to the tools listed below.
You help users by searching the web, managing calendars, sending emails,
searching and saving notes, setting reminders, checking weather, and
summarizing web pages.

Guidelines:
- Think step-by-step. If the user's request needs multiple pieces of information,
  call tools in sequence and synthesize the results.
- Be concise but thorough. Slack messages should be scannable.
- Use markdown formatting sparingly — Slack supports *bold*, _italic_, and ```code```.
- If a tool fails, explain what happened and suggest an alternative.
- Never fabricate information. If you don't know, say so or search for it.
- When you have enough information, respond directly to the user.
"""


async def run_agent(
    user_message: str,
    channel: str,
    user_id: str = "",
    thread_ts: str = "",
) -> str:
    """
    Execute the full agent loop and return the final text response.

    Args:
        user_message: The user's message text.
        channel: Slack channel ID (used for memory keying).
        user_id: Slack user ID.
        thread_ts: Thread timestamp for threaded replies.

    Returns:
        The agent's final text response.
    """
    # 1. Persist incoming message
    await memory.add(
        channel,
        Message(role="user", content=user_message, user_id=user_id),
    )

    # 2. Build messages array
    context = await memory.get_context(channel)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *context,
    ]

    tools = get_tool_schemas()
    steps = 0
    max_steps = settings.agent_max_steps

    while steps < max_steps:
        steps += 1
        log.info("Agent step %d/%d (channel=%s)", steps, max_steps, channel)

        # 3. Call LLM
        llm_response = await _call_llm(messages, tools)

        # 4. Check for tool calls
        tool_calls = llm_response.get("tool_calls") or []
        content = llm_response.get("content") or ""

        if not tool_calls:
            # No tool calls — this is the final response
            if content:
                await memory.add(
                    channel,
                    Message(role="assistant", content=content),
                )
            return content or "(No response generated)"

        # 5. Execute each tool call and feed results back
        # Append the assistant message (with tool_calls) to context
        messages.append(
            {
                "role": "assistant",
                "content": content or None,
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["function"]["name"],
                            "arguments": tc["function"]["arguments"],
                        },
                    }
                    for tc in tool_calls
                ],
            }
        )

        for tc in tool_calls:
            func_name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError:
                args = {}

            log.info("  Tool call: %s(%s)", func_name, json.dumps(args)[:200])
            result = await execute_tool(func_name, args)
            log.info("  Tool result (%s): %s", func_name, result[:200])

            # Record tool use in memory
            await memory.add(
                channel,
                Message(role="tool", content=result, tool_name=func_name),
            )

            # Append tool response for the LLM
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                }
            )

    # Exhausted steps — return whatever we have
    final = "I've done the research but hit my step limit. Here's what I found so far."
    await memory.add(channel, Message(role="assistant", content=final))
    return final


async def _call_llm(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Call the configured LLM endpoint with function-calling support.
    Returns the assistant message dict (with optional tool_calls).
    """
    if not settings.ai_api_key:
        # Demo mode: simulate a simple response without real LLM
        return _demo_response(messages)

    payload: dict[str, Any] = {
        "model": settings.ai_model,
        "messages": messages,
        "max_tokens": settings.ai_max_tokens,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            settings.ai_api_url,
            headers={
                "Authorization": f"Bearer {settings.ai_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

    choice = data["choices"][0]["message"]
    return {
        "content": choice.get("content", ""),
        "tool_calls": choice.get("tool_calls"),
    }


def _demo_response(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Offline demo: parse the user's last message and pick a sensible tool
    or return a canned response. This lets the project run without an API key.
    """
    last_user = ""
    for m in reversed(messages):
        if m["role"] == "user":
            last_user = m["content"].lower()
            break

    # Check if this is a follow-up after a tool result
    if messages and messages[-1].get("role") == "tool":
        tool_content = messages[-1].get("content", "")
        return {
            "content": f"Here's what I found:\n\n{tool_content}",
            "tool_calls": None,
        }

    # Route to tools based on keywords
    if "weather" in last_user:
        city = last_user.split("weather")[-1].strip(" ?.,!in") or "Tel Aviv"
        return _make_tool_call("get_weather", {"city": city})

    if "search" in last_user or "find" in last_user or "what is" in last_user:
        query = last_user.replace("search", "").replace("find", "").replace("what is", "").strip()
        return _make_tool_call("web_search", {"query": query or last_user})

    if "calendar" in last_user or "events" in last_user or "schedule" in last_user:
        return _make_tool_call("google_calendar", {"action": "list"})

    if "remind" in last_user:
        return _make_tool_call("create_reminder", {"message": last_user, "when": "in 30 minutes"})

    if "summarize" in last_user or "http" in last_user:
        import re
        urls = re.findall(r"https?://\S+", last_user)
        if urls:
            return _make_tool_call("summarize_url", {"url": urls[0]})

    if "save" in last_user or "note" in last_user:
        return _make_tool_call("vault_save", {"filename": "quick-note.md", "content": last_user})

    if "vault" in last_user or "knowledge" in last_user:
        return _make_tool_call("vault_search", {"query": last_user})

    return {
        "content": (
            "I'm your AI assistant. I can help you with:\n"
            "- *Web search* — ask me to search for anything\n"
            "- *Calendar* — list or create events\n"
            "- *Email* — send emails\n"
            "- *Notes* — search or save to vault\n"
            "- *Reminders* — set timed reminders\n"
            "- *Weather* — check any city\n"
            "- *Summarize* — paste a URL and I'll summarize it\n\n"
            "What do you need?"
        ),
        "tool_calls": None,
    }


def _make_tool_call(name: str, args: dict) -> dict[str, Any]:
    return {
        "content": "",
        "tool_calls": [
            {
                "id": f"demo_{name}_{int(time.time())}",
                "function": {
                    "name": name,
                    "arguments": json.dumps(args),
                },
            }
        ],
    }
