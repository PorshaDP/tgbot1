import asyncio
import html
import logging
import os
import random
from typing import Any, Optional

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
ADMIN_ID: Optional[int] = int(ADMIN_ID_RAW) if ADMIN_ID_RAW.isdigit() else None

if not BOT_TOKEN or not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError(
        "Не заданы BOT_TOKEN, SUPABASE_URL или SUPABASE_KEY. "
        "Скопируйте .env.example в .env и заполните переменные."
    )

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("secret_santa")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
router = Router()

BOT_USERNAME: Optional[str] = None


# -------------------- Вспомогательные функции --------------------

def esc(text: Any) -> str:
    return html.escape(str(text or ""))


def generate_room_code(length: int = 6) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(random.choice(alphabet) for _ in range(length))


def is_super_admin(user_id: int) -> bool:
    return ADMIN_ID is not None and user_id == ADMIN_ID


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎄 Создать игру", callback_data="create_game")],
            [InlineKeyboardButton(text="🎟 Войти по коду", callback_data="join_game")],
            [InlineKeyboardButton(text="📋 Мои игры", callback_data="my_games")],
            [InlineKeyboardButton(text="❄️ Весёлости и d20", callback_data="fun_menu")],
        ]
    )


async def db_call(fn):
    """Выполняет синхронный запрос supabase-py в отдельном потоке, чтобы не блокировать event loop."""
    return await asyncio.to_thread(fn)


async def ensure_user(tg_user) -> dict:
    payload = {
        "telegram_id": tg_user.id,
        "first_name": tg_user.first_name or "Участник",
        "username": tg_user.username,
    }
    await db_call(lambda: supabase.table("users").upsert(payload, on_conflict="telegram_id").execute())
    res = await db_call(
        lambda: supabase.table("users").select("*").eq("telegram_id", tg_user.id).single().execute()
    )
    return res.data


async def set_state(telegram_id: int, state: Optional[str]) -> None:
    await db_call(
        lambda: supabase.table("users").update({"state": state}).eq("telegram_id", telegram_id).execute()
    )


async def send_main_menu(message: Message, first_name: Optional[str]) -> None:
    await message.answer(
        f"🎅 <b>Привет, {esc(first_name or 'друг')}! Это Тайный Санта.</b>\n\n"
        "Создайте комнату, пригласите друзей по ссылке, проведите жеребьёвку "
        "и общайтесь <b>полностью анонимно</b> прямо здесь!",
        reply_markup=main_menu_kb(),
    )


# -------------------- Механика снежков (1-20) и d20 --------------------

SNOWBALL_OUTCOMES: dict[int, tuple[str, str]] = {
    1: (
        "КРИТИЧЕСКИЙ ПРОМАХ! Снежок развалился прямо в варежках, пока ты замахивался.",
        "Мокрые ладошки и лёгкий стыд (-2 к ловкости, +1 к сочувствию)",
    ),
    2: (
        "Снежок выскользнул при замахе и приземлился прямо в твой же капюшон.",
        "Самообстрел (снег предательски тает за ухом)",
    ),
    3: (
        "Ты так долго целился, что забыл зачем, и кинул снежок в ближайший сугроб.",
        "Лёгкая рассеянность (-1 к внимательности, +2 к дзену)",
    ),
    4: (
        "Мимо! Снежок улетел в ёлку и обрушил сугроб на случайного прохожего.",
        "Под прицелом недовольной белки (требуется орешек для перемирия)",
    ),
    5: (
        "Рикошет от фонарного столба! Цель даже не поняла, что в неё целились.",
        "Подозрительный звон в воздухе (физика сегодня против тебя)",
    ),
    6: (
        "Снежок просвистел в миллиметре от носа, сдув только пару снежинок.",
        "Свежий ветерок в лицо (+1 к бодрости, 0 урона)",
    ),
    7: (
        "Попал в ботинок! Неопасно, но теперь в носке подозрительно сыро.",
        "Хлюпающий левый ботинок (требуется срочная сушка на батарее)",
    ),
    8: (
        "Снежок рассыпался в воздухе, устроив цели мини-снегопад на плечи.",
        "Эффект новогодней открытки (+3 к атмосфере, -1 к сухости куртки)",
    ),
    9: (
        "Касательное попадание! Снежок задел шапку, но помпон героически устоял.",
        "Слегка припорошен (-1 к серьёзности, +5 к румянцу)",
    ),
    10: (
        "Попал ровно в шарф! Половина снега осталась украшать узел.",
        "Снежный воротничок (+2 к стилю, лёгкая прохлада)",
    ),
    11: (
        "Прямое попадание в рукав! Снежок мягко развалился, оставив белую кляксу.",
        "Помечен белой меткой (пора искать укрытие)",
    ),
    12: (
        "Чёткое попадание в плечо! Цель вздрогнула и оглянулась в поисках виновного.",
        "Паранойя 1-го уровня (кажется, все вокруг что-то замышляют)",
    ),
    13: (
        "Мистический бросок: снежок попал точно в цель, хотя летел по странной дуге.",
        "Околдован декабрём (-1 к логике, +3 к вере в чудеса)",
    ),
    14: (
        "Бам! Прямо в центр спины с глухим «пуф» — классика дворовых баталий.",
        "Снег за шиворотом (лёгкий озноб и жажда новогодней мести)",
    ),
    15: (
        "Снежок врезался в капюшон и сработал как снежная хлопушка.",
        "Эффект неожиданности (+10 к пульсу, готовность к ответному залпу)",
    ),
    16: (
        "Плотный снежок попал в колено — цель эффектно подпрыгнула!",
        "Новогодний подскок (+5 к прыгучести, -2 к равновесию)",
    ),
    17: (
        "Снайперский залп прямо в грудь! Пуховик принял удар, но эхо услышал весь двор.",
        "Слегка ошеломлён (требуется горячий какао для восстановления)",
    ),
    18: (
        "Снежок прилетел идеально по дуге прямо в лицо — даже очки запотели!",
        "Временная слепота от сугроба (боевой дух на максимуме!)",
    ),
    19: (
        "ПОЧТИ ЛЕГЕНДА! Снежок сбил шапку с головы и аккуратно уложил её в сугроб.",
        "Без шапки на морозе (срочно вернуть головной убор на базу!)",
    ),
    20: (
        "НАТУРАЛЬНАЯ 20-КА! Легендарный снежный крит с блёстками и эффектом салюта!",
        "Заморожен в статус «Снеговик-2026» (+100 к новогоднему чуду, иммунитет к хандре)",
    ),
}


def roll_snowball_d20() -> tuple[int, str, str]:
    roll = random.randint(1, 20)
    outcome, status = SNOWBALL_OUTCOMES[roll]
    return roll, outcome, status


