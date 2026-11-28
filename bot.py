"""
Бот-воронка: бесплатный курс в обмен на подписку на канал.
Работает бесплатно: Render (free web service) + Google Таблица вместо базы.

1. Человек жмёт /start -> бот просит подписаться на канал.
2. После подписки уроки приходят по расписанию из lessons.json.
3. Перед каждым уроком бот проверяет подписку. Отписался - курс на паузе,
   подписался обратно - продолжает с того же урока.
4. После последнего урока - оффер платного сообщества.

Как бот просыпается на бесплатном Render:
- когда кто-то пишет боту (Telegram присылает webhook);
- раз в час его будит скрипт Google Таблицы (запрос на /tick).
"""
import asyncio
import html
import json
import logging
import os
import re
import time
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (CallbackQuery, ChatJoinRequest, InlineKeyboardButton, InlineKeyboardMarkup,
                           Message, MessageOriginChannel)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
import stripe
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()
BASE = Path(__file__).parent
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ["CHANNEL_ID"]            # @username канала или -100...
CHANNEL_URL = os.environ["CHANNEL_URL"]          # ссылка для кнопки "Подписаться"
STORAGE_CHAT_ID = os.getenv("STORAGE_CHAT_ID")   # закрытый канал, где лежат уроки
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
SHEET_URL = os.environ["SHEET_URL"]              # адрес веб-приложения Apps Script
SECRET = os.environ["SECRET"]                    # общий пароль: таблица, /tick, webhook
PUBLIC_URL = os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL")  # Render подставляет сам
PORT = int(os.getenv("PORT", "10000"))
PROTECT = os.getenv("PROTECT_CONTENT", "1") != "0"   # запрет пересылки и сохранения уроков
REMIND_AFTER_H = float(os.getenv("REMIND_AFTER_HOURS", "2"))  # напомнить про подписку через N часов
REMIND_MAX_AGE_H = 72   # тем, кто пришёл больше 3 дней назад, не напоминаем

