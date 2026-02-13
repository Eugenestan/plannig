from __future__ import annotations

import time
from typing import Any

import requests

from .config import settings


class TelegramNotifierError(RuntimeError):
    pass


def _build_send_message_url(bot_token: str) -> str:
    base = settings.telegram_api_base_url.rstrip("/")
    return f"{base}/bot{bot_token}/sendMessage"


def send_message(chat_id: str, text: str, *, disable_notification: bool = False, retries: int = 3) -> dict[str, Any]:
    if not settings.telegram_enabled:
        raise TelegramNotifierError("Telegram disabled by TELEGRAM_ENABLED=false")

    bot_token = (settings.telegram_bot_token or "").strip()
    if not bot_token:
        raise TelegramNotifierError("TELEGRAM_BOT_TOKEN is empty")

    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_notification": disable_notification,
    }

    url = _build_send_message_url(bot_token)
    timeout = (settings.telegram_connect_timeout_seconds, settings.telegram_read_timeout_seconds)
    last_error: Exception | None = None

    for attempt in range(1, max(retries, 1) + 1):
        try:
            response = requests.post(url, json=payload, timeout=timeout)
            if response.status_code >= 400:
                raise TelegramNotifierError(
                    f"Telegram API HTTP {response.status_code}: {response.text[:300]}"
                )
            data = response.json()
            if not data.get("ok"):
                raise TelegramNotifierError(f"Telegram API error: {data}")
            return data
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt >= max(retries, 1):
                break
            time.sleep(min(2.0 * attempt, 5.0))

    raise TelegramNotifierError(f"Failed to send Telegram message: {last_error}")
