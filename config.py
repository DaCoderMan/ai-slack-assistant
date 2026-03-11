"""
Configuration — all settings loaded from environment variables.
Provides typed, validated config with sensible defaults for local dev.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent


@dataclass(frozen=True)
class Settings:
    # --- Slack ---
    slack_bot_token: str = ""
    slack_signing_secret: str = ""

    # --- AI / LLM ---
    ai_api_url: str = "https://api.openai.com/v1/chat/completions"
    ai_api_key: str = ""
    ai_model: str = "gpt-4o"
    ai_max_tokens: int = 4096

    # --- External APIs ---
    weather_api_key: str = ""                # OpenWeatherMap
    google_calendar_credentials: str = ""    # path to service-account JSON
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""

    # --- Paths ---
    vault_dir: str = str(BASE_DIR / "data" / "vault")
    conversation_dir: str = str(BASE_DIR / "data" / "conversations")

    # --- Agent ---
    agent_max_steps: int = 8
    memory_window: int = 20  # messages kept in sliding window

    # --- Server ---
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        """Build settings from environment variables."""
        return cls(
            slack_bot_token=os.getenv("SLACK_BOT_TOKEN", ""),
            slack_signing_secret=os.getenv("SLACK_SIGNING_SECRET", ""),
            ai_api_url=os.getenv("AI_API_URL", cls.ai_api_url),
            ai_api_key=os.getenv("AI_API_KEY", ""),
            ai_model=os.getenv("AI_MODEL", cls.ai_model),
            ai_max_tokens=int(os.getenv("AI_MAX_TOKENS", str(cls.ai_max_tokens))),
            weather_api_key=os.getenv("WEATHER_API_KEY", ""),
            google_calendar_credentials=os.getenv("GOOGLE_CALENDAR_CREDENTIALS", ""),
            smtp_host=os.getenv("SMTP_HOST", cls.smtp_host),
            smtp_port=int(os.getenv("SMTP_PORT", str(cls.smtp_port))),
            smtp_user=os.getenv("SMTP_USER", ""),
            smtp_password=os.getenv("SMTP_PASSWORD", ""),
            vault_dir=os.getenv("VAULT_DIR", cls.vault_dir),
            conversation_dir=os.getenv("CONVERSATION_DIR", cls.conversation_dir),
            agent_max_steps=int(os.getenv("AGENT_MAX_STEPS", str(cls.agent_max_steps))),
            memory_window=int(os.getenv("MEMORY_WINDOW", str(cls.memory_window))),
            host=os.getenv("HOST", cls.host),
            port=int(os.getenv("PORT", str(cls.port))),
            debug=os.getenv("DEBUG", "").lower() in ("1", "true", "yes"),
        )


# Singleton — import this everywhere
settings = Settings.from_env()
