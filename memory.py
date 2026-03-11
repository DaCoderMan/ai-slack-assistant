"""
Conversation memory — per-channel history with sliding window and JSON persistence.

Each channel gets its own JSON file. Messages are appended in real time and
the sliding window keeps the last N messages for the LLM context, while the
full history is preserved on disk for the admin dashboard.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from config import settings


@dataclass
class Message:
    role: str          # "user" | "assistant" | "tool"
    content: str
    ts: float = field(default_factory=time.time)
    user_id: str = ""
    tool_name: str = ""  # set when role == "tool"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_llm_format(self) -> dict[str, str]:
        """Format for the LLM messages array."""
        msg: dict[str, str] = {"role": self.role, "content": self.content}
        if self.role == "tool" and self.tool_name:
            msg["role"] = "user"  # flatten tool results into user context
            msg["content"] = f"[Tool Result — {self.tool_name}]\n{self.content}"
        return msg


class ConversationMemory:
    """Manages conversation histories for all channels."""

    def __init__(self, storage_dir: str | None = None, window: int | None = None):
        self.storage_dir = Path(storage_dir or settings.conversation_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.window = window or settings.memory_window
        self._channels: dict[str, list[Message]] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def add(self, channel: str, message: Message) -> None:
        async with self._lock:
            history = await self._load(channel)
            history.append(message)
            self._channels[channel] = history
            await self._save(channel, history)

    async def get_context(self, channel: str) -> list[dict[str, str]]:
        """Return the sliding-window view formatted for the LLM."""
        async with self._lock:
            history = await self._load(channel)
        window = history[-self.window :]
        return [m.to_llm_format() for m in window]

    async def get_full_history(self, channel: str) -> list[Message]:
        async with self._lock:
            return list(await self._load(channel))

    async def list_channels(self) -> list[str]:
        """Return channel IDs that have stored conversations."""
        return [
            p.stem for p in self.storage_dir.glob("*.json")
        ]

    async def recent_messages(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most recent messages across all channels (for admin)."""
        all_msgs: list[dict[str, Any]] = []
        for path in self.storage_dir.glob("*.json"):
            channel = path.stem
            async with self._lock:
                history = await self._load(channel)
            for m in history[-limit:]:
                entry = asdict(m)
                entry["channel"] = channel
                all_msgs.append(entry)
        all_msgs.sort(key=lambda x: x["ts"], reverse=True)
        return all_msgs[:limit]

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def _load(self, channel: str) -> list[Message]:
        if channel in self._channels:
            return self._channels[channel]
        path = self.storage_dir / f"{channel}.json"
        if not path.exists():
            self._channels[channel] = []
            return []
        data = await asyncio.to_thread(path.read_text, encoding="utf-8")
        raw = json.loads(data)
        messages = [
            Message(
                role=m["role"],
                content=m["content"],
                ts=m.get("ts", 0),
                user_id=m.get("user_id", ""),
                tool_name=m.get("tool_name", ""),
                metadata=m.get("metadata", {}),
            )
            for m in raw
        ]
        self._channels[channel] = messages
        return messages

    async def _save(self, channel: str, history: list[Message]) -> None:
        path = self.storage_dir / f"{channel}.json"
        data = json.dumps([asdict(m) for m in history], indent=2, ensure_ascii=False)
        await asyncio.to_thread(path.write_text, data, encoding="utf-8")


# Singleton
memory = ConversationMemory()
