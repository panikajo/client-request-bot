import asyncio
import logging
import os
import re
import time
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart, Filter, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    InlineKeyboardButton,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

from db import DB

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Kyiv"))
MAX_OPEN_REQUESTS = 3
MAX_MESSAGES_IN_REQUEST = 10
PAGE_SIZE = 8

db = DB(os.getenv("DB_PATH", "bot.db"))

WELCOME = (
    "👋 <b>Здравствуйте!</b>\n\n"
    "Я делаю сайты, Telegram-ботов, моушн-видео и всё, что связано с вайбкодингом.\n\n"
    "Просто нажмите «Оставить заявку» и напишите, что вам нужно. "
    "Чат полностью приватный — я отвечу вам прямо здесь."
)


# ============================================================ helpers
def esc(x) -> str:
    return escape(str(x)) if x is not None else ""


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def ulabel(u: dict) -> str:
    """username(id)(телефон, если есть) — формат из ТЗ."""
    name = f"@{u['username']}" if u.get("username") else (u.get("first_name") or "без_имени")
    s = f"{name} ({u['user_id']})"
    if u.get("phone"):
        s += f" ({u['phone']})"
    return s


def short(t: str | None, n: int = 40) -> str:
    t = (t or "без текста").replace("\n", " ")
    return t if len(t) <= n else t[:n] + "…"


def fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m.%Y %H:%M")


def ban_text(until: int, reason: str | None) -> str:
    t = "🚫 <b>Вы заблокированы</b> "
    t += "навсегда." if until == -1 else f"до {fmt_ts(until)}."
    if reason:
        t += f"\nПричина: {esc(reason)}"
    return t


DUR_RE = re.compile(r"^(\d+)([mhdwмчдн])$", re.I)
DUR_MUL = {"m": 60, "м": 60, "h": 3600, "ч": 3600, "d": 86400, "д": 86400, "w": 604800, "н": 604800}
PERM_WORDS = {"perm", "forever", "навсегда", "inf", "0"}


def parse_duration(args: list[str]):
    """['7d','спам'] -> (until_ts, ['спам']). Нет срока -> навсегда (-1)."""
    if args:
        m = DUR_RE.match(args[0])
        if m:
            secs = int(m.group(1)) * DUR_MUL[m.group(2).lower()]
            return int(time.time()) + secs, args[1:]
        if args[0].lower() in PERM_WORDS:
            return -1, args[1:]
    return -1, args


async def reply(event, text: str, kb=None):
    """Для Message — новое сообщение, для CallbackQuery — правим текущее."""
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                await event.message.answer(text, reply_markup=kb)
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb)


def kb_from(rows: list[list[tuple[str, str]]]):
    """rows: [[(text, callback_data | 'url:...'), ...], ...]"""
    b = InlineKeyboardBuilder()
    for row in rows:
        btns = []
        for text, data in row:
            if data.startswith("url:"):
                btns.append(InlineKeyboardButton(text=text, url=data[4:]))
            else:
                btns.append(InlineKeyboardButton(text=text, callback_data=data))
        b.row(*btns)
    return b.as_markup()


def main_kb(uid: int):
    rows = [
        [("📝 Оставить заявку", "new")],
        [("📨 Мои заявки", "my")],
    ]
    if is_admin(uid):
        rows.append([("🛠 Админ-панель", "adm")])
    return kb_from(rows)


# ============================================================ middleware
class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = event.from_user
        if user and not user.is_bot:
            await db.upsert_user(user)
            if not is_admin(user.id):
                banned, until, reason = await db.check_ban(user.id)
                if banned:
                    if isinstance(event, CallbackQuery):
                        await event.answer("🚫 Вы заблокированы", show_alert=True)
                    else:
                        await event.answer(ban_text(until, reason))
                    return
        return await handler(event, data)


class IsAdmin(Filter):
    async def __call__(self, event) -> bool:
        return bool(event.from_user and is_admin(event.from_user.id))