OLIVIER_PREDICTIONS = [
    "Тебя ждёт подарок, который идеально влезет под ёлку (и в твои планы).",
    "Осторожнее с мандаринами: сегодня возможен передоз витамина C и хорошего настроения.",
    "Твой Тайный Санта прямо сейчас мучительно гуглит, что тебе подарить.",
    "В этом году оливье будет особенно вкусным, даже если нарежешь его криво.",
    "Звёзды шепчут: пора обновить вишлист, чтобы не получить третью кружку за год.",
    "Тайный Санта уже почти выбрал подарок. Но это не точно.",
    "Сегодня отличный день, чтобы кинуть в кого-нибудь снежком и уйти в закат.",
    "Удача на твоей стороне: гирлянда распутается с первой попытки!",
]


# -------------------- Комната и интерфейс --------------------

async def send_room_view(
    bot: Bot, chat_id: int, user_id: int, room_id: int, banner: Optional[str] = None
) -> None:
    room_res = await db_call(lambda: supabase.table("rooms").select("*").eq("id", room_id).execute())
    if not room_res.data:
        await bot.send_message(chat_id, "Комната не найдена.", reply_markup=main_menu_kb())
        return
    room = room_res.data[0]

    part_res = await db_call(
        lambda: supabase.table("participants")
        .select("*")
        .eq("room_id", room_id)
        .order("id")
        .execute()
    )
    participants = part_res.data or []
    me = next((p for p in participants if p["telegram_id"] == user_id), None)
    if not me:
        await bot.send_message(chat_id, "Вы не являетесь участником этой комнаты.", reply_markup=main_menu_kb())
        return

    invite_link = (
        f"https://t.me/{BOT_USERNAME}?start=join_{room['code']}"
        if BOT_USERNAME
        else f"Код: {room['code']}"
    )
    names_list = "\n".join(f"{i + 1}. {esc(p['display_name'])}" for i, p in enumerate(participants))
    status_text = "🎁 Жеребьёвка проведена" if room["status"] == "drawn" else "⏳ Сбор участников"

    text = f"{esc(banner)}\n\n" if banner else ""
    text += (
        f"🎄 <b>{esc(room['title'])}</b>\n"
        f"💰 Бюджет: <b>{esc(room['budget'])}</b>\n"
        f"📌 Статус: {status_text}\n"
        f"🔑 Код: <code>{room['code']}</code>\n"
        f"🔗 Ссылка-приглашение:\n{invite_link}\n\n"
        f"👤 Ваше имя: <b>{esc(me['display_name'])}</b>\n"
        f"📝 Ваш вишлист: <i>{esc(me['wishlist'])}</i>\n\n"
        f"👥 <b>Участники ({len(participants)}):</b>\n{names_list}"
    )

    buttons: list[list[InlineKeyboardButton]] = []
    if room["status"] == "drawn":
        buttons.append([InlineKeyboardButton(text="🎁 Кому я дарю?", callback_data=f"my_target:{room_id}")])
        buttons.append(
            [InlineKeyboardButton(text="🎅 Написать Подопечному (как Санта)", callback_data=f"chat_santa:{room_id}")]
        )
        buttons.append(
            [InlineKeyboardButton(text="✉️ Написать своему Санте", callback_data=f"chat_rec:{room_id}")]
        )

    buttons.append(
        [
            InlineKeyboardButton(text="👤 Изменить имя", callback_data=f"edit_name:{room_id}"),
            InlineKeyboardButton(text="📝 Вишлист", callback_data=f"edit_wish:{room_id}"),
        ]
    )
    buttons.append([InlineKeyboardButton(text="❄️ Бросить снежок (d20)", callback_data=f"snow_menu:{room_id}")])

    if room["admin_id"] == user_id:
        if room["status"] == "open":
            buttons.append([InlineKeyboardButton(text="🎲 Провести жеребьёвку", callback_data=f"draw:{room_id}")])
        buttons.append([InlineKeyboardButton(text="⚙️ Управление игрой (Админ)", callback_data=f"manage:{room_id}")])
    elif room["status"] == "open":
        buttons.append([InlineKeyboardButton(text="🚪 Выйти из игры", callback_data=f"leave:{room_id}")])

    buttons.append([InlineKeyboardButton(text="⬅️ Мои игры", callback_data="my_games")])

    await bot.send_message(
        chat_id=chat_id,
        text=text,
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


async def join_room_by_code(bot: Bot, chat_id: int, user: dict, code: str) -> None:
    room_res = await db_call(lambda: supabase.table("rooms").select("*").eq("code", code).execute())
    if not room_res.data:
        await bot.send_message(
            chat_id,
            f"❌ Комната с кодом «{esc(code)}» не найдена. Проверьте код и попробуйте снова.",
            reply_markup=main_menu_kb(),
        )
        return

    room = room_res.data[0]
    existing_res = await db_call(
        lambda: supabase.table("participants")
        .select("*")
        .eq("room_id", room["id"])
        .eq("telegram_id", user["telegram_id"])
        .execute()
    )
    if existing_res.data:
        await send_room_view(bot, chat_id, user["telegram_id"], room["id"], "Вы уже участвуете в этой игре:")
        return

    if room["status"] == "drawn":
        await bot.send_message(
            chat_id,
            f"😔 В игре «{esc(room['title'])}» жеребьёвка уже прошла, новые участники не принимаются.",
            reply_markup=main_menu_kb(),
        )
        return

    await db_call(
        lambda: supabase.table("participants")
        .insert(
            {
                "room_id": room["id"],
                "telegram_id": user["telegram_id"],
                "display_name": user.get("first_name") or "Участник",
            }
        )
        .execute()
    )
    await send_room_view(
        bot,
        chat_id,
        user["telegram_id"],
        room["id"],
        "🎉 Вы в игре! Заполните вишлист, чтобы вашему Санте было проще выбрать подарок:",
    )


# -------------------- Команды --------------------

@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot) -> None:
    if not message.from_user:
        return
    user = await ensure_user(message.from_user)
    await set_state(message.from_user.id, None)

    parts = (message.text or "").strip().split(maxsplit=1)
    if len(parts) == 2 and parts[1].startswith("join_"):
        code = parts[1][5:].strip().upper()
        await join_room_by_code(bot, message.chat.id, user, code)
        return

    await send_main_menu(message, message.from_user.first_name)


@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    if not message.from_user:
        return
    await ensure_user(message.from_user)
    await set_state(message.from_user.id, None)
    await send_main_menu(message, message.from_user.first_name)


@router.message(Command("myid"))
async def cmd_myid(message: Message) -> None:
    if not message.from_user:
        return
    await message.answer(
        f"🆔 Ваш Telegram ID: <code>{message.from_user.id}</code>\n"
        "Его можно вписать в <code>ADMIN_ID</code> в файле <code>.env</code> для доступа к <code>/admin</code>."
    )


