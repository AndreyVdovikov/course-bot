"""
Бот-воронка: бесплатный курс в обмен на подписку на канал.

1. Человек жмёт /start -> бот просит подписаться на канал.
2. После подписки уроки приходят по расписанию из lessons.json.
3. Перед каждым уроком бот проверяет подписку. Отписался - курс на паузе,
   подписался обратно - продолжает с того же урока.
4. После последнего урока - оффер платного сообщества.
"""
import asyncio
import html
import json
import logging
import os
import sqlite3
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           Message, MessageOriginChannel)
from dotenv import load_dotenv

load_dotenv()
BASE = Path(__file__).parent
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ["CHANNEL_ID"]            # @username канала или -100...
CHANNEL_URL = os.environ["CHANNEL_URL"]          # ссылка для кнопки "Подписаться"
STORAGE_CHAT_ID = os.getenv("STORAGE_CHAT_ID")   # закрытый канал, где лежат уроки
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
CHECK_EVERY_SEC = 60

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

# ---------- уроки ----------
def load_course():
    with open(BASE / "lessons.json", encoding="utf-8") as f:
        return json.load(f)

COURSE = load_course()

# ---------- база ----------
DB_PATH = os.getenv("DB_PATH", str(BASE / "users.db"))  # на Render: /data/users.db (постоянный диск)
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.execute("""CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY,
    username TEXT, first_name TEXT,
    joined_at INTEGER,
    started_at INTEGER,          -- момент старта курса (после подписки)
    next_lesson INTEGER DEFAULT 0,
    paused INTEGER DEFAULT 0,    -- 1 = отписался от канала
    offer_sent INTEGER DEFAULT 0,
    blocked INTEGER DEFAULT 0    -- 1 = заблокировал бота
)""")
db.commit()

def get_user(uid):
    return db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()

def upd(uid, **kw):
    cols = ", ".join(f"{k}=?" for k in kw)
    db.execute(f"UPDATE users SET {cols} WHERE user_id=?", (*kw.values(), uid))
    db.commit()

# ---------- тексты и кнопки ----------
WELCOME = ("Привет, {name}! 👋\n\n"
           "Здесь бесплатный курс <b>«Хендимен-бизнес в США с нуля»</b> — {n} уроков.\n"
           "Уроки будут приходить сюда по одному в день.\n\n"
           "Чтобы получить доступ, подпишись на мой канал и нажми «Я подписался ✅».")
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

def offer_kb():
    o = COURSE["final_offer"]
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=o["button_text"], url=o["button_url"])]])

# ---------- логика ----------
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

async def is_subscribed(uid: int) -> bool:
    try:
        m = await bot.get_chat_member(CHANNEL_ID, uid)
    except TelegramBadRequest as e:
        log.error("Не могу проверить подписку (бот админ в канале?): %s", e)
        return False
    if m.status in ("member", "administrator", "creator"):
        return True
    return m.status == "restricted" and getattr(m, "is_member", False)

def lesson_due_at(u, idx):
    lessons = COURSE["lessons"]
    if idx < len(lessons):
        return u["started_at"] + int(lessons[idx]["delay_hours"] * 3600)
    last = lessons[-1]["delay_hours"] if lessons else 0
    return u["started_at"] + int((last + COURSE["final_offer"]["delay_hours_after_last"]) * 3600)

async def send_lesson(uid: int, idx: int):
    les = COURSE["lessons"][idx]
    await bot.send_message(uid, f"📚 <b>{html.escape(les['title'])}</b>")
    if les.get("source_message_id") and STORAGE_CHAT_ID:
        await bot.copy_message(uid, STORAGE_CHAT_ID, les["source_message_id"])
    if les.get("text"):
        await bot.send_message(uid, les["text"])

async def start_course(uid: int):
    upd(uid, started_at=int(time.time()), next_lesson=0, paused=0)
    await deliver_due(get_user(uid))   # урок с delay 0 приходит сразу

async def resume_course(u):
    # сдвигаем расписание, чтобы следующий урок пришёл сейчас, а остальные — с прежними интервалами
    lessons = COURSE["lessons"]
    idx = u["next_lesson"]
    if idx < len(lessons):
        base_delay = lessons[idx]["delay_hours"]
    else:
        base_delay = (lessons[-1]["delay_hours"] if lessons else 0) + COURSE["final_offer"]["delay_hours_after_last"]
    upd(u["user_id"], paused=0, started_at=int(time.time() - base_delay * 3600))
    await deliver_due(get_user(u["user_id"]))