# ---------- оплата сообщества (Stripe) ----------
# Если STRIPE_SECRET_KEY не задан, бот работает как раньше: кнопка оффера ведёт на button_url.
STRIPE_KEY = os.getenv("STRIPE_SECRET_KEY")              # лучше restricted key (rk_...), см. README
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")   # whsec_... из Dashboard → Webhooks
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID")           # price_... подписки $50/мес
COMMUNITY_CHAT_ID = os.getenv("COMMUNITY_CHAT_ID")       # закрытый чат/канал сообщества (-100...)
STRIPE_AUTOMATIC_TAX = os.getenv("STRIPE_AUTOMATIC_TAX", "0") == "1"  # включать только после настройки Stripe Tax
PRICE_LABEL = os.getenv("PRICE_LABEL", "$50 в месяц")   # текст на кнопке оплаты
PAYMENTS_ON = bool(STRIPE_KEY and STRIPE_PRICE_ID and COMMUNITY_CHAT_ID)
CHECKOUT_LABEL = "tg_community_kqwhzmra"   # integration_identifier: метка потока оплаты в Dashboard
ACTIVE_STATUSES = {"active", "trialing"}
GRACE_STATUSES = {"past_due"}               # оплата не прошла, Stripe ещё пробует списать — доступ оставляем
END_STATUSES = {"canceled", "unpaid", "incomplete_expired", "paused"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

# ---------- уроки ----------
def load_course():
    with open(BASE / "lessons.json", encoding="utf-8") as f:
        return json.load(f)

COURSE = load_course()

# ---------- хранилище: Google Таблица ----------
FIELDS = ["user_id", "username", "first_name", "joined_at", "started_at",
          "next_lesson", "paused", "offer_sent", "blocked", "reminded", "source",
          "stripe_customer", "stripe_sub", "sub_status"]
INT_FIELDS = {"user_id", "joined_at", "started_at", "next_lesson", "paused", "offer_sent", "blocked", "reminded"}
USERS: dict[int, dict] = {}   # копия таблицы в памяти, грузится при каждом старте сервера
http: aiohttp.ClientSession | None = None

async def sheet_call(payload: dict):
    payload["key"] = SECRET
    for attempt in range(3):
        try:
            async with http.post(SHEET_URL, data=json.dumps(payload),
                                 timeout=aiohttp.ClientTimeout(total=60)) as r:
                data = json.loads(await r.text())
                if not data.get("ok"):
                    raise RuntimeError(data.get("error", "ошибка таблицы"))
                return data
        except Exception as e:
            log.warning("Таблица не ответила (%s), попытка %d", e, attempt + 1)
            await asyncio.sleep(2 * (attempt + 1))
    raise RuntimeError("Google Таблица недоступна")

def norm(row: dict) -> dict:
    u = {}
    for f in FIELDS:
        v = row.get(f, "")
        if f in INT_FIELDS:
            u[f] = int(float(v)) if v not in ("", None) else (None if f == "started_at" else 0)
        else:
            u[f] = v or None
    return u

async def load_users():
    data = await sheet_call({"action": "list"})
    USERS.clear()
    for row in data["users"]:
        u = norm(row)
        if u["user_id"]:
            USERS[u["user_id"]] = u
    log.info("Из таблицы загружено пользователей: %d", len(USERS))

def get_user(uid):
    return USERS.get(uid)

async def upd(uid, **kw):
    USERS[uid].update(kw)
    await sheet_call({"action": "upsert", "user": USERS[uid]})

async def add_user(uid, username, first_name, source=None):
    USERS[uid] = {"user_id": uid, "username": username, "first_name": first_name,
                  "joined_at": int(time.time()), "started_at": None, "next_lesson": 0,
                  "paused": 0, "offer_sent": 0, "blocked": 0, "reminded": 0,
                  "source": source or "direct",
                  "stripe_customer": None, "stripe_sub": None, "sub_status": None}
    await sheet_call({"action": "upsert", "user": USERS[uid]})

# ---------- тексты и кнопки ----------
DIGITS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

def program_text() -> str:
    lines = []
    for i, les in enumerate(COURSE["lessons"]):
        line = les.get("teaser") or les["title"].split(". ", 1)[-1]   # "Урок 2. Инструмент…" -> "Инструмент…"
        head, sep, rest = line.partition(":")
        text = f"<b>{html.escape(head)}:</b>{html.escape(rest)}" if sep else f"<b>{html.escape(line)}</b>"
        num = DIGITS[i] if i < len(DIGITS) else f"{i + 1}."
        lines.append(f"{num} {text}")
    return "\n".join(lines)

def welcome_text(name: str) -> str:
    n = len(COURSE["lessons"])
    return (
        f"Привет, {name}! 👋\n\n"
        "Это <b>бесплатный курс «Хендимен-бизнес в США с нуля»</b> — первый шаг в сервисный бизнес. "
        "Никаких оплат, карт и скрытых платежей.\n\n"
        f"📚 <b>{n} видео-уроков, по одному в день</b> — приходят прямо сюда, в бот:\n\n"
        f"{program_text()}\n\n"
        "💵 <b>Сколько можно заработать.</b> Если делать по шагам, первые клиенты появляются уже в первую неделю — "
        "это <b>$150–$1 000</b>. В первый месяц новички выходят на <b>$2 000–$3 000</b>, дальше — "
        "сколько готов работать.\n"
        "<i>Цифры — ориентир, а не гарантия: результат зависит от тебя.</i>\n\n"
        "Условие одно — подписка на мой канал, там я каждую неделю разбираю сервисный бизнес в США.\n\n"
        "👇 Подпишись и нажми <b>«Я подписался ✅»</b> — первый урок придёт сразу."
    )

REMINDER = ("{name}, курс тебя ждёт 🙌\n\n"
            "Напоминаю: он <b>действительно бесплатный</b> — {n} видео-уроков о том, как запустить "
            "хендимен-бизнес в США с нуля и найти первых клиентов уже в первую неделю. Платить ничего не нужно.\n\n"
            "Осталось одно: подписаться на канал и нажать «Я подписался ✅». "
            "Первый урок придёт сразу.")
NOT_SUBSCRIBED = "Не вижу подписки 🤔 Подпишись на канал и нажми кнопку ещё раз."
STARTED = "Отлично, доступ открыт! 🚀 Первый урок уже летит."
PAUSED = ("Ты отписался от канала, поэтому курс на паузе ⏸\n"
          "Подпишись обратно — и уроки продолжат приходить с того же места.")
RESUMED = "С возвращением! Продолжаем курс ▶️"
FINISHED = "Ты уже прошёл весь курс. Если что-то пропустил — напиши /lessons."

def sub_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться на канал", url=CHANNEL_URL)],
        [InlineKeyboardButton(text="Я подписался ✅", callback_data="check_sub")],
    ])

SOURCES = [("tiktok", "TikTok"), ("insta", "Instagram"), ("yt", "YouTube"),
           ("fb", "Facebook"), ("tg", "Telegram"), ("friend", "Друг посоветовал"), ("other", "Другое")]