@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    if not message.from_user:
        return
    if not is_super_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа. Укажите ваш ID в <code>ADMIN_ID</code> в файле <code>.env</code>.")
        return
    await ensure_user(message.from_user)
    await set_state(message.from_user.id, None)
    await send_super_admin_menu(message)


@router.message(Command("stopchat", "cancel"))
async def cmd_stop(message: Message) -> None:
    if not message.from_user:
        return
    await ensure_user(message.from_user)
    await set_state(message.from_user.id, None)
    await message.answer("🚪 Вы вышли из текущего режима.", reply_markup=main_menu_kb())


# -------------------- Супер-админка (/admin) --------------------

async def send_super_admin_menu(target_msg: Message) -> None:
    users_res = await db_call(lambda: supabase.table("users").select("telegram_id", count="exact").execute())
    rooms_res = await db_call(lambda: supabase.table("rooms").select("id, status").execute())
    parts_res = await db_call(lambda: supabase.table("participants").select("id", count="exact").execute())

    rooms = rooms_res.data or []
    open_cnt = sum(1 for r in rooms if r["status"] == "open")
    drawn_cnt = sum(1 for r in rooms if r["status"] == "drawn")

    await target_msg.answer(
        "🛠 <b>Супер-админка Тайного Санты</b>\n\n"
        f"👤 Пользователей: <b>{users_res.count or 0}</b>\n"
        f"🎄 Комнат всего: <b>{len(rooms)}</b> (⏳ сбор: {open_cnt}, 🎁 проведено: {drawn_cnt})\n"
        f"👥 Участий в играх: <b>{parts_res.count or 0}</b>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🏠 Список всех комнат", callback_data="sa_rooms")],
                [InlineKeyboardButton(text="📢 Рассылка всем пользователям", callback_data="sa_broadcast")],
                [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu")],
            ]
        ),
    )


@router.callback_query(F.data == "sa_menu")
async def cb_sa_menu(cb: CallbackQuery) -> None:
    await cb.answer()
    if not is_super_admin(cb.from_user.id):
        return
    await set_state(cb.from_user.id, None)
    await send_super_admin_menu(cb.message)


@router.callback_query(F.data == "sa_rooms")
async def cb_sa_rooms(cb: CallbackQuery) -> None:
    await cb.answer()
    if not is_super_admin(cb.from_user.id):
        return

    rooms_res = await db_call(
        lambda: supabase.table("rooms").select("id, title, code, status, budget").order("id", desc=True).limit(20).execute()
    )
    rooms = rooms_res.data or []
    if not rooms:
        await cb.message.answer("Комнат пока нет.", reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ В админку", callback_data="sa_menu")]]
        ))
        return

    kb = []
    for r in rooms:
        icon = "🎁" if r["status"] == "drawn" else "⏳"
        kb.append([
            InlineKeyboardButton(text=f"{icon} {r['title']} ({r['code']})", callback_data=f"sa_room_info:{r['id']}"),
            InlineKeyboardButton(text="🗑", callback_data=f"sa_del_room:{r['id']}"),
        ])
    kb.append([InlineKeyboardButton(text="⬅️ В админку", callback_data="sa_menu")])

    await cb.message.answer(
        "🏠 <b>Последние 20 комнат</b> (пары скрыты, чтобы не спойлерить):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
    )


@router.callback_query(F.data.startswith("sa_room_info:"))
async def cb_sa_room_info(cb: CallbackQuery) -> None:
    await cb.answer()
    if not is_super_admin(cb.from_user.id):
        return
    room_id = int(cb.data.split(":")[1])
    room_res = await db_call(lambda: supabase.table("rooms").select("*").eq("id", room_id).execute())
    if not room_res.data:
        await cb.message.answer("Комната не найдена.")
        return
    room = room_res.data[0]
    part_res = await db_call(
        lambda: supabase.table("participants").select("display_name").eq("room_id", room_id).execute()
    )
    names = ", ".join(esc(p["display_name"]) for p in (part_res.data or [])) or "пусто"
    await cb.message.answer(
        f"🎄 <b>{esc(room['title'])}</b>\n"
        f"Код: <code>{room['code']}</code> | Бюджет: {esc(room['budget'])}\n"
        f"Статус: {room['status']} | Admin ID: <code>{room['admin_id']}</code>\n"
        f"Участники: {names}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🗑 Удалить комнату", callback_data=f"sa_del_room:{room_id}")],
                [InlineKeyboardButton(text="⬅️ Назад к списку", callback_data="sa_rooms")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("sa_del_room:"))
async def cb_sa_del_room(cb: CallbackQuery) -> None:
    await cb.answer()
    if not is_super_admin(cb.from_user.id):
        return
    room_id = int(cb.data.split(":")[1])
    await db_call(lambda: supabase.table("rooms").delete().eq("id", room_id).execute())
    await cb.message.answer(
        f"🗑 Комната #{room_id} удалена.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад к списку", callback_data="sa_rooms")]]
        ),
    )


@router.callback_query(F.data == "sa_broadcast")
async def cb_sa_broadcast(cb: CallbackQuery) -> None:
    await cb.answer()
    if not is_super_admin(cb.from_user.id):
        return
    await set_state(cb.from_user.id, "awaiting_broadcast")
    await cb.message.answer(
        "📢 Пришлите текст объявления — я отправлю его всем пользователям бота.\nДля отмены: /cancel",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="sa_menu")]]
        ),
    )


# -------------------- Управление комнатой (для организатора) --------------------

@router.callback_query(F.data.startswith("manage:"))
async def cb_manage_room(cb: CallbackQuery) -> None:
    await cb.answer()
    room_id = int(cb.data.split(":")[1])
    room_res = await db_call(lambda: supabase.table("rooms").select("*").eq("id", room_id).execute())
    if not room_res.data or room_res.data[0]["admin_id"] != cb.from_user.id:
        await cb.message.answer("Только организатор может управлять этой комнатой.")
        return

    room = room_res.data[0]
    kb = [
        [InlineKeyboardButton(text="✏️ Изменить название / бюджет", callback_data=f"edit_room:{room_id}")],
    ]
    if room["status"] == "open":
        kb.append([InlineKeyboardButton(text="🦵 Удалить участника", callback_data=f"kick_list:{room_id}")])
    else:
        kb.append([InlineKeyboardButton(text="🔄 Сбросить жеребьёвку", callback_data=f"reset_draw:{room_id}")])

    kb.append([InlineKeyboardButton(text="🗑 Удалить комнату", callback_data=f"del_room_ask:{room_id}")])
    kb.append([InlineKeyboardButton(text="⬅️ Назад в комнату", callback_data=f"room:{room_id}")])

    await cb.message.answer(
        f"⚙️ <b>Управление игрой «{esc(room['title'])}»</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
    )


