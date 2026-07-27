"""
Minimal Telegram Bot API client using long polling.

Long polling (getUpdates) rather than a webhook means the bot doesn't need
a public HTTPS endpoint of its own at all — it just needs to keep running
somewhere with outbound internet access. That's why the deploy story here
is "a small always-on VM running this script", not "a web service".
"""
from __future__ import annotations

import requests

from . import config


def get_updates(offset: int | None, timeout: int) -> list[dict]:
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    resp = requests.get(
        f"{config.TELEGRAM_API_BASE}/getUpdates",
        params=params,
        timeout=timeout + 10,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram getUpdates failed: {data}")
    return data["result"]


def send_message(chat_id: int, text: str) -> None:
    resp = requests.post(
        f"{config.TELEGRAM_API_BASE}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=15,
    )
    resp.raise_for_status()
