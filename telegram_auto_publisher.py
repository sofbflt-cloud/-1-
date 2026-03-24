#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram Auto-Publisher Bot – Professional Edition

Features:
- Private admin‑only control panel with inline buttons
- Auto‑publishing to a private channel (English quotes, Arabic poetry)
- Islamic reminders on a separate schedule
- AI generation via OpenAI (optional; falls back to safe templates if unavailable)
- Alternating mode: English ↔ Arabic, or fixed mode
- Custom intervals for posts and reminders
- SQLite persistence, async aiogram 3.x, long polling
- Unicode styling for English texts, diacritized Arabic wrapping

Environment variables (set before running):
  BOT_TOKEN       – Telegram bot token
  ADMIN_ID        – your numeric Telegram user ID
  CHANNEL_ID      – channel username (e.g. @my_channel) or numeric ID (with -100 prefix)
  OPENAI_API_KEY  – optional, OpenAI API key
  AI_MODEL        – optional, default "gpt-4o-mini"
  TIMEZONE        – optional, default "UTC"
  DB_PATH         – optional, default "bot_state.sqlite3"
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import random
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Config:
    bot_token: str
    admin_id: int
    channel_id: str
    openai_api_key: str = ""
    ai_model: str = "gpt-4o-mini"
    timezone_name: str = "UTC"
    db_path: str = "bot_state.sqlite3"

    @classmethod
    def from_env(cls) -> Config:
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("BOT_TOKEN is missing")

        admin_raw = os.getenv("ADMIN_ID", "").strip()
        if not admin_raw.isdigit():
            raise RuntimeError("ADMIN_ID must be a numeric Telegram user ID")

        channel = os.getenv("CHANNEL_ID", "").strip()
        if not channel:
            raise RuntimeError("CHANNEL_ID is missing")

        return cls(
            bot_token=token,
            admin_id=int(admin_raw),
            channel_id=channel,
            openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
            ai_model=os.getenv("AI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini",
            timezone_name=os.getenv("TIMEZONE", "UTC").strip() or "UTC",
            db_path=os.getenv("DB_PATH", "bot_state.sqlite3").strip() or "bot_state.sqlite3",
        )


# ---------------------------------------------------------------------------
# Database (SQLite)
# ---------------------------------------------------------------------------
class DB:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                language TEXT,
                content TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        self.conn.commit()

        defaults = {
            "posting_enabled": "1",
            "reminders_enabled": "1",
            "post_mode": "alternate",  # english | arabic | alternate
            "post_interval_min": "60",
            "reminder_interval_min": "30",
            "alternate_next": "english",
            "next_post_at": "0",
            "next_reminder_at": "0",
            "preview_enabled": "1",
            "generated_count": "0",
        }
        for k, v in defaults.items():
            self.set_if_missing(k, v)

    def set_if_missing(self, key: str, value: str) -> None:
        cur = self.conn.cursor()
        cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))
        self.conn.commit()

    def get(self, key: str, default: str = "") -> str:
        cur = self.conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, str(value)),
        )
        self.conn.commit()

    def update_many(self, items: dict[str, Any]) -> None:
        cur = self.conn.cursor()
        cur.executemany(
            """
            INSERT INTO settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            [(k, str(v)) for k, v in items.items()],
        )
        self.conn.commit()

    def as_dict(self) -> dict[str, str]:
        cur = self.conn.cursor()
        cur.execute("SELECT key, value FROM settings")
        return {row["key"]: row["value"] for row in cur.fetchall()}

    def add_history(self, kind: str, content: str, language: str = "") -> None:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO history(kind, language, content, created_at) VALUES (?, ?, ?, ?)",
            (kind, language, content, int(time.time())),
        )
        self.conn.commit()

    def last_history(self) -> Optional[sqlite3.Row]:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM history ORDER BY id DESC LIMIT 1")
        return cur.fetchone()

    def increment_generated(self) -> None:
        val = int(self.get("generated_count", "0")) + 1
        self.set("generated_count", str(val))

    def close(self) -> None:
        self.conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
FALLBACK_QUOTES = [
    "The quietest minds often build the loudest futures.",
    "A single honest choice can outgrow a thousand excuses.",
    "What you repeat becomes your destiny.",
    "Discipline is the art of proving yourself right in silence.",
    "Clarity is a form of courage.",
]

FALLBACK_ARABIC = [
    "﴿وَقَلْبٌ يُجَاهِدُ لِيَبْقَى النُّورُ فِيهِ﴾",
    "﴿إِذَا صَفَا الْقَلْبُ أَبْصَرَ مَا لَا يُبْصِرُهُ الضَّوْءُ﴾",
    "﴿وَفِي الصَّبْرِ مِفْتَاحُ أَبْوَابٍ تُغَلَّقُ دُونَ الْعَجَلَةِ﴾",
]

REMINDER_TEXTS = [
    "﴿سُبْحَانَ اللَّهِ وَبِحَمْدِهِ﴾",
    "﴿أَسْتَغْفِرُ اللَّهَ الْعَظِيمَ وَأَتُوبُ إِلَيْهِ﴾",
    "﴿اللَّهُمَّ صَلِّ عَلَى مُحَمَّدٍ وَآلِ مُحَمَّدٍ﴾",
    "﴿رَبَّنَا آتِنَا فِي الدُّنْيَا حَسَنَةً وَفِي الْآخِرَةِ حَسَنَةً﴾",
    "﴿لَا إِلَهَ إِلَّا اللَّهُ وَحْدَهُ لَا شَرِيكَ لَهُ﴾",
    "﴿اللَّهُمَّ اجْعَلْ فِي قَلْبِي نُورًا﴾",
]

# Unicode style mappings for English
ENGLISH_STYLES = {
    "bold": str.maketrans({
        **{c: chr(ord("𝐀") + (ord(c) - ord("A"))) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"},
        **{c: chr(ord("𝐚") + (ord(c) - ord("a"))) for c in "abcdefghijklmnopqrstuvwxyz"},
        **{c: chr(ord("𝟎") + (ord(c) - ord("0"))) for c in "0123456789"},
    }),
    "italic": str.maketrans({
        **{c: chr(ord("𝐴") + (ord(c) - ord("A"))) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"},
        **{c: chr(ord("𝑎") + (ord(c) - ord("a"))) for c in "abcdefghijklmnopqrstuvwxyz"},
    }),
    "mono": str.maketrans({
        **{c: chr(ord("𝙰") + (ord(c) - ord("A"))) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"},
        **{c: chr(ord("𝚊") + (ord(c) - ord("a"))) for c in "abcdefghijklmnopqrstuvwxyz"},
        **{c: chr(ord("𝟶") + (ord(c) - ord("0"))) for c in "0123456789"},
    }),
    "sans": str.maketrans({
        **{c: chr(ord("𝗔") + (ord(c) - ord("A"))) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"},
        **{c: chr(ord("𝗮") + (ord(c) - ord("a"))) for c in "abcdefghijklmnopqrstuvwxyz"},
        **{c: chr(ord("𝟬") + (ord(c) - ord("0"))) for c in "0123456789"},
    }),
}


def now_ts() -> int:
    return int(time.time())


def safe_html(text: str) -> str:
    return html.escape(text, quote=False)


def human_delta(seconds: int) -> str:
    seconds = max(0, int(seconds))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{sec}s")
    return " ".join(parts)


def apply_random_english_style(text: str) -> str:
    style = random.choice(list(ENGLISH_STYLES.values()))
    return text.translate(style)


def clean_text(text: str) -> str:
    text = text.strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[\u200f\u200e]", "", text)
    return text


def format_english(text: str) -> str:
    text = clean_text(text)
    text = text.strip().strip("\"“”'")
    styled = apply_random_english_style(text)
    return f"✨ <b>{safe_html(styled)}</b> ✨"


def format_arabic(text: str) -> str:
    text = clean_text(text)
    text = text.strip("﴿﴾[](){}<>「」『』\"' ")
    # Wrap in decorative brackets
    return f"﴿\n{safe_html(text)}\n﴾"


def format_reminder(text: str) -> str:
    return f"🕊️ <b>{safe_html(text)}</b>"


def validate_positive_minutes(value: int, minimum: int = 1, maximum: int = 24 * 60) -> int:
    return max(minimum, min(maximum, int(value)))


# ---------------------------------------------------------------------------
# AI Engine
# ---------------------------------------------------------------------------
class AIEngine:
    def __init__(self, api_key: str, model: str):
        self.available = bool(api_key and AsyncOpenAI is not None)
        self.model = model
        self.client = AsyncOpenAI(api_key=api_key) if self.available else None

    async def _chat_completion(self, system: str, user: str) -> str:
        if not self.available or self.client is None:
            raise RuntimeError("AI not configured")
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.8,
            max_tokens=150,
        )
        text = resp.choices[0].message.content or ""
        return clean_text(text)

    async def english_quote(self) -> str:
        system = (
            "You write short, deep, elegant English quotes for a private Telegram channel. "
            "Return only the quote text. No bullets, no labels, no hashtags, no mention of AI. "
            "Keep it concise, emotionally resonant, refined, maximum 18 words."
        )
        user = "Generate one original English quote."
        try:
            text = await self._chat_completion(system, user)
            if not text:
                raise RuntimeError("empty")
            return text
        except Exception:
            return random.choice(FALLBACK_QUOTES)

    async def arabic_poem(self) -> str:
        system = (
            "أنت شاعر عربي فصيح تكتب بيتًا أو سطرًا شعريًا واحدًا بأسلوب تراثي بلاغي. "
            "أعد النص فقط، بدون شرح، بدون عنوان، بدون ذكر أنه مولّد. "
            "اجعل اللغة فصيحة، قوية، جميلة الإيقاع، ومشكولة بالكامل."
        )
        user = "اكتب نصًا شعريًا عربيًا فصيحًا قصيرًا جدًا (بيت واحد أو نصف بيت) مشكولًا بالكامل."
        try:
            text = await self._chat_completion(system, user)
            if not text:
                raise RuntimeError("empty")
            return text
        except Exception:
            return random.choice(FALLBACK_ARABIC)

    async def reminder(self) -> str:
        # Static reminders for reliability
        return random.choice(REMINDER_TEXTS)


# ---------------------------------------------------------------------------
# Keyboards & Callbacks
# ---------------------------------------------------------------------------
class MenuCB:
    prefix = "menu"

    @staticmethod
    def pack(action: str) -> str:
        return f"{MenuCB.prefix}:{action}"


class InputStates(StatesGroup):
    waiting_post_interval = State()
    waiting_reminder_interval = State()


def panel_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(text="▶ تشغيل النشر", callback_data=MenuCB.pack("start_posts")),
        InlineKeyboardButton(text="⏸ إيقاف النشر", callback_data=MenuCB.pack("stop_posts")),
    )
    builder.row(
        InlineKeyboardButton(text="🧭 عربي فقط", callback_data=MenuCB.pack("mode_ar")),
        InlineKeyboardButton(text="🌐 إنجليزي فقط", callback_data=MenuCB.pack("mode_en")),
        InlineKeyboardButton(text="🔁 تناوب", callback_data=MenuCB.pack("mode_alt")),
    )
    builder.row(
        InlineKeyboardButton(text="⏱ مدة النشر", callback_data=MenuCB.pack("set_post_interval")),
        InlineKeyboardButton(text="🕋 مدة التذكير", callback_data=MenuCB.pack("set_reminder_interval")),
    )
    builder.row(
        InlineKeyboardButton(text="📿 تشغيل/إيقاف التذكير", callback_data=MenuCB.pack("toggle_reminders")),
        InlineKeyboardButton(text="📝 توليد الآن", callback_data=MenuCB.pack("post_now")),
    )
    builder.row(
        InlineKeyboardButton(text="👁 معاينة", callback_data=MenuCB.pack("preview")),
        InlineKeyboardButton(text="💾 حفظ", callback_data=MenuCB.pack("save")),
    )
    builder.row(
        InlineKeyboardButton(text="📊 الحالة", callback_data=MenuCB.pack("status")),
        InlineKeyboardButton(text="🕘 آخر منشور", callback_data=MenuCB.pack("last_post")),
    )
    builder.row(
        InlineKeyboardButton(text="⚙ الإعدادات", callback_data=MenuCB.pack("settings")),
        InlineKeyboardButton(text="♻ إعادة ضبط", callback_data=MenuCB.pack("reset")),
        InlineKeyboardButton(text="🔄 تحديث اللوحة", callback_data=MenuCB.pack("refresh")),
    )
    return builder.as_markup()


def mode_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="عربي فقط", callback_data=MenuCB.pack("mode_ar")),
        InlineKeyboardButton(text="إنجليزي فقط", callback_data=MenuCB.pack("mode_en")),
        InlineKeyboardButton(text="تناوب", callback_data=MenuCB.pack("mode_alt")),
    )
    builder.row(InlineKeyboardButton(text="⬅ رجوع", callback_data=MenuCB.pack("refresh")))
    return builder.as_markup()


def durations_keyboard(kind: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    presets = [5, 10, 15, 30, 60, 120]
    for minutes in presets:
        builder.add(InlineKeyboardButton(text=f"{minutes} دقيقة", callback_data=MenuCB.pack(f"{kind}_{minutes}")))
    builder.adjust(3, 3)
    builder.row(
        InlineKeyboardButton(text="مخصص", callback_data=MenuCB.pack(f"{kind}_custom")),
        InlineKeyboardButton(text="⬅ رجوع", callback_data=MenuCB.pack("refresh")),
    )
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Application State Helpers
# ---------------------------------------------------------------------------
def load_settings(db: DB) -> dict[str, Any]:
    raw = db.as_dict()
    return {
        "posting_enabled": raw.get("posting_enabled", "1") == "1",
        "reminders_enabled": raw.get("reminders_enabled", "1") == "1",
        "post_mode": raw.get("post_mode", "alternate"),
        "post_interval_min": int(raw.get("post_interval_min", "60")),
        "reminder_interval_min": int(raw.get("reminder_interval_min", "30")),
        "alternate_next": raw.get("alternate_next", "english"),
        "next_post_at": int(raw.get("next_post_at", "0")),
        "next_reminder_at": int(raw.get("next_reminder_at", "0")),
        "preview_enabled": raw.get("preview_enabled", "1") == "1",
        "generated_count": int(raw.get("generated_count", "0")),
    }


def save_settings(db: DB, **kwargs: Any) -> None:
    db.update_many(kwargs)


def sync_timers(db: DB) -> None:
    s = load_settings(db)
    now = now_ts()
    if s["next_post_at"] <= 0:
        save_settings(db, next_post_at=str(now + s["post_interval_min"] * 60))
    if s["next_reminder_at"] <= 0:
        save_settings(db, next_reminder_at=str(now + s["reminder_interval_min"] * 60))


def panel_text(db: DB) -> str:
    s = load_settings(db)
    mode_label = {
        "english": "إنجليزي فقط",
        "arabic": "عربي فقط",
        "alternate": "تناوب عربي/إنجليزي",
    }.get(s["post_mode"], s["post_mode"])

    next_post_in = s["next_post_at"] - now_ts()
    next_rem_in = s["next_reminder_at"] - now_ts()

    return (
        "🤖 <b>لوحة التحكم الخاصة</b>\n\n"
        f"• النشر: <b>{'مفعل' if s['posting_enabled'] else 'متوقف'}</b>\n"
        f"• التذكيرات: <b>{'مفعلة' if s['reminders_enabled'] else 'متوقفة'}</b>\n"
        f"• النمط: <b>{safe_html(mode_label)}</b>\n"
        f"• مدة النشر: <b>{s['post_interval_min']} دقيقة</b>\n"
        f"• مدة التذكير: <b>{s['reminder_interval_min']} دقيقة</b>\n"
        f"• المنشور التالي خلال: <b>{human_delta(next_post_in)}</b>\n"
        f"• التذكير التالي خلال: <b>{human_delta(next_rem_in)}</b>\n"
        f"• عدد ما تم توليده: <b>{s['generated_count']}</b>\n"
    )


async def safe_send_or_edit(
    target: Message | CallbackQuery,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    try:
        if isinstance(target, Message):
            await target.answer(text, reply_markup=reply_markup)
        else:  # CallbackQuery
            if target.message:
                await target.message.edit_text(text, reply_markup=reply_markup)
            await target.answer()
    except TelegramBadRequest:
        if isinstance(target, CallbackQuery):
            await target.answer("تعذر تحديث اللوحة الآن", show_alert=True)


# ---------------------------------------------------------------------------
# Core Bot Logic
# ---------------------------------------------------------------------------
class AutoPublisher:
    def __init__(self, bot: Bot, db: DB, ai: AIEngine):
        self.bot = bot
        self.db = db
        self.ai = ai
        self.post_task: asyncio.Task | None = None
        self.rem_task: asyncio.Task | None = None

    def settings(self) -> dict[str, Any]:
        return load_settings(self.db)

    def choose_language(self) -> str:
        s = self.settings()
        mode = s["post_mode"]
        if mode == "english":
            return "english"
        if mode == "arabic":
            return "arabic"
        # alternate
        next_lang = s["alternate_next"]
        return "english" if next_lang not in {"english", "arabic"} else next_lang

    def advance_alternate(self, current: str) -> None:
        next_lang = "arabic" if current == "english" else "english"
        save_settings(self.db, alternate_next=next_lang)

    async def generate_post(self, preview: bool = False) -> tuple[str, str]:
        lang = self.choose_language()
        if lang == "english":
            raw = await self.ai.english_quote()
            final = format_english(raw)
        else:
            raw = await self.ai.arabic_poem()
            final = format_arabic(raw)

        if not preview:
            self.db.increment_generated()
            self.db.add_history("post", final, lang)
            if self.settings()["post_mode"] == "alternate":
                self.advance_alternate(lang)
        return lang, final

    async def generate_reminder(self, preview: bool = False) -> str:
        raw = await self.ai.reminder()
        final = format_reminder(raw)
        if not preview:
            self.db.add_history("reminder", final, "ar")
        return final

    async def post_to_channel(self, text: str) -> None:
        await self.bot.send_chat_action(CONFIG.channel_id, ChatAction.TYPING)
        await self.bot.send_message(
            chat_id=CONFIG.channel_id,
            text=text,
            disable_web_page_preview=True,
        )

    async def publish_now(self) -> str:
        lang, text = await self.generate_post(preview=False)
        await self.post_to_channel(text)
        # Reschedule next post
        interval = self.settings()["post_interval_min"]
        save_settings(self.db, next_post_at=str(now_ts() + interval * 60))
        return f"✅ تم النشر بنجاح ({lang})"

    async def preview_post(self) -> tuple[str, str]:
        return await self.generate_post(preview=True)

    async def send_reminder_now(self) -> str:
        text = await self.generate_reminder(preview=False)
        await self.post_to_channel(text)
        interval = self.settings()["reminder_interval_min"]
        save_settings(self.db, next_reminder_at=str(now_ts() + interval * 60))
        return "✅ تم إرسال التذكير"

    async def post_loop(self) -> None:
        while True:
            s = self.settings()
            if not s["posting_enabled"]:
                await asyncio.sleep(5)
                continue

            due = s["next_post_at"]
            wait = max(1, due - now_ts()) if due else 0
            if wait > 0:
                await asyncio.sleep(min(wait, 30))
                continue

            try:
                await self.bot.send_chat_action(CONFIG.channel_id, ChatAction.TYPING)
                lang, text = await self.generate_post(preview=False)
                await self.bot.send_message(CONFIG.channel_id, text, disable_web_page_preview=True)
                interval = s["post_interval_min"]
                save_settings(self.db, next_post_at=str(now_ts() + interval * 60))
                logging.info("Posted auto content (%s)", lang)
            except Exception:
                logging.exception("Failed to publish auto post")
                # Reschedule anyway to avoid infinite loop
                interval = s["post_interval_min"]
                save_settings(self.db, next_post_at=str(now_ts() + interval * 60))
                await asyncio.sleep(5)

    async def reminder_loop(self) -> None:
        while True:
            s = self.settings()
            if not s["reminders_enabled"]:
                await asyncio.sleep(5)
                continue

            due = s["next_reminder_at"]
            wait = max(1, due - now_ts()) if due else 0
            if wait > 0:
                await asyncio.sleep(min(wait, 30))
                continue

            try:
                text = await self.generate_reminder(preview=False)
                await self.bot.send_message(CONFIG.channel_id, text, disable_web_page_preview=True)
                interval = s["reminder_interval_min"]
                save_settings(self.db, next_reminder_at=str(now_ts() + interval * 60))
                logging.info("Posted reminder")
            except Exception:
                logging.exception("Failed to publish reminder")
                interval = s["reminder_interval_min"]
                save_settings(self.db, next_reminder_at=str(now_ts() + interval * 60))
                await asyncio.sleep(5)

    def start_background_tasks(self) -> None:
        if self.post_task is None or self.post_task.done():
            self.post_task = asyncio.create_task(self.post_loop(), name="post_loop")
        if self.rem_task is None or self.rem_task.done():
            self.rem_task = asyncio.create_task(self.reminder_loop(), name="reminder_loop")

    async def stop_background_tasks(self) -> None:
        for task in (self.post_task, self.rem_task):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(
            *(t for t in (self.post_task, self.rem_task) if t),
            return_exceptions=True,
        )


# ---------------------------------------------------------------------------
# Routers and Handlers
# ---------------------------------------------------------------------------
router = Router()
CONFIG: Config
DB_INSTANCE: DB
APP_INSTANCE: AutoPublisher


def is_admin(user_id: int) -> bool:
    return user_id == CONFIG.admin_id


async def admin_only_reply(message: Message) -> None:
    await message.answer("هذا البوت خاص بالإدارة فقط")


@router.message(Command("start"))
@router.message(Command("panel"))
async def cmd_start_panel(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await admin_only_reply(message)
        return
    await state.clear()
    await message.answer(panel_text(DB_INSTANCE), reply_markup=panel_keyboard())


@router.message(lambda m: m.from_user and not is_admin(m.from_user.id))
async def any_other_message(message: Message) -> None:
    await admin_only_reply(message)


@router.callback_query(F.data.startswith(f"{MenuCB.prefix}:"))
async def menu_handler(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("هذا البوت خاص بالإدارة فقط", show_alert=True)
        return

    action = callback.data.split(":", 1)[1]
    db = DB_INSTANCE
    pub = APP_INSTANCE

    async def update_panel(extra: str = "") -> None:
        text = panel_text(db) + (f"\n{extra}" if extra else "")
        await safe_send_or_edit(callback, text, panel_keyboard())

    if action == "refresh":
        await update_panel()
        return

    if action == "start_posts":
        save_settings(db, posting_enabled="1", next_post_at=str(now_ts()))
        await update_panel("✅ تم تشغيل النشر")
        return

    if action == "stop_posts":
        save_settings(db, posting_enabled="0")
        await update_panel("⏸ تم إيقاف النشر")
        return

    if action == "mode_ar":
        save_settings(db, post_mode="arabic")
        await update_panel("✅ النمط: عربي فقط")
        return

    if action == "mode_en":
        save_settings(db, post_mode="english")
        await update_panel("✅ النمط: إنجليزي فقط")
        return

    if action == "mode_alt":
        save_settings(db, post_mode="alternate", alternate_next="english")
        await update_panel("✅ النمط: تناوب")
        return

    if action == "set_post_interval":
        await state.set_state(InputStates.waiting_post_interval)
        await safe_send_or_edit(
            callback,
            "أرسل مدة النشر بالدقائق، أو اختر قيمة من الأزرار:",
            durations_keyboard("post"),
        )
        return

    if action.startswith("post_") and action != "post_now":
        if action.endswith("custom"):
            await state.set_state(InputStates.waiting_post_interval)
            await safe_send_or_edit(callback, "أرسل مدة النشر بالدقائق كرقم فقط:")
        else:
            minutes = int(action.split("_", 1)[1])
            minutes = validate_positive_minutes(minutes)
            save_settings(db, post_interval_min=str(minutes), next_post_at=str(now_ts() + minutes * 60))
            await update_panel(f"✅ تم ضبط مدة النشر إلى {minutes} دقيقة")
        return

    if action == "set_reminder_interval":
        await state.set_state(InputStates.waiting_reminder_interval)
        await safe_send_or_edit(
            callback,
            "أرسل مدة التذكير بالدقائق، أو اختر قيمة من الأزرار:",
            durations_keyboard("rem"),
        )
        return

    if action.startswith("rem_"):
        if action.endswith("custom"):
            await state.set_state(InputStates.waiting_reminder_interval)
            await safe_send_or_edit(callback, "أرسل مدة التذكير بالدقائق كرقم فقط:")
        else:
            minutes = int(action.split("_", 1)[1])
            minutes = validate_positive_minutes(minutes)
            save_settings(db, reminder_interval_min=str(minutes), next_reminder_at=str(now_ts() + minutes * 60))
            await update_panel(f"✅ تم ضبط مدة التذكير إلى {minutes} دقيقة")
        return

    if action == "toggle_reminders":
        s = load_settings(db)
        new_val = "0" if s["reminders_enabled"] else "1"
        save_settings(db, reminders_enabled=new_val, next_reminder_at=str(now_ts() + s["reminder_interval_min"] * 60))
        await update_panel(f"✅ التذكيرات: {'مفعلة' if new_val == '1' else 'متوقفة'}")
        return

    if action == "post_now":
        await callback.answer("جارِ النشر...", show_alert=False)
        try:
            result = await pub.publish_now()
            await update_panel(result)
        except Exception as e:
            logging.exception("Manual post failed")
            await safe_send_or_edit(callback, f"❌ فشل النشر: {e}", panel_keyboard())
        return

    if action == "preview":
        try:
            lang, text = await pub.preview_post()
            preview = f"👁 <b>معاينة ({safe_html(lang)})</b>\n\n{text}"
            await callback.message.answer(preview)
            await callback.answer("تمت المعاينة")
        except Exception as e:
            await callback.answer(f"فشل المعاينة: {e}", show_alert=True)
        return

    if action == "save":
        await update_panel("💾 تم حفظ الإعدادات")
        return

    if action == "status":
        await update_panel()
        return

    if action == "settings":
        await safe_send_or_edit(
            callback,
            "⚙ <b>الإعدادات الحالية</b>\n\n" + panel_text(db),
            mode_keyboard(),
        )
        return

    if action == "last_post":
        row = db.last_history()
        if not row:
            await callback.answer("لا يوجد منشور سابق", show_alert=True)
            return
        created = datetime.fromtimestamp(row["created_at"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        content = row["content"]
        await callback.message.answer(
            f"🕘 <b>آخر عنصر في السجل</b>\n"
            f"• النوع: <b>{safe_html(row['kind'])}</b>\n"
            f"• اللغة: <b>{safe_html(row['language'] or '-')}</b>\n"
            f"• الوقت: <b>{created}</b>\n\n{content}"
        )
        await callback.answer()
        return

    if action == "reset":
        save_settings(
            db,
            posting_enabled="1",
            reminders_enabled="1",
            post_mode="alternate",
            post_interval_min="60",
            reminder_interval_min="30",
            alternate_next="english",
            next_post_at=str(now_ts() + 60 * 60),
            next_reminder_at=str(now_ts() + 30 * 60),
        )
        await update_panel("♻ تمت إعادة الضبط إلى الإعدادات الافتراضية")
        return

    await callback.answer("أمر غير معروف", show_alert=True)


@router.message(InputStates.waiting_post_interval)
async def input_post_interval(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await admin_only_reply(message)
        return
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("أرسل رقمًا صحيحًا بالدقائق فقط.")
        return
    minutes = validate_positive_minutes(int(text))
    save_settings(DB_INSTANCE, post_interval_min=str(minutes), next_post_at=str(now_ts() + minutes * 60))
    await state.clear()
    await message.answer(f"✅ تم ضبط مدة النشر إلى {minutes} دقيقة", reply_markup=panel_keyboard())


@router.message(InputStates.waiting_reminder_interval)
async def input_reminder_interval(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await admin_only_reply(message)
        return
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("أرسل رقمًا صحيحًا بالدقائق فقط.")
        return
    minutes = validate_positive_minutes(int(text))
    save_settings(DB_INSTANCE, reminder_interval_min=str(minutes), next_reminder_at=str(now_ts() + minutes * 60))
    await state.clear()
    await message.answer(f"✅ تم ضبط مدة التذكير إلى {minutes} دقيقة", reply_markup=panel_keyboard())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def on_startup(bot: Bot) -> None:
    logging.info("Starting bot...")
    APP_INSTANCE.start_background_tasks()


async def on_shutdown(bot: Bot) -> None:
    logging.info("Shutting down...")
    await APP_INSTANCE.stop_background_tasks()
    DB_INSTANCE.close()
    await bot.session.close()


async def main() -> None:
    global CONFIG, DB_INSTANCE, APP_INSTANCE

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    CONFIG = Config.from_env()
    DB_INSTANCE = DB(CONFIG.db_path)
    sync_timers(DB_INSTANCE)

    bot = Bot(
        token=CONFIG.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    ai = AIEngine(CONFIG.openai_api_key, CONFIG.ai_model)
    APP_INSTANCE = AutoPublisher(bot=bot, db=DB_INSTANCE, ai=ai)

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await dp.start_polling(
        bot,
        allowed_updates=dp.resolve_used_update_types(),
        on_startup=on_startup,
        on_shutdown=on_shutdown,
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass