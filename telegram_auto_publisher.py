#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import subprocess
import importlib
import logging
import asyncio
import json
import os
import random
import time
from typing import Optional, Dict, Any, List, Tuple, Union
from dataclasses import dataclass, asdict, field
from contextlib import suppress
import signal
import traceback

# =========================
#  AUTO INSTALL DEPENDENCIES
# =========================
required_packages = [
    "aiogram",
    "openai",
    "python-dotenv"
]

for package in required_packages:
    try:
        importlib.import_module(package)
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", package])

# Now import after ensuring installation
import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message
from aiogram.filters import Command, CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
import openai

# =========================
#  CONFIGURATION (EDIT THESE)
# =========================
BOT_TOKEN = "8749230463:AAEu1zuSSkJ8qA287uIvqOQkpRvWrI0bW2s"           # Replace with your bot token
ADMIN_ID = 6891530912                        # Replace with your Telegram user ID
CHANNEL_ID = -1003729799230                 # Replace with your channel ID (negative for private supergroup/channel)

DEFAULT_POST_INTERVAL_MINUTES = 60
DEFAULT_REMINDER_INTERVAL_MINUTES = 30
DEFAULT_POST_MODE = "alternating"           # "arabic_only", "english_only", "alternating"
DEFAULT_LANGUAGE_ROTATION_MODE = "alternating"
DEFAULT_REMINDERS_ENABLED = True

OPENAI_API_KEY = "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"  # Required for AI generation

# =========================
#  CONSTANTS & TEMPLATES
# =========================
SETTINGS_FILE = "bot_settings.json"
LOG_FILE = "bot.log"

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Fallback templates for AI failures
FALLBACK_ENGLISH_QUOTES = [
    "The only way to do great work is to love what you do.",
    "Life is what happens when you're busy making other plans.",
    "The future belongs to those who believe in the beauty of their dreams.",
    "It does not matter how slowly you go as long as you do not stop.",
    "Your time is limited, don't waste it living someone else's life."
]

FALLBACK_ARABIC_POETRY = [
    "﴿ أَلاَ بِذِكْرِ اللَّهِ تَطْمَئِنُّ الْقُلُوبُ ﴾",
    "﴿ وَإِنْ يَمْسَسْكَ اللَّهُ بِضُرٍّ فَلَا كَاشِفَ لَهُ إِلَّا هُوَ ﴾",
    "﴿ فَإِنَّ مَعَ الْعُسْرِ يُسْرًا ﴾",
    "﴿ رَبَّنَا آتِنَا فِي الدُّنْيَا حَسَنَةً وَفِي الْآخِرَةِ حَسَنَةً ﴾",
    "﴿ وَاصْبِرْ فَإِنَّ اللَّهَ لَا يُضِيعُ أَجْرَ الْمُحْسِنِينَ ﴾"
]

ISLAMIC_REMINDERS = [
    "سبحان الله وبحمده، سبحان الله العظيم",
    "اللهم صلِّ على محمد وعلى آل محمد كما صليت على إبراهيم وعلى آل إبراهيم إنك حميد مجيد",
    "أستغفر الله العظيم الذي لا إله إلا هو الحي القيوم وأتوب إليه",
    "لا إله إلا الله وحده لا شريك له، له الملك وله الحمد وهو على كل شيء قدير",
    "حسبي الله لا إله إلا هو عليه توكلت وهو رب العرش العظيم",
    "اللهم إني أسألك العفو والعافية في الدنيا والآخرة",
    "اللهم أعني على ذكرك وشكرك وحسن عبادتك",
    "اللهم إني ظلمت نفسي ظلماً كثيراً ولا يغفر الذنوب إلا أنت فاغفر لي مغفرة من عندك وارحمني إنك أنت الغفور الرحيم"
]