def bonus_ready() -> bool:
    b = COURSE.get("source_bonus") or {}
    return bool(b.get("source_message_id") and STORAGE_CHAT_ID)

def source_question() -> str:
    q = "Кстати, откуда ты узнал о курсе? Нажми один вариант"
    return q + (" — и я пришлю тебе бонус: прайс-лист хендимена в США 🎁" if bonus_ready() else
                " — это поможет мне делать больше полезного 🙏")

def source_kb():
    rows, row = [], []
    for key, label in SOURCES:
        row.append(InlineKeyboardButton(text=label, callback_data=f"src:{key}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)

def needs_source_question(u) -> bool:
    """Спрашиваем, только если человек пришёл не по ссылке с меткой."""
    return (u.get("source") or "direct") == "direct"

def offer_kb():
    o = COURSE["final_offer"]
    if PAYMENTS_ON:   # оплата через бота: кнопка создаёт личную ссылку Stripe Checkout
        btn = InlineKeyboardButton(text=o["button_text"], callback_data="join")
    else:
        btn = InlineKeyboardButton(text=o["button_text"], url=o["button_url"])
    return InlineKeyboardMarkup(inline_keyboard=[[btn]])

# ---------- логика курса ----------
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
deliver_lock = asyncio.Lock()

async def is_subscribed(uid: int) -> bool:
    try:
        m = await bot.get_chat_member(CHANNEL_ID, uid)
    except TelegramBadRequest as e:
        log.error("Не могу проверить подписку (бот админ в канале?): %s", e)
        return False
    if m.status in ("member", "administrator", "creator"):
        return True
    return m.status == "restricted" and getattr(m, "is_member", False)

def offer_delay_hours():
    lessons = COURSE["lessons"]
    return (lessons[-1]["delay_hours"] if lessons else 0) + COURSE["final_offer"]["delay_hours_after_last"]

def step_delay_hours(idx):
    lessons = COURSE["lessons"]
    return lessons[idx]["delay_hours"] if idx < len(lessons) else offer_delay_hours()

async def send_lesson(uid: int, idx: int):
    les = COURSE["lessons"][idx]
    await bot.send_message(uid, f"📚 <b>{html.escape(les['title'])}</b>")
    if les.get("source_message_id") and STORAGE_CHAT_ID:
        await bot.copy_message(uid, STORAGE_CHAT_ID, les["source_message_id"], protect_content=PROTECT)
    if les.get("text"):
        await bot.send_message(uid, les["text"], protect_content=PROTECT)

async def remind_due(uid: int):
    """Одно напоминание тем, кто нажал Start, но так и не подписался."""
    u = get_user(uid)
    if not u or u["started_at"] or u["blocked"] or u["reminded"]:
        return
    age_h = (time.time() - (u["joined_at"] or 0)) / 3600
    if age_h < REMIND_AFTER_H or age_h > REMIND_MAX_AGE_H:
        return
    try:
        name = html.escape(u["first_name"] or "Привет")
        await bot.send_message(uid, REMINDER.format(name=name, n=len(COURSE["lessons"])), reply_markup=sub_kb())
        await upd(uid, reminded=1)
    except TelegramForbiddenError:
        await upd(uid, blocked=1, reminded=1)
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)


async def deliver_due(uid: int):
    """Отправляет этому пользователю всё, что уже пора отправить."""
    lessons = COURSE["lessons"]
    now = time.time()
    try:
        while True:
            u = get_user(uid)
            if not u or not u["started_at"] or u["paused"] or u["blocked"] or u["offer_sent"]:
                return
            idx = u["next_lesson"]
            if u["started_at"] + step_delay_hours(idx) * 3600 > now:
                return
            if not await is_subscribed(uid):
                await upd(uid, paused=1)
                await bot.send_message(uid, PAUSED, reply_markup=sub_kb())
                return
            if idx < len(lessons):
                await send_lesson(uid, idx)
                await upd(uid, next_lesson=idx + 1)
            else:
                await bot.send_message(uid, COURSE["final_offer"]["text"], reply_markup=offer_kb())
                await upd(uid, offer_sent=1)
                return
            await asyncio.sleep(0.5)
    except TelegramForbiddenError:
        await upd(uid, blocked=1)
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)

async def deliver_all():
    if deliver_lock.locked():
        return
    async with deliver_lock:
        for uid in list(USERS):
            try:
                await remind_due(uid)
                await deliver_due(uid)
            except Exception:
                log.exception("Ошибка при отправке пользователю %s", uid)
            await asyncio.sleep(0.05)

