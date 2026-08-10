"""Alert notifiers. Telegram per the charter's default stack.

Failures are logged and swallowed by the AlertEngine — notification is a
convenience, never a dependency of the trading loop. Configuration is
env-only (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID), consistent with the
no-secrets-in-code rule.
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

_SEVERITY_PREFIX = {"info": "[i]", "warning": "[!]", "critical": "[!!]"}


class LogNotifier:
    def send(self, alert: dict) -> None:
        level = {"info": logging.INFO, "warning": logging.WARNING,
                 "critical": logging.CRITICAL}.get(alert["severity"], logging.INFO)
        logger.log(level, "ALERT %s: %s", alert["rule_id"], alert["message"])


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, timeout: float = 5.0) -> None:
        if not bot_token or not chat_id:
            raise ValueError("TelegramNotifier requires bot token and chat id")
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id
        self._timeout = timeout

    @classmethod
    def from_env(cls) -> "TelegramNotifier | None":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat = os.environ.get("TELEGRAM_CHAT_ID", "")
        return cls(token, chat) if token and chat else None

    def send(self, alert: dict) -> None:
        prefix = _SEVERITY_PREFIX.get(alert["severity"], "")
        text = f"{prefix} VNEDGE {alert['rule_id']}\n{alert['message']}\n({alert.get('mode', '')})"
        response = httpx.post(
            self._url,
            json={"chat_id": self._chat_id, "text": text},
            timeout=self._timeout,
        )
        response.raise_for_status()