client = Router()
admin = Router()
admin.message.filter(IsAdmin())
admin.callback_query.filter(IsAdmin())


# ============================================================ FSM
class RequestForm(StatesGroup):
    details = State()
    phone = State()
    confirm = State()


class AdminFSM(StatesGroup):
    reason = State()
    search = State()


# ============================================================ CLIENT: меню
@client.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext, command: CommandObject):
    await state.clear()
    if command.args == "order":
        return await begin_request(m, state)
    await m.answer(WELCOME, reply_markup=main_kb(m.from_user.id))


@client.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await reply(cb, WELCOME, main_kb(cb.from_user.id))


STATUS_EMOJI = {"new": "🕓 на рассмотрении", "accepted": "✅ принята", "rejected": "❌ отклонена"}


@client.callback_query(F.data == "my")
async def cb_my(cb: CallbackQuery):
    rows = await db.user_requests(cb.from_user.id)
    if not rows:
        text = "У вас пока нет заявок."
    else:
        lines = ["📨 <b>Ваши заявки</b>\n"]
        for r in rows:
            line = f"<b>#{r['id']}</b> · {esc(short(r['description']))} — {STATUS_EMOJI[r['status']]}"
            if r["status"] == "rejected":
                line += f"\n   Причина: {esc(r['reject_reason'])}" if r["reject_reason"] else " (без причины)"
            lines.append(line)
        text = "\n".join(lines)
    await reply(cb, text, kb_from([[("⬅ Меню", "menu")]]))


# ============================================================ CLIENT: подача заявки
async def begin_request(event, state: FSMContext):
    uid = event.from_user.id
    if await db.count_open(uid) >= MAX_OPEN_REQUESTS:
        return await reply(
            event,
            f"⏳ У вас уже {MAX_OPEN_REQUESTS} заявки на рассмотрении. Дождитесь ответа, "
            "прежде чем отправлять новые.",
            kb_from([[("⬅ Меню", "menu")]]),
        )
    await state.clear()
    await state.set_state(RequestForm.details)
    await state.update_data(texts=[], media=[], count=0)
    await reply(
        event,
        "📝 <b>Новая заявка</b>\n\n"
        "Напишите, что вам нужно: задача, примеры, сроки, бюджет. Можно несколькими "
        "сообщениями, с фото, файлами и голосовыми.\n\n"
        "Когда закончите — нажмите «Готово».",
        kb_from([[("✖ Отмена", "menu")]]),
    )


@client.callback_query(F.data == "new")
async def cb_new(cb: CallbackQuery, state: FSMContext):
    await begin_request(cb, state)


@client.message(RequestForm.details)
async def req_details(m: Message, state: FSMContext):
    data = await state.get_data()
    if data["count"] >= MAX_MESSAGES_IN_REQUEST:
        return await m.answer("Достаточно сообщений 🙂 Нажмите «Готово».", reply_markup=kb_from([[("✅ Готово", "rd")]]))
    texts, media = data["texts"], data["media"]
    if m.text:
        texts.append(m.text)
    else:
        if m.caption:
            texts.append(m.caption)
        media.append(m.message_id)
    await state.update_data(texts=texts, media=media, count=data["count"] + 1)
    await m.answer(
        f"📎 Добавлено ({data['count'] + 1}). Можно дописать ещё или нажать «Готово».",
        reply_markup=kb_from([[("✅ Готово", "rd")], [("✖ Отмена", "menu")]]),
    )


@client.callback_query(F.data == "rd", RequestForm.details)
async def req_done(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data["texts"] and not data["media"]:
        return await cb.answer("Сначала напишите, что вам нужно", show_alert=True)
    user = await db.get_user(cb.from_user.id)
    if user and user["phone"]:
        return await req_confirm(cb, state)
    await state.set_state(RequestForm.phone)
    await cb.message.answer(
        "📱 Хотите оставить номер телефона для связи? Это <b>необязательно</b> — "
        "можно общаться и через бота.",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📱 Поделиться номером", request_contact=True)],
                [KeyboardButton(text="⏭ Пропустить")],
            ],
            resize_keyboard=True,
            one_time_keyboard=True,
        ),
    )
    await cb.answer()