# =========================
#  PERSISTENCE SETTINGS
# =========================
@dataclass
class BotSettings:
    posting_enabled: bool = True
    reminders_enabled: bool = DEFAULT_REMINDERS_ENABLED
    post_mode: str = DEFAULT_POST_MODE  # "arabic_only", "english_only", "alternating"
    post_interval_minutes: int = DEFAULT_POST_INTERVAL_MINUTES
    reminder_interval_minutes: int = DEFAULT_REMINDER_INTERVAL_MINUTES
    last_language: str = "english"  # for alternating mode
    last_post_content: Optional[str] = None
    last_generated_content: Optional[str] = None
    formatting_style_english: int = 0  # 0=bold, 1=italic, 2=code, etc.
    formatting_style_arabic: int = 0   # 0=decorative, 1=plain
    rotation_mode: str = "alternating"  # same as post_mode but kept for clarity
    version: int = 1

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "BotSettings":
        return cls(**data)

class SettingsManager:
    _lock = asyncio.Lock()

    def __init__(self, filepath: str):
        self.filepath = filepath
        self.settings = self.load()

    def load(self) -> BotSettings:
        try:
            if os.path.exists(self.filepath):
                with open(self.filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return BotSettings.from_dict(data)
        except Exception as e:
            logger.error(f"Failed to load settings: {e}")
        return BotSettings()

    async def save(self) -> bool:
        async with self._lock:
            try:
                with open(self.filepath, "w", encoding="utf-8") as f:
                    json.dump(self.settings.to_dict(), f, ensure_ascii=False, indent=2)
                return True
            except Exception as e:
                logger.error(f"Failed to save settings: {e}")
                return False

    async def update(self, **kwargs) -> bool:
        for key, value in kwargs.items():
            if hasattr(self.settings, key):
                setattr(self.settings, key, value)
        return await self.save()

    def get(self) -> BotSettings:
        return self.settings

    async def reload(self) -> None:
        self.settings = self.load()

# =========================
#  AI GENERATION (OpenAI)
# =========================
class AIGenerator:
    def __init__(self, api_key: str):
        openai.api_key = api_key
        self.client = openai.AsyncOpenAI(api_key=api_key)

    async def generate_english_quote(self, style: int = None) -> str:
        styles = [
            "profound and inspiring",
            "minimalist and deep",
            "philosophical and thought-provoking",
            "poetic and lyrical"
        ]
        chosen_style = styles[style % len(styles)] if style is not None else random.choice(styles)
        prompt = f"Generate a short, profound English quote (max 15 words) in the style of {chosen_style}. Do not use clichés. Make it original and impactful."

        try:
            response = await self.client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=50,
                temperature=0.8
            )
            quote = response.choices[0].message.content.strip()
            if not quote:
                raise ValueError("Empty response")
            return quote
        except Exception as e:
            logger.error(f"AI English generation failed: {e}")
            return random.choice(FALLBACK_ENGLISH_QUOTES)

    async def generate_arabic_poetry(self, style: int = None) -> str:
        styles = [
            "classical Arabic poetry with full diacritics (tashkeel)",
            "ancient style of Imru' al-Qais",
            "wisdom poetry like Al-Mutanabbi",
            "mystical Sufi poetry with rich imagery"
        ]
        chosen_style = styles[style % len(styles)] if style is not None else random.choice(styles)
        prompt = f"اكتب بيتاً واحداً من الشعر العربي الفصيح (شطرين) بالكامل مع التشكيل، بأسلوب {chosen_style}. تأكد من صحة اللغة وجودة السبك. اخرج النص فقط دون تعليقات."

        try:
            response = await self.client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=100,
                temperature=0.9
            )
            poetry = response.choices[0].message.content.strip()
            if not poetry:
                raise ValueError("Empty response")
            # Ensure it has diacritics? Not mandatory but we trust AI.
            return poetry
        except Exception as e:
            logger.error(f"AI Arabic generation failed: {e}")
            return random.choice(FALLBACK_ARABIC_POETRY)

