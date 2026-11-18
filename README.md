<div align="center">

# Memory Bot

**A Telegram bot that remembers people, switches persona, and answers support questions from its owner's knowledge base.**

Python Â· FastAPI Â· SQLite Â· optional OpenAI-compatible LLM

</div>

---

## What it does

| | |
|---|---|
| **Persistent memory** | Name, job, city and interests are extracted from conversation and stored per user in SQLite. Restart the service and it still remembers. |
| **Three personas** | `friend` (casual), `colleague` (practical, no small talk), `support` (answers only from the knowledge base). Switch with `/persona`. |
| **Grounded support** | The support persona retrieves the best matching section of `knowledge.md`. When nothing matches it says so and promises a human follow-up instead of inventing an answer. |
| **Works without an LLM key** | With a key, replies are generated. Without one, a deterministic template answers using the same memory â€” so anyone can clone and run it. |
| **Browser demo** | `/` serves a chat UI that uses the exact same engine as Telegram. No token required to try it. |

## Commands

```
/persona دوست|همکار|پشتیبان   switch tone
/memory                        show exactly what is remembered
/forget                        delete everything for that user
/help                          list commands
```

## Quick start

```powershell
cd D:\Projects\telegram-memory-bot
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\uvicorn app.main:app --port 7800
```

Open <http://127.0.0.1:7800> for the demo. `/health` reports whether Telegram and the
LLM are configured.

## Going live on Telegram

1. Create a bot with **@BotFather** (`/newbot`) and copy the token.
2. Set `BOT_TOKEN` and a `WEBHOOK_SECRET` in the environment.
3. Deploy (Docker or `render.yaml`).
4. Point Telegram at the deployment once:

```
POST /admin/set-webhook?public_url=https://your-service.onrender.com
```

## Why memory is a table, not a chat log

Every reply needs two different things: a few recent turns (for flow) and a small set of
durable facts (for continuity). Storing them together makes both slow and expensive, so
`users.facts` holds the distilled facts and `messages` holds the last turns. `/memory`
reads the first one, which is why the bot can honestly show what it knows.

Fact extraction is regex-based on purpose: it never costs a request and never invents a
fact the user did not state. Swap `FACT_PATTERNS` for an LLM call if you want broader
coverage, and keep the same shape.

## Honest limits

- Heuristics catch the common phrasings only. "My name is Ali" works; "people call me Ali" does not.
- The knowledge base retrieval is lexical, not semantic. Paraphrases can miss.
- SQLite is fine for one instance. Multiple instances need Postgres and a shared queue.
- No conversation memory across *different* users, by design.

## Deploy

`render.yaml` describes a free Docker web service. The bot needs no database server and no
key to start; both are optional and supplied as environment variables.