@client.message(RequestForm.phone, F.contact)
async def req_phone(m: Message, state: FSMContext):
    if m.contact.user_id == m.from_user.id:
        phone = m.contact.phone_number
        await db.set_phone(m.from_user.id, phone if phone.startswith("+") else "+" + phone)
    await m.answer("Спасибо!", reply_markup=ReplyKeyboardRemove())
    await req_confirm(m, state)


@client.message(RequestForm.phone)
async def req_phone_skip(m: Message, state: FSMContext):
    await m.answer("Хорошо, без номера.", reply_markup=ReplyKeyboardRemove())
    await req_confirm(m, state)


async def req_confirm(event, state: FSMContext):
    await state.set_state(RequestForm.confirm)
    data = await state.get_data()
    text = "\n".join(data["texts"]) or "—"
    if len(text) > 700:
        text = text[:700] + "…"
    summary = (
        "🔎 <b>Проверьте заявку</b>\n\n"
        f"{esc(text)}\n\n"
        f"📎 Вложений: {len(data['media'])}"
    )
    await reply(event, summary, kb_from([[("📨 Отправить", "rc:send")], [("✖ Отмена", "menu")]]))


@client.callback_query(F.data == "rc:send", RequestForm.confirm)
async def req_send(cb: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    if await db.count_open(cb.from_user.id) >= MAX_OPEN_REQUESTS:
        return await reply(cb, "⏳ Лимит открытых заявок исчерпан.", kb_from([[("⬅ Меню", "menu")]]))
    req_id = await db.create_request(
        cb.from_user.id, None, "\n\n".join(data["texts"]), None
    )
    await reply(
        cb,
        f"✅ <b>Заявка #{req_id} отправлена!</b>\n\nЯ рассмотрю её и отвечу прямо здесь.",
        kb_from([[("⬅ Меню", "menu")]]),
    )
    req = await db.get_request(req_id)
    user = await db.get_user(cb.from_user.id)
    for aid in ADMIN_IDS:
        try:
            sent = await bot.send_message(aid, render_card(req, user), reply_markup=req_kb(req))
            await db.add_card(aid, sent.message_id, user["user_id"], req_id)
            for mid in data["media"]:
                c = await bot.copy_message(
                    aid, cb.from_user.id, mid, reply_to_message_id=sent.message_id
                )
                await db.add_card(aid, c.message_id, user["user_id"], req_id)
        except TelegramAPIError as e:
            log.warning("не смог отправить заявку админу %s: %s", aid, e)


# ============================================================ CLIENT: чат после принятия заявки
@client.message(StateFilter(None))
async def client_free_message(m: Message, bot: Bot):
    if m.text and m.text.startswith("/"):
        return
    uid = m.from_user.id
    acc = None if is_admin(uid) else await db.last_accepted(uid)
    if not acc:
        return await m.answer("Выберите действие 👇", reply_markup=main_kb(uid))
    user = await db.get_user(uid)
    for aid in ADMIN_IDS:
        try:
            head = await bot.send_message(
                aid, f"💬 <b>Сообщение по заявке #{acc['id']}</b>\n👤 {esc(ulabel(user))}"
            )
            c = await bot.copy_message(aid, uid, m.message_id, reply_to_message_id=head.message_id)
            for mid in (head.message_id, c.message_id):
                await db.add_card(aid, mid, uid, acc["id"])
        except TelegramAPIError as e:
            log.warning("relay failed: %s", e)
    await m.answer("✉️ Доставлено.")


# ============================================================ ADMIN: карточка заявки
STATUS_CARD = {"new": "🆕 Ожидает решения", "accepted": "✅ Принята", "rejected": "❌ Отклонена"}


def render_card(r: dict, u: dict) -> str:
    t = (
        f"📨 <b>Заявка #{r['id']}</b> — {STATUS_CARD[r['status']]}\n\n"
        f"👤 {esc(ulabel(u))}\n"
        f"🕐 {fmt_ts(r['created_at'])}\n\n"
        f"📝 {esc((r['description'] or '—')[:2500])}"
    )
    if r["status"] == "rejected":
        t += f"\n\n<b>Причина отказа:</b> {esc(r['reject_reason']) if r['reject_reason'] else 'без причины'}"
    return t


def req_kb(r: dict):
    rows = []
    if r["status"] == "new":
        rows.append([("✅ Принять", f"q:acc:{r['id']}"), ("❌ Отклонить", f"q:rej:{r['id']}")])
    rows.append([("👤 Профиль / бан", f"u:v:{r['user_id']}")])
    return kb_from(rows)


async def notify(bot: Bot, uid: int, text: str) -> bool:
    try:
        await bot.send_message(uid, text)
        return True
    except TelegramAPIError:
        return False


@admin.callback_query(F.data.startswith("q:acc:"))
async def q_accept(cb: CallbackQuery, bot: Bot):
    rid = int(cb.data.split(":")[2])
    if not await db.decide(rid, "accepted"):
        return await cb.answer("Заявка уже обработана", show_alert=True)
    r = await db.get_request(rid)
    u = await db.get_user(r["user_id"])
    await notify(
        bot,
        r["user_id"],
        f"✅ <b>Заявка #{rid} принята!</b>\n\nЯ свяжусь с вами здесь, в боте. "
        "Можете сразу писать сюда дополнительные детали.",
    )
    await reply(cb, render_card(r, u), req_kb(r))


@admin.callback_query(F.data.startswith("q:rej:"))
async def q_reject_menu(cb: CallbackQuery):
    rid = int(cb.data.split(":")[2])
    await reply(
        cb,
        cb.message.html_text + "\n\n<b>Отклонить как?</b>",
        kb_from(
            [
                [("Без причины", f"q:rn:{rid}"), ("С причиной", f"q:rr:{rid}")],
                [("⬅ Назад", f"q:back:{rid}")],
            ]
        ),
    )


@admin.callback_query(F.data.startswith("q:back:"))
async def q_back(cb: CallbackQuery):
    rid = int(cb.data.split(":")[2])
    r = await db.get_request(rid)
    u = await db.get_user(r["user_id"])
    await reply(cb, render_card(r, u), req_kb(r))


async def finish_reject(bot: Bot, rid: int, reason: str | None):
    if not await db.decide(rid, "rejected", reason):
        return None
    r = await db.get_request(rid)
    msg = f"❌ <b>Заявка #{rid} отклонена</b>"
    msg += f"\n\nПричина: {esc(reason)}" if reason else " (без причины)."
    await notify(bot, r["user_id"], msg)
    return r


@admin.callback_query(F.data.startswith("q:rn:"))
async def q_reject_no_reason(cb: CallbackQuery, bot: Bot):
    rid = int(cb.data.split(":")[2])
    r = await finish_reject(bot, rid, None)
    if not r:
        return await cb.answer("Заявка уже обработана", show_alert=True)
    await reply(cb, render_card(r, await db.get_user(r["user_id"])), req_kb(r))


@admin.callback_query(F.data.startswith("q:rr:"))
async def q_reject_with_reason(cb: CallbackQuery, state: FSMContext):
    rid = int(cb.data.split(":")[2])
    await state.set_state(AdminFSM.reason)
    await state.update_data(rid=rid, chat=cb.message.chat.id, mid=cb.message.message_id)
    await cb.message.answer(
        f"✍️ Напишите причину отказа по заявке #{rid}.\n/skip — отклонить без причины, /cancel — отмена."
    )
    await cb.answer()


@admin.message(AdminFSM.reason)
async def q_reason_text(m: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    text = (m.text or "").strip()
    if text == "/cancel":
        await state.clear()
        return await m.answer("Отменено.")
    reason = None if text == "/skip" or not text else text[:500]
    await state.clear()
    r = await finish_reject(bot, data["rid"], reason)
    if not r:
        return await m.answer("Заявка уже обработана.")
    await m.answer(f"Заявка #{r['id']} отклонена" + (" с причиной." if reason else " без причины."))
    try:
        await bot.edit_message_text(
            render_card(r, await db.get_user(r["user_id"])),
            chat_id=data["chat"],
            message_id=data["mid"],
            reply_markup=req_kb(r),
        )
    except TelegramAPIError:
        pass


# ============================================================ ADMIN: панель
@admin.message(Command("admin"))
async def cmd_admin(m: Message, state: FSMContext):
    await state.clear()
    await admin_menu(m)


@admin.callback_query(F.data == "adm")
async def cb_admin(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await admin_menu(cb)


async def admin_menu(event):
    await reply(
        event,
        "🛠 <b>Админ-панель</b>",
        kb_from(
            [
                [("📋 Новые заявки", "a:new")],
                [("👥 Пользователи", "u:menu:all"), ("🚫 Забаненные", "u:menu:banned")],
                [("📊 Статистика", "a:stats")],
                [("⬅ Меню", "menu")],
            ]
        ),
    )


@admin.callback_query(F.data == "a:new")
async def cb_new_requests(cb: CallbackQuery):
    rows = await db.new_requests()
    if not rows:
        return await reply(cb, "Новых заявок нет 🎉", kb_from([[("⬅ Админка", "adm")]]))
    kb = [[(f"#{r['id']} · {short(r['description'], 30)}", f"a:req:{r['id']}")] for r in rows]
    kb.append([("⬅ Админка", "adm")])
    await reply(cb, f"📋 <b>Ожидают решения:</b> {len(rows)}", kb_from(kb))


@admin.callback_query(F.data.startswith("a:req:"))
async def cb_show_request(cb: CallbackQuery):
    r = await db.get_request(int(cb.data.split(":")[2]))
    u = await db.get_user(r["user_id"])
    sent = await cb.message.answer(render_card(r, u), reply_markup=req_kb(r))
    await db.add_card(sent.chat.id, sent.message_id, r["user_id"], r["id"])
    await cb.answer()


@admin.callback_query(F.data == "a:stats")
async def cb_stats(cb: CallbackQuery):
    u, r = await db.stats()
    await reply(
        cb,
        "📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей: <b>{u['total']}</b> (🚫 забанено: {u['banned'] or 0})\n"
        f"📨 Заявок: <b>{r['total']}</b>\n"
        f"   🆕 новых: {r['new'] or 0}\n"
        f"   ✅ принято: {r['accepted'] or 0}\n"
        f"   ❌ отклонено: {r['rejected'] or 0}",
        kb_from([[("⬅ Админка", "adm")]]),
    )


# ============================================================ ADMIN: /ban /unban
async def resolve_target(m: Message, args: list[str]):
    """Цель: ответ на сообщение клиента (карточка/пересланное) либо id/@username первым аргументом."""
    if m.reply_to_message:
        card = await db.get_card(m.chat.id, m.reply_to_message.message_id)
        if card:
            return card["user_id"]
    if args:
        a = args.pop(0)
        if a.lstrip("-").isdigit():
            return int(a)
        u = await db.find_username(a)
        return u["user_id"] if u else None
    return None


@admin.message(Command("ban"))
async def cmd_ban(m: Message, command: CommandObject, bot: Bot):
    args = (command.args or "").split()
    target = await resolve_target(m, args)
    if target is None:
        return await m.answer(
            "Ответьте на сообщение клиента командой:\n<code>/ban 7d причина</code>\n\n"
            "Или: <code>/ban @username 1d причина</code> / <code>/ban 123456 perm</code>\n"
            "Срок: <code>30m</code>, <code>2h</code>, <code>7d</code>, <code>2w</code>; "
            "без срока — навсегда. Причина необязательна."
        )
    if is_admin(target):
        return await m.answer("Админа забанить нельзя.")
    user = await db.get_user(target)
    if not user:
        return await m.answer("Пользователь не найден в базе.")
    until, rest = parse_duration(args)
    reason = " ".join(rest) or None
    await db.ban(target, until, reason)
    await notify(bot, target, ban_text(until, reason))
    await m.answer(
        f"🚫 {esc(ulabel(user))} забанен "
        + ("навсегда" if until == -1 else f"до {fmt_ts(until)}")
        + (f"\nПричина: {esc(reason)}" if reason else "")
    )


@admin.message(Command("unban"))
async def cmd_unban(m: Message, command: CommandObject, bot: Bot):
    args = (command.args or "").split()
    target = await resolve_target(m, args)
    user = await db.get_user(target) if target else None
    if not user:
        return await m.answer("Использование: ответом на сообщение клиента или <code>/unban @username|id</code>")
    await db.unban(target)
    await notify(bot, target, "✅ Вы разблокированы. Можно снова пользоваться ботом.")
    await m.answer(f"✅ {esc(ulabel(user))} разбанен.")


# ============================================================ ADMIN: список пользователей
def users_page_kb(rows: list[dict], page: int, query: str, mode: str):
    total_pages = max(1, -(-len(rows) // PAGE_SIZE))
    page = min(max(page, 0), total_pages - 1)
    chunk = rows[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    kb = [[(ulabel(u), f"u:v:{u['user_id']}")] for u in chunk]
    nav = []
    if page > 0:
        nav.append(("◀", f"u:p:{page - 1}"))
    nav.append((f"{page + 1}/{total_pages}", "noop"))
    if page < total_pages - 1:
        nav.append(("▶", f"u:p:{page + 1}"))
    kb.append(nav)
    kb.append([("🔎 Поиск", "u:search")] + ([("♻️ Сбросить", f"u:menu:{mode}")] if query else []))
    kb.append([("⬅ Админка", "adm")])
    title = "🚫 Забаненные" if mode == "banned" else "👥 Пользователи"
    head = f"{title}: <b>{len(rows)}</b>" + (f"\n🔎 Поиск: <code>{esc(query)}</code>" if query else "")
    if not rows:
        head += "\n\nНичего не найдено."
    return head, kb_from(kb)


async def show_users(event, state: FSMContext, page: int = 0):
    data = await state.get_data()
    mode, query = data.get("um", "all"), data.get("uq", "")
    rows = await db.list_users(mode, query)
    head, kb = users_page_kb(rows, page, query, mode)
    await reply(event, head, kb)


@admin.callback_query(F.data.startswith("u:menu:"))
async def u_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.update_data(um=cb.data.split(":")[2], uq="")
    await show_users(cb, state)


@admin.message(Command("users"))
async def cmd_users(m: Message, state: FSMContext):
    await state.clear()
    await state.update_data(um="all", uq="")
    await show_users(m, state)


@admin.callback_query(F.data.startswith("u:p:"))
async def u_page(cb: CallbackQuery, state: FSMContext):
    await show_users(cb, state, int(cb.data.split(":")[2]))


@admin.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery):
    await cb.answer()


@admin.callback_query(F.data == "u:search")
async def u_search(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.set_state(AdminFSM.search)
    await state.update_data(um=data.get("um", "all"))
    await cb.message.answer("🔎 Введите username, id, имя или часть номера телефона:")
    await cb.answer()


@admin.message(AdminFSM.search)
async def u_search_query(m: Message, state: FSMContext):
    data = await state.get_data()
    mode = data.get("um", "all")
    await state.clear()
    await state.update_data(um=mode, uq=(m.text or "").strip())
    await show_users(m, state)


def user_card(u: dict, n_req: int) -> str:
    t = f"👤 <b>{esc(ulabel(u))}</b>\n\n📅 В боте с {fmt_ts(u['created_at'])}\n📨 Заявок: {n_req}"
    ub = u["banned_until"]
    if ub == -1 or ub > time.time():
        t += "\n\n🚫 <b>Забанен</b> " + ("навсегда" if ub == -1 else f"до {fmt_ts(ub)}")
        if u["ban_reason"]:
            t += f"\nПричина: {esc(u['ban_reason'])}"
    return t


@admin.callback_query(F.data.startswith("u:v:"))
async def u_view(cb: CallbackQuery):
    uid = int(cb.data.split(":")[2])
    u = await db.get_user(uid)
    if not u:
        return await cb.answer("Не найден", show_alert=True)
    n = len(await db.user_requests(uid, 1000))
    banned = u["banned_until"] == -1 or u["banned_until"] > time.time()
    if banned:
        rows = [[("✅ Разбанить", f"u:ub:{uid}")]]
    elif is_admin(uid):
        rows = []
    else:
        rows = [
            [("🚫 1 час", f"u:b:{uid}:1h"), ("🚫 1 день", f"u:b:{uid}:1d")],
            [("🚫 7 дней", f"u:b:{uid}:7d"), ("🚫 Навсегда", f"u:b:{uid}:perm")],
        ]
    rows.append([("⬅ К списку", "u:p:0")])
    await reply(cb, user_card(u, n), kb_from(rows))


@admin.callback_query(F.data.startswith("u:b:"))
async def u_ban(cb: CallbackQuery, bot: Bot):
    _, _, uid, dur = cb.data.split(":")
    uid = int(uid)
    if is_admin(uid):
        return await cb.answer("Админа забанить нельзя", show_alert=True)
    until, _ = parse_duration([dur])
    await db.ban(uid, until, None)
    await notify(bot, uid, ban_text(until, None))
    cb.data = f"u:v:{uid}"
    await u_view(cb)


@admin.callback_query(F.data.startswith("u:ub:"))
async def u_unban(cb: CallbackQuery, bot: Bot):
    uid = int(cb.data.split(":")[2])
    await db.unban(uid)
    await notify(bot, uid, "✅ Вы разблокированы. Можно снова пользоваться ботом.")
    cb.data = f"u:v:{uid}"
    await u_view(cb)


# ============================================================ ADMIN: ответ клиенту (анонимный чат)
@admin.message(F.reply_to_message, StateFilter(None))
async def admin_reply_relay(m: Message, bot: Bot):
    if m.text and m.text.startswith("/"):
        return
    card = await db.get_card(m.chat.id, m.reply_to_message.message_id)
    if not card:
        return
    try:
        await bot.send_message(card["user_id"], f"💬 <b>Сообщение по заявке #{card['request_id']}</b>")
        await bot.copy_message(card["user_id"], m.chat.id, m.message_id)
        await m.answer("✉️ Отправлено клиенту.")
    except TelegramAPIError:
        await m.answer("⚠️ Не доставлено — клиент мог заблокировать бота.")


# ============================================================ запуск
async def main():
    await db.init()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(AccessMiddleware())
    dp.callback_query.outer_middleware(AccessMiddleware())
    dp.include_router(admin)   # админские хендлеры раньше клиентских
    dp.include_router(client)

    await bot.set_my_commands([BotCommand(command="start", description="Главное меню")])
    for aid in ADMIN_IDS:
        try:
            await bot.set_my_commands(
                [
                    BotCommand(command="start", description="Главное меню"),
                    BotCommand(command="admin", description="Админ-панель"),
                    BotCommand(command="users", description="Список пользователей"),
                    BotCommand(command="ban", description="Бан: ответом /ban 7d причина"),
                    BotCommand(command="unban", description="Разбан"),
                ],
                scope=BotCommandScopeChat(chat_id=aid),
            )
        except TelegramAPIError:
            pass
    log.info("Бот запущен. Админы: %s", ADMIN_IDS)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