async def start_course(uid: int):
    await upd(uid, started_at=int(time.time()), next_lesson=0, paused=0)
    await deliver_due(uid)   # урок с delay 0 приходит сразу

async def resume_course(uid: int):
    # следующий урок приходит сейчас, дальше — с прежними интервалами
    u = get_user(uid)
    await upd(uid, paused=0, started_at=int(time.time() - step_delay_hours(u["next_lesson"]) * 3600))
    await deliver_due(uid)

# ---------- хендлеры пользователя ----------
def clean_source(raw: str | None) -> str | None:
    """Метка из ссылки t.me/bot?start=tiktok -> 'tiktok'."""
    if not raw:
        return None
    src = re.sub(r"[^a-z0-9_-]", "", raw.strip().lower())[:40]
    return src or None

@dp.message(CommandStart())
async def on_start(msg: Message, command: CommandObject):
    uid = msg.from_user.id
    if command.args == "paid":   # вернулся со страницы оплаты Stripe
        await on_paid_return(msg)
        return
    u = get_user(uid)
    if not u:
        await add_user(uid, msg.from_user.username, msg.from_user.first_name, clean_source(command.args))
        u = get_user(uid)
    elif u["blocked"]:
        await upd(uid, blocked=0)
    if u["started_at"]:
        if u["offer_sent"] or u["next_lesson"] >= len(COURSE["lessons"]):
            await msg.answer(FINISHED)
        elif u["paused"]:
            await msg.answer(PAUSED, reply_markup=sub_kb())
        else:
            await msg.answer(f"Курс идёт: пройдено {u['next_lesson']} из {len(COURSE['lessons'])}. "
                             "Следующий урок придёт по расписанию. Все полученные — /lessons")
        return
    name = html.escape(msg.from_user.first_name or "друг")
    await msg.answer(welcome_text(name), reply_markup=sub_kb())

@dp.callback_query(F.data == "check_sub")
async def on_check(cb: CallbackQuery):
    uid = cb.from_user.id
    u = get_user(uid)
    if not u:
        await cb.answer("Нажми /start", show_alert=True)
        return
    if not await is_subscribed(uid):
        await cb.answer(NOT_SUBSCRIBED, show_alert=True)
        return
    await cb.answer()
    if not u["started_at"]:
        await cb.message.answer(STARTED)
        await start_course(uid)
        if needs_source_question(get_user(uid)):
            await asyncio.sleep(2)
            await cb.message.answer(source_question(), reply_markup=source_kb())
    elif u["paused"]:
        await cb.message.answer(RESUMED)
        await resume_course(uid)
    else:
        await cb.message.answer("Подписка есть, курс идёт 👍")

@dp.callback_query(F.data.startswith("src:"))
async def on_source(cb: CallbackQuery):
    uid = cb.from_user.id
    key = cb.data.split(":", 1)[1]
    labels = dict(SOURCES)
    u = get_user(uid)
    if not u or key not in labels:
        await cb.answer()
        return
    first_answer = needs_source_question(u)
    if first_answer:
        await upd(uid, source=key)
    await cb.answer("Спасибо!")
    try:
        await cb.message.edit_text(f"Записал: {labels[key]} 👍")
    except TelegramBadRequest:
        pass
    if first_answer and bonus_ready():
        b = COURSE["source_bonus"]
        try:
            if b.get("text"):
                await bot.send_message(uid, b["text"])
            await bot.copy_message(uid, STORAGE_CHAT_ID, b["source_message_id"], protect_content=PROTECT)
        except TelegramForbiddenError:
            await upd(uid, blocked=1)

@dp.message(Command("lessons"))
async def on_lessons(msg: Message):
    """Повторно прислать уже открытые уроки."""
    u = get_user(msg.from_user.id)
    if not u or not u["started_at"] or u["next_lesson"] == 0:
        await msg.answer("Пока нет открытых уроков. Нажми /start")
        return
    if not await is_subscribed(msg.from_user.id):
        await msg.answer(PAUSED, reply_markup=sub_kb())
        return
    for i in range(u["next_lesson"]):
        await send_lesson(msg.from_user.id, i)
        await asyncio.sleep(0.5)

