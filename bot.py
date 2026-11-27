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
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           Message, MessageOriginChannel)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

# ---------- уроки ----------
def load_course():
    with open(BASE / "lessons.json", encoding="utf-8") as f:
        return json.load(f)

COURSE = load_course()

# ---------- хранилище: Google Таблица ----------
FIELDS = ["user_id", "username", "first_name", "joined_at", "started_at",
          "next_lesson", "paused", "offer_sent", "blocked", "reminded", "source"]
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
                  "source": source or "direct"}
    await sheet_call({"action": "upsert", "user": USERS[uid]})

# ---------- тексты и кнопки ----------
WELCOME = ("Привет, {name}! 👋\n\n"
           "Здесь курс <b>«Хендимен-бизнес в США с нуля»</b> — {n} видео-уроков.\n\n"
           "Курс <b>полностью бесплатный</b>: никаких оплат, карт и скрытых платежей. "
           "Единственное условие — подписка на мой канал.\n\n"
           "Уроки будут приходить сюда по одному в день.\n"
           "Подпишись на канал и нажми «Я подписался ✅».")
REMINDER = ("{name}, курс тебя ждёт 🙌\n\n"
            "Напоминаю: он <b>действительно бесплатный</b> — {n} видео-уроков о том, как запустить "
            "хендимен-бизнес в США с нуля. Платить ничего не нужно.\n\n"
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
SOURCE_Q = "Кстати, откуда ты узнал о курсе? Нажми один вариант — это поможет мне делать больше полезного 🙏"

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
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=o["button_text"], url=o["button_url"])]])

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
    await msg.answer(WELCOME.format(name=name, n=len(COURSE["lessons"])), reply_markup=sub_kb())

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
            await cb.message.answer(SOURCE_Q, reply_markup=source_kb())
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
    if needs_source_question(u):
        await upd(uid, source=key)
    await cb.answer("Спасибо!")
    try:
        await cb.message.edit_text(f"Спасибо! Записал: {labels[key]} 👍")
    except TelegramBadRequest:
        pass

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
             f"💰 Дошли до оффера: {cnt(lambda u: u['offer_sent'])}", "", "Получили уроков:"]
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
    global http
    http = aiohttp.ClientSession()
    await load_users()
    if PUBLIC_URL:
        await bot.set_webhook(f"{PUBLIC_URL}/webhook", secret_token=SECRET,
                              allowed_updates=["message", "callback_query"])
        log.info("Webhook: %s/webhook", PUBLIC_URL)
    asyncio.create_task(deliver_all())   # после пробуждения сразу проверяем, кому пора

async def on_shutdown(app):
    await http.close()

def main():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/tick", tick)
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=SECRET).register(app, path="/webhook")
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