# =========================
#  TEXT FORMATTERS
# =========================
class TextFormatter:
    @staticmethod
    def format_english(text: str, style: int = 0) -> str:
        # Style 0: Bold, 1: Italic, 2: Code, 3: Underline
        if style == 0:
            return f"<b>{text}</b>"
        elif style == 1:
            return f"<i>{text}</i>"
        elif style == 2:
            return f"<code>{text}</code>"
        elif style == 3:
            return f"<u>{text}</u>"
        else:
            return f"<b>{text}</b>"  # default bold

    @staticmethod
    def format_arabic(text: str, style: int = 0) -> str:
        # Style 0: Decorative with ﴿ ﴾, Style 1: Plain
        if style == 0:
            return f"﴿ {text} ﴾"
        else:
            return text

# =========================
#  SCHEDULER TASKS
# =========================
class Scheduler:
    def __init__(self, bot: Bot, settings_mgr: SettingsManager, ai_gen: AIGenerator):
        self.bot = bot
        self.settings_mgr = settings_mgr
        self.ai_gen = ai_gen
        self.posting_task: Optional[asyncio.Task] = None
        self.reminder_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    async def start_posting_task(self):
        if self.posting_task and not self.posting_task.done():
            self.posting_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.posting_task
        self.posting_task = asyncio.create_task(self._posting_loop())

    async def stop_posting_task(self):
        if self.posting_task and not self.posting_task.done():
            self.posting_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.posting_task
            self.posting_task = None

    async def start_reminder_task(self):
        if self.reminder_task and not self.reminder_task.done():
            self.reminder_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.reminder_task
        self.reminder_task = asyncio.create_task(self._reminder_loop())

    async def stop_reminder_task(self):
        if self.reminder_task and not self.reminder_task.done():
            self.reminder_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.reminder_task
            self.reminder_task = None

    async def _posting_loop(self):
        while True:
            try:
                settings = self.settings_mgr.get()
                if not settings.posting_enabled:
                    await asyncio.sleep(10)
                    continue

                # Determine next language based on mode
                lang = None
                if settings.post_mode == "arabic_only":
                    lang = "arabic"
                elif settings.post_mode == "english_only":
                    lang = "english"
                else:  # alternating
                    lang = "arabic" if settings.last_language == "english" else "english"
                    # update last_language after sending
                # Generate content
                if lang == "english":
                    quote = await self.ai_gen.generate_english_quote(settings.formatting_style_english)
                    formatted = TextFormatter.format_english(quote, settings.formatting_style_english)
                else:
                    poetry = await self.ai_gen.generate_arabic_poetry(settings.formatting_style_arabic)
                    formatted = TextFormatter.format_arabic(poetry, settings.formatting_style_arabic)

                # Send to channel
                try:
                    await self.bot.send_message(chat_id=CHANNEL_ID, text=formatted, parse_mode="HTML")
                    logger.info(f"Posted {lang} content to channel")
                    # Update last_language if alternating
                    if settings.post_mode == "alternating":
                        settings.last_language = lang
                        await self.settings_mgr.update(last_language=lang)
                    await self.settings_mgr.update(last_post_content=formatted)
                except Exception as e:
                    logger.error(f"Failed to send post: {e}")

                # Wait for next interval
                await asyncio.sleep(settings.post_interval_minutes * 60)
            except asyncio.CancelledError:
                logger.info("Posting loop cancelled")
                break
            except Exception as e:
                logger.error(f"Unexpected error in posting loop: {e}")
                await asyncio.sleep(30)  # backoff

    async def _reminder_loop(self):
        while True:
            try:
                settings = self.settings_mgr.get()
                if not settings.reminders_enabled:
                    await asyncio.sleep(10)
                    continue

                # Pick random reminder
                reminder = random.choice(ISLAMIC_REMINDERS)
                try:
                    await self.bot.send_message(chat_id=CHANNEL_ID, text=reminder, parse_mode="HTML")
                    logger.info("Sent Islamic reminder")
                except Exception as e:
                    logger.error(f"Failed to send reminder: {e}")

                await asyncio.sleep(settings.reminder_interval_minutes * 60)
            except asyncio.CancelledError:
                logger.info("Reminder loop cancelled")
                break
            except Exception as e:
                logger.error(f"Unexpected error in reminder loop: {e}")
                await asyncio.sleep(30)

    async def restart_all(self):
        await self.stop_posting_task()
        await self.stop_reminder_task()
        await self.start_posting_task()
        await self.start_reminder_task()