# ---------- платное сообщество: оплата через Stripe ----------
# Как это работает:
# 1. Человек жмёт «Хочу в сообщество» (или /join) -> бот создаёт для него ссылку Stripe Checkout.
# 2. Stripe присылает событие на /stripe/webhook -> бот записывает подписку в таблицу
#    и присылает кнопку входа в закрытый чат (заявка на вступление, бот одобряет только оплативших).
# 3. Подписка закончилась / не оплачена -> бот убирает человека из чата.
# Доступ выдаётся ТОЛЬКО по событиям Stripe (страница «спасибо» ничего не решает).
stripe_client: "stripe.StripeClient | None" = None
BOT_USERNAME = ""
PROCESSED_EVENTS: set[str] = set()   # повторная доставка одного события не выдаёт доступ дважды

CHECKOUT_TEXT = ("Отлично! Вот твоя личная ссылка на оплату 👇\n\n"
                 "После оплаты я сразу пришлю сюда кнопку входа в закрытое сообщество. "
                 "Отменить подписку можно в любой момент командой /subscription.")
PAID_WELCOME = ("Оплата прошла, добро пожаловать в сообщество! 🎉\n\n"
                "Жми кнопку ниже и отправь заявку на вступление — я одобрю её автоматически.\n"
                "Управлять подпиской (карта, отмена) — /subscription")
ALREADY_MEMBER = "Ты уже в сообществе 👍 Если вышел из чата — вот кнопка, чтобы вернуться."
PAID_PENDING = ("Спасибо! Платёж обрабатывается — кнопка входа придёт сюда в течение пары минут. "
                "Если за 10 минут ничего не пришло, напиши мне.")
NO_ACCESS = ("Вход в сообщество — для участников с активной подпиской. "
             "Оформить её можно здесь 👇")
PAYMENT_FAILED = ("Не получилось списать оплату за сообщество 😕 Stripe попробует ещё раз через несколько дней. "
                  "Проверь карту по кнопке ниже, чтобы доступ не закрылся.")
ACCESS_ENDED = ("Подписка на сообщество закончилась, поэтому доступ к чату закрыт. "
                "Вернуться можно в любой момент 👇")
CHECKOUT_FAILED = "Не получилось создать ссылку на оплату. Попробуй через пару минут или напиши мне."

def has_access(u) -> bool:
    return bool(u) and u.get("sub_status") in (ACTIVE_STATUSES | GRACE_STATUSES)

def find_user(field: str, value):
    if not value:
        return None
    return next((u for u in USERS.values() if u.get(field) == value), None)

def sget(obj, *path):
    """Достать вложенное поле из объекта Stripe, не падая, если его нет."""
    cur = obj
    for key in path:
        if cur is None:
            return None
        try:
            cur = cur[key]
        except (KeyError, TypeError, AttributeError):
            return None
    return cur

def obj_id(x):
    """Поле Stripe бывает строкой-ID или развёрнутым объектом."""
    return x if isinstance(x, str) or x is None else sget(x, "id")

def join_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=COURSE["final_offer"]["button_text"], callback_data="join")]])

async def notify_admins(text: str):
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, text)
        except Exception:
            log.warning("Не смог написать админу %s", aid)

async def safe_send(uid: int, text: str, reply_markup=None):
    try:
        await bot.send_message(uid, text, reply_markup=reply_markup)
    except TelegramForbiddenError:
        if get_user(uid):
            await upd(uid, blocked=1)

async def ensure_user(uid: int, username=None, first_name=None, source="join") -> dict:
    if not get_user(uid):
        await add_user(uid, username, first_name, source)
    return get_user(uid)

async def invite_kb():
    """Ссылка в закрытый чат с заявкой на вступление: даже если её перешлют, бот пустит только оплативших."""
    link = await bot.create_chat_invite_link(COMMUNITY_CHAT_ID, creates_join_request=True)
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔓 Войти в сообщество", url=link.invite_link)]])

async def portal_kb(u):
    s = await stripe_client.v1.billing_portal.sessions.create_async(params={
        "customer": u["stripe_customer"],
        "return_url": f"https://t.me/{BOT_USERNAME}",
    })
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💳 Управлять подпиской", url=s.url)]])