@router.callback_query(F.data.startswith("edit_room:"))
async def cb_edit_room(cb: CallbackQuery) -> None:
    await cb.answer()
    room_id = int(cb.data.split(":")[1])
    await set_state(cb.from_user.id, f"awaiting_edit_room:{room_id}")
    await cb.message.answer(
        "✏️ Введите новое название и бюджет через вертикальную черту:\n"
        "<code>Новогодний чилл | до 3000 ₽</code>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=f"manage:{room_id}")]]
        ),
    )


@router.callback_query(F.data.startswith("kick_list:"))
async def cb_kick_list(cb: CallbackQuery) -> None:
    await cb.answer()
    room_id = int(cb.data.split(":")[1])
    room_res = await db_call(lambda: supabase.table("rooms").select("*").eq("id", room_id).execute())
    if not room_res.data or room_res.data[0]["admin_id"] != cb.from_user.id:
        return
    if room_res.data[0]["status"] == "drawn":
        await cb.message.answer("Нельзя удалять участников после жеребьёвки (сначала сбросьте её).")
        return

    part_res = await db_call(
        lambda: supabase.table("participants").select("*").eq("room_id", room_id).execute()
    )
    others = [p for p in (part_res.data or []) if p["telegram_id"] != cb.from_user.id]
    if not others:
        await cb.message.answer("Кроме вас в комнате пока никого нет.")
        return

    kb = [
        [InlineKeyboardButton(text=f"🦵 {p['display_name']}", callback_data=f"kick:{room_id}:{p['telegram_id']}")]
        for p in others
    ]
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"manage:{room_id}")])
    await cb.message.answer("Кого удалить из комнаты?", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


@router.callback_query(F.data.startswith("kick:"))
async def cb_kick(cb: CallbackQuery, bot: Bot) -> None:
    await cb.answer()
    _, room_id_str, target_str = cb.data.split(":")
    room_id, target_id = int(room_id_str), int(target_str)

    room_res = await db_call(lambda: supabase.table("rooms").select("*").eq("id", room_id).execute())
    if not room_res.data or room_res.data[0]["admin_id"] != cb.from_user.id:
        return

    await db_call(
        lambda: supabase.table("participants")
        .delete()
        .eq("room_id", room_id)
        .eq("telegram_id", target_id)
        .execute()
    )
    try:
        await bot.send_message(target_id, f"🚪 Организатор удалил вас из игры «{esc(room_res.data[0]['title'])}».")
    except Exception:
        pass
    await cb.message.answer("✅ Участник удалён.", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ В управление", callback_data=f"manage:{room_id}")]]
    ))


@router.callback_query(F.data.startswith("reset_draw:"))
async def cb_reset_draw(cb: CallbackQuery, bot: Bot) -> None:
    await cb.answer()
    room_id = int(cb.data.split(":")[1])
    room_res = await db_call(lambda: supabase.table("rooms").select("*").eq("id", room_id).execute())
    if not room_res.data or room_res.data[0]["admin_id"] != cb.from_user.id:
        return

    await db_call(
        lambda: supabase.table("participants").update({"target_telegram_id": None}).eq("room_id", room_id).execute()
    )
    await db_call(lambda: supabase.table("rooms").update({"status": "open"}).eq("id", room_id).execute())

    part_res = await db_call(
        lambda: supabase.table("participants").select("telegram_id").eq("room_id", room_id).execute()
    )
    for p in part_res.data or []:
        if p["telegram_id"] != cb.from_user.id:
            try:
                await bot.send_message(
                    p["telegram_id"],
                    f"🔄 Организатор сбросил жеребьёвку в игре «{esc(room_res.data[0]['title'])}». Скоро проведём заново!",
                )
            except Exception:
                pass

    await send_room_view(bot, cb.message.chat.id, cb.from_user.id, room_id, "🔄 Жеребьёвка сброшена! Комната снова открыта.")


@router.callback_query(F.data.startswith("del_room_ask:"))
async def cb_del_room_ask(cb: CallbackQuery) -> None:
    await cb.answer()
    room_id = int(cb.data.split(":")[1])
    await cb.message.answer(
        "⚠️ Точно удалить комнату? Все участники и история игры в этой комнате сотрутся.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"del_room_yes:{room_id}")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data=f"manage:{room_id}")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("del_room_yes:"))
async def cb_del_room_yes(cb: CallbackQuery) -> None:
    await cb.answer()
    room_id = int(cb.data.split(":")[1])
    room_res = await db_call(lambda: supabase.table("rooms").select("*").eq("id", room_id).execute())
    if not room_res.data or room_res.data[0]["admin_id"] != cb.from_user.id:
        return
    await db_call(lambda: supabase.table("rooms").delete().eq("id", room_id).execute())
    await cb.message.answer("🗑 Комната удалена.", reply_markup=main_menu_kb())


# -------------------- Весёлости и снежки (d20) --------------------

@router.callback_query(F.data == "fun_menu")
async def cb_fun_menu(cb: CallbackQuery) -> None:
    await cb.answer()
    await cb.message.answer(
        "❄️ <b>Новогодние весёлости</b>\n\n"
        "Хотите кинуть снежок в друзей — зайдите в нужную игру через «📋 Мои игры» и нажмите <b>❄️ Бросить снежок (d20)</b>!",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔮 Предсказание от Оливье (d20)", callback_data="oracle_d20")],
                [InlineKeyboardButton(text="🎲 Удача на подарок (d20)", callback_data="gift_luck_d20")],
                [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu")],
            ]
        ),
    )


@router.callback_query(F.data == "oracle_d20")
async def cb_oracle_d20(cb: CallbackQuery) -> None:
    await cb.answer()
    roll = random.randint(1, 20)
    pred = random.choice(OLIVIER_PREDICTIONS)
    await cb.message.answer(
        f"🔮 <b>Салат Оливье бросает d20...</b>\n"
        f"Выпало: <b>🎲 {roll}</b>\n\n"
        f"<i>«{pred}»</i>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔮 Ещё предсказание", callback_data="oracle_d20")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="fun_menu")],
            ]
        ),
    )


@router.callback_query(F.data == "gift_luck_d20")
async def cb_gift_luck_d20(cb: CallbackQuery) -> None:
    await cb.answer()
    roll = random.randint(1, 20)
    if roll == 1:
        desc = "Критическая единица! Готовься к носкам с оленями (зато тёплым)."
    elif roll <= 7:
        desc = "Скромно, но душевно: скорее всего, будет кружка, свечка или что-то к чаю."
    elif roll <= 14:
        desc = "Уверенный бросок! Подарок точно попадёт в вишлист."
    elif roll <= 19:
        desc = "Отличный ролл! Санта явно заморочился и добавил секретный бонус."
    else:
        desc = "НАТУРАЛЬНАЯ 20-КА! Тебя ждёт подарок уровня «вау, откуда ты знал?!»"

    await cb.message.answer(
        f"🎲 <b>Бросок d20 на удачу подарка: {roll}</b>\n\n{desc}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🎲 Перебросить", callback_data="gift_luck_d20")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="fun_menu")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("snow_menu:"))
