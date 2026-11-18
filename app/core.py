"""The engine: persistent memory, personas, and reply generation.

Everything that decides *what the bot says* lives here so it can be tested
without Telegram, a network, or an LLM key.

Design notes
------------
* Memory is per-user and survives restarts (SQLite). Facts are stored as a flat
  JSON object so `/memory` can show the user exactly what is remembered.
* Three personas share one engine. Switching persona never wipes memory; it
  changes tone and, for support, which knowledge is allowed to answer.
* The LLM is optional. With a key the reply is generated; without one a
  deterministic template answers using the same memory. That keeps the demo
  runnable by anyone who clones the repository.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# --------------------------------------------------------------------- config

DB_PATH = Path(os.getenv("BOT_DB", Path(__file__).resolve().parent.parent / "bot.db"))
KNOWLEDGE_PATH = Path(
    os.getenv("BOT_KNOWLEDGE", Path(__file__).resolve().parent.parent / "knowledge.md")
)
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").rstrip("/")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "llama-3.3-70b-versatile")

PERSONAS = ("friend", "colleague", "support")

# Users type Persian; the engine keys English. Map both.
PERSONA_ALIASES = {
    "دوست": "friend", "friend": "friend", "رفیق": "friend",
    "همکار": "colleague", "colleague": "colleague",
    "پشتیبان": "support", "support": "support", "پشتیبانی": "support",
}

PERSONA_PROMPTS: dict[str, str] = {
    "friend": (
        "You are a warm, casual friend chatting on Telegram. Use short sentences, "
        "occasional humour, and no corporate language. Remember what the person told "
        "you and bring it up naturally. Never invent facts about them."
    ),
    "colleague": (
        "You are a competent, friendly colleague. Be concise and practical: give the "
        "answer first, then the reason if it matters. No small talk unless they start it. "
        "Use the remembered context, but never pretend to know something you do not."
    ),
    "support": (
        "You are a customer support agent. Answer ONLY from the knowledge base provided. "
        "If the knowledge does not cover the question, say you will check and pass it on "
        "to a human. Never guess policies, prices, or delivery times."
    ),
}

PERSONA_GREETINGS = {
    "friend": "سلام! چه خبر؟ 🙂",
    "colleague": "سلام. در خدمتم — چه چیزی لازم دارید؟",
    "support": "سلام 👋 سؤالتان را بپرسید؛ از دانش محصول جواب می‌دهم.",
}

# ---------------------------------------------------------------- persistence


@dataclass
class UserState:
    external_id: str
    persona: str = "friend"
    facts: dict[str, str] = field(default_factory=dict)
    summary: str = ""
    turns: int = 0

    def remember(self, key: str, value: str) -> None:
        key = key.strip().lower()
        value = value.strip()
        if not key or not value:
            return
        self.facts[key] = value[:200]


class Store:
    """SQLite-backed store. One file, no server."""

    def __init__(self, path: Path | str = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # FastAPI runs sync endpoints in a worker thread, so the connection must
        # not be bound to the thread that created it. One lock keeps writes safe.
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                external_id TEXT PRIMARY KEY,
                persona     TEXT NOT NULL DEFAULT 'friend',
                facts       TEXT NOT NULL DEFAULT '{}',
                summary     TEXT NOT NULL DEFAULT '',
                turns       INTEGER NOT NULL DEFAULT 0,
                updated_at  REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                external_id TEXT NOT NULL,
                role        TEXT NOT NULL,
                content     TEXT NOT NULL,
                created_at  REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(external_id, id DESC);
            """
        )
        self._conn.commit()

    # -- users ---------------------------------------------------------------

    def get(self, external_id: str) -> UserState:
        row = self._conn.execute(
            "SELECT * FROM users WHERE external_id = ?", (external_id,)
        ).fetchone()
        if row is None:
            return UserState(external_id=external_id)
        return UserState(
            external_id=row["external_id"],
            persona=row["persona"],
            facts=json.loads(row["facts"] or "{}"),
            summary=row["summary"] or "",
            turns=row["turns"],
        )

    def save(self, state: UserState) -> None:
        with self._lock:
            self._save_unlocked(state)

    def _save_unlocked(self, state: UserState) -> None:
        self._conn.execute(
            """
            INSERT INTO users (external_id, persona, facts, summary, turns, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(external_id) DO UPDATE SET
                persona = excluded.persona,
                facts = excluded.facts,
                summary = excluded.summary,
                turns = excluded.turns,
                updated_at = excluded.updated_at
            """,
            (
                state.external_id,
                state.persona,
                json.dumps(state.facts, ensure_ascii=False),
                state.summary,
                state.turns,
                time.time(),
            ),
        )
        self._conn.commit()

    def forget(self, external_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM users WHERE external_id = ?", (external_id,))
            self._conn.execute("DELETE FROM messages WHERE external_id = ?", (external_id,))
            self._conn.commit()

    # -- messages ------------------------------------------------------------

    def add_message(self, external_id: str, role: str, content: str) -> None:
        with self._lock:
            self._add_message_unlocked(external_id, role, content)

    def _add_message_unlocked(self, external_id: str, role: str, content: str) -> None:
        self._conn.execute(
            "INSERT INTO messages (external_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (external_id, role, content, time.time()),
        )
        self._conn.commit()

    def history(self, external_id: str, limit: int = 12) -> list[dict[str, str]]:
        rows = self._conn.execute(
            "SELECT role, content FROM messages WHERE external_id = ? ORDER BY id DESC LIMIT ?",
            (external_id, limit),
        ).fetchall()
        return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]

    def close(self) -> None:
        self._conn.close()