async def deliver_due(u):
    """Отправляет всё, что уже пора отправить этому пользователю."""
    uid = u["user_id"]
    now = time.time()
    lessons = COURSE["lessons"]
    try:
        while True:
            u = get_user(uid)
            idx = u["next_lesson"]
            if u["paused"] or u["blocked"] or u["offer_sent"]:
                return
            if lesson_due_at(u, idx) > now:
                return
            if not await is_subscribed(uid):
                upd(uid, paused=1)
                await bot.send_message(uid, PAUSED, reply_markup=sub_kb())
                return
            if idx < len(lessons):
                await send_lesson(uid, idx)
                upd(uid, next_lesson=idx + 1)
            else:
                await bot.send_message(uid, COURSE["final_offer"]["text"], reply_markup=offer_kb())
                upd(uid, offer_sent=1)
                return
            await asyncio.sleep(0.5)
    except TelegramForbiddenError:
        upd(uid, blocked=1)
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)

async def scheduler():
    while True:
        rows = db.execute("SELECT * FROM users WHERE started_at IS NOT NULL "
                          "AND paused=0 AND blocked=0 AND offer_sent=0").fetchall()
        for u in rows:
            try:
                await deliver_due(u)
            except Exception:
                log.exception("Ошибка при отправке пользователю %s", u["user_id"])
            await asyncio.sleep(0.05)
        await asyncio.sleep(CHECK_EVERY_SEC)

# ---------- хендлеры пользователя ----------
@dp.message(CommandStart())
async def on_start(msg: Message):
    uid = msg.from_user.id
    u = get_user(uid)
    if not u:
        db.execute("INSERT INTO users(user_id, username, first_name, joined_at) VALUES(?,?,?,?)",
                   (uid, msg.from_user.username, msg.from_user.first_name, int(time.time())))
        db.commit()
        u = get_user(uid)
    if u["blocked"]:
        upd(uid, blocked=0)
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
    elif u["paused"]:
        await cb.message.answer(RESUMED)
        await resume_course(u)
    else:
        await cb.message.answer("Подписка есть, курс идёт 👍")

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
    q = lambda sql: db.execute(sql).fetchone()[0]
    total = q("SELECT COUNT(*) FROM users")
    started = q("SELECT COUNT(*) FROM users WHERE started_at IS NOT NULL")
    lines = [f"👥 Зашли в бота: {total}", f"✅ Подписались и начали: {started}",
             f"⏸ На паузе (отписались): {q('SELECT COUNT(*) FROM users WHERE paused=1')}",
             f"🚫 Заблокировали бота: {q('SELECT COUNT(*) FROM users WHERE blocked=1')}",
             f"💰 Дошли до оффера: {q('SELECT COUNT(*) FROM users WHERE offer_sent=1')}", "", "Получили уроков:"]
    for i, les in enumerate(COURSE["lessons"]):
        c = db.execute("SELECT COUNT(*) FROM users WHERE next_lesson>?", (i,)).fetchone()[0]
        lines.append(f"  {i + 1}. {c}")
    await msg.answer("\n".join(lines))

@dp.message(Command("reload"), is_admin)
async def on_reload(msg: Message):
    global COURSE
    try:
        COURSE = load_course()
        await msg.answer(f"lessons.json перечитан, уроков: {len(COURSE['lessons'])}")
    except Exception as e:
        await msg.answer(f"Ошибка в lessons.json: {html.escape(str(e))}")

@dp.message(Command("broadcast"), is_admin)
async def on_broadcast(msg: Message, command: CommandObject):
    """/broadcast текст — или ответом на любое сообщение: /broadcast"""
    if not command.args and not msg.reply_to_message:
        await msg.answer("Напиши /broadcast текст, или ответь командой /broadcast на сообщение, которое разослать.")
        return
    ids = [r[0] for r in db.execute("SELECT user_id FROM users WHERE blocked=0")]
    ok = 0
    for uid in ids:
        try:
            if msg.reply_to_message:
                await bot.copy_message(uid, msg.chat.id, msg.reply_to_message.message_id)
            else:
                await bot.send_message(uid, command.args)
            ok += 1
        except TelegramForbiddenError:
            upd(uid, blocked=1)
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

async def main():
    asyncio.create_task(scheduler())
    log.info("Бот запущен, уроков: %d", len(COURSE["lessons"]))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
