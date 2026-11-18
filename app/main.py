"""FastAPI surface: Telegram webhook, a browser demo, and health.

Run locally:
    uvicorn app.main:app --port 7800
Then open http://127.0.0.1:7800 for the demo, or point Telegram at /webhook/<secret>.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from . import telegram
from .core import Store, handle_message, load_knowledge, RateLimiter

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change-me")
app = FastAPI(title="Telegram memory bot", version="1.0.0")

store = Store()
limiter = RateLimiter(limit=int(os.getenv("RATE_LIMIT", "8")), window_seconds=60)
knowledge = load_knowledge()

DEMO_HTML = (Path(__file__).parent / "demo.html").read_text(encoding="utf-8")


class ChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    user: str = "demo-user"


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "telegram_configured": telegram.is_configured(),
        "llm_configured": bool(os.getenv("LLM_API_KEY")),
        "personas": ["friend", "colleague", "support"],
        "knowledge_sections": len(knowledge),
    }


@app.get("/", response_class=HTMLResponse)
def demo() -> str:
    return DEMO_HTML


@app.post("/api/chat")
def chat(payload: ChatIn, request: Request) -> JSONResponse:
    key = request.client.host if request.client else "anon"
    if not limiter.allow(key):
        raise HTTPException(status_code=429, detail="Too many messages, wait a minute.")
    answer = handle_message(store, f"web:{payload.user}", payload.text, knowledge=knowledge)
    return JSONResponse(
        {
            "reply": answer.text,
            "persona": answer.persona,
            "used_llm": answer.used_llm,
            "remembered": answer.remembered,
        }
    )


@app.post("/webhook/{secret}")
async def webhook(secret: str, request: Request, background: BackgroundTasks) -> dict:
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="bad secret")

    update = await request.json()
    parsed = telegram.parse_update(update)
    if parsed is None:
        return {"ok": True, "skipped": True}

    chat_id, text = parsed
    if not telegram.is_configured():
        return {"ok": False, "reason": "BOT_TOKEN missing"}

    if not limiter.allow(f"tg:{chat_id}"):
        background.add_task(telegram.send_message, chat_id, "یک دقیقه صبر کن 🙂")
        return {"ok": True}

    answer = handle_message(store, f"tg:{chat_id}", text, knowledge=knowledge)
    background.add_task(telegram.send_message, chat_id, answer.text)
    return {"ok": True}


@app.post("/admin/set-webhook")
async def admin_set_webhook(public_url: str) -> dict:
    """One call to point Telegram at this deployment."""
    return await telegram.set_webhook(public_url, WEBHOOK_SECRET)