# ------------------------------------------------------- fact extraction

# Deliberately small and predictable. An LLM would extract more, but heuristics
# never cost a request and never invent something the user did not say.
FACT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?:اسم|نام)\s*(?:من|م)\s+(?:هست\s*)?([A-Za-z\u0600-\u06FF][\w\u0600-\u06FF ]{1,30})", re.I), "name"),
    (re.compile(r"my name is\s+([A-Za-z][\w ]{1,30})", re.I), "name"),
    (re.compile(r"(?:من\s+)?(?:کارم|شغلم|شغل\s+من)\s+([^\n.]{2,40})", re.I), "job"),
    (re.compile(r"i(?:'m| am) (?:a|an)\s+([^\n.]{2,40})", re.I), "job"),
    (re.compile(r"(?:من\s+)?(?:دوست دارم|علاقه\s+دارم به)\s+([^\n.]{2,40})", re.I), "likes"),
    (re.compile(r"i (?:like|love)\s+([^\n.]{2,40})", re.I), "likes"),
    (re.compile(r"(?:شهرم|ساکن)\s+([^\n.]{2,30})", re.I), "city"),
    (re.compile(r"i live in\s+([^\n.]{2,30})", re.I), "city"),
]


def extract_facts(text: str) -> dict[str, str]:
    """Pull durable facts out of one message. Returns {} when there is nothing."""
    found: dict[str, str] = {}
    for pattern, key in FACT_PATTERNS:
        match = pattern.search(text)
        if match:
            value = match.group(1).strip(" .،,!")
            # The capture is greedy, so cut at the first copula or punctuation:
            # "علی هست، من دوست دارم قهوه" must become "علی".
            value = re.split(
                r"[،,.!؟?]|\s+(?:هست|هستم|هستند|است|ام|ای|بود|می)\s*",
                value,
                maxsplit=1,
            )[0].strip()
            if 1 < len(value) <= 60:
                found.setdefault(key, value)
    return found


# ------------------------------------------------------------- knowledge base


@dataclass
class Section:
    heading: str
    body: str


def load_knowledge(path: Path | str = KNOWLEDGE_PATH) -> list[Section]:
    """Split a markdown file on '## ' headings."""
    path = Path(path)
    if not path.exists():
        return []
    sections: list[Section] = []
    heading, lines = "", []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            if heading:
                sections.append(Section(heading, "\n".join(lines).strip()))
            heading, lines = line[3:].strip(), []
        elif heading:
            lines.append(line)
    if heading:
        sections.append(Section(heading, "\n".join(lines).strip()))
    return sections


def best_section(question: str, sections: Iterable[Section]) -> Section | None:
    """Cheap lexical retrieval: score overlap of words, ignore very short ones."""
    words = {w for w in re.findall(r"[\w\u0600-\u06FF]{3,}", question.lower())}
    if not words:
        return None
    best, best_score = None, 0
    for section in sections:
        haystack = (section.heading + " " + section.body).lower()
        score = sum(1 for word in words if word in haystack)
        if score > best_score:
            best, best_score = section, score
    return best if best_score else None


# ---------------------------------------------------------------- the engine


def _facts_sentence(facts: dict[str, str]) -> str:
    if not facts:
        return ""
    bits = []
    if "name" in facts:
        bits.append(facts["name"])
    if "job" in facts:
        bits.append(facts["job"])
    if "city" in facts:
        bits.append(facts["city"])
    if "likes" in facts:
        bits.append(facts["likes"])
    return "، ".join(bits)