async def cb_snow_menu(cb: CallbackQuery) -> None:
    await cb.answer()
    room_id = int(cb.data.split(":")[1])
    room_res = await db_call(lambda: supabase.table("rooms").select("*").eq("id", room_id).execute())
    part_res = await db_call(
        lambda: supabase.table("participants").select("*").eq("room_id", room_id).execute()
    )
    if not room_res.data:
        return
    room = room_res.data[0]
    others = [p for p in (part_res.data or []) if p["telegram_id"] != cb.from_user.id]
    if not others:
        await cb.message.answer("В комнате пока больше никого нет — кидать снежок не в кого!")
        return

    kb: list[list[InlineKeyboardButton]] = []
    if room["status"] == "drawn":
        kb.append([InlineKeyboardButton(text="🎅 В своего Подопечного (анонимно)", callback_data=f"snow_anon:{room_id}:as_santa")])
        kb.append([InlineKeyboardButton(text="🎁 В своего Санту (анонимно)", callback_data=f"snow_anon:{room_id}:as_recipient")])

    for p in others:
        kb.append([InlineKeyboardButton(text=f"❄️ В {p['display_name']}", callback_data=f"snow_user:{room_id}:{p['telegram_id']}")])
    kb.append([InlineKeyboardButton(text="⬅️ Назад в комнату", callback_data=f"room:{room_id}")])

    await cb.message.answer(
        f"❄️ <b>Снежная битва в игре «{esc(room['title'])}»</b>\nВыберите цель для броска (куб d20 решит исход!):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
    )


@router.callback_query(F.data.startswith("snow_user:"))
async def cb_snow_user(cb: CallbackQuery, bot: Bot) -> None:
    await cb.answer()
    _, room_id_str, target_str = cb.data.split(":")
    room_id, target_id = int(room_id_str), int(target_str)

    room_res = await db_call(lambda: supabase.table("rooms").select("*").eq("id", room_id).execute())
    part_res = await db_call(
        lambda: supabase.table("participants").select("*").eq("room_id", room_id).execute()
    )
    if not room_res.data:
        return
    room = room_res.data[0]
    participants = part_res.data or []
    sender = next((p for p in participants if p["telegram_id"] == cb.from_user.id), None)
    target = next((p for p in participants if p["telegram_id"] == target_id), None)
    if not sender or not target:
        return

    roll, outcome, status = roll_snowball_d20()

    await cb.message.answer(
        f"❄️ Вы запустили снежок в <b>{esc(target['display_name'])}</b>!\n"
        f"🎲 Бросок d20: <b>{roll}</b>\n\n"
        f"{outcome}\n"
        f"📌 Состояние цели: <i>{status}</i>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="❄️ Кинуть ещё раз", callback_data=f"snow_user:{room_id}:{target_id}")],
                [InlineKeyboardButton(text="⬅️ Назад в комнату", callback_data=f"room:{room_id}")],
            ]
        ),
    )

    try:
        await bot.send_message(
            target_id,
            f"❄️ <b>{esc(sender['display_name'])}</b> бросил(а) в вас снежок в игре «{esc(room['title'])}»!\n"
            f"🎲 Бросок d20: <b>{roll}</b>\n\n"
            f"{outcome}\n"
            f"📌 Ваше состояние: <i>{status}</i>",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="❄️ Кинуть снежок в ответ!", callback_data=f"snow_user:{room_id}:{sender['telegram_id']}")]
                ]
            ),
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("snow_anon:"))
async def cb_snow_anon(cb: CallbackQuery, bot: Bot) -> None:
    await cb.answer()
    _, room_id_str, role = cb.data.split(":")
    room_id = int(room_id_str)
    sender_id = cb.from_user.id

    room_res = await db_call(lambda: supabase.table("rooms").select("*").eq("id", room_id).execute())
    if not room_res.data or room_res.data[0]["status"] != "drawn":
        return
    room = room_res.data[0]

    if role == "as_santa":
        me_res = await db_call(
            lambda: supabase.table("participants")
            .select("target_telegram_id")
            .eq("room_id", room_id)
            .eq("telegram_id", sender_id)
            .execute()
        )
        if not me_res.data or not me_res.data[0].get("target_telegram_id"):
            return
        target_id = me_res.data[0]["target_telegram_id"]
        target_label = "вашего Подопечного"
        victim_header = f"🎅❄️ <b>Ваш Тайный Санта</b> запустил в вас снежком (игра «{esc(room['title'])}»):"
        reply_cb = f"snow_anon:{room_id}:as_recipient"
    else:
        me_res = await db_call(
            lambda: supabase.table("participants")
            .select("display_name")
            .eq("room_id", room_id)
            .eq("telegram_id", sender_id)
            .single()
            .execute()
        )
        santa_res = await db_call(
            lambda: supabase.table("participants")
            .select("telegram_id")
            .eq("room_id", room_id)
            .eq("target_telegram_id", sender_id)
            .execute()
        )
        if not santa_res.data:
            return
        target_id = santa_res.data[0]["telegram_id"]
        target_label = "вашего Тайного Санту"
        victim_header = (
            f"🎁❄️ <b>Ваш Подопечный ({esc(me_res.data['display_name'])})</b> "
            f"запустил в вас снежком (игра «{esc(room['title'])}»):"
        )
        reply_cb = f"snow_anon:{room_id}:as_santa"

    roll, outcome, status = roll_snowball_d20()

    await cb.message.answer(
        f"❄️ Вы анонимно кинули снежок в <b>{target_label}</b>!\n"
        f"🎲 Бросок d20: <b>{roll}</b>\n\n"
        f"{outcome}\n"
        f"📌 Состояние цели: <i>{status}</i>"
    )

    try:
        await bot.send_message(
            target_id,
            f"{victim_header}\n"
            f"🎲 Бросок d20: <b>{roll}</b>\n\n"
            f"{outcome}\n"
            f"📌 Ваше состояние: <i>{status}</i>",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="❄️ Кинуть снежок в ответ!", callback_data=reply_cb)]]
            ),
        )
    except Exception:
        pass


# -------------------- Основные Callback-кнопки --------------------

@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery) -> None:
    await cb.answer()
    await ensure_user(cb.from_user)
    await set_state(cb.from_user.id, None)
    await send_main_menu(cb.message, cb.from_user.first_name)