async def send_checkout(uid: int, username=None, first_name=None):
    if not PAYMENTS_ON:
        await safe_send(uid, "Оплата скоро появится — напиши мне, и я пришлю ссылку.")
        return
    u = await ensure_user(uid, username, first_name)
    if has_access(u):
        await safe_send(uid, ALREADY_MEMBER, await invite_kb())
        return
    params = {
        "mode": "subscription",
        "line_items": [{"price": STRIPE_PRICE_ID, "quantity": 1}],
        "client_reference_id": str(uid),                       # по нему находим человека в вебхуке
        "subscription_data": {"metadata": {"telegram_id": str(uid)}},   # запасной способ
        "success_url": f"https://t.me/{BOT_USERNAME}?start=paid",
        "cancel_url": f"https://t.me/{BOT_USERNAME}",
        "integration_identifier": CHECKOUT_LABEL,
    }
    if u.get("stripe_customer"):          # уже платил раньше — тот же клиент в Stripe
        params["customer"] = u["stripe_customer"]
    if STRIPE_AUTOMATIC_TAX:
        params["automatic_tax"] = {"enabled": True}
        if u.get("stripe_customer"):
            params["customer_update"] = {"address": "auto"}
            params["billing_address_collection"] = "required"
    try:
        session = await stripe_client.v1.checkout.sessions.create_async(params=params)
    except stripe.StripeError as e:
        log.error("Stripe Checkout не создан для %s: %s", uid, e)
        await safe_send(uid, CHECKOUT_FAILED)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"💳 Оплатить {PRICE_LABEL}", url=session.url)]])
    await safe_send(uid, CHECKOUT_TEXT, kb)

async def grant_access(uid: int):
    await safe_send(uid, PAID_WELCOME, await invite_kb())

async def revoke_access(uid: int):
    try:   # бан + сразу разбан = просто убрать из чата, вернуться можно после новой оплаты
        await bot.ban_chat_member(COMMUNITY_CHAT_ID, uid)
        await bot.unban_chat_member(COMMUNITY_CHAT_ID, uid, only_if_banned=True)
    except TelegramBadRequest as e:
        log.warning("Не смог убрать %s из сообщества: %s", uid, e)
    await safe_send(uid, ACCESS_ENDED, join_kb())

async def apply_status(uid: int, sub_id: str, customer: str, status: str):
    """Записать новый статус подписки и открыть/закрыть доступ, если он поменялся."""
    u = get_user(uid)
    old = u.get("sub_status")
    await upd(uid, stripe_sub=sub_id, stripe_customer=customer or u.get("stripe_customer"), sub_status=status)
    if status in END_STATUSES and old not in END_STATUSES and old is not None:
        await revoke_access(uid)
        await notify_admins(f"👋 Подписка закончилась ({status}): {user_label(u)}")
    elif status in ACTIVE_STATUSES and old in END_STATUSES:
        await grant_access(uid)

def user_label(u) -> str:
    name = html.escape(u.get("first_name") or "")
    un = f" @{u['username']}" if u.get("username") else ""
    return f"{name}{un} (id <code>{u['user_id']}</code>)"

async def sync_subscription(sub_id: str, deleted_obj=None):
    """Берём свежую подписку из Stripe (события могут прийти не по порядку) и применяем её статус."""
    if deleted_obj is not None:
        sub, status = deleted_obj, "canceled"
    else:
        sub = await stripe_client.v1.subscriptions.retrieve_async(sub_id)
        status = sub.status
    u = find_user("stripe_sub", sub_id)
    if not u:
        tid = sget(sub, "metadata", "telegram_id")
        u = get_user(int(tid)) if tid and str(tid).isdigit() else None
    if not u or u.get("stripe_sub") not in (None, sub_id):
        return   # подписку ещё не записал checkout.session.completed — он всё сделает сам
    if u.get("sub_status") is None:
        return
    await apply_status(u["user_id"], sub_id, obj_id(sget(sub, "customer")), status)