# =========================
#  INLINE KEYBOARDS
# =========================
def main_menu(settings: BotSettings) -> InlineKeyboardMarkup:
    kb = []
    # Row 1: Toggle Posting
    status_text = "✅ نشر نشط" if settings.posting_enabled else "⛔ نشر متوقف"
    kb.append([InlineKeyboardButton(text=status_text, callback_data="toggle_posting")])
    # Row 2: Post Mode
    mode_text = f"نمط النشر: {settings.post_mode}"
    kb.append([InlineKeyboardButton(text=mode_text, callback_data="change_mode")])
    # Row 3: Change intervals
    kb.append([
        InlineKeyboardButton(text=f"⏱️ مدة النشر ({settings.post_interval_minutes} د)", callback_data="set_post_interval"),
        InlineKeyboardButton(text=f"🕰️ مدة التذكير ({settings.reminder_interval_minutes} د)", callback_data="set_reminder_interval")
    ])
    # Row 4: Reminders toggle
    reminder_text = "🔔 التذكيرات مفعلة" if settings.reminders_enabled else "🔕 التذكيرات معطلة"
    kb.append([InlineKeyboardButton(text=reminder_text, callback_data="toggle_reminders")])
    # Row 5: Manual actions
    kb.append([
        InlineKeyboardButton(text="📝 توليد منشور الآن", callback_data="generate_now"),
        InlineKeyboardButton(text="👁️ معاينة", callback_data="preview")
    ])
    # Row 6: Additional
    kb.append([
        InlineKeyboardButton(text="📋 آخر منشور", callback_data="last_post"),
        InlineKeyboardButton(text="⚙️ الإعدادات", callback_data="show_settings")
    ])
    # Row 7: System
    kb.append([
        InlineKeyboardButton(text="🔄 إعادة تعيين الإعدادات", callback_data="reset_settings"),
        InlineKeyboardButton(text="📊 حالة النظام", callback_data="system_status")
    ])
    # Row 8: Formatting styles
    kb.append([
        InlineKeyboardButton(text="🎨 تنسيق إنجليزي", callback_data="change_english_style"),
        InlineKeyboardButton(text="🎨 تنسيق عربي", callback_data="change_arabic_style")
    ])
    # Row 9: Emergency
    kb.append([
        InlineKeyboardButton(text="⏹️ إيقاف فوري", callback_data="emergency_stop"),
        InlineKeyboardButton(text="▶️ تشغيل تلقائي", callback_data="auto_start")
    ])
    # Row 10: Reload settings
    kb.append([InlineKeyboardButton(text="🔄 إعادة تحميل الإعدادات", callback_data="reload_settings")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def mode_selection_menu() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(text="عربي فقط", callback_data="mode_arabic_only")],
        [InlineKeyboardButton(text="إنجليزي فقط", callback_data="mode_english_only")],
        [InlineKeyboardButton(text="تناوب", callback_data="mode_alternating")],
        [InlineKeyboardButton(text="🔙 رجوع", callback_data="back_to_main")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def interval_menu(interval_type: str) -> InlineKeyboardMarkup:
    kb = []
    for minutes in [15, 30, 60, 120, 240]:
        kb.append([InlineKeyboardButton(text=f"{minutes} دقيقة", callback_data=f"{interval_type}_{minutes}")])
    kb.append([InlineKeyboardButton(text="🔙 رجوع", callback_data="back_to_main")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def style_menu(style_type: str) -> InlineKeyboardMarkup:
    kb = []
    if style_type == "english":
        options = ["غامق (Bold)", "مائل (Italic)", "كود (Code)", "مسطر (Underline)"]
        for idx, opt in enumerate(options):
            kb.append([InlineKeyboardButton(text=opt, callback_data=f"style_eng_{idx}")])
    else:
        options = ["زخرفي (﴿ ﴾)", "عادي"]
        for idx, opt in enumerate(options):
            kb.append([InlineKeyboardButton(text=opt, callback_data=f"style_arb_{idx}")])
    kb.append([InlineKeyboardButton(text="🔙 رجوع", callback_data="back_to_main")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

# =========================
#  BOT HANDLERS
# =========================
def admin_only(func):
    async def wrapper(message_or_callback, *args, **kwargs):
        user_id = None
        if isinstance(message_or_callback, Message):
            user_id = message_or_callback.from_user.id
        elif isinstance(message_or_callback, CallbackQuery):
            user_id = message_or_callback.from_user.id
        else:
            return
        if user_id != ADMIN_ID:
            if isinstance(message_or_callback, Message):
                await message_or_callback.reply("هذا البوت خاص بالإدارة فقط")
            elif isinstance(message_or_callback, CallbackQuery):
                await message_or_callback.answer("غير مصرح لك", show_alert=True)
            return
        return await func(message_or_callback, *args, **kwargs)
    return wrapper

@admin_only
async def cmd_start(message: Message, bot: Bot, settings_mgr: SettingsManager):
    settings = settings_mgr.get()
    await message.reply(
        "مرحباً أيها الأدمن! لوحة التحكم الرئيسية:",
        reply_markup=main_menu(settings)
    )

@admin_only
async def handle_callback(callback: CallbackQuery, bot: Bot, settings_mgr: SettingsManager, ai_gen: AIGenerator, scheduler: Scheduler):
    data = callback.data
    settings = settings_mgr.get()
    await callback.answer()  # acknowledge

    if data == "toggle_posting":
        new_state = not settings.posting_enabled
        await settings_mgr.update(posting_enabled=new_state)
        if new_state:
            await scheduler.start_posting_task()
        else:
            await scheduler.stop_posting_task()
        await callback.message.edit_text(
            f"تم {'تفعيل' if new_state else 'إيقاف'} النشر التلقائي.",
            reply_markup=main_menu(settings_mgr.get())
        )

    elif data == "change_mode":
        await callback.message.edit_text("اختر نمط النشر:", reply_markup=mode_selection_menu())

    elif data.startswith("mode_"):
        mode = data.split("_")[1]
        if mode == "arabic_only":
            await settings_mgr.update(post_mode="arabic_only")
        elif mode == "english_only":
            await settings_mgr.update(post_mode="english_only")
        elif mode == "alternating":
            await settings_mgr.update(post_mode="alternating")
        await callback.message.edit_text(f"تم ضبط نمط النشر على {mode}.", reply_markup=main_menu(settings_mgr.get()))

    elif data == "set_post_interval":
        await callback.message.edit_text("اختر مدة النشر (بالدقائق):", reply_markup=interval_menu("post_interval"))

    elif data == "set_reminder_interval":
        await callback.message.edit_text("اختر مدة التذكير (بالدقائق):", reply_markup=interval_menu("reminder_interval"))

    elif data.startswith("post_interval_"):
        minutes = int(data.split("_")[2])
        await settings_mgr.update(post_interval_minutes=minutes)
        await scheduler.restart_all()  # restart tasks to apply new interval
        await callback.message.edit_text(f"تم ضبط مدة النشر إلى {minutes} دقيقة.", reply_markup=main_menu(settings_mgr.get()))

    elif data.startswith("reminder_interval_"):
        minutes = int(data.split("_")[2])
        await settings_mgr.update(reminder_interval_minutes=minutes)
        await scheduler.restart_all()
        await callback.message.edit_text(f"تم ضبط مدة التذكير إلى {minutes} دقيقة.", reply_markup=main_menu(settings_mgr.get()))

    elif data == "toggle_reminders":
        new_state = not settings.reminders_enabled
        await settings_mgr.update(reminders_enabled=new_state)
        if new_state:
            await scheduler.start_reminder_task()
        else:
            await scheduler.stop_reminder_task()
        await callback.message.edit_text(
            f"تم {'تفعيل' if new_state else 'إيقاف'} التذكيرات.",
            reply_markup=main_menu(settings_mgr.get())
        )

    elif data == "generate_now":
        # Generate based on current mode, but allow preview
        # For simplicity, we generate one random (but honor mode?)
        if settings.post_mode == "arabic_only":
            content = await ai_gen.generate_arabic_poetry(settings.formatting_style_arabic)
            formatted = TextFormatter.format_arabic(content, settings.formatting_style_arabic)
        elif settings.post_mode == "english_only":
            content = await ai_gen.generate_english_quote(settings.formatting_style_english)
            formatted = TextFormatter.format_english(content, settings.formatting_style_english)
        else:
            # alternating - choose opposite of last, but for manual we can just pick random
            lang = random.choice(["arabic", "english"])
            if lang == "arabic":
                content = await ai_gen.generate_arabic_poetry(settings.formatting_style_arabic)
                formatted = TextFormatter.format_arabic(content, settings.formatting_style_arabic)
            else:
                content = await ai_gen.generate_english_quote(settings.formatting_style_english)
                formatted = TextFormatter.format_english(content, settings.formatting_style_english)
        await settings_mgr.update(last_generated_content=formatted)
        await callback.message.edit_text(
            f"تم توليد النص:\n\n{formatted}\n\nهل تريد نشره الآن؟",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="نشر الآن", callback_data="publish_generated")],
                [InlineKeyboardButton(text="إلغاء", callback_data="back_to_main")]
            ])
        )

    elif data == "publish_generated":
        last_gen = settings.last_generated_content
        if last_gen:
            try:
                await bot.send_message(chat_id=CHANNEL_ID, text=last_gen, parse_mode="HTML")
                await callback.message.edit_text("تم نشر المحتوى في القناة.", reply_markup=main_menu(settings_mgr.get()))
            except Exception as e:
                await callback.message.edit_text(f"فشل النشر: {e}", reply_markup=main_menu(settings_mgr.get()))
        else:
            await callback.message.edit_text("لا يوجد محتوى مولد مسبقاً.", reply_markup=main_menu(settings_mgr.get()))

    elif data == "preview":
        # Generate preview without sending to channel
        if settings.post_mode == "arabic_only":
            content = await ai_gen.generate_arabic_poetry(settings.formatting_style_arabic)
            formatted = TextFormatter.format_arabic(content, settings.formatting_style_arabic)
        elif settings.post_mode == "english_only":
            content = await ai_gen.generate_english_quote(settings.formatting_style_english)
            formatted = TextFormatter.format_english(content, settings.formatting_style_english)
        else:
            lang = random.choice(["arabic", "english"])
            if lang == "arabic":
                content = await ai_gen.generate_arabic_poetry(settings.formatting_style_arabic)
                formatted = TextFormatter.format_arabic(content, settings.formatting_style_arabic)
            else:
                content = await ai_gen.generate_english_quote(settings.formatting_style_english)
                formatted = TextFormatter.format_english(content, settings.formatting_style_english)
        await settings_mgr.update(last_generated_content=formatted)
        await callback.message.edit_text(
            f"معاينة النص:\n\n{formatted}\n\nهل تريد نشره؟",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="نشر الآن", callback_data="publish_generated")],
                [InlineKeyboardButton(text="إلغاء", callback_data="back_to_main")]
            ])
        )

    elif data == "last_post":
        last = settings.last_post_content or "لا يوجد منشورات سابقة."
        await callback.message.edit_text(f"آخر منشور:\n{last}", reply_markup=main_menu(settings_mgr.get()))

    elif data == "show_settings":
        text = f"""الإعدادات الحالية:
- النشر التلقائي: {'مفعل' if settings.posting_enabled else 'معطل'}
- نمط النشر: {settings.post_mode}
- مدة النشر: {settings.post_interval_minutes} دقيقة
- التذكيرات: {'مفعلة' if settings.reminders_enabled else 'معطلة'}
- مدة التذكير: {settings.reminder_interval_minutes} دقيقة
- آخر لغة: {settings.last_language}
"""
        await callback.message.edit_text(text, reply_markup=main_menu(settings_mgr.get()))

    elif data == "reset_settings":
        new_settings = BotSettings()
        await settings_mgr.update(**new_settings.to_dict())
        await scheduler.restart_all()
        await callback.message.edit_text("تم إعادة تعيين جميع الإعدادات.", reply_markup=main_menu(settings_mgr.get()))

    elif data == "system_status":
        text = f"حالة النظام:\n- البوت يعمل\n- النشر: {'نشط' if settings.posting_enabled else 'متوقف'}\n- التذكيرات: {'نشطة' if settings.reminders_enabled else 'متوقفة'}\n- إصدار الإعدادات: {settings.version}"
        await callback.message.edit_text(text, reply_markup=main_menu(settings_mgr.get()))

    elif data == "change_english_style":
        await callback.message.edit_text("اختر نمط التنسيق للإنجليزية:", reply_markup=style_menu("english"))

    elif data == "change_arabic_style":
        await callback.message.edit_text("اختر نمط التنسيق للعربية:", reply_markup=style_menu("arabic"))

    elif data.startswith("style_eng_"):
        style_idx = int(data.split("_")[2])
        await settings_mgr.update(formatting_style_english=style_idx)
        await callback.message.edit_text("تم تغيير نمط التنسيق للإنجليزية.", reply_markup=main_menu(settings_mgr.get()))

    elif data.startswith("style_arb_"):
        style_idx = int(data.split("_")[2])
        await settings_mgr.update(formatting_style_arabic=style_idx)
        await callback.message.edit_text("تم تغيير نمط التنسيق للعربية.", reply_markup=main_menu(settings_mgr.get()))

    elif data == "emergency_stop":
        await settings_mgr.update(posting_enabled=False, reminders_enabled=False)
        await scheduler.stop_posting_task()
        await scheduler.stop_reminder_task()
        await callback.message.edit_text("تم إيقاف جميع المهام فورياً.", reply_markup=main_menu(settings_mgr.get()))

    elif data == "auto_start":
        await settings_mgr.update(posting_enabled=True, reminders_enabled=DEFAULT_REMINDERS_ENABLED)
        await scheduler.start_posting_task()
        await scheduler.start_reminder_task()
        await callback.message.edit_text("تم تشغيل النشر والتذكيرات تلقائياً.", reply_markup=main_menu(settings_mgr.get()))

    elif data == "reload_settings":
        await settings_mgr.reload()
        await scheduler.restart_all()
        await callback.message.edit_text("تم إعادة تحميل الإعدادات من الملف.", reply_markup=main_menu(settings_mgr.get()))

    elif data == "back_to_main":
        await callback.message.edit_text("لوحة التحكم الرئيسية:", reply_markup=main_menu(settings_mgr.get()))

    else:
        await callback.message.edit_text("خيار غير معروف.", reply_markup=main_menu(settings_mgr.get()))

