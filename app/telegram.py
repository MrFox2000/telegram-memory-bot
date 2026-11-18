"""Telegram transport: thin wrapper over the Bot API.

Kept separate from the engine so `core.py` stays testable without a token.
"""

from __future__ import annotations

import os

import httpx

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
API_ROOT = os.getenv("TELEGRAM_API_ROOT", "https://api.telegram.org")


def is_configured() -> bool:
    return bool(BOT_TOKEN)


def parse_update(update: dict) -> tuple[str, str] | None:
    """Return (chat_id, text) for a plain text message, else None."""
    message = update.get("message") or update.get("edited_message")
    if not message:
        return None
    text = message.get("text")
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    if not text or chat_id is None:
        return None
    return str(chat_id), text


async def send_message(chat_id: str, text: str) -> dict:
    """Send a reply. Markdown is attempted first, plain text as the fallback."""
    if not BOT_TOKEN:
        return {"ok": False, "description": "BOT_TOKEN is not set"}

    payload = {"chat_id": chat_id, "text": text[:4000], "parse_mode": "Markdown"}
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(f"{API_ROOT}/bot{BOT_TOKEN}/sendMessage", json=payload)
        if response.status_code == 400:
            # Telegram rejects unescaped Markdown; resend without formatting.
            payload.pop("parse_mode")
            response = await client.post(f"{API_ROOT}/bot{BOT_TOKEN}/sendMessage", json=payload)
        return response.json()


async def set_webhook(public_url: str, secret: str) -> dict:
    if not BOT_TOKEN:
        return {"ok": False, "description": "BOT_TOKEN is not set"}
    url = f"{public_url.rstrip('/')}/webhook/{secret}"
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            f"{API_ROOT}/bot{BOT_TOKEN}/setWebhook",
            json={"url": url, "allowed_updates": ["message", "edited_message"]},
        )
        return response.json()