async def handle_stripe_event(event):
    t = event.type
    obj = event.data.object
    if t in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        if sget(obj, "mode") != "subscription" or sget(obj, "payment_status") == "unpaid":
            return   # оплата ещё не прошла (отложенные способы) — ждём async_payment_succeeded
        ref = sget(obj, "client_reference_id") or ""
        if not ref.isdigit():
            await notify_admins(f"⚠️ Оплата без Telegram-ID, сессия <code>{obj.id}</code> — проверь в Stripe")
            return
        uid = int(ref)
        u = await ensure_user(uid, source="stripe")
        sub_id, customer = obj_id(sget(obj, "subscription")), obj_id(sget(obj, "customer"))
        if has_access(u) and u.get("stripe_sub") not in (None, sub_id):
            await notify_admins(f"⚠️ У {user_label(u)} уже была активная подписка <code>{u['stripe_sub']}</code>, "
                                f"а сейчас оплачена ещё одна <code>{sub_id}</code>. Проверь в Stripe, лишнюю отмени.")
        await upd(uid, stripe_customer=customer, stripe_sub=sub_id, sub_status="active")
        await grant_access(uid)
        await notify_admins(f"💰 Новый участник сообщества: {user_label(get_user(uid))}")
    elif t == "customer.subscription.updated":
        await sync_subscription(obj.id)
    elif t == "customer.subscription.deleted":
        await sync_subscription(obj.id, deleted_obj=obj)
    elif t == "invoice.paid":
        sub_id = obj_id(sget(obj, "parent", "subscription_details", "subscription"))
        if sub_id:
            await sync_subscription(sub_id)
    elif t == "invoice.payment_failed":
        u = find_user("stripe_customer", obj_id(sget(obj, "customer")))
        if u:
            await safe_send(u["user_id"], PAYMENT_FAILED, await portal_kb(u))
            await notify_admins(f"⚠️ Не прошла оплата: {user_label(u)}")
    elif t in ("charge.refunded", "charge.dispute.created", "radar.early_fraud_warning.created"):
        what = {"charge.refunded": "💸 Возврат", "charge.dispute.created": "🚨 Спор по платежу (chargeback)",
                "radar.early_fraud_warning.created": "🚨 Предупреждение о мошенничестве"}[t]
        u = find_user("stripe_customer", obj_id(sget(obj, "customer")))
        who = user_label(u) if u else "клиент не найден в таблице"
        await notify_admins(f"{what}: {who}\nОбъект <code>{obj.id}</code> — разберись в Stripe Dashboard. "
                            "Доступ к чату бот сам не трогает.")

async def stripe_webhook(request: web.Request):
    if not (PAYMENTS_ON and STRIPE_WEBHOOK_SECRET):
        return web.Response(status=404, text="payments off")
    payload = await request.read()
    try:
        event = stripe_client.construct_event(payload, request.headers.get("Stripe-Signature"),
                                              STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.SignatureVerificationError):
        return web.Response(status=400, text="bad signature")
    if event.id in PROCESSED_EVENTS:
        return web.Response(text="duplicate")
    try:
        await handle_stripe_event(event)
    except Exception:
        log.exception("Ошибка обработки события Stripe %s (%s)", event.id, event.type)
        return web.Response(status=500, text="error")   # Stripe пришлёт событие повторно
    PROCESSED_EVENTS.add(event.id)
    return web.Response(text="ok")

@dp.callback_query(F.data == "join")
async def on_join_button(cb: CallbackQuery):
    await cb.answer()
    await send_checkout(cb.from_user.id, cb.from_user.username, cb.from_user.first_name)

@dp.message(Command("join"))
async def on_join_cmd(msg: Message):
    await send_checkout(msg.from_user.id, msg.from_user.username, msg.from_user.first_name)

async def on_paid_return(msg: Message):
    u = get_user(msg.from_user.id)
    if PAYMENTS_ON and has_access(u):
        await msg.answer(ALREADY_MEMBER, reply_markup=await invite_kb())
    else:
        await msg.answer(PAID_PENDING)

@dp.message(Command("subscription"))
async def on_subscription(msg: Message):
    u = get_user(msg.from_user.id)
    if not PAYMENTS_ON or not u or not u.get("stripe_customer"):
        await msg.answer("У тебя пока нет подписки на сообщество.", reply_markup=join_kb() if PAYMENTS_ON else None)
        return
    status = {"active": "активна ✅", "trialing": "пробный период ✅", "past_due": "оплата не прошла ⚠️"}.get(
        u.get("sub_status"), "не активна")
    await msg.answer(f"Подписка на сообщество: {status}\nКарта, чеки и отмена — по кнопке:",
                     reply_markup=await portal_kb(u))

@dp.chat_join_request()
async def on_join_request(req: ChatJoinRequest):
    if not PAYMENTS_ON or str(req.chat.id) != str(COMMUNITY_CHAT_ID):
        return
    if has_access(get_user(req.from_user.id)):
        await req.approve()
    else:
        await req.decline()
        await safe_send(req.from_user.id, NO_ACCESS, join_kb())

# ---------- команды админа ----------
def is_admin(msg: Message):
    return msg.from_user.id in ADMIN_IDS