# =========================
#  MAIN ENTRYPOINT
# =========================
async def main():
    # Validate configurations
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE" or ADMIN_ID == 123456789 or CHANNEL_ID == -1001234567890 or OPENAI_API_KEY == "YOUR_OPENAI_API_KEY_HERE":
        logger.critical("Please set your BOT_TOKEN, ADMIN_ID, CHANNEL_ID, and OPENAI_API_KEY in the script.")
        sys.exit(1)

    # Initialize bot and dispatcher
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    # Initialize managers and AI
    settings_mgr = SettingsManager(SETTINGS_FILE)
    ai_gen = AIGenerator(OPENAI_API_KEY)
    scheduler = Scheduler(bot, settings_mgr, ai_gen)

    # Register handlers
    dp.message.register(cmd_start, CommandStart())
    dp.callback_query.register(lambda c: handle_callback(c, bot, settings_mgr, ai_gen, scheduler))

    # Start scheduler tasks based on current settings
    settings = settings_mgr.get()
    if settings.posting_enabled:
        await scheduler.start_posting_task()
    if settings.reminders_enabled:
        await scheduler.start_reminder_task()

    # Graceful shutdown
    async def shutdown():
        logger.info("Shutting down...")
        await scheduler.stop_posting_task()
        await scheduler.stop_reminder_task()
        await bot.session.close()

    dp.shutdown.register(shutdown)

    # Start polling
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await shutdown()

if __name__ == "__main__":
    asyncio.run(main())