@router.callback_query(F.data == "create_game")
async def cb_create_game(cb: CallbackQuery) -> None:
    await cb.answer()
    await ensure_user(cb.from_user)
    await set_state(cb.from_user.id, "awaiting_game_title")
    await cb.message.answer(
        "🎄 Введите <b>название игры</b>.\n"
        "Можно сразу указать бюджет через вертикальную черту:\n\n"
        "<code>Офис 2026 | до 2000 ₽</code>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="menu")]]
        ),
    )


@router.callback_query(F.data == "join_game")
async def cb_join_game(cb: CallbackQuery) -> None:
    await cb.answer()
    await ensure_user(cb.from_user)
    await set_state(cb.from_user.id, "awaiting_join_code")
    await cb.message.answer(
        "🎟 Введите 6-значный код комнаты (например, ABC123):",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="menu")]]
        ),
    )


@router.callback_query(F.data == "my_games")
async def cb_my_games(cb: CallbackQuery) -> None:
    await cb.answer()
    await ensure_user(cb.from_user)
    await set_state(cb.from_user.id, None)

    parts_res = await db_call(
        lambda: supabase.table("participants")
        .select("room_id, rooms(id, title, status, created_at)")
        .eq("telegram_id", cb.from_user.id)
        .execute()
    )
    rows = [r["rooms"] for r in (parts_res.data or []) if r.get("rooms")]
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)

    if not rows:
        await cb.message.answer(
            "У вас пока нет игр. Создайте новую или войдите по коду!",
            reply_markup=main_menu_kb(),
        )
        return

    kb = [
        [
            InlineKeyboardButton(
                text=f"{'🎁' if r['status'] == 'drawn' else '⏳'} {r['title']}",
                callback_data=f"room:{r['id']}",
            )
        ]
        for r in rows
    ]
    kb.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu")])

    await cb.message.answer("📋 Ваши игры Тайного Санты:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


@router.callback_query(F.data.startswith("room:"))
async def cb_room(cb: CallbackQuery, bot: Bot) -> None:
    await cb.answer()
    room_id = int(cb.data.split(":")[1])
    await ensure_user(cb.from_user)
    await set_state(cb.from_user.id, None)
    await send_room_view(bot, cb.message.chat.id, cb.from_user.id, room_id)


@router.callback_query(F.data.startswith("edit_name:"))
async def cb_edit_name(cb: CallbackQuery) -> None:
    await cb.answer()
    room_id = int(cb.data.split(":")[1])
    await set_state(cb.from_user.id, f"awaiting_name:{room_id}")
    await cb.message.answer(
        "👤 Напишите ваше имя (так вас увидят друзья и ваш Тайный Санта):",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=f"room:{room_id}")]]
        ),
    )


@router.callback_query(F.data.startswith("edit_wish:"))
async def cb_edit_wish(cb: CallbackQuery) -> None:
    await cb.answer()
    room_id = int(cb.data.split(":")[1])
    await set_state(cb.from_user.id, f"awaiting_wishlist:{room_id}")
    await cb.message.answer(
        "📝 Напишите пожелания к подарку (что любите, размеры, чего точно не дарить):",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=f"room:{room_id}")]]
        ),
    )


@router.callback_query(F.data.startswith("leave:"))
async def cb_leave(cb: CallbackQuery) -> None:
    await cb.answer()
    room_id = int(cb.data.split(":")[1])
    room_res = await db_call(lambda: supabase.table("rooms").select("*").eq("id", room_id).execute())
    if not room_res.data:
        return
    room = room_res.data[0]
    if room["status"] == "drawn":
        await cb.message.answer("Нельзя выйти после жеребьёвки.")
        return
    if room["admin_id"] == cb.from_user.id:
        await cb.message.answer("Организатор не может выйти из своей комнаты.")
        return

    await db_call(
        lambda: supabase.table("participants")
        .delete()
        .eq("room_id", room_id)
        .eq("telegram_id", cb.from_user.id)
        .execute()
    )
    await cb.message.answer(f"🚪 Вы вышли из игры «{esc(room['title'])}».", reply_markup=main_menu_kb())


@router.callback_query(F.data.startswith("draw:"))
async def cb_draw(cb: CallbackQuery, bot: Bot) -> None:
    await cb.answer()
    room_id = int(cb.data.split(":")[1])
    room_res = await db_call(lambda: supabase.table("rooms").select("*").eq("id", room_id).execute())
    if not room_res.data:
        return
    room = room_res.data[0]

    if room["admin_id"] != cb.from_user.id:
        await cb.message.answer("Только организатор может провести жеребьёвку.")
        return
    if room["status"] == "drawn":
        await cb.message.answer("Жеребьёвка уже проведена!")
        return

    part_res = await db_call(
        lambda: supabase.table("participants").select("*").eq("room_id", room_id).execute()
    )
    participants = part_res.data or []
    if len(participants) < 3:
        await cb.message.answer(
            f"❗ Для честной жеребьёвки нужно минимум 3 участника (сейчас: {len(participants)})."
        )
        return

    shuffled = list(participants)
    random.shuffle(shuffled)

    for i, santa in enumerate(shuffled):
        target = shuffled[(i + 1) % len(shuffled)]
        await db_call(
            lambda s=santa, t=target: supabase.table("participants")
            .update({"target_telegram_id": t["telegram_id"]})
            .eq("room_id", room_id)
            .eq("telegram_id", s["telegram_id"])
            .execute()
        )

    await db_call(lambda: supabase.table("rooms").update({"status": "drawn"}).eq("id", room_id).execute())

    for i, santa in enumerate(shuffled):
        target = shuffled[(i + 1) % len(shuffled)]
        try:
            await bot.send_message(
                chat_id=santa["telegram_id"],
                text=(
                    f"🎉 <b>Жеребьёвка в игре «{esc(room['title'])}» прошла!</b>\n\n"
                    f"Вы стали Тайным Сантой для: <b>{esc(target['display_name'])}</b>\n"
                    f"💰 Бюджет: <b>{esc(room['budget'])}</b>\n"
                    f"📝 Вишлист подопечного:\n<i>{esc(target['wishlist'])}</i>\n\n"
                    "Вы можете анонимно переписываться и со своим подопечным, и со своим Сантой:"
                ),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="🎅 Написать Подопечному (как Санта)", callback_data=f"chat_santa:{room_id}")],
                        [InlineKeyboardButton(text="✉️ Написать своему Санте", callback_data=f"chat_rec:{room_id}")],
                        [InlineKeyboardButton(text="❄️ Кинуть снежок (d20)", callback_data=f"snow_menu:{room_id}")],
                    ]
                ),
            )
        except Exception as exc:
            logger.warning("Не удалось отправить уведомление %s: %s", santa["telegram_id"], exc)