def template_reply(persona: str, text: str, state: UserState, sections: list[Section]) -> str:
    """The no-key path: still uses memory, still persona-aware, never invents."""
    facts = state.facts
    name = facts.get("name", "")

    if persona == "support":
        section = best_section(text, sections)
        if section is None:
            return (
                "این مورد در دانش فعلی من نیست. ثبتش می‌کنم تا همکار انسانی‌ام دقیق "
                "بررسی کند و همین امروز جواب بدهد."
            )
        return f"**{section.heading}**\n\n{section.body}"

    if persona == "colleague":
        opener = f"{name} جان، " if name else ""
        return (
            f"{opener}برای اینکه دقیق جواب بدهم دو چیز لازم دارم: خروجی مورد انتظار، "
            "و مهلت. با این دو، امروز جواب قطعی می‌دهم."
        )

    # friend
    if name:
        return f"{name} جان 🙂 یادم هست. بگو ادامه بدهم یا موضوع عوض شود؟"
    return "خوش آمدی 🙂 کمی از خودت بگو تا یادم بماند و بعداً مجبور نباشی تکرار کنی."


def llm_reply(
    persona: str,
    text: str,
    state: UserState,
    history: list[dict[str, str]],
    sections: list[Section],
) -> str | None:
    """Ask an OpenAI-compatible endpoint. Returns None when unavailable."""
    if not (LLM_BASE_URL and LLM_API_KEY):
        return None

    import httpx  # imported lazily so the engine works without the dependency

    system = PERSONA_PROMPTS[persona]
    if state.facts:
        system += "\n\nKnown facts about the user: " + json.dumps(state.facts, ensure_ascii=False)
    if persona == "support" and sections:
        knowledge = "\n\n".join(f"## {s.heading}\n{s.body}" for s in sections[:12])
        system += "\n\nKnowledge base:\n" + knowledge

    messages = [{"role": "system", "content": system}]
    messages += history[-8:]
    messages.append({"role": "user", "content": text})

    try:
        response = httpx.post(
            f"{LLM_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
            json={"model": LLM_MODEL, "messages": messages, "temperature": 0.6, "max_tokens": 500},
            timeout=30.0,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


# ---------------------------------------------------------------- rate limits


class RateLimiter:
    """Fixed-window limiter. Enough to stop a public demo burning the free tier."""

    def __init__(self, limit: int = 8, window_seconds: int = 60):
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.time()
        hits = [t for t in self._hits.get(key, []) if now - t < self.window]
        if len(hits) >= self.limit:
            self._hits[key] = hits
            return False
        hits.append(now)
        self._hits[key] = hits
        return True


# ------------------------------------------------------------------ commands


@dataclass
class Answer:
    text: str
    persona: str
    used_llm: bool
    remembered: dict[str, str]


def handle_message(
    store: Store,
    external_id: str,
    text: str,
    *,
    knowledge: list[Section] | None = None,
) -> Answer:
    """The single entry point used by both Telegram and the web demo."""
    knowledge = knowledge if knowledge is not None else load_knowledge()
    state = store.get(external_id)
    text = (text or "").strip()

    # --- commands -------------------------------------------------------
    if text.startswith("/"):
        command, _, rest = text.partition(" ")
        command = command.lower()

        if command in {"/start", "/help"}:
            reply = (
                "سلام 👋\n"
                "/persona دوست | همکار | پشتیبان — تغییر لحن\n"
                "/memory — آنچه از تو به یاد دارم\n"
                "/forget — پاک کردن حافظه\n"
                "/support — پاسخ از دانش محصول"
            )
        elif command == "/persona":
            wanted = PERSONA_ALIASES.get(rest.strip().lower(), "")
            if wanted in PERSONAS:
                state.persona = wanted
                store.save(state)
                reply = f"لحن عوض شد: {wanted}\n{PERSONA_GREETINGS[wanted]}"
            else:
                reply = "یکی از این‌ها را بنویس: دوست، همکار، پشتیبان"
        elif command == "/memory":
            if state.facts:
                lines = "\n".join(f"• {k}: {v}" for k, v in state.facts.items())
                reply = f"این‌ها را به یاد دارم:\n{lines}"
            else:
                reply = "هنوز چیزی یادم نیست. کمی از خودت بگو 🙂"
        elif command == "/forget":
            store.forget(external_id)
            reply = "همه‌چیز پاک شد. از صفر شروع می‌کنیم."
            return Answer(reply, state.persona, False, {})
        else:
            reply = "این دستور را نمی‌شناسم. /help را بزن."
        store.add_message(external_id, "user", text)
        store.add_message(external_id, "assistant", reply)
        return Answer(reply, state.persona, False, {})

    # --- normal message -------------------------------------------------
    remembered = extract_facts(text)
    for key, value in remembered.items():
        state.remember(key, value)

    history = store.history(external_id, limit=10)
    store.add_message(external_id, "user", text)

    reply = llm_reply(state.persona, text, state, history, knowledge)
    used_llm = reply is not None
    if reply is None:
        reply = template_reply(state.persona, text, state, knowledge)

    store.add_message(external_id, "assistant", reply)
    state.turns += 1
    store.save(state)
    return Answer(reply, state.persona, used_llm, remembered)