@dp.message(Command("stats"), is_admin)
async def on_stats(msg: Message):
    us = list(USERS.values())
    cnt = lambda f: sum(1 for u in us if f(u))
    lines = [f"👥 Зашли в бота: {len(us)}",
             f"✅ Подписались и начали: {cnt(lambda u: u['started_at'])}",
             f"🔔 Не подписались, получили напоминание: {cnt(lambda u: u['reminded'] and not u['started_at'])}",
             f"↩️ Подписались после напоминания: {cnt(lambda u: u['reminded'] and u['started_at'])}",
             f"⏸ На паузе (отписались): {cnt(lambda u: u['paused'])}",
             f"🚫 Заблокировали бота: {cnt(lambda u: u['blocked'])}",
             f"💰 Дошли до оффера: {cnt(lambda u: u['offer_sent'])}",
             f"💳 В сообществе сейчас (платят): {cnt(has_access)}", "", "Получили уроков:"]
    for i in range(len(COURSE["lessons"])):
        lines.append(f"  {i + 1}. {cnt(lambda u: u['next_lesson'] > i)}")
    by_src: dict[str, list] = {}
    for u in us:
        by_src.setdefault(u.get("source") or "до меток", []).append(u)
    lines.append("\nОткуда пришли (зашли → начали курс → дошли до оффера):")
    for src, group in sorted(by_src.items(), key=lambda kv: -len(kv[1])):
        st = sum(1 for u in group if u["started_at"])
        of = sum(1 for u in group if u["offer_sent"])
        lines.append(f"  {src}: {len(group)} → {st} → {of}")
    lines.append("\nПолный список — в Google Таблице.")
    await msg.answer("\n".join(lines))

@dp.message(Command("reload"), is_admin)
async def on_reload(msg: Message):
    global COURSE
    try:
        COURSE = load_course()
        await load_users()
        await msg.answer(f"Перечитал lessons.json (уроков: {len(COURSE['lessons'])}) и таблицу ({len(USERS)} чел.)")
    except Exception as e:
        await msg.answer(f"Ошибка: {html.escape(str(e))}")

@dp.message(Command("broadcast"), is_admin)
async def on_broadcast(msg: Message, command: CommandObject):
    """/broadcast текст — или ответом на любое сообщение: /broadcast"""
    if not command.args and not msg.reply_to_message:
        await msg.answer("Напиши /broadcast текст, или ответь командой /broadcast на сообщение, которое разослать.")
        return
    ids = [u["user_id"] for u in USERS.values() if not u["blocked"]]
    ok = 0
    for uid in ids:
        try:
            if msg.reply_to_message:
                await bot.copy_message(uid, msg.chat.id, msg.reply_to_message.message_id)
            else:
                await bot.send_message(uid, command.args)
            ok += 1
        except TelegramForbiddenError:
            await upd(uid, blocked=1)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await msg.answer(f"Разослано: {ok} из {len(ids)}")

@dp.message(is_admin, F.forward_origin)
async def on_forward(msg: Message):
    """Переслал боту пост из канала-хранилища — бот скажет ID для lessons.json."""
    o = msg.forward_origin
    if isinstance(o, MessageOriginChannel):
        await msg.answer(f"Канал: <code>{o.chat.id}</code>\nsource_message_id: <code>{o.message_id}</code>")
    else:
        await msg.answer("Это не пост из канала. Перешли пост из канала-хранилища.")

@dp.message(Command("id"))
async def on_id(msg: Message):
    await msg.answer(f"Твой user_id: <code>{msg.from_user.id}</code>")

# ---------- веб-сервер для Render ----------
async def tick(request: web.Request):
    if request.query.get("key") != SECRET:
        return web.Response(status=403, text="no")
    asyncio.create_task(deliver_all())
    return web.Response(text="ok")

async def health(request):
    return web.Response(text="bot is running")

async def on_startup(app):
    global http, stripe_client, BOT_USERNAME
    http = aiohttp.ClientSession()
    await load_users()
    BOT_USERNAME = (await bot.get_me()).username
    if PAYMENTS_ON:
        stripe_client = stripe.StripeClient(STRIPE_KEY, http_client=stripe.AIOHTTPClient(), max_network_retries=2)
        log.info("Оплата через Stripe включена (%s)", "test" if "_test_" in STRIPE_KEY else "LIVE")
    if PUBLIC_URL:
        await bot.set_webhook(f"{PUBLIC_URL}/webhook", secret_token=SECRET,
                              allowed_updates=["message", "callback_query", "chat_join_request"])
        log.info("Webhook: %s/webhook", PUBLIC_URL)
    asyncio.create_task(deliver_all())   # после пробуждения сразу проверяем, кому пора

async def on_shutdown(app):
    await http.close()
    if stripe_client:
        try:
            await stripe_client._requestor._client.close_async()   # закрыть HTTP-сессию Stripe
        except Exception:
            pass

def main():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/tick", tick)
    app.router.add_post("/stripe/webhook", stripe_webhook)
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=SECRET).register(app, path="/webhook")
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