@router.callback_query(F.data.startswith("my_target:"))
async def cb_my_target(cb: CallbackQuery) -> None:
    await cb.answer()
    room_id = int(cb.data.split(":")[1])
    room_res = await db_call(lambda: supabase.table("rooms").select("*").eq("id", room_id).execute())
    me_res = await db_call(
        lambda: supabase.table("participants")
        .select("target_telegram_id")
        .eq("room_id", room_id)
        .eq("telegram_id", cb.from_user.id)
        .execute()
    )
    if not room_res.data or not me_res.data or not me_res.data[0].get("target_telegram_id"):
        await cb.message.answer("Жеребьёвка ещё не проведена.")
        return

    room = room_res.data[0]
    target_id = me_res.data[0]["target_telegram_id"]
    target_res = await db_call(
        lambda: supabase.table("participants")
        .select("display_name, wishlist")
        .eq("room_id", room_id)
        .eq("telegram_id", target_id)
        .single()
        .execute()
    )
    target = target_res.data

    await cb.message.answer(
        f"🎁 <b>Игра «{esc(room['title'])}»</b>\n"
        f"Вы дарите подарок: <b>{esc(target['display_name'])}</b>\n"
        f"💰 Бюджет: <b>{esc(room['budget'])}</b>\n"
        f"📝 Вишлист:\n<i>{esc(target['wishlist'])}</i>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🎅 Написать Подопечному", callback_data=f"chat_santa:{room_id}")],
                [InlineKeyboardButton(text="❄️ Кинуть снежок в Подопечного", callback_data=f"snow_anon:{room_id}:as_santa")],
                [InlineKeyboardButton(text="⬅️ Назад в комнату", callback_data=f"room:{room_id}")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("chat_santa:"))
async def cb_chat_santa(cb: CallbackQuery) -> None:
    await cb.answer()
    room_id = int(cb.data.split(":")[1])
    await start_chat_mode(cb.message, cb.from_user.id, room_id, "as_santa")


@router.callback_query(F.data.startswith("chat_rec:"))
async def cb_chat_rec(cb: CallbackQuery) -> None:
    await cb.answer()
    room_id = int(cb.data.split(":")[1])
    await start_chat_mode(cb.message, cb.from_user.id, room_id, "as_recipient")


@router.callback_query(F.data == "stop_chat")
async def cb_stop_chat(cb: CallbackQuery) -> None:
    await cb.answer()
    await set_state(cb.from_user.id, None)
    await cb.message.answer("🚪 Анонимный чат завершён.", reply_markup=main_menu_kb())


async def start_chat_mode(message: Message, user_id: int, room_id: int, role: str) -> None:
    room_res = await db_call(lambda: supabase.table("rooms").select("*").eq("id", room_id).execute())
    if not room_res.data or room_res.data[0]["status"] != "drawn":
        await message.answer("Анонимный чат доступен после жеребьёвки.")
        return

    room = room_res.data[0]
    await set_state(user_id, f"chat:{room_id}:{role}")
    label = (
        "🎅 Вы в анонимном чате со своим <b>Подопечным</b>.\n"
        "Отправьте текст, фото, стикер или голосовое — получатель не узнает, кто вы."
        if role == "as_santa"
        else "🎁 Вы в анонимном чате со своим <b>Тайным Сантой</b>.\n"
        "Отправьте текст, фото, стикер или голосовое — Санта получит сообщение мгновенно."
    )
    await message.answer(
        f"{label}\n\nИгра: «{esc(room['title'])}». Для выхода нажмите кнопку ниже или /stopchat.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="❄️ Кинуть снежок (d20)", callback_data=f"snow_anon:{room_id}:{role}")],
                [InlineKeyboardButton(text="🚪 Выйти из чата", callback_data="stop_chat")],
            ]
        ),
    )


# -------------------- Обработка сообщений и состояний --------------------

@router.message()
async def handle_message(message: Message, bot: Bot) -> None:
    if not message.from_user or message.from_user.is_bot:
        return

    user = await ensure_user(message.from_user)
    state = user.get("state")
    text = (message.text or "").strip()

    if not state:
        await send_main_menu(message, message.from_user.first_name)
        return

    if state == "awaiting_broadcast" and is_super_admin(message.from_user.id):
        if not text:
            await message.answer("Пришлите текст рассылки (или /cancel).")
            return
        await set_state(message.from_user.id, None)
        users_res = await db_call(lambda: supabase.table("users").select("telegram_id").execute())
        sent_cnt = 0
        for u in users_res.data or []:
            try:
                await bot.send_message(u["telegram_id"], f"📢 <b>Объявление:</b>\n\n{esc(text)}")
                sent_cnt += 1
            except Exception:
                pass
        await message.answer(f"✅ Рассылка завершена. Доставлено: {sent_cnt}.")
        return

    if state == "awaiting_game_title":
        if not text:
            await message.answer("Пришлите название игры текстом (или /cancel).")
            return
        parts = [p.strip() for p in text.split("|", maxsplit=1)]
        title = parts[0][:60]
        budget = parts[1][:40] if len(parts) > 1 and parts[1] else "Не ограничен"
        code = generate_room_code()

        room_res = await db_call(
            lambda: supabase.table("rooms")
            .insert({"code": code, "title": title, "budget": budget, "admin_id": message.from_user.id})
            .execute()
        )
        room_id = room_res.data[0]["id"]
        await db_call(
            lambda: supabase.table("participants")
            .insert(
                {
                    "room_id": room_id,
                    "telegram_id": message.from_user.id,
                    "display_name": message.from_user.first_name or "Организатор",
                }
            )
            .execute()
        )
        await set_state(message.from_user.id, None)
        await send_room_view(
            bot,
            message.chat.id,
            message.from_user.id,
            room_id,
            "🎄 Игра создана! Поделитесь ссылкой с друзьями:",
        )
        return

    if state.startswith("awaiting_edit_room:"):
        if not text:
            await message.answer("Пришлите название и бюджет через | (или /cancel).")
            return
        room_id = int(state.split(":")[1])
        parts = [p.strip() for p in text.split("|", maxsplit=1)]
        title = parts[0][:60]
        budget = parts[1][:40] if len(parts) > 1 and parts[1] else "Не ограничен"
        await db_call(
            lambda: supabase.table("rooms")
            .update({"title": title, "budget": budget})
            .eq("id", room_id)
            .eq("admin_id", message.from_user.id)
            .execute()
        )
        await set_state(message.from_user.id, None)
        await send_room_view(bot, message.chat.id, message.from_user.id, room_id, "✅ Настройки игры обновлены!")
        return

    if state == "awaiting_join_code":
        if not text:
            await message.answer("Введите 6-значный код комнаты (или /cancel).")
            return
        await set_state(message.from_user.id, None)
        await join_room_by_code(bot, message.chat.id, user, text.upper())
        return

    if state.startswith("awaiting_name:"):
        if not text:
            await message.answer("Пришлите имя текстом (или /cancel).")
            return
        room_id = int(state.split(":")[1])
        await db_call(
            lambda: supabase.table("participants")
            .update({"display_name": text[:50]})
            .eq("room_id", room_id)
            .eq("telegram_id", message.from_user.id)
            .execute()
        )
        await set_state(message.from_user.id, None)
        await send_room_view(bot, message.chat.id, message.from_user.id, room_id, "✅ Имя обновлено!")
        return

    if state.startswith("awaiting_wishlist:"):
        if not text:
            await message.answer("Пришлите пожелания к подарку текстом (или /cancel).")
            return
        room_id = int(state.split(":")[1])
        wishlist = text[:500]
        await db_call(
            lambda: supabase.table("participants")
            .update({"wishlist": wishlist})
            .eq("room_id", room_id)
            .eq("telegram_id", message.from_user.id)
            .execute()
        )
        await set_state(message.from_user.id, None)

        room_res = await db_call(lambda: supabase.table("rooms").select("*").eq("id", room_id).execute())
        if room_res.data and room_res.data[0]["status"] == "drawn":
            room = room_res.data[0]
            santa_res = await db_call(
                lambda: supabase.table("participants")
                .select("telegram_id")
                .eq("room_id", room_id)
                .eq("target_telegram_id", message.from_user.id)
                .execute()
            )
            me_res = await db_call(
                lambda: supabase.table("participants")
                .select("display_name")
                .eq("room_id", room_id)
                .eq("telegram_id", message.from_user.id)
                .single()
                .execute()
            )
            if santa_res.data:
                try:
                    await bot.send_message(
                        chat_id=santa_res.data[0]["telegram_id"],
                        text=(
                            f"🔔 <b>Ваш подопечный ({esc(me_res.data['display_name'])})</b> "
                            f"обновил вишлист в игре «{esc(room['title'])}»:\n\n"
                            f"<i>{esc(wishlist)}</i>"
                        ),
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[
                                [InlineKeyboardButton(text="🎅 Написать подопечному", callback_data=f"chat_santa:{room_id}")]
                            ]
                        ),
                    )
                except Exception as exc:
                    logger.warning("Не удалось уведомить Санту: %s", exc)

        await send_room_view(bot, message.chat.id, message.from_user.id, room_id, "✅ Вишлист сохранён!")
        return

    if state.startswith("chat:"):
        _, room_id_str, role = state.split(":")
        await relay_anonymous_message(bot, message, int(room_id_str), role)


