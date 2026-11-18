"""Tests for the engine. No network, no token, no LLM key."""

from __future__ import annotations

import pytest

from app.core import (
    PERSONAS,
    RateLimiter,
    Store,
    best_section,
    extract_facts,
    handle_message,
    load_knowledge,
    Section,
)


@pytest.fixture()
def store(tmp_path):
    store = Store(tmp_path / "test.db")
    yield store
    store.close()


def test_extracts_persian_name():
    assert extract_facts("اسم من علی هست")["name"] == "علی"


def test_extracts_english_job():
    facts = extract_facts("I am a backend developer")
    assert facts.get("job", "").lower().startswith("backend")


def test_extracts_likes():
    assert "پیتزا" in extract_facts("من دوست دارم پیتزا")["likes"]


def test_ignores_small_talk():
    assert extract_facts("سلام خوبی؟") == {}


def test_memory_survives_reopen(tmp_path):
    first = Store(tmp_path / "m.db")
    handle_message(first, "u1", "اسم من رضا هست")
    first.close()

    second = Store(tmp_path / "m.db")
    state = second.get("u1")
    assert state.facts["name"] == "رضا"
    second.close()


def test_memory_command_lists_facts(store):
    handle_message(store, "u2", "اسم من سارا هست")
    answer = handle_message(store, "u2", "/memory")
    assert "سارا" in answer.text
    assert "name" in answer.text


def test_forget_clears_everything(store):
    handle_message(store, "u3", "اسم من مریم هست")
    handle_message(store, "u3", "/forget")
    assert store.get("u3").facts == {}
    assert handle_message(store, "u3", "/memory").text.startswith("هنوز")


def test_persona_switch_changes_state(store):
    handle_message(store, "u4", "/persona همکار")
    assert store.get("u4").persona == "colleague"
    assert store.get("u4").persona in PERSONAS


def test_unknown_persona_is_refused(store):
    answer = handle_message(store, "u5", "/persona pirate")
    assert "یکی از این‌ها" in answer.text
    assert store.get("u5").persona == "friend"


def test_support_answers_from_knowledge(store, tmp_path):
    knowledge = [Section("ساعات کاری", "شنبه تا چهارشنبه ۹ تا ۶")]
    answer = handle_message(
        store, "u6", "ساعات کاری شما چنده؟", knowledge=knowledge
    )
    # persona is friend by default, so switch first
    handle_message(store, "u6", "/persona پشتیبان")
    answer = handle_message(
        store, "u6", "ساعات کاری شما چنده؟", knowledge=knowledge
    )
    assert "۹ تا ۶" in answer.text


def test_support_says_when_it_does_not_know(store):
    handle_message(store, "u7", "/persona پشتیبان")
    answer = handle_message(store, "u7", "قیمت ماهواره چنده؟", knowledge=[])
    assert "دانش فعلی" in answer.text


def test_best_section_picks_relevant():
    sections = [
        Section("ارسال", "هزینهٔ ارسال ۵۰ هزار تومان"),
        Section("مرجوعی", "بازگشت تا ۷ روز"),
    ]
    assert best_section("هزینهٔ ارسال چقدره؟", sections).heading == "ارسال"
    assert best_section("zzz qqq", sections) is None


def test_load_knowledge_parses_headings(tmp_path):
    path = tmp_path / "k.md"
    path.write_text("## الف\nمتن ۱\n## ب\nمتن ۲\n", encoding="utf-8")
    sections = load_knowledge(path)
    assert [s.heading for s in sections] == ["الف", "ب"]


def test_rate_limiter_blocks_after_limit():
    limiter = RateLimiter(limit=2, window_seconds=60)
    assert limiter.allow("ip")
    assert limiter.allow("ip")
    assert not limiter.allow("ip")
    assert limiter.allow("other")


def test_template_reply_uses_remembered_name(store):
    handle_message(store, "u8", "اسم من کاوه هست")
    answer = handle_message(store, "u8", "چطوری؟")
    assert "کاوه" in answer.text


def test_history_is_recorded(store):
    handle_message(store, "u9", "پیام اول")
    handle_message(store, "u9", "پیام دوم")
    assert len(store.history("u9")) >= 2