async def relay_anonymous_message(bot: Bot, message: Message, room_id: int, role: str) -> None:
    sender_id = message.from_user.id
    room_res = await db_call(lambda: supabase.table("rooms").select("*").eq("id", room_id).execute())
    if not room_res.data:
        await set_state(sender_id, None)
        return
    room = room_res.data[0]

    if role == "as_santa":
        me_res = await db_call(
            lambda: supabase.table("participants")
            .select("target_telegram_id")
            .eq("room_id", room_id)
            .eq("telegram_id", sender_id)
            .execute()
        )
        if not me_res.data or not me_res.data[0].get("target_telegram_id"):
            return
        recipient_id = me_res.data[0]["target_telegram_id"]
        header = f"🎅 <b>Сообщение от вашего Тайного Санты</b> (игра «{esc(room['title'])}»):"
        reply_btn = InlineKeyboardButton(text="↩️ Ответить Санте", callback_data=f"chat_rec:{room_id}")
        snow_btn = InlineKeyboardButton(text="❄️ Кинуть снежок в ответ", callback_data=f"snow_anon:{room_id}:as_recipient")
    else:
        me_res = await db_call(
            lambda: supabase.table("participants")
            .select("display_name")
            .eq("room_id", room_id)
            .eq("telegram_id", sender_id)
            .single()
            .execute()
        )
        santa_res = await db_call(
            lambda: supabase.table("participants")
            .select("telegram_id")
            .eq("room_id", room_id)
            .eq("target_telegram_id", sender_id)
            .execute()
        )
        if not santa_res.data:
            return
        recipient_id = santa_res.data[0]["telegram_id"]
        header = (
            f"🎁 <b>Сообщение от Подопечного ({esc(me_res.data['display_name'])})</b> "
            f"(игра «{esc(room['title'])}»):"
        )
        reply_btn = InlineKeyboardButton(text="↩️ Ответить Подопечному", callback_data=f"chat_santa:{room_id}")
        snow_btn = InlineKeyboardButton(text="❄️ Кинуть снежок в ответ", callback_data=f"snow_anon:{room_id}:as_santa")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [reply_btn, snow_btn],
            [InlineKeyboardButton(text="🚪 Выйти из чата", callback_data="stop_chat")],
        ]
    )

    try:
        if message.text:
            await bot.send_message(recipient_id, f"{header}\n\n{esc(message.text)}", reply_markup=kb)
        elif message.photo:
            caption = f"\n\n{esc(message.caption)}" if message.caption else ""
            await bot.send_photo(
                recipient_id,
                photo=message.photo[-1].file_id,
                caption=f"{header}{caption}",
                reply_markup=kb,
            )
        elif message.sticker:
            await bot.send_message(recipient_id, header)
            await bot.send_sticker(recipient_id, sticker=message.sticker.file_id, reply_markup=kb)
        elif message.voice:
            await bot.send_voice(recipient_id, voice=message.voice.file_id, caption=header, reply_markup=kb)
        elif message.video_note:
            await bot.send_message(recipient_id, header)
            await bot.send_video_note(recipient_id, video_note=message.video_note.file_id, reply_markup=kb)
        elif message.document:
            caption = f"\n\n{esc(message.caption)}" if message.caption else ""
            await bot.send_document(
                recipient_id,
                document=message.document.file_id,
                caption=f"{header}{caption}",
                reply_markup=kb,
            )
        else:
            await message.answer("Поддерживаются текст, фото, стикеры, голосовые, кружочки и документы.")
            return

        await message.answer(
            "✅ Отправлено анонимно.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🚪 Выйти из чата", callback_data="stop_chat")]]
            ),
        )
    except Exception as exc:
        logger.warning("Ошибка доставки анонимного сообщения: %s", exc)
        await message.answer("⚠️ Не удалось доставить сообщение (возможно, получатель заблокировал бота).")


# -------------------- HTTP healthcheck для бесплатных хостингов --------------------

async def start_healthcheck_if_needed() -> None:
    port_raw = os.getenv("PORT", "").strip()
    if not port_raw.isdigit():
        return
    app = web.Application()
    app.router.add_get("/", lambda _: web.Response(text="Secret Santa Bot is running!"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(port_raw))
    await site.start()
    logger.info("HTTP healthcheck запущен на порту %s", port_raw)


# -------------------- Точка входа --------------------

async def main() -> None:
    global BOT_USERNAME
    await start_healthcheck_if_needed()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    BOT_USERNAME = me.username
    logger.info("Бот @%s запущен!", BOT_USERNAME)

    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
