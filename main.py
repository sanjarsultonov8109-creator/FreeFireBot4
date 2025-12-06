# main.py — referal + almaz yechish tizimi + LIGA (Mening darajam)
import asyncio
import os
import random
import time
import logging
from typing import Optional

from decouple import config
from openai import AsyncOpenAI

from database import get_setting, set_setting

from ai_service import get_ai_response
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ContentType
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, BotCommand
)
from dotenv import load_dotenv

# === Anti-spam AI Lock ===
USER_AI_LOCK = {}

ACTIVE_AI_REQUEST = set()   # aylangan foydalanuvchilar ro'yxati

ACTIVE_AI = set()


OPENAI_API_KEY = config("OPENAI_API_KEY")

# 🔥 OpenAI klientini yaratamiz
client = AsyncOpenAI(api_key=OPENAI_API_KEY)


from config import OWNER_ID, REQUIRED_CHANNELS_DEFAULT
from database import (
    init_db, add_user, get_user, add_almaz, get_leaderboard,
    get_ref_by, set_ref_by_if_empty, is_verified, set_verified, set_phone_verified,
    list_admins, add_admin, remove_admin, is_admin,
    list_groups, add_group,
    get_dynamic_text, update_dynamic_text,
    list_required_channels, add_required_channel, remove_required_channel, required_channels_count,
    set_suspension, get_suspension_remaining,
    # Referral va statistika:
    create_referral, mark_referral_verified, count_verified_referrals, count_all_referrals,
    get_top_referrers_today,
    # Withdraw:
    create_withdraw_request, get_withdraw_request, update_withdraw_status,
    get_withdraw_stats,
    add_withdraw_notification, get_withdraw_notifications,
    # Rank (Liga) maydonlari:
    get_rank_fields, update_rank_fields,
)

DEVICE_KEYWORDS = [
    "samsung", "galaxy", "iphone", "redmi", "note",
    "poco", "oppo", "vivo", "huawei", "honor", "tecno",
    "infinix", "realme","pc","Notebook","Kompyuter",
]

# ==== Logging ====
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("bot")

# ==== ENV / BOT ====
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN .env dan topilmadi")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
from aiogram.filters import BaseFilter

class AILockFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        uid = message.from_user.id
        # Agar user AI band bo'lsa — handlerga umuman kiritmaymiz!
        if uid in ACTIVE_AI:
            await message.answer(
                "⏳ Oldingi AI javobi tayyorlanmoqda...\n"
                "Iltimos kuting 😊"
            )
            return False
        return True


# =================== STATES ===================
class VerifyStates(StatesGroup):
    CAPTCHA = State()
    PHONE = State()


class TextEdit(StatesGroup):
    new_text = State()
    section = State()


class Broadcast(StatesGroup):
    WAITING = State()


class ChanManage(StatesGroup):
    ADD = State()
    REMOVE = State()


class AdminManage(StatesGroup):
    ADD = State()
    REMOVE = State()


class SearchUser(StatesGroup):
    WAIT = State()


class WithdrawStates(StatesGroup):
    WAITING_FF_ID = State()


class WithdrawEdit(StatesGroup):
    WAITING_TEXT = State()


class SuspensionInput(StatesGroup):
    WAIT = State()


class GiveAlmaz(StatesGroup):
    WAIT = State()

class AiMenuStates(StatesGroup):
    ROOT = State()
    CHAT = State()
    VOICE = State()
    DEVICE = State()

class NickGenStates(StatesGroup):
    WAIT_NAME = State()


# === RAM bloklash (noto'g'ri captcha uchun 1 daqiqa) ===
BLOCKED_USERS: dict[int, float] = {}     # user_id -> blok tugash epoch
NEED_CAPTCHA_AGAIN: set[int] = set()     # noto'g'ri captcha qilganlar: keyingi /start da yana captcha


def is_blocked(user_id: int) -> tuple[bool, int]:
    until = BLOCKED_USERS.get(user_id)
    if until is None:
        return False, 0
    remain = int(until - time.time())
    if remain <= 0:
        BLOCKED_USERS.pop(user_id, None)
        return False, 0
    return True, remain


def format_user_short(name: str, username: Optional[str]) -> str:
    """
    Referal xabarlarida chiqarish uchun qulay helper:
    username bo'lsa @user, bo'lmasa ism.
    """
    if username:
        return f"@{username}"
    return name


async def is_owner_or_admin(user_id: int) -> bool:
    return user_id == OWNER_ID or await is_admin(user_id)

async def get_referral_reward() -> int:
    v = await get_setting("referral_reward")
    try:
        return int(v)
    except:
        return 10  # default qiymat


# ================== RANK / LIGA SISTEMASI ==================

# Ballar asosida liga chegaralari:
# score = (umumiy takliflar * 2) + (tasdiqlangan takliflar * 8)
RANK_LEVELS = [
    {"name": "Bronze",     "emoji": "🥉", "min": 0,   "max": 49},
    {"name": "Silver",     "emoji": "🥈", "min": 50,  "max": 149},
    {"name": "Gold",       "emoji": "🥇", "min": 150, "max": 349},
    {"name": "Diamond",    "emoji": "💎", "min": 350, "max": 749},
    {"name": "Master",     "emoji": "🔱", "min": 750, "max": 1499},
    {"name": "GrandMaster","emoji": "👑🔥", "min": 1500, "max": 10_000_000},
]


def _get_rank_info(score: int):
    """
    score'dan kelib chiqib, joriy liga va keyingi liga haqida ma'lumot qaytaradi.
    """
    current = RANK_LEVELS[-1]
    idx = len(RANK_LEVELS) - 1
    for i, r in enumerate(RANK_LEVELS):
        if r["min"] <= score <= r["max"]:
            current = r
            idx = i
            break
    next_rank = RANK_LEVELS[idx + 1] if idx + 1 < len(RANK_LEVELS) else None
    if next_rank:
        to_next = max(0, next_rank["min"] - score)
    else:
        to_next = 0
    return current, next_rank, to_next


async def calculate_rank_components(user_id: int) -> dict:
    """
    Foydalanuvchi uchun reyting ballari va statistikani hisoblaydi.
    score = umumiy_takliflar * 2 + tasdiqlangan_takliflar * 8
    """
    total_refs = await count_all_referrals(user_id)
    verified_refs = await count_verified_referrals(user_id)

    total_refs = total_refs or 0
    verified_refs = verified_refs or 0
    if verified_refs > total_refs:
        total_refs = verified_refs

    score = total_refs * 2 + verified_refs * 8
    current, next_rank, to_next = _get_rank_info(score)

    return {
        "score": score,
        "level": current["name"],
        "emoji": current["emoji"],
        "min": current["min"],
        "max": current["max"],
        "next_name": next_rank["name"] if next_rank else None,
        "next_min": next_rank["min"] if next_rank else None,
        "to_next": to_next,
        "total_refs": total_refs,
        "verified_refs": verified_refs,
    }


async def update_user_rank(user_id: int, with_notification: bool = False) -> dict:
    """
    Foydalanuvchi ligasini qayta hisoblaydi, bazaga saqlaydi.
    Agar with_notification=True bo'lsa, liga o'zgarganda foydalanuvchiga xabar yuboradi.
    """
    old_score, old_level = await get_rank_fields(user_id)
    data = await calculate_rank_components(user_id)
    score = data["score"]
    new_level = data["level"]

    # Bazadagi rank maydonlarini yangilaymiz
    await update_rank_fields(user_id, score, new_level)

    # Liga o'zgargan bo'lsa va xabar jo'natish yoqilgan bo'lsa
    if with_notification and (old_level != new_level):
        # ilk Bronze uchun xabar yubormasak ham bo'ladi (faqat yuqoriroq ligalarda)
        if (old_level in (None, "", "Bronze")) and new_level == "Bronze":
            return data

        if data["next_name"]:
            extra = (
                f"🎯 Keyingi liga: <b>{data['next_name']}</b> uchun yana "
                f"<b>{data['to_next']}</b> ball kerak.\n\n"
            )
        else:
            extra = "👑 Siz eng yuqori liga — <b>GrandMaster</b> ligasidasiz! 👑🔥\n\n"

        try:
            await bot.send_message(
                user_id,
                "🏅 <b>Darajangiz yangilandi!</b>\n\n"
                f"Yangi ligangiz: {data['emoji']} <b>{new_level} liga</b>\n"
                f"📊 Jami reyting ballaringiz: <b>{score}</b>\n\n"
                f"{extra}"
                "🔜 Tez orada ligalar uchun alohida mukofotlar qo‘shiladi.\n"
                "Darajangizni oshirib boring – sizning faolligingiz hisobga olinmoqda! 🚀",
                parse_mode="HTML"
            )
        except Exception:
            pass

    return data


# =================== MENUS ===================
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🤖 Sun’iy Intellekt")],
        [KeyboardButton(text="💎 Almaz ishlash"), KeyboardButton(text="📊 Profilim")],
        [KeyboardButton(text="🏅 Mening darajam"), KeyboardButton(text="🏆 Reyting")],
        [KeyboardButton(text="🛒 Akkount Bozor"), KeyboardButton(text="💰 Almaz sotib olish")],
        [KeyboardButton(text="📢 Reklama va yangiliklar")],
    ], resize_keyboard=True
)

# === Sun’iy Intellekt menyusi ===
ai_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🤖 AI Suhbat")],
        [KeyboardButton(text="🎭 Personaj Ovozida AI")],
        [KeyboardButton(text="⚙️ Telefonga mos sozlamalar")],
        [KeyboardButton(text="✨ Nickname Yaratish")],
        [KeyboardButton(text="⬅️ Orqaga")],
    ],
    resize_keyboard=True
)


admin_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📊 Foydalanuvchilar soni"), KeyboardButton(text="📋 Guruhlar ro‘yxati")],
        [KeyboardButton(text="📰 Reklama/Yangilik sozlash"), KeyboardButton(text="💰 Almaz sotib olish matni")],
        [KeyboardButton(text="📢 Reklama yuborish"), KeyboardButton(text="🔎 Foydalanuvchini topish")],
        [KeyboardButton(text="🧩 Majburiy kanallar"), KeyboardButton(text="🛡 Admin boshqaruvi")],
        [KeyboardButton(text="💎 Qo‘lda almaz berish"), KeyboardButton(text="📈 Statistika")],
        [KeyboardButton(text="⏳ Tanaffus berish")], [KeyboardButton(text="🔧 Referal almaz qiymati")],
        [KeyboardButton(text="⬅️ Chiqish")],
    ], resize_keyboard=True
)

back_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="⬅️ Orqaga")]],
    resize_keyboard=True
)


def sub_required_markup(channels: list[str]):
    buttons = [[InlineKeyboardButton(text=f"📢 {ch}", url=f"https://t.me/{ch.lstrip('@')}")] for ch in channels]
    buttons.append([InlineKeyboardButton(text="✅ Tekshirish", callback_data="check_subs")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ================ HELPERS ================
async def setup_bot_commands():
    await bot.set_my_commands([
        BotCommand(command="start", description="Botni ishga tushirish"),
        BotCommand(command="help", description="Yordam"),
        BotCommand(command="admin", description="Admin panel"),
    ])


async def _get_required_channels():
    # DB bo'sh bo'lsa defaultlardan to'ldiramiz (bir marta)
    current = await list_required_channels()
    if not current:
        for ch in REQUIRED_CHANNELS_DEFAULT:
            try:
                await add_required_channel(ch)
            except Exception:
                pass
        current = await list_required_channels()
    return current


async def check_subscription(user_id: int) -> list[str]:
    """
    Har bir kanal bo'yicha obuna holatini tekshiradi.
    Bot kanalga qo'shilmagan bo'lsa/idoraga ega bo'lmasa — not_sub ga qo'shiladi.
    """
    not_sub = []
    channels = await _get_required_channels()
    for ch in channels:
        chat_ref: Optional[str | int] = ch.strip()
        try:
            if chat_ref.startswith("@"):
                chat_ref = chat_ref
            else:
                try:
                    chat_ref = int(chat_ref)
                except ValueError:
                    chat_ref = chat_ref
            member = await bot.get_chat_member(chat_ref, user_id)
            if member.status not in ("member", "administrator", "creator"):
                not_sub.append(ch)
        except Exception:
            not_sub.append(ch)
    return not_sub


def positive_arith():
    a, b = random.randint(1, 9), random.randint(1, 9)
    op = random.choice(["+", "-"])
    if op == "-" and a < b:
        a, b = b, a
    ans = a + b if op == "+" else a - b
    return a, op, b, ans


async def guard_common(message: Message, allow_ai: bool = False) -> bool:
    """
    True qaytsa — oqimni to'xtatish kerak (suspension/obuna).
    """
    if message.chat.type != "private":
        return False

    # suspension (DB orqali)
    remain = await get_suspension_remaining(message.from_user.id)
    if remain > 0 and not allow_ai:
        await message.answer(
            "😴 Siz hozircha tanaffusdasiz.\n"
            f"⏰ {remain} soniyadan so‘ng bot qayta faollashadi.\n\n"
            "Ushbu muddatda faqat <b>🧠 AI bilan suhbat</b> bo‘limidan foydalanishingiz mumkin.",
            parse_mode="HTML"
        )
        return True

    # majburiy obuna
    not_sub = await check_subscription(message.from_user.id)
    if not_sub:
        await message.answer(
            "🔒 <b>Obuna talab qilinadi</b>\n\n"
            "Quyidagi kanallarimizga a’zo bo‘ling, so‘ng <b>✅ Tekshirish</b> tugmasini bosing.",
            parse_mode="HTML", reply_markup=sub_required_markup(not_sub)
        )
        return True
    return False


# ---- Withdraw admin xabarlarini yangilash helperi ----
async def update_withdraw_admin_messages(request_id: int, status_label: str):
    notifications = await get_withdraw_notifications(request_id)
    req = await get_withdraw_request(request_id)
    if not req:
        return
    r_id, user_id, amount, ff_id, status, created_at, processed_at, processed_by, note = req
    user = await get_user(user_id)
    username = user[1] if user else None
    almaz = user[3] if user and len(user) > 3 and user[3] is not None else 0
    refs = await count_verified_referrals(user_id)

    created_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(created_at or int(time.time())))
    processed_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(processed_at)) if processed_at else "—"
    note_part = f"\n📝 Izoh: {note}" if note else ""

    base_text = (
        "🧾 <b>Almaz yechish so‘rovi</b>\n\n"
        f"👤 Foydalanuvchi: @{username or 'Anonim'} (ID: <code>{user_id}</code>)\n"
        f"💎 Joriy balans (so'rovdan keyingi holat bo‘lishi mumkin): <b>{almaz} Almaz</b>\n"
        f"📥 Yechmoqchi bo‘lgan miqdor: <b>{amount} Almaz</b>\n"
        f"👥 Umumiy tasdiqlangan takliflar: <b>{refs}</b>\n"
        f"🎮 Free Fire ID: <code>{ff_id}</code>\n"
        f"🕒 So‘rov vaqti: {created_str}\n"
        f"🕒 Qayta ishlangan: {processed_str}"
        f"{note_part}\n\n"
        f"{status_label}"
    )

    for chat_id, message_id in notifications:
        try:
            await bot.edit_message_text(
                base_text,
                chat_id=chat_id,
                message_id=message_id,
                parse_mode="HTML"
            )
            # tugmalarni olib tashlaymiz
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
        except Exception as e:
            log.warning("withdraw msg edit failed: %s", e)


async def notify_admins_about_withdraw(request_id: int):
    req = await get_withdraw_request(request_id)
    if not req:
        return
    r_id, user_id, amount, ff_id, status, created_at, processed_at, processed_by, note = req
    user = await get_user(user_id)
    username = user[1] if user else None
    almaz = user[3] if user and len(user) > 3 and user[3] is not None else 0
    refs = await count_verified_referrals(user_id)
    created_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(created_at or int(time.time())))

    text = (
        "🧾 <b>Yangi almaz yechish so‘rovi</b>\n\n"
        f"👤 Foydalanuvchi: @{username or 'Anonim'} (ID: <code>{user_id}</code>)\n"
        f"💎 Joriy balans: <b>{almaz} Almaz</b>\n"
        f"📥 Yechmoqchi: <b>{amount} Almaz</b>\n"
        f"👥 Umumiy tasdiqlangan takliflar: <b>{refs}</b>\n"
        f"🎮 Free Fire ID: <code>{ff_id}</code>\n"
        f"🕒 So‘rov vaqti: {created_str}\n\n"
        "Quyidagi tugmalar orqali so‘rovni boshqaring 👇"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"wd_ok:{request_id}"),
                InlineKeyboardButton(text="✏️ Tahrirlash", callback_data=f"wd_edit:{request_id}"),
                InlineKeyboardButton(text="❌ Rad etish", callback_data=f"wd_reject:{request_id}"),
            ]
        ]
    )

    # owner + adminlar
    admin_ids = [OWNER_ID]
    extra_admins = await list_admins()
    for uid, _ in extra_admins:
        if uid not in admin_ids:
            admin_ids.append(uid)

    for chat_id in admin_ids:
        try:
            msg = await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)
            await add_withdraw_notification(request_id, chat_id, msg.message_id)
        except Exception as e:
            log.warning("failed to send withdraw notify to %s: %s", chat_id, e)


# ============== CALLBACK: obunani qayta tekshirish ==============
@dp.callback_query(F.data == "check_subs")
async def recheck_subs(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    not_sub = await check_subscription(cb.from_user.id)
    if not_sub:
        return await cb.message.edit_text(
            "⚠️ Hali barcha kanallarga obuna bo‘lmagansiz.\nQuyidagilarga obuna bo‘ling va qayta tekshiring.",
            reply_markup=sub_required_markup(not_sub)
        )
    await cb.message.edit_text("✅ Obuna tasdiqlandi! Davom etamiz…")
    await cmd_start(cb.message, state)


# ============== /start ==============
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    if message.chat.type != "private":
        return

    user_id = message.from_user.id

    # ➤ Blok tekshiruvi
    blocked, remain_local = is_blocked(user_id)
    if blocked:
        await message.answer(f"🚫 Siz bloklangansiz. ⏳ {remain_local} soniyadan so‘ng urinib ko‘ring.")
        return

    if user_id in BLOCKED_USERS and BLOCKED_USERS[user_id] <= time.time():
        BLOCKED_USERS.pop(user_id, None)

    # ➤ Majburiy kanal tekshiruvi
    not_sub = await check_subscription(user_id)
    if not_sub:
        await message.answer(
            "📣 Botdan foydalanish uchun quyidagi kanallarga obuna bo‘ling.\n"
            "Tayyor bo‘lsangiz, <b>✅ Tekshirish</b> tugmasini bosing.",
            parse_mode="HTML",
            reply_markup=sub_required_markup(not_sub),
        )
        return
    # Agar user hali verified bo‘lmagan bo‘lsa VA captcha state bor bo‘lsa → captcha’ni qayta ko‘rsatamiz
    current_state = await state.get_state()
    if current_state == VerifyStates.CAPTCHA.state:
        await message.answer("🔐 Iltimos, captcha misolini yeching.")
        return

    # 2) Telefon verifikatsiyasi davom etayotgan bo‘lsa
    if current_state == VerifyStates.PHONE.state:
        await message.answer(
            "📱 Iltimos, telefon raqamingizni ulashing.",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text='📱 Kontaktni ulashish', request_contact=True)]],
                resize_keyboard=True
            )
        )
        return

    # ➤ Agar Captcha’da yiqilgan bo‘lsa — qayta Captcha
    if user_id in NEED_CAPTCHA_AGAIN:
        a, op, b, ans = positive_arith()
        await state.set_state(VerifyStates.CAPTCHA)
        await state.update_data(captcha_answer=ans, captcha_deadline=time.time() + 90)
        await message.answer(
            "🧮 <b>Captcha tekshiruvi</b>\n"
            "Quyidagi misolni yeching va faqat javobni yuboring:\n"
            f"<b>{a} {op} {b} = ?</b>\n\n"
            "⏳ Eslatma: 90 soniya ichida javob yuboring.",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    # ============================================================
    #   🔥 1) REFERRAL ID-ni TO'G'RI ANIQLAYMIZ
    # ============================================================
    ref_id = None
    payload = (message.text or "").replace("/start", "", 1).strip()

    if payload:
        first = payload.split()[0]

        if first.startswith("ref_"):
            # ref_123?aa @bot kabi holatlarni tozalab olamiz
            cleaned = first.split("\n")[0].split("?")[0].split("@")[0]
            try:
                ref_id = int(cleaned.replace("ref_", ""))
            except:
                ref_id = None

        # Agar oddiy faqat raqam bo'lsa: "/start 123456789"
        elif first.isdigit():
            ref_id = int(first)

    # Self-referral blok
    if ref_id == user_id:
        ref_id = None

    # ============================================
    #   🔥 2) FOYDALANUVCHI YANGIMI? — TO'G'RI ANIQLASH
    # ============================================
    existing_before = await get_user(user_id)
    was_created = existing_before is None

    # Userni bazaga yozamiz (ref_id ni shu joyda yubormaymiz)
    await add_user(user_id, message.from_user.username, None)

    # Agar bazada ref_by bo'sh bo'lsa va payload orqali ref_id bo'lsa — set qilamiz
    if ref_id:
        await set_ref_by_if_empty(user_id, ref_id)

    # DBdan yangilab olamiz
    existing_after = await get_user(user_id)
    is_new = was_created

    # ============================================================
    #   🔥 3) Agar yangi user va real referrer bo'lsa → referral yozamiz
    # ============================================================
    if is_new and ref_id:
        # Qo'shimcha tekshiruv: DBda ref_by haqiqatan ref_id ekanligini tasdiqlaymiz
        actual_ref = await get_ref_by(user_id)

        try:
            if actual_ref and actual_ref == ref_id:
                await create_referral(ref_id, user_id)

                # --------------- 1-XABAR (Yangi Taklif) ---------------
                try:
                    invited_label = format_user_short(
                        message.from_user.first_name,
                        message.from_user.username
                    )
                    reward = await get_referral_reward()

                    await bot.send_message(
                        ref_id,
                        "🧑‍🤝‍🧑 <b>Yangi taklif!</b>\n\n"
                        f"Siz taklif qilgan {invited_label} botga qo‘shildi.\n"
                        "Endi u kanalga a’zo bo‘lish, captcha va telefon raqam tekshiruvlaridan "
                        "muvaffaqiyatli o‘tsa —\n"
                        f"sizga <b>{reward} Almaz</b> taqdim qilinadi! 💎",
                        parse_mode="HTML"
                    )
                except Exception as e:
                    log.warning("Referral 1-xabar error: %s", e)
            else:
                log.info(f"Referral not created: actual_ref={actual_ref}, expected={ref_id}")
        except Exception as e:
            log.warning("Referral creation skipped/failed: %s", e)

    # ============================================================
    #   🔥 4) Agar user verified bo‘lmagan bo‘lsa → Captcha
    # ============================================================
    verified = await is_verified(user_id)
    if not verified:
        a, op, b, ans = positive_arith()
        await state.set_state(VerifyStates.CAPTCHA)
        await state.update_data(captcha_answer=ans, captcha_deadline=time.time() + 90)
        await message.answer(
            "🧮 <b>Captcha tekshiruvi</b>\n"
            "Quyidagi misolni yeching va faqat javobni yuboring:\n"
            f"<b>{a} {op} {b} = ?</b>\n\n"
            "⏳ Eslatma: 90 soniya ichida javob yuboring.",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove(),
        )
        return


    # ============================================================
    #   🔥 5) Verified user → menyu
    # ============================================================
    await message.answer(
        f"✨ Xush kelibsiz, {message.from_user.first_name}!\n\n"
        "Siz barcha tekshiruvlardan muvaffaqiyatli o‘tdingiz — endi botning barcha imkoniyatlari ochildi. 🚀\n\n"
        "Quyidagi menyudan keragini tanlang 👇",
        reply_markup=main_menu
)

# ==================== CAPTCHA ====================
@dp.message(VerifyStates.CAPTCHA)
async def handle_captcha(message: Message, state: FSMContext):
    user_id = message.from_user.id
    now = time.time()

    if user_id in BLOCKED_USERS and BLOCKED_USERS[user_id] > now:
        remain = int(BLOCKED_USERS[user_id] - now)
        await message.answer(f"🚫 Siz bloklangansiz. ⏳ {remain} soniyadan so‘ng urinib ko‘ring.")
        return

    data = await state.get_data()
    correct = data.get("captcha_answer")
    deadline = float(data.get("captcha_deadline", 0))

    if correct is None or time.time() > deadline:
        await state.clear()
        await message.answer("⚠️ Captcha muddati tugadi.\n/start yuborib qayta urinib ko‘ring.")
        return

    try:
        user_answer = int((message.text or "").strip())
    except:
        await message.answer("❌ Faqat raqam kiriting.")
        return

    if user_answer != int(correct):
        BLOCKED_USERS[user_id] = now + 60
        NEED_CAPTCHA_AGAIN.add(user_id)
        await state.clear()
        await message.answer(
            "❌ Noto‘g‘ri javob. Siz 1 daqiqaga bloklandingiz.\n"
            "⏳ 1 daqiqadan so‘ng /start yuborib yana urinib ko‘rishingiz mumkin."
        )
        return

    # To‘g‘ri bo‘lsa:
    NEED_CAPTCHA_AGAIN.discard(user_id)

    if await is_verified(user_id):
        await state.clear()
        await message.answer(
            f"✅ To‘g‘ri! Captcha tasdiqlandi.\n\n"
            f"👋 Salom, {message.from_user.first_name}!\nQuyidagi menyudan tanlang 👇",
            reply_markup=main_menu
        )
        return

    await state.set_state(VerifyStates.PHONE)
    await message.answer(
        "📞 Endi o‘z telefon raqamingizni tasdiqlang.",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📱 Kontaktni ulashish", request_contact=True)],
                [KeyboardButton(text="⬅️ Orqaga")]
            ],
            resize_keyboard=True
        ),
    )

# ==================== TELEFON VERIFICATION ====================
@dp.message(VerifyStates.PHONE, F.content_type == ContentType.CONTACT)
async def phone_contact_ok(message: Message, state: FSMContext):
    user_id = message.from_user.id
    contact = message.contact

    # ❌ Boshqa odamning raqami bo‘lsa
    if not contact or contact.user_id != user_id:
        await message.answer(
            "⚠️ Iltimos, faqat o‘z raqamingizni ulashing.\n"
            "Pastdagi '📱 Kontaktni ulashish' tugmasidan foydalaning."
        )
        return

    phone = (contact.phone_number or "").strip()
    phone = phone if phone.startswith("+") else "+" + phone
    if not phone.startswith("+998"):
        await message.answer("❌ Faqat O‘zbekiston raqamlari qabul qilinadi (+998...).")
        return

    await set_phone_verified(user_id, phone)
    await set_verified(user_id)
    await state.clear()

    # ---------------- 2-XABAR + BONUS (faqat 1 marta) ----------------
    ref_by = await get_ref_by(user_id)

    if ref_by and ref_by != user_id:
        await mark_referral_verified(user_id)

        try:
            reward = await get_referral_reward()
            await add_almaz(ref_by, reward)
            total = await count_verified_referrals(ref_by)

            invited_label = format_user_short(
                message.from_user.first_name or "Foydalanuvchi",
                message.from_user.username
            )

            txt = (
                "🎉 <b>Fantastik yangilik!</b>\n\n"
                f"Siz taklif qilgan {invited_label} barcha tekshiruvlarni muvaffaqiyatli yakunladi! 👏\n\n"
                f"💎 Sizga <b>{reward}</b> Almaz muvaffaqiyatli tarzda taqdim qilindi!\n"
                f"👥 Tasdiqlangan umumiy takliflaringiz: <b>{total}</b>\n\n"
                "Do‘stlaringizni taklif qilishda davom eting — ko‘proq do‘stlar → ko‘proq Almaz! 🚀"
            )

            await bot.send_message(ref_by, txt, parse_mode="HTML")

            # Liga sistemasi bo‘lsa
            await update_user_rank(ref_by, with_notification=True)

        except Exception as e:
            print("Referral reward error:", e)
            pass

    # → Userga menyu
    await message.answer(
        "🎉 Telefon raqamingiz tasdiqlandi!\nEndi botdan to‘liq foydalanishingiz mumkin.",
        reply_markup=main_menu
    )

@dp.message(VerifyStates.PHONE)
async def phone_contact_waiting(message: Message, state: FSMContext):
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Kontaktni ulashish", request_contact=True)],
            [KeyboardButton(text="⬅️ Orqaga")]
        ],
        resize_keyboard=True
    )
    await message.answer(
        "⚠️ Raqam qo‘lda yozilmadi.\n"
        "Iltimos, pastdagi <b>“📱 Kontaktni ulashish”</b> tugmasidan foydalaning.",
        parse_mode="HTML", reply_markup=kb
    )



# ============== HELP ==============
@dp.message(Command("help"))
async def user_help(message: Message):
    await message.answer(
        "🆘 <b>Yordam</b>\n\n"
        "🧠 AI bilan suhbat — Sun’iy intellekt bilan muloqot\n"
        "💎 Almaz ishlash — Do‘st chaqirish orqali Almaz\n"
        "📊 Profilim — Profil va balans ma’lumotlari\n"
        "🏅 Mening darajam — Liga va shaxsiy reytingingiz\n"
        "🏆 Reyting — Top 15 foydalanuvchi (Almaz bo‘yicha)\n"
        "🛒 Akkount Bozor — (beta)\n"
        "💰 Almaz sotib olish — To‘lov variantlari\n"
        "📢 Reklama va yangiliklar — E’lonlar",
        parse_mode="HTML"
    )

# --- AI bo‘limlari uchun orqaga tugmasi ---



# ============== AI (private) ==============
@dp.message(F.text == "🤖 Sun’iy Intellekt")
async def ai_root(message: Message, state: FSMContext):

    await state.clear()   # <<<<<<<<<< MUHIM TUZATISH

    if await guard_common(message, allow_ai=True):
        return

    await state.set_state(AiMenuStates.ROOT)
    text = (
        "🤖 <b>Sun’iy Intellekt markazi</b>\n\n"
        "Bu yerda siz Free Fire bo‘yicha eng professional AI xizmatlaridan foydalana olasiz.\n\n"
        "👇 Quyidagi bo‘limlardan birini tanlang:\n"
        "• 🤖 AI Suhbat — Pro Coach bilan suhbat\n"
        "• 🎭 Personaj Ovozida AI — FF qahramonlari ohangida javob\n"
        "• ⚙️ Telefonga mos sozlamalar — telefoningiz uchun ideal settings\n"
    )

    await message.answer(text, parse_mode="HTML", reply_markup=ai_menu)

@dp.message(F.text == "⬅️ Orqaga", StateFilter(AiMenuStates.ROOT))
async def ai_back_from_root(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🏠 Asosiy menyuga qaytdingiz.",
        reply_markup=main_menu
    )


@dp.message(F.text == "🤖 AI Suhbat")
async def ai_chat_intro(message: Message, state: FSMContext):
    if await guard_common(message, allow_ai=True):
        return

    await state.set_state(AiMenuStates.CHAT)

    intro = (
        "🤖 <b>AI Suhbat — Free Fire Professional Coach</b>\n\n"
        "10+ yillik tajribaga ega PRO o‘yinchi kabi javob beruvchi AI.\n\n"
        "Yordam beradigan yo‘nalishlari:\n"
        "• Headshot oshirish bo`yicha maslahatlar\n"
        "• FreeFire bo`yicha Savollar va maslahatlar\n"
        "• Lag kamaytirish bo`yicha maslahatlar\n"
        "• Qurollar bo‘yicha pro maslahat\n"
        "• Rank ko‘tarish strategiyasi\n\n"
        "Xoxlagan Savolingizni yozishingiz mumkin — sizga PRO darajada javob beraman. 🔥"
    )

    await message.answer(intro, parse_mode="HTML", reply_markup=back_kb)

@dp.message(AiMenuStates.CHAT, AILockFilter())
async def ai_chat(message: Message, state: FSMContext):
    # 0) Orqaga tugmasi – alohida ishlov
    if message.text == "⬅️ Orqaga":
        await state.set_state(AiMenuStates.ROOT)
        await message.answer(
            "🔙 Sun’iy Intellekt menyusiga qaytdingiz.",
            reply_markup=ai_menu
        )
        return

    # 1) Bo‘sh matnni e’tiborsiz qoldiramiz
    user_text = (message.text or "").strip()
    if not user_text:
        return

    lower = user_text.lower()

    # 2) Qurilma so‘zlari – bu bo‘limda taqiqlanadi
    if any(key in lower for key in DEVICE_KEYWORDS):
        await message.answer(device_block_response(), parse_mode="HTML")
        return

    # 3) Anti-spam: faqat bitta aktiv so‘rov
    uid = message.from_user.id
    ACTIVE_AI.add(uid)

    try:
        wait_msg = await message.answer("🧠 Javob tayyorlanmoqda...")

        response = await get_ai_response(user_text, short=True)

        # loading xabarini o‘chiramiz
        try:
            await bot.delete_message(message.chat.id, wait_msg.message_id)
        except:
            pass

        await message.answer(response)

    except Exception as e:
        # loadingni baribir o‘chirib qo‘yamiz
        try:
            await bot.delete_message(message.chat.id, wait_msg.message_id)
        except:
            pass

        await message.answer(f"❌ Xatolik: {e}")

    finally:
        # foydalanuvchini ACTIVE_AI ro‘yxatidan chiqaramiz
        ACTIVE_AI.discard(uid)


@dp.message(F.text == "⬅️ Orqaga", StateFilter(AiMenuStates.CHAT))
async def ai_back_from_chat(message: Message, state: FSMContext):
    await state.set_state(AiMenuStates.ROOT)
    await message.answer(
        "🔙 Sun’iy Intellekt menyusiga qaytdingiz.",
        reply_markup=ai_menu
    )


@dp.message(F.text == "🎭 Personaj Ovozida AI")
async def ai_voice_intro(message: Message, state: FSMContext):
    if await guard_common(message, allow_ai=True):
        return

    await state.set_state(AiMenuStates.VOICE)

    text = (
        "🎭 <b>Personaj Ovozida AI</b>\n\n"
        "Bu rejimda AI sizning matningizni Free Fire qahramonlari uslubida qayta yozib beradi.\n\n"
        "Mavjud personajlar:\n"
        "• ALOK — do‘stona, motivatsion\n"
        "• HAYATO — qat’iy jangchi\n"
        "• KELLY — chaqqon, shiddatli\n"
        "• CR7 — sovuq, professional\n"
        "• MOCO — xaker, yashil matrix-style\n\n"
        "Misol uchun yozing:\n"
        "<code>Hayato: Men hech qachon orqaga chekinmayman</code>\n"
        "yoki:\n"
        "<code>Alok: Menga motivatsion gap yozib ber</code>\n\n"
        "Endi personaj nomi va matn yuboring 🔥"
    )

    await message.answer(text, parse_mode="HTML", reply_markup=back_kb)


@dp.message(F.text == "⬅️ Orqaga", StateFilter(AiMenuStates.DEVICE))
async def ai_back_from_device(message: Message, state: FSMContext):
    await state.set_state(AiMenuStates.ROOT)
    await message.answer(
        "🔙 Sun’iy Intellekt menyusiga qaytdingiz.",
        reply_markup=ai_menu
    )

@dp.message(AiMenuStates.VOICE, AILockFilter())
async def ai_voice_chat(message: Message, state: FSMContext):

    # 🟢 0) ORQAGA — SHOX handler
    if message.text == "⬅️ Orqaga":
        await state.set_state(AiMenuStates.ROOT)
        await message.answer(
            "🔙 Sun’iy Intellekt menyusiga qaytdingiz.",
            reply_markup=ai_menu
        )
        return

    user_text = (message.text or "").strip()
    if not user_text:
        return

    # 🟡 1) PERSONAJ ANIQLASH — sening eski logikingni aynan qoldirdim
    lower = user_text.lower()
    persona = "ALOK"
    if "hayato" in lower: persona = "HAYATO"
    elif "kelly" in lower: persona = "KELLY"
    elif "cr7" in lower or "chrono" in lower: persona = "CR7"
    elif "moco" in lower: persona = "MOCO"

    # 🧠 2) PROMPT — aynan sening original ko‘rinishing
    prompt = (
        f"Rejim: Free Fire personaj ovozida javob.\n"
        f"Tanlangan personaj: {persona}.\n"
        f"Matn: {user_text}"
    )

    # 🔒 3) ANTI-SPAM YAMOQ
    uid = message.from_user.id
    ACTIVE_AI.add(uid)

    try:
        # 🌀 4) LOADING
        wait = await message.answer("🎭 Personaj javobi tayyorlanmoqda...")

        # 🤖 5) AI JAVOB
        reply = await get_ai_response(prompt, short=True)

        # 🧹 loadingni o‘chirish
        try:
            await bot.delete_message(message.chat.id, wait.message_id)
        except:
            pass

        # ✨ 6) NATIJA
        await message.answer(reply)

    except Exception as e:

        # loadingni o‘chirishga harakat qilamiz
        try:
            await bot.delete_message(message.chat.id, wait.message_id)
        except:
            pass

        await message.answer(f"❌ Personaj AI xatosi: {e}")

    finally:
        # 🔓 7) ANTI-SPAM YAMOG‘INI YOPAMIZ
        ACTIVE_AI.discard(uid)




def normalize_model_name(name: str) -> str:
    """
    Telefon nomini tozalaydi va yagona formatga keltiradi.
    Katta-kichik harflarni to‘g‘rilaydi.
    'samsung' va 'galaxy' alias — bitta tizimga birlashtiriladi.
    Boshqa brendlar uchun brend yozilishi majburiy.
    """
    name = name.lower().strip()

    # ikki va undan ortiq bo‘sh joylarni tozalaymiz
    while "  " in name:
        name = name.replace("  ", " ")

    # "-" + boshqa simvollarni tozalaymiz
    name = name.replace("-", " ")
    name = name.replace("+", " +")

    parts = name.split()
    if len(parts) == 0:
        return ""

    # Samsung / Galaxy alias
    if parts[0] in ["galaxy", "samsung"]:
        # Samsung S/A seriyalari uchun brend + model majburiy
        parts[0] = "samsung"

    # Boshqa brendlar uchun brend majburiy bo‘lishi kerak
    allowed_brands = [
        "samsung", "iphone", "redmi", "huawei", "oppo",
        "vivo", "infinix", "tecno", "honor", "realme"
    ]

    if parts[0] not in allowed_brands:
        # faqat model yozilgan bo‘lsa → taninmaydi
        return name  # keyin valid_devices ichidan topilmaydi

    return " ".join(parts)


VALID_DEVICES = {
    # =====================
    #   SAMSUNG S SERIES
    # =====================
    "samsung s9": ("kuchli", "android"),
    "samsung s9+": ("kuchli", "android"),
    "samsung s10e": ("kuchli", "android"),
    "samsung s10": ("kuchli", "android"),
    "samsung s10+": ("kuchli", "android"),
    "samsung s10 5g": ("kuchli", "android"),
    "samsung s20": ("kuchli", "android"),
    "samsung s20+": ("kuchli", "android"),
    "samsung s20 ultra": ("kuchli", "android"),
    "samsung s21": ("kuchli", "android"),
    "samsung s21+": ("kuchli", "android"),
    "samsung s21 ultra": ("kuchli", "android"),
    "samsung s22": ("kuchli", "android"),
    "samsung s22+": ("kuchli", "android"),
    "samsung s22 ultra": ("kuchli", "android"),
    "samsung s23": ("kuchli", "android"),
    "samsung s23+": ("kuchli", "android"),
    "samsung s23 ultra": ("kuchli", "android"),
    "samsung s24": ("kuchli", "android"),
    "samsung s24+": ("kuchli", "android"),
    "samsung s24 ultra": ("kuchli", "android"),
    "samsung s25": ("kuchli", "android"),
    "samsung s25+": ("kuchli", "android"),
    "samsung s25 ultra": ("kuchli", "android"),

    # =====================
    #   SAMSUNG A SERIES
    # =====================
    "samsung a10": ("kuchsiz", "android"),
    "samsung a10s": ("kuchsiz", "android"),
    "samsung a11": ("kuchsiz", "android"),
    "samsung a12": ("kuchsiz", "android"),
    "samsung a13": ("kuchsiz", "android"),
    "samsung a14": ("kuchsiz", "android"),
    "samsung a15": ("kuchsiz", "android"),
    "samsung a16": ("kuchsiz", "android"),


    "samsung a17": ("o`rtacha", "android"),
    "samsung a18": ("o`rtacha", "android"),
    "samsung a20": ("o'rtacha", "android"),
    "samsung a20e": ("o'rtacha", "android"),
    "samsung a20s": ("o'rtacha", "android"),
    "samsung a21": ("o'rtacha", "android"),
    "samsung a21s": ("o'rtacha", "android"),
    "samsung a22": ("o'rtacha", "android"),
    "samsung a23": ("o'rtacha", "android"),
    "samsung a24": ("o'rtacha", "android"),
    "samsung a25": ("o'rtacha", "android"),
    "samsung a26": ("o'rtacha", "android"),
    "samsung a27": ("o'rtacha", "android"),
    "samsung a28": ("o'rtacha", "android"),
    "samsung a30": ("o'rtacha", "android"),
    "samsung a30s": ("o'rtacha", "android"),
    "samsung a31": ("o'rtacha", "android"),
    "samsung a32": ("o'rtacha", "android"),
    "samsung a33": ("o'rtacha", "android"),
    "samsung a34": ("o'rtacha", "android"),
    "samsung a35": ("o'rtacha", "android"),
    "samsung a36": ("o'rtacha", "android"),
    "samsung a37": ("o'rtacha", "android"),
    "samsung a38": ("o'rtacha", "android"),
    "samsung a39": ("o'rtacha", "android"),

    "samsung a40": ("o'rtacha", "android"),
    "samsung a41": ("o'rtacha", "android"),
    "samsung a42": ("o'rtacha", "android"),
    "samsung a50": ("o'rtacha", "android"),
    "samsung a50s": ("o'rtacha", "android"),
    "samsung a51": ("o'rtacha", "android"),

    "samsung a52": ("o'rtacha", "android"),
    "samsung a52s": ("kuchli", "android"),
    "samsung a53": ("kuchli", "android"),
    "samsung a54": ("kuchli", "android"),
    "samsung a55": ("kuchli", "android"),
    "samsung a56": ("kuchli", "android"),
    "samsung a57": ("kuchli", "android"),
    "samsung a58": ("kuchli", "android"),

    "samsung a60": ("o'rtacha", "android"),
    "samsung a70": ("o'rtacha", "android"),
    "samsung a70s": ("o'rtacha", "android"),
    "samsung a71": ("o'rtacha", "android"),
    "samsung a72": ("o'rtacha", "android"),
    "samsung a73": ("o'rtacha", "android"),
    "samsung a74": ("o'rtacha", "android"),
    "samsung a75": ("o'rtacha", "android"),
    "samsung a77": ("o'rtacha", "android"),
    "samsung a78": ("o'rtacha", "android"),
    "samsung a90": ("o'rtacha", "android"),

    # =====================
    #   IPHONE SERIES
    # =====================
    "iphone 7": ("kuchsiz", "iphone"),
    "iphone 7 plus": ("kuchsiz", "iphone"),
    "iphone 8": ("kuchsiz", "iphone"),
    "iphone 8 plus": ("kuchsiz", "iphone"),

    "iphone x": ("o'rtacha", "iphone"),
    "iphone xr": ("o'rtacha", "iphone"),
    "iphone xs": ("o'rtacha", "iphone"),
    "iphone xs max": ("o'rtacha", "iphone"),

    "iphone 11": ("o'rtacha", "iphone"),
    "iphone 11 pro": ("o'rtacha", "iphone"),
    "iphone 11 pro max": ("o'rtacha", "iphone"),

    "iphone se 2": ("o'rtacha", "iphone"),

    "iphone 12": ("o'rtacha", "iphone"),
    "iphone 12 mini": ("o'rtacha", "iphone"),
    "iphone 12 pro": ("o'rtacha", "iphone"),
    "iphone 12 pro max": ("o'rtacha", "iphone"),

    "iphone 13": ("kuchli", "iphone"),
    "iphone 13 mini": ("kuchli", "iphone"),
    "iphone 13 pro": ("kuchli", "iphone"),
    "iphone 13 pro max": ("kuchli", "iphone"),

    "iphone se 3": ("o'rtacha", "iphone"),

    "iphone 14": ("kuchli", "iphone"),
    "iphone 14 plus": ("kuchli", "iphone"),
    "iphone 14 pro": ("kuchli", "iphone"),
    "iphone 14 pro max": ("kuchli", "iphone"),

    "iphone 15": ("kuchli", "iphone"),
    "iphone 15 plus": ("kuchli", "iphone"),
    "iphone 15 pro": ("kuchli", "iphone"),
    "iphone 15 pro max": ("kuchli", "iphone"),

    "iphone 16": ("kuchli", "iphone"),
    "iphone 16 plus": ("kuchli", "iphone"),
    "iphone 16 pro": ("kuchli", "iphone"),
    "iphone 16 pro max": ("kuchli", "iphone"),
    "iphone 16e": ("o'rtacha", "iphone"),

    "iphone 17": ("kuchli", "iphone"),
    "iphone 17 pro": ("kuchli", "iphone"),
    "iphone 17 pro max": ("kuchli", "iphone"),
    "iphone air": ("kuchli", "iphone"),

        # =====================
    #   REDMI SERIES
    # =====================
    "redmi 5": ("kuchsiz", "android"),
    "redmi 5a": ("kuchsiz", "android"),
    "redmi 6": ("kuchsiz", "android"),
    "redmi 6a": ("kuchsiz", "android"),
    "redmi 7": ("kuchsiz", "android"),
    "redmi 7a": ("kuchsiz", "android"),
    "redmi 8": ("kuchsiz", "android"),
    "redmi 8a": ("kuchsiz", "android"),

    "redmi 9": ("o'rtacha", "android"),
    "redmi 9a": ("kuchsiz", "android"),
    "redmi 9c": ("kuchsiz", "android"),
    "redmi 9t": ("o'rtacha", "android"),

    "redmi 10": ("o'rtacha", "android"),
    "redmi 10 pro": ("o'rtacha", "android"),
    "redmi 10a": ("kuchsiz", "android"),
    "redmi 10c": ("o'rtacha", "android"),

    "redmi 11": ("o'rtacha", "android"),
    "redmi 11 pro": ("kuchli", "android"),
    "redmi 11c": ("o'rtacha", "android"),

    "redmi 12": ("o'rtacha", "android"),
    "redmi 12 pro": ("kuchli", "android"),
    "redmi 12c": ("o'rtacha", "android"),

    "redmi 13": ("o'rtacha", "android"),
    "redmi 13 pro": ("kuchli", "android"),
    "redmi 13c": ("o'rtacha", "android"),

    "redmi 14": ("o'rtacha", "android"),
    "redmi 14 pro": ("kuchli", "android"),
    "redmi 14c": ("o'rtacha", "android"),

    "redmi 15": ("o'rtacha", "android"),
    "redmi 15 pro": ("kuchli", "android"),
    "redmi 15c": ("o'rtacha", "android"),

    # =====================
    #   REDMI NOTE SERIES
    # =====================
    "redmi note 5": ("o'rtacha", "android"),
    "redmi note 6 pro": ("o'rtacha", "android"),

    "redmi note 7": ("o'rtacha", "android"),
    "redmi note 7 pro": ("o'rtacha", "android"),

    "redmi note 8": ("o'rtacha", "android"),
    "redmi note 8 pro": ("o'rtacha", "android"),

    "redmi note 9": ("o'rtacha", "android"),
    "redmi note 9s": ("o'rtacha", "android"),
    "redmi note 9 pro": ("o'rtacha", "android"),

    "redmi note 10": ("o'rtacha", "android"),
    "redmi note 10 pro": ("kuchli", "android"),
    "redmi note 10 pro max": ("kuchli", "android"),
    "redmi note 10s": ("o'rtacha", "android"),

    "redmi note 11": ("o'rtacha", "android"),
    "redmi note 11s": ("o'rtacha", "android"),
    "redmi note 11 pro": ("kuchli", "android"),
    "redmi note 11 pro+": ("kuchli", "android"),

    "redmi note 12": ("o'rtacha", "android"),
    "redmi note 12s": ("o'rtacha", "android"),
    "redmi note 12 pro": ("kuchli", "android"),
    "redmi note 12 pro+": ("kuchli", "android"),

    "redmi note 13": ("o'rtacha", "android"),
    "redmi note 13 pro": ("kuchli", "android"),
    "redmi note 13 pro+": ("kuchli", "android"),

    "redmi note 14 pro": ("kuchli", "android"),
    "redmi note 14 pro+": ("kuchli", "android"),

    "redmi note 15 pro": ("kuchli", "android"),
    "redmi note 15 pro+": ("kuchli", "android"),

    # =====================
    #   REDMI K SERIES
    # =====================

    "redmi k20": ("kuchli", "android"),
    "redmi k20 pro": ("kuchli", "android"),

    "redmi k30": ("kuchli", "android"),
    "redmi k30 5g": ("kuchli", "android"),
    "redmi k30 pro": ("kuchli", "android"),
    "redmi k30 ultra": ("kuchli", "android"),

    "redmi k40": ("kuchli", "android"),
    "redmi k40 pro": ("kuchli", "android"),
    "redmi k40 pro+": ("kuchli", "android"),
    "redmi k40 gaming": ("kuchli", "android"),

    "redmi k50": ("kuchli", "android"),
    "redmi k50 pro": ("kuchli", "android"),
    "redmi k50 ultra": ("kuchli", "android"),

    "redmi k60": ("kuchli", "android"),
    "redmi k60 pro": ("kuchli", "android"),
    "redmi k60 ultra": ("kuchli", "android"),

    "redmi k70": ("kuchli", "android"),
    "redmi k70 pro": ("kuchli", "android"),
    "redmi k70 ultra": ("kuchli", "android"),

    # =====================
    #   OPPO SERIES
    # =====================
    "oppo a57": ("o'rtacha", "android"),
    "oppo a58": ("o'rtacha", "android"),
    "oppo a59": ("o'rtacha", "android"),
    "oppo a76": ("o'rtacha", "android"),
    "oppo a78": ("o'rtacha", "android"),

    "oppo reno 7": ("kuchli", "android"),
    "oppo reno 8": ("kuchli", "android"),
    "oppo reno 9": ("kuchli", "android"),

    # =====================
    #   VIVO SERIES
    # =====================
    "vivo y21": ("kuchsiz", "android"),
    "vivo y22": ("kuchsiz", "android"),
    "vivo y27": ("o'rtacha", "android"),
    "vivo y33s": ("o'rtacha", "android"),
    "vivo y36": ("o'rtacha", "android"),

    "vivo v23": ("kuchli", "android"),
    "vivo v25": ("kuchli", "android"),

    # =====================
    #   HUAWEI SERIES
    # =====================
    "huawei nova 9": ("o'rtacha", "android"),
    "huawei nova 10": ("kuchli", "android"),
    "huawei nova 11": ("kuchli", "android"),
    "huawei nova 12": ("kuchli", "android"),

    "huawei p40 lite": ("o'rtacha", "android"),
    "huawei p smart 2021": ("o'rtacha", "android"),
    "huawei y9a": ("o'rtacha", "android"),

    # =====================
    #   HONOR SERIES
    # =====================
    "honor x6a": ("kuchsiz", "android"),
    "honor x7": ("o'rtacha", "android"),
    "honor x8": ("o'rtacha", "android"),
    "honor x9": ("o'rtacha", "android"),

    "honor 90 lite": ("o'rtacha", "android"),
    "honor 90": ("kuchli", "android"),
    # =====================
    #   INFINIX SERIES
    # =====================
    "infinix hot 30": ("kuchsiz", "android"),
    "infinix hot 40": ("kuchsiz", "android"),

    "infinix note 12": ("o'rtacha", "android"),
    "infinix note 30": ("o'rtacha", "android"),
    "infinix note 40": ("o'rtacha", "android"),

    "infinix zero 20": ("kuchli", "android"),
    "infinix zero 30": ("kuchli", "android"),

    # =====================
    #   TECNO SERIES
    # =====================
    "tecno spark 10": ("kuchsiz", "android"),
    "tecno spark 20": ("kuchsiz", "android"),

    "tecno pova 3": ("o'rtacha", "android"),
    "tecno pova 4": ("o'rtacha", "android"),
    "tecno pova 5": ("o'rtacha", "android"),

    "tecno camon 18": ("o'rtacha", "android"),
    "tecno camon 20": ("o'rtacha", "android"),

    # =====================
    #   REALME SERIES
    # =====================
    "realme c53": ("kuchsiz", "android"),
    "realme c55": ("kuchsiz", "android"),
    "realme c67": ("kuchsiz", "android"),

    "realme 9": ("o'rtacha", "android"),
    "realme 10": ("o'rtacha", "android"),
    "realme 11": ("o'rtacha", "android"),

    "realme 11 pro": ("kuchli", "android"),


}

def device_block_response():
    return (
        "⚠️ Bu bo‘lim faqat AI bilan suhbat uchun.\n"
        "Free Fire qurilma sozlamalari shu yerda taqdim etilmaydi.\n\n"
        "📱 Telefoningizga mos PRO nastroyka olish uchun:\n"
        "<b>⚙️ Telefonga mos sozlamalar</b> bo‘limiga o‘ting.\n\n"
        "U yerda AI sizning modelga eng ideal sensitivity + DPI + tugma o‘lchamlarini yaratadi 🔥"
    )

from openai import AsyncOpenAI
client = AsyncOpenAI(api_key=OPENAI_API_KEY)



def classify_device_model(model: str):
    model = normalize_model_name(model)
    if model in VALID_DEVICES:
        return VALID_DEVICES[model]
    return "unknown", "unknown"

def generate_android_settings(daraja: str):
    import random

    # DPI diapazoni
    if daraja == "kuchli":
        dpi = random.randint(490, 550)
    elif daraja == "o'rtacha":
        dpi = random.randint(520, 600)
    else:
        dpi = random.randint(550, 650)

    # General / RedDot diapazonlari
    if daraja == "kuchli":
        general = random.randint(121, 150)
        red_dot = random.randint(100, 125)
    elif daraja == "o'rtacha":
        general = random.randint(115, 152)
        red_dot = random.randint(95, 115)
    else:
        general = random.randint(115, 135)
        red_dot = random.randint(85, 100)

    # Qo‘shimcha
    two_x = max(60, general - random.randint(8, 18))
    four_x = max(50, general - random.randint(12, 30))
    awm = max(40, four_x - random.randint(5, 17))
    free_look = min(200, general + random.randint(5, 25))

    # DPI ga bog‘liq otish tugmasi
    if 490 <= dpi <= 550:     # Kuchli
        fire_btn = random.randint(42, 46)
    elif 520 <= dpi <= 600:   # O‘rtacha
        fire_btn = random.randint(38, 44)
    else:                     # Kuchsiz
        fire_btn = random.randint(36, 40)

    return dpi, general, red_dot, two_x, four_x, awm, free_look, fire_btn


def generate_iphone_settings(daraja: str):
    if daraja == "kuchli":
        general = random.randint(130, 155)
        reddot = random.randint(95, 115)
    elif daraja == "o'rtacha":
        general = random.randint(120, 160)
        reddot = random.randint(105, 124)
    else:
        general = random.randint(140, 165)
        reddot = random.randint(105, 130)

    two_x = max(70, general - random.randint(10, 25))
    four_x = max(60, general - random.randint(15, 28))
    awm = max(50, four_x - random.randint(5, 18))
    free_look = random.randint(160, 200)
    fire_btn = random.randint(40, 50)

    return general, reddot, two_x, four_x, awm, free_look, fire_btn


def build_ff_settings_text(model: str, daraja: str, platforma: str, data):
    import random

    # Unpack data
    if platforma == "iphone":
        general, red_dot, two_x, four_x, awm, free_look, fire_btn = data
        dpi_text = "iPhone DPI ishlatmaydi."
    else:
        dpi, general, red_dot, two_x, four_x, awm, free_look, fire_btn = data
        dpi_text = str(dpi)

    # Qurilma darajasi nomi
    daraja_label = {
        "kuchli": "Kuchli",
        "o'rtacha": "O‘rtacha",
        "kuchsiz": "Kuchsiz"
    }.get(daraja, "O‘rtacha")

    # Drag matni
    drag_text = (
        "Otish tugmasini yuqoriga ~1.5–2 sm torting, boshga tortishga harakat qiling."
        if platforma == "iphone"
        else "Otish tugmasini yuqoriga ~1.8–2.3 sm torting, boshga tortishga harakat qiling."
    )

    # Lagni kamaytirish (o‘zgarmaydi)
    lag_tips = [
        "Fon ilovalarini yopib qo‘ying.",
        "Grafikni Smooth yoki Balanced ga tushiring.",
        "FPS → High/Ultra qilib qo‘ying.",
        "Telefon qizib ketmasin.",
        "Har 2–3 matchdan keyin RAMni tozalang."
    ]

    # PRO maslahatlar — 20+ ta
    pro_tips = [
        "Dragni har kuni 10 daqiqa mashq qiling — natija o‘zi keladi.",
        "Tugma joylashuvini doimiy qiling — barqarorlik PROlar siri.",
        "Doimo ustun pozitsiyani egallashga harakat qiling.",
        "Boshga drag qilishda ortiqcha qo‘l titrog‘idan qoching.",
        "Dushman ko‘rtilishi bilan dragni torting — kechiktirmang.",
        "Yaqin masofada vertikal drag juda samarali bo‘ladi.",
        "400+ DPI da kamerani haddan oshirmang — beqarorlashtiradi.",
        "Otish tugmasini juda katta qilmay — boshga dragni yo‘qotmang.",
        "Kontrol punktlarini o‘zgartirmay o‘sha joyga moslashib oling.",
        "Snayperda sezgirlikni baland qo‘ymang — boshni otib yuboradi.",
        "Har kuni 3–5 match faqat drag mashqi uchun o‘ynang.",
        "Oldinga yurganda drag sekinroq tortiladi — moslashing.",
        "Panel shift (Side Drag) ni o‘rganing — PROlar shuni ishlatadi.",
        "HUD joylashuvida bosh barmoq bilan AWM dragni mashq qiling.",
        "Juda yaqin masofada 2X drag yaxshi ishlaydi.",
        "MP40 bilan dragni diagonal tepkida torting.",
        "Drag uzunligi har safar biroz farq qilsin — anti-recoil chiqmaydi.",
        "Yugurish paytida drag qilishni o‘rganing — ustunlik beradi.",
        "Kam qiziyotgan telefonlarda DPI +10 ko‘tarish yaxshi ishlaydi.",
        "Ko‘p qizadigan telefonlarda DPI past bo‘lishi kerak.",
        "Dragni boshidan oxirigacha bitta tezlikda torting."
    ]

    pro_tip = random.choice(pro_tips)

    # Matnni chiroyli formatda qaytarish
    return f"""
📱 <b>1) Qurilma darajasi</b>
{daraja_label}

🎯 <b>2) Sensitivity + DPI + Otish Tugmasi</b>
- General / Обзор: <b>{general}</b>
- Red Dot / Коллиматор: <b>{red_dot}</b>
- 2X: <b>{two_x}</b>
- 4X: <b>{four_x}</b>
- AWM: <b>{awm}</b>
- Free Look: <b>{free_look}</b>
- DPI: <b>{dpi_text}</b>
- Otish Tugmasi: <b>{fire_btn}</b>

⚡ <b>3) Drag masofasi</b>
{drag_text}

🧹 <b>4) Lagni kamaytirish</b>
- {lag_tips[0]}
- {lag_tips[1]}
- {lag_tips[2]}
- {lag_tips[3]}
- {lag_tips[4]}

🔥 <b>5) Yakuniy PRO maslahat</b>
{pro_tip}
"""



@dp.message(F.text == "⚙️ Telefonga mos sozlamalar")
async def ai_device_intro(message: Message, state: FSMContext):
    if await guard_common(message, allow_ai=True):
        return

    await state.set_state(AiMenuStates.DEVICE)

    text = (
    "⚙️ <b>Telefonga mos Free Fire sozlamalari</b>\n\n"
    "AI sizning qurilmangiz uchun ideal nastroykani yaratadi:\n"
    "• General / Red Dot / 2X / 4X / AWM\n"
    "• DPI tavsiyasi\n"
    "• Otish tugmasi o‘lchami\n"
    "• Lagni kamaytirish bo‘yicha maslahatlar\n"
    "• 350+ telefon modeli uchun PRO optimizatsiya 😎\n\n"
    
    "📱 <b>Telefon modelini kiriting:</b>\n"
    "Masalan: <code>Redmi Note 9</code>, <code>Samsung A12</code>, <code>iPhone 11</code>\n\n"

    "❗ <b>Telefon nomini to‘g‘ri yozing:</b>\n"
    "<code>Redmi note13pro</code> ❌ — noto‘g‘ri\n"
    "<code>Redmi note 13 pro</code> ✅ — to‘g‘ri (bo‘sh joyga e’tibor bering)\n\n"

    "🔥 Sizga PRO-level Free Fire nastroykani tayyorlab beraman!"
)

    await message.answer(text, parse_mode="HTML", reply_markup=back_kb)

@dp.message(AiMenuStates.DEVICE, AILockFilter())
async def ai_device_chat(message: Message, state: FSMContext):

    uid = message.from_user.id

    # 🔒 Anti-spam: faqat bitta xabarni qabul qiladi
    ACTIVE_AI.add(uid)

    try:
        dev = (message.text or "").strip()
        daraja, platforma = classify_device_model(dev)

        if daraja == "unknown":
            await message.answer(
                "❗ Bu model bazada yo‘q.\n"
                "Faqat mavjud ro‘yxatdagi real qurilmalardan foydalaning.",
                parse_mode="HTML",
            )
            return

        # SETTINGS GENERATOR (sening eski tiziming o‘z holida)
        if platforma == "iphone":
            data = generate_iphone_settings(daraja)
        else:
            data = generate_android_settings(daraja)

        # FINAL BEAUTIFUL MESSAGE (5 bo‘limli)
        text = build_ff_settings_text(dev, daraja, platforma, data)

        await message.answer(text, parse_mode="HTML")

    except Exception as e:
        await message.answer(f"❌ Device AI xatosi: {e}")

    finally:
        ACTIVE_AI.discard(uid)


@dp.message(F.text == "✨ Nickname Yaratish", StateFilter(AiMenuStates.ROOT), AILockFilter())
async def nickname_generator_intro(message: Message, state: FSMContext):

    # Bu bo‘lim AI ishlatmaydi, shuning uchun ACTIVE_AI ga qo‘shmaymiz

    await state.set_state(NickGenStates.WAIT_NAME)

    text = (
        "✨ <b>Premium Free Fire Nickname Generator</b>\n\n"
        "Sizga mos, premium darajadagi stil niklar tayyorlab beraman.\n\n"
        "👉 Faqat <b>birta so‘z</b> yoki <b>nik uslubi</b> yozing:\n"
        "Masalan:\n"
        "<code>Fire</code>\n"
        "<code>Dragon</code>\n"
        "<code>Samuray</code>\n"
        "<code>Dark King</code>\n"
        "<code>AloX</code>\n\n"
        "🔥 20–30 ta PRO darajadagi nik variantlarini tayyorlab beraman!"
    )

    await message.answer(text, parse_mode="HTML", reply_markup=back_kb)


@dp.message(NickGenStates.WAIT_NAME, AILockFilter())
async def nickname_generate(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    uid = message.from_user.id   # Anti-spam uchun uid

    # -----------------------------
    # 1) Asosiy menyu tugmalarida nick yaratmaslik
    # -----------------------------
    MAIN_MENU_BUTTONS = [
        "📊 Profilim", "💎 Almaz ishlash", "🤖 Sun’iy Intellekt",
        "🏅 Mening darajam", "🏆 Reyting", "🛒 Akkount Bozor",
        "💰 Almaz sotib olish", "📢 Reklama va yangiliklar"
    ]

    if text in MAIN_MENU_BUTTONS:
        await state.clear()
        await message.answer("🏠 Asosiy menyuga qaytdingiz.", reply_markup=main_menu)
        return

    # -----------------------------
    # 2) ORQAGA tugmasi
    # -----------------------------
    if text == "⬅️ Orqaga":
        await state.clear()
        await state.set_state(AiMenuStates.ROOT)
        await message.answer(
            "🔙 Sun’iy Intellekt menyusiga qaytdingiz.",
            reply_markup=ai_menu
        )
        return

    # -----------------------------
    # 3) Minimal matn tekshiruvi
    # -----------------------------
    if not text or len(text) < 2:
        return await message.answer("❗ Iltimos, kamida 2 harfli ism yoki uslub yozing.")

    # -----------------------------
    # 4) 🔒 ANTI-SPAM YAMOQ
    #    — Aynan shu bo‘lim AI ishlatadi → bir vaqtning o‘zida faqat 1 so‘rov!
    # -----------------------------
    ACTIVE_AI.add(uid)

    try:
        # Loading xabari
        loading = await message.answer("⏳ Premium niklar tayyorlanmoqda…")

        # PROMPT — aynan sening prompting, o‘zgartirilmagan
        prompt = f"""
        Free Fire uchun premium, kreativ, professional va juda chiroyli NICKNAME generator bo‘lib ishlagin.
        Foydalanuvchi bergan so‘z: {text}

        Talablar:
        - Kamida 30 ta
        - Turlik stil: Fire, Neon, Dark, Samurai, Cyber, Luxury, Cute, Minimal, Pro
        - Unicode, bold, italic, stylish shakllardan foydalansin
        - Har bir nik 1 qator bo‘lsin
        - Takrorlanmasin
        - Juda sifatli bo‘lsin
        - TikTok/FF Pro niklarga o‘xshash bo‘lsin

        Faqat RO‘YXAT HOLIDA chiqaring.
        """

        # AI javobini olish
        response = await get_ai_response(prompt, short=False)

        # Loadingni o‘chiramiz
        try:
            await loading.delete()
        except:
            pass

        # Yakuniy javob
        await message.answer(
            f"✨ <b>Siz uchun tayyor niklar:</b>\n\n{response}",
            parse_mode="HTML"
        )

    except Exception as e:

        # Loadingni baribir yo‘q qilishga urinib ko‘ramiz
        try:
            await loading.delete()
        except:
            pass

        await message.answer(f"❌ Nick AI xatosi: {e}")

    finally:
        # 🔓 ANTI-SPAM BANDLIKNI YOPAMIZ
        ACTIVE_AI.discard(uid)

        # Foydalanuvchi yana nick yaratishi uchun state tiklanadi (seniki kabi)
        await state.set_state(NickGenStates.WAIT_NAME)



@dp.message(F.text == "⬅️ Orqaga", StateFilter(AiMenuStates.ROOT))
async def ai_back_from_root(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🏠 Asosiy menyuga qaytdingiz.",
        reply_markup=main_menu
    )


# ============== Profil / Reyting / Almaz / News / Market / Buy ==============
@dp.message(F.text == "📊 Profilim")
async def show_profile(message: Message):
    if await guard_common(message):
        return
    user = await get_user(message.from_user.id)
    if not user:
        await add_user(message.from_user.id, message.from_user.username or "Noma’lum")
        user = await get_user(message.from_user.id)

    almaz = user[3] if len(user) > 3 and user[3] is not None else 0
    total_refs = await count_verified_referrals(message.from_user.id)
    username = user[1] or (message.from_user.username or "Anonim")

    # Liga va rank ballarini ham yangilab olamiz
    rank_data = await update_user_rank(message.from_user.id, with_notification=False)

    text = (
        "👤 <b>Profilingiz</b>\n\n"
        f"👤 Username: @{username}\n"
        f"🏅 Liga: {rank_data['emoji']} <b>{rank_data['level']} liga</b>\n"
        f"📊 Reyting ballari: <b>{rank_data['score']}</b>\n"
        f"💎 Almaz: <b>{almaz}</b>\n"
        f"🤝 Umumiy tasdiqlangan takliflar: <b>{total_refs}</b>\n\n"
        "Almazlaringizni istagan paytda yechib olishingiz mumkin 👇"
    )

    withdraw_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Almazni yechish", callback_data="withdraw_start")]
        ]
    )

    await message.answer(text, parse_mode="HTML", reply_markup=withdraw_kb)


@dp.message(F.text == "🏅 Mening darajam")
async def show_my_rank(message: Message):
    """
    Shaxsiy liga va batafsil statistika + INLINE TUGMA.
    """
    if await guard_common(message):
        return

    user = await get_user(message.from_user.id)
    if not user:
        await add_user(message.from_user.id, message.from_user.username or "Noma’lum")
        user = await get_user(message.from_user.id)

    almaz = user[3] if len(user) > 3 and user[3] is not None else 0

    # Liga hisoblash
    rank_data = await update_user_rank(message.from_user.id, with_notification=False)

    text = (
        "🏅 <b>Mening darajam</b>\n\n"
        f"⭐ Joriy liga: {rank_data['emoji']} <b>{rank_data['level']} liga</b>\n"
        f"📊 Reyting ballari: <b>{rank_data['score']}</b>\n\n"
        "📌 <b>Shaxsiy statistika:</b>\n"
        f"• Tasdiqlangan takliflar: <b>{rank_data['verified_refs']}</b>\n"
        f"• Umumiy takliflar: <b>{rank_data['total_refs']}</b>\n"
        f"• Joriy Almaz balansi: <b>{almaz}</b>\n\n"
    )

    if rank_data["next_name"]:
        text += (
            f"🎯 Keyingi liga: <b>{rank_data['next_name']}</b>\n"
            f"Unga yetish uchun yana <b>{rank_data['to_next']}</b> ball kerak.\n\n"
        )
    else:
        text += "👑 Siz eng yuqori liga — <b>GrandMaster</b> darajasidasiz! 👑🔥\n\n"

    text += (
        "ℹ️ <i>Eslatma:</i> Ligalar hozircha faqat obro‘ sifatida ishlaydi.\n"
        "🔜 Yaqin vaqt ichida ligalar uchun alohida bonuslar qo‘shiladi.\n"
        "Faol bo‘ling – birinchilar qatorida bo‘lasiz! 🚀"
    )

    # 🔥 Inline tugma
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Darajani qanday oshiramiz❔",
                    callback_data="rank_how_to"
                )
            ]
        ]
    )

    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@dp.message(F.text == "🏆 Reyting")
async def show_leaderboard_handler(message: Message):
    """
    GLOBAL reyting — Almaz bo'yicha TOP-15.
    """
    if await guard_common(message):
        return
    leaders = await get_leaderboard(limit=15)
    if not leaders:
        return await message.answer("📉 Hozircha reyting bo‘sh.")
    text = "🏆 <b>Top 15 foydalanuvchi (Almaz bo‘yicha)</b>\n\n"
    for i, (username, almaz) in enumerate(leaders[:15], 1):
        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else "⭐"
        text += f"{medal} @{(username or 'Anonim')} — 💎 {almaz}\n"
    await message.answer(text, parse_mode="HTML")


@dp.message(F.text == "💎 Almaz ishlash")
async def earn_almaz(message: Message):
    if await guard_common(message):
        return

    me = await bot.get_me()
    bot_username = me.username
    user_id = message.from_user.id
    reward = await get_referral_reward()

    # 🔹 HTML formatdagi to‘liq xabar
    html_text = (
        "💎 <b>Almaz ishlash — o‘yin ichidagi boylikka eng tez yo‘l!</b>\n\n"
        "Free Fire’da kuchli bo‘lishni xohlaysizmi? 🔥\n"
        "Unda siz aynan to‘g‘ri joydasiz! Bu yerda hech qanday sarmoyasiz, "
        "faqat do‘stlaringizni taklif qilish orqali <b>almaz ishlab olishingiz</b> mumkin.\n\n"

        "👥 <b>Qanday ishlaydi?</b>\n"
        f"Do‘st taklif qilasiz → U botga kiradi → Captcha + Telefon tasdiqlaydi →\n"
        f"Siz esa darhol <b>{reward} Almaz</b> olasiz! 💎🎉\n\n"

        "🚀 <b>Nima uchun hozir boshlash kerak?</b>\n"
        "• Almazlar tez yig‘iladi\n"
        "• O‘yin ichida kuchliroq bo‘lasiz\n"
        "• Qimmat skin va itemlarga tez erishasiz\n"
        "• Hammasi <b>BEPUL</b> 🎁\n\n"

        "👇 <b>Sizning shaxsiy Taklif havolangiz:</b>\n"
        f"👉 https://t.me/{bot_username}?start=ref_{user_id}\n\n"

        "🔥 Qancha ko‘p do‘st — shuncha ko‘p Almaz!\n"
        "Bugunoq boshlang — natija darhol ko‘rina boshlaydi! 💎😎"
    )

    # 🔹 Oddiy matn (SHARE uchun, HTML YO‘Q!)
    plain_text = (
        f"💎 Almaz ishlash — eng tez yo‘l!\n\n"
        f"Free Fire’da kuchli bo‘lishni xohlaysizmi? 🔥\n"
        f"Qimmat skinlar, elita pass va itemlarga bepul ega bo‘lish imkoniyatini qo‘ldan boy bermang! 💎\n\n"
        f"Do‘stingizni taklif qiling va darhol {reward} Almaz oling!\n"
        f"Taklif havolangiz: https://t.me/{bot_username}?start={user_id}"
    )

    # 🔹 Inline tugma (HTMLsiz matn bilan)
    share_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📤 Do‘stlarga ulashish",
                    switch_inline_query=plain_text
                )
            ]
        ]
    )

    await message.answer(html_text, parse_mode="HTML", reply_markup=share_kb)


@dp.message(F.text == "📢 Reklama va yangiliklar")
async def show_news(message: Message):
    if await guard_common(message):
        return
    content = await get_dynamic_text("news")
    if not content:
        return await message.answer("📭 Hozircha yangiliklar yo‘q.")
    if content.startswith("MSG:"):
        try:
            _, chat_id_str, msg_id_str = content.split(":")
            await bot.copy_message(message.chat.id, int(chat_id_str), int(msg_id_str))
            return
        except Exception as e:
            log.warning("copy news failed: %s", e)
    await message.answer(f"📰 <b>So‘nggi yangiliklar</b>\n\n{content}", parse_mode="HTML")


@dp.message(F.text == "🛒 Akkount Bozor")
async def market(message: Message):
    if await guard_common(message):
        return
    await message.answer("🛒 Bozor bo‘limi tez orada.")


@dp.message(F.text == "💰 Almaz sotib olish")
async def buy_almaz(message: Message):
    if await guard_common(message):
        return
    text = await get_dynamic_text("almaz_buy")
    if not text:
        text = (
            "💰 <b>Almaz sotib olish</b>\n\n"
            "1️⃣ 10 000 so‘m → 100 Almaz\n"
            "2️⃣ 25 000 so‘m → 300 Almaz\n"
            "3️⃣ 40 000 so‘m → 500 Almaz\n\n"
            "To‘lovdan so‘ng shuni yozing: <code>10000 123456789</code>"
        )
    await message.answer(text, parse_mode="HTML")


# ============== Almaz YECHISH (foydalanuvchi taraf) ==============

@dp.callback_query(F.data == "withdraw_start")
async def withdraw_start(cb: CallbackQuery, state: FSMContext):
    user_id = cb.from_user.id
    user = await get_user(user_id)
    if not user:
        await cb.answer("Avval ro‘yxatdan o‘ting.", show_alert=True)
        return
    balance = user[3] if len(user) > 3 and user[3] is not None else 0
    if balance < 105:
        await cb.message.answer(
            "⚠️ Hali almaz yechish uchun balansingiz yetarli emas.\n\n"
            "Eng kamida <b>105 Almaz</b> to‘plasangiz, ishlagan almazlaringizni Free Fire akkauntingizga yechib olishingiz mumkin.",
            parse_mode="HTML"
        )
        await cb.answer()
        return

    buttons = []
    if balance >= 105:
        buttons.append(InlineKeyboardButton(text="105 Almaz 💎", callback_data="wd_amount:105"))
    if balance >= 210:
        buttons.append(InlineKeyboardButton(text="210 Almaz 💎", callback_data="wd_amount:210"))
    if balance >= 326:
        buttons.append(InlineKeyboardButton(text="326 Almaz 💎", callback_data="wd_amount:326"))

    kb = InlineKeyboardMarkup(inline_keyboard=[buttons])

    await cb.message.answer(
        "💳 <b>Almaz yechish</b>\n\n"
        f"Balansingiz: <b>{balance} Almaz</b>\n"
        "Qancha almazni yechmoqchi ekanligingizni tanlang:",
        parse_mode="HTML",
        reply_markup=kb
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("wd_amount:"))
async def withdraw_choose_amount(cb: CallbackQuery, state: FSMContext):
    user_id = cb.from_user.id
    user = await get_user(user_id)
    if not user:
        await cb.answer("Foydalanuvchi topilmadi.", show_alert=True)
        return
    balance = user[3] if len(user) > 3 and user[3] is not None else 0
    try:
        amount = int(cb.data.split(":")[1])
    except ValueError:
        await cb.answer("Noto‘g‘ri miqdor.", show_alert=True)
        return

    if balance < amount:
        await cb.answer("Balansingiz o‘zgargan, yechish uchun yetarli emas.", show_alert=True)
        return

    await state.set_state(WithdrawStates.WAITING_FF_ID)
    await state.update_data(withdraw_amount=amount)

    await cb.message.answer(
        "🎮 Endi, almaz yechmoqchi bo‘lgan Free Fire akkauntingiz <b>ID raqamini</b> yuboring.\n\n"
        "ID ni diqqat bilan tekshirib yuboring — almaz aynan shu akkauntga tushiriladi.",
        parse_mode="HTML"
    )
    await cb.answer()


@dp.message(WithdrawStates.WAITING_FF_ID)
async def withdraw_receive_ff_id(message: Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()
    amount = data.get("withdraw_amount")
    ff_id = (message.text or "").strip()

    if not amount or not ff_id:
        await message.answer("❌ Noto‘g‘ri ma’lumot. /start yuborib qayta urinib ko‘ring.")
        await state.clear()
        return

    if len(ff_id) < 3:
        await message.answer("⚠️ Free Fire ID juda qisqa ko‘rinmoqda. Iltimos, qayta tekshirib yuboring.")
        return

    # so'rovni DBga yozamiz
    request_id = await create_withdraw_request(user_id, int(amount), ff_id)
    await state.clear()

    await message.answer(
        "✅ Almaz yechish bo‘yicha so‘rovingiz qabul qilindi!\n\n"
        f"📥 Miqdor: <b>{amount} Almaz</b>\n"
        f"🎮 Free Fire ID: <code>{ff_id}</code>\n\n"
        "Almaz 24 soat ichida Free Fire akkauntingizga tushiriladi. Iltimos, sabr qiling 🙂",
        parse_mode="HTML"
    )

    # adminlarga xabar
    await notify_admins_about_withdraw(request_id)


# ============== Withdraw admin callbacklari ==============

@dp.callback_query(F.data.startswith("wd_ok:"))
async def withdraw_approve(cb: CallbackQuery):
    if not await is_owner_or_admin(cb.from_user.id):
        await cb.answer("Sizda bu amal uchun ruxsat yo‘q.", show_alert=True)
        return
    try:
        req_id = int(cb.data.split(":")[1])
    except ValueError:
        await cb.answer("Noto‘g‘ri so‘rov.", show_alert=True)
        return

    req = await get_withdraw_request(req_id)
    if not req:
        await cb.answer("So‘rov topilmadi yoki o‘chirib yuborilgan.", show_alert=True)
        return

    r_id, user_id, amount, ff_id, status, created_at, processed_at, processed_by, note = req
    if status != "pending":
        await cb.answer(f"Bu so‘rov allaqachon '{status}' holatida.", show_alert=True)
        return

    user = await get_user(user_id)
    balance = user[3] if user and len(user) > 3 and user[3] is not None else 0
    if balance < amount:
        await cb.answer("Foydalanuvchi balansida bu miqdor yetarli emas.", show_alert=True)
        return

    # balansdan ayrib tashlaymiz va so'rovni tasdiqlaymiz
    await add_almaz(user_id, -amount)
    await update_withdraw_status(req_id, "approved", cb.from_user.id, None)
    await update_withdraw_admin_messages(req_id, "✅ Tasdiqlandi")

    # foydalanuvchiga xabar
    try:
        await bot.send_message(
            user_id,
            "🎉 Almaz yechish so‘rovingiz tasdiqlandi!\n\n"
            f"💎 Miqdor: <b>{amount} Almaz</b>\n"
            "Almazingiz Free Fire akkauntingizga muvaffaqiyatli tashlab berildi. Rahmat! 😊",
            parse_mode="HTML"
        )
    except Exception:
        pass

    await cb.answer("So‘rov tasdiqlandi.")


@dp.callback_query(F.data.startswith("wd_reject:"))
async def withdraw_reject(cb: CallbackQuery):
    if not await is_owner_or_admin(cb.from_user.id):
        await cb.answer("Sizda bu amal uchun ruxsat yo‘q.", show_alert=True)
        return
    try:
        req_id = int(cb.data.split(":")[1])
    except ValueError:
        await cb.answer("Noto‘g‘ri so‘rov.", show_alert=True)
        return

    req = await get_withdraw_request(req_id)
    if not req:
        await cb.answer("So‘rov topilmadi yoki o‘chirib yuborilgan.", show_alert=True)
        return

    r_id, user_id, amount, ff_id, status, created_at, processed_at, processed_by, note = req
    if status != "pending":
        await cb.answer(f"Bu so‘rov allaqachon '{status}' holatida.", show_alert=True)
        return

    await update_withdraw_status(req_id, "rejected", cb.from_user.id, None)
    await update_withdraw_admin_messages(req_id, "❌ Rad etildi")

    try:
        await bot.send_message(
            user_id,
            "❌ Almaz yechish bo‘yicha so‘rovingiz rad etildi.\n\n"
            "Bunga turli sabablar bo‘lishi mumkin (qoidabuzarlik, noto‘g‘ri ma’lumot va hokazo).\n"
            "Agar bu xatolik deb o‘ylasangiz, qo‘llab-quvvatlashga murojaat qilishingiz mumkin.",
            parse_mode="HTML"
        )
    except Exception:
        pass

    await cb.answer("So‘rov rad etildi.")


@dp.callback_query(F.data.startswith("wd_edit:"))
async def withdraw_edit_start(cb: CallbackQuery, state: FSMContext):
    if not await is_owner_or_admin(cb.from_user.id):
        await cb.answer("Sizda bu amal uchun ruxsat yo‘q.", show_alert=True)
        return
    try:
        req_id = int(cb.data.split(":")[1])
    except ValueError:
        await cb.answer("Noto‘g‘ri so‘rov.", show_alert=True)
        return

    req = await get_withdraw_request(req_id)
    if not req:
        await cb.answer("So‘rov topilmadi yoki o‘chirib yuborilgan.", show_alert=True)
        return
    if req[4] != "pending":
        await cb.answer(f"Bu so‘rov allaqachon '{req[4]}' holatida.", show_alert=True)
        return

    await state.set_state(WithdrawEdit.WAITING_TEXT)
    await state.update_data(edit_request_id=req_id)

    await cb.message.answer(
        "✏️ Foydalanuvchiga yuboriladigan xabar matnini kiriting.\n"
        "Masalan: Free Fire ID noto‘g‘ri ko‘rsatilgan, iltimos, qayta yuboring.",
        reply_markup=back_kb
    )
    await cb.answer()


@dp.message(WithdrawEdit.WAITING_TEXT)
async def withdraw_edit_send(message: Message, state: FSMContext):
    if not await is_owner_or_admin(message.from_user.id):
        return
    data = await state.get_data()
    req_id = data.get("edit_request_id")
    if not req_id:
        await state.clear()
        await message.answer("❌ So‘rov topilmadi. Qaytadan urinib ko‘ring.", reply_markup=admin_menu)
        return

    note = (message.text or "").strip()
    req = await get_withdraw_request(int(req_id))
    if not req:
        await state.clear()
        await message.answer("❌ So‘rov bazadan topilmadi.", reply_markup=admin_menu)
        return

    r_id, user_id, amount, ff_id, status, created_at, processed_at, processed_by, old_note = req
    if status != "pending":
        await state.clear()
        await message.answer(f"ℹ️ Bu so‘rov allaqachon '{status}' holatiga o‘tkazilgan.", reply_markup=admin_menu)
        return

    # foydalanuvchiga izoh yuboramiz, balans o'zgarmaydi
    try:
        await bot.send_message(
            user_id,
            f"✏️ Almaz yechish so‘rovi bo‘yicha xabar:\n\n{note}"
        )
    except Exception:
        pass

    await update_withdraw_status(int(req_id), "edited", message.from_user.id, note)
    await update_withdraw_admin_messages(int(req_id), "✏️ Tahrirlandi")

    await state.clear()
    await message.answer(
        "✅ Izoh foydalanuvchiga yuborildi va so‘rov <b>tahrirlandi</b> holatiga o‘tkazildi.",
        parse_mode="HTML",
        reply_markup=admin_menu
    )


# ============== ADMIN PANEL ==============
@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if not (message.from_user.id == OWNER_ID or await is_admin(message.from_user.id)):
        return await message.answer("🚫 Siz admin emassiz.")
    await message.answer("👑 <b>Admin panel</b>", parse_mode="HTML", reply_markup=admin_menu)


@dp.message(F.text == "📊 Foydalanuvchilar soni")
async def user_count(message: Message):
    if not (message.from_user.id == OWNER_ID or await is_admin(message.from_user.id)):
        return
    from database import DB_NAME
    import aiosqlite
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        total = (await cur.fetchone())[0]
    await message.answer(f"📈 Jami foydalanuvchilar: <b>{total}</b>", parse_mode="HTML")


@dp.message(F.text == "📋 Guruhlar ro‘yxati")
async def show_groups_handler(message: Message):
    if not (message.from_user.id == OWNER_ID or await is_admin(message.from_user.id)):
        return
    groups = await list_groups()
    if not groups:
        return await message.answer("📭 Guruhlar ro‘yxati bo‘sh.")
    text = "📋 <b>Guruhlar:</b>\n\n" + "\n".join(f"🔹 {title} — <code>{gid}</code>" for gid, title in groups)
    await message.answer(text, parse_mode="HTML")


# --- Dynamic matnlar tahriri ---
@dp.message(F.text == "📰 Reklama/Yangilik sozlash")
async def edit_news(message: Message, state: FSMContext):
    if not (message.from_user.id == OWNER_ID or await is_admin(message.from_user.id)):
        return
    await state.set_state(TextEdit.new_text)
    await state.update_data(section="news")
    await message.answer("📰 Yangi yangilik xabarini yuboring (matn yoki media).", reply_markup=back_kb)


@dp.message(F.text == "💰 Almaz sotib olish matni")
async def edit_buy_text(message: Message, state: FSMContext):
    if not (message.from_user.id == OWNER_ID or await is_admin(message.from_user.id)):
        return
    await state.set_state(TextEdit.new_text)
    await state.update_data(section="almaz_buy")
    await message.answer("💰 Almaz sotib olish bo‘limi uchun matn yuboring:", reply_markup=back_kb)


# ❗ TextEdit state'da "⬅️ Orqaga" — o'zgarishni BEKOR qil
@dp.message(F.text == "⬅️ Orqaga", StateFilter(TextEdit.new_text))
async def cancel_text_edit(message: Message, state: FSMContext):
    if not (message.from_user.id == OWNER_ID or await is_admin(message.from_user.id)):
        return
    await state.clear()
    await message.answer("❎ O‘zgartirish bekor qilindi.", reply_markup=admin_menu)


@dp.message(StateFilter(TextEdit.new_text))
async def save_dynamic_text(message: Message, state: FSMContext):
    if not (message.from_user.id == OWNER_ID or await is_admin(message.from_user.id)):
        return
    data = await state.get_data()
    section = data.get("section")
    if section == "news":
        if message.text and not message.caption:
            await update_dynamic_text("news", message.text)
        else:
            await update_dynamic_text("news", f"MSG:{message.chat.id}:{message.message_id}")
        await state.clear()
        return await message.answer("✅ Yangilik xabari yangilandi.", reply_markup=admin_menu)
    if section == "almaz_buy":
        await update_dynamic_text("almaz_buy", message.text or "")
        await state.clear()
        return await message.answer("✅ Matn yangilandi.", reply_markup=admin_menu)


# --- Reklama yuborish ---
@dp.message(F.text == "📢 Reklama yuborish")
async def ask_broadcast(message: Message, state: FSMContext):
    if not (message.from_user.id == OWNER_ID or await is_admin(message.from_user.id)):
        return
    await message.answer("📢 Reklama xabarini yuboring (matn yoki media).", reply_markup=back_kb)
    await state.set_state(Broadcast.WAITING)


# ❗ Broadcast state'da "⬅️ Orqaga" — BEKOR
@dp.message(F.text == "⬅️ Orqaga", StateFilter(Broadcast.WAITING))
async def cancel_broadcast(message: Message, state: FSMContext):
    if not (message.from_user.id == OWNER_ID or await is_admin(message.from_user.id)):
        return
    await state.clear()
    await message.answer("❎ Reklama yuborish bekor qilindi.", reply_markup=admin_menu)


@dp.message(
    StateFilter(Broadcast.WAITING),
    F.content_type.in_({
        ContentType.TEXT, ContentType.PHOTO, ContentType.VIDEO, ContentType.AUDIO,
        ContentType.DOCUMENT, ContentType.VOICE, ContentType.STICKER, ContentType.VIDEO_NOTE
    })
)
async def handle_broadcast(message: Message, state: FSMContext):
    if not (message.from_user.id == OWNER_ID or await is_admin(message.from_user.id)):
        return
    await state.clear()
    await message.answer("🚀 Reklama yuborilmoqda… ⏳")

    from database import DB_NAME
    import aiosqlite
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT user_id FROM users")
        users = [r[0] for r in await cur.fetchall()]

    total, success, failed = len(users), 0, 0
    for uid in users:
        try:
            await bot.copy_message(chat_id=uid, from_chat_id=message.chat.id, message_id=message.message_id)
            success += 1
            await asyncio.sleep(0.03)
        except Exception:
            failed += 1

    await message.answer(
        f"✅ Reklama yakunlandi!\n📬 Yuborilgan: <b>{success}</b>\n❌ Yetkazilmagan: <b>{failed}</b>\n👥 Jami: <b>{total}</b>",
        parse_mode="HTML", reply_markup=admin_menu
    )


# --- Majburiy kanallar boshqaruvi ---
@dp.message(F.text == "🧩 Majburiy kanallar")
async def channels_menu(message: Message, state: FSMContext):
    if not (message.from_user.id == OWNER_ID or await is_admin(message.from_user.id)):
        return
    channels = await list_required_channels()
    text = "🧩 <b>Majburiy kanallar</b>\n\n" + ("\n".join(f"• {ch}" for ch in channels) if channels else "— Hozircha kanal yo‘q.")
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Kanal qo‘shish"), KeyboardButton(text="➖ Kanal o‘chirish")],
            [KeyboardButton(text="⬅️ Chiqish")]
        ], resize_keyboard=True
    )
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@dp.message(F.text == "➕ Kanal qo‘shish")
async def channel_add_prompt(message: Message, state: FSMContext):
    if not (message.from_user.id == OWNER_ID or await is_admin(message.from_user.id)):
        return
    cnt = await required_channels_count()
    if cnt >= 6:
        return await message.answer("⚠️ 6 tadan ortiq kanal ulash mumkin emas.")
    await state.set_state(ChanManage.ADD)
    await message.answer("Kanal username’ini yuboring (masalan: @mychannel yoki -100... ID):", reply_markup=back_kb)


@dp.message(StateFilter(ChanManage.ADD))
async def channel_add(message: Message, state: FSMContext):
    ch = (message.text or "").strip()
    if not (message.from_user.id == OWNER_ID or await is_admin(message.from_user.id)):
        return
    if ch == "⬅️ Orqaga":
        await state.clear()
        return await message.answer("❎ Bekor qilindi.", reply_markup=admin_menu)
    if not (ch.startswith("@") or ch.startswith("-100") or ch.lstrip("-").isdigit()):
        return await message.answer("⚠️ Iltimos, @username yoki -100... chat ID yuboring.")
    ok = await add_required_channel(ch)
    await state.clear()
    if ok:
        await message.answer("✅ Kanal qo‘shildi.", reply_markup=admin_menu)
    else:
        await message.answer("ℹ️ Bu kanal allaqachon mavjud.", reply_markup=admin_menu)


@dp.message(F.text == "➖ Kanal o‘chirish")
async def channel_remove_prompt(message: Message, state: FSMContext):
    if not (message.from_user.id == OWNER_ID or await is_admin(message.from_user.id)):
        return
    await state.set_state(ChanManage.REMOVE)
    await message.answer("O‘chirish uchun @username yoki -100... chat ID yuboring:", reply_markup=back_kb)


@dp.message(StateFilter(ChanManage.REMOVE))
async def channel_remove(message: Message, state: FSMContext):
    ch = (message.text or "").strip()
    if not (message.from_user.id == OWNER_ID or await is_admin(message.from_user.id)):
        return
    if ch == "⬅️ Orqaga":
        await state.clear()
        return await message.answer("❎ Bekor qilindi.", reply_markup=admin_menu)
    ok = await remove_required_channel(ch)
    await state.clear()
    if ok:
        await message.answer("✅ Kanal o‘chirildi.", reply_markup=admin_menu)
    else:
        await message.answer("ℹ️ Bunday kanal topilmadi.", reply_markup=admin_menu)


# --- Admin boshqaruvi (faqat owner) ---
@dp.message(F.text == "🛡 Admin boshqaruvi")
async def admin_manage_menu(message: Message):
    if message.from_user.id != OWNER_ID:
        return await message.answer("🚫 Bu bo‘lim faqat egasi uchun.")
    admins = await list_admins()
    text = "🛡 <b>Adminlar ro‘yxati:</b>\n" + ("\n".join(f"• @{u or 'unknown'} — <code>{uid}</code>" for uid, u in admins) if admins else "— Hech kim yo‘q.")
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👤 Admin qo‘shish"), KeyboardButton(text="🗑 Adminni o‘chirish")],
            [KeyboardButton(text="⬅️ Chiqish")]
        ], resize_keyboard=True
    )
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@dp.message(F.text == "👤 Admin qo‘shish")
async def admin_add_prompt(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return
    await state.set_state(AdminManage.ADD)
    await message.answer("Admin qilish uchun foydalanuvchi ID yuboring:", reply_markup=back_kb)


@dp.message(StateFilter(AdminManage.ADD))
async def admin_add_exec(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return
    if (message.text or "").strip() == "⬅️ Orqaga":
        await state.clear()
        return await message.answer("❎ Bekor qilindi.", reply_markup=admin_menu)
    if not (message.text or "").isdigit():
        return await message.answer("ID faqat raqamlardan iborat bo‘lishi kerak.")
    uid = int(message.text)
    ok = await add_admin(uid, None)
    await state.clear()
    await message.answer("✅ Admin qo‘shildi." if ok else "ℹ️ Bu foydalanuvchi allaqachon admin.", reply_markup=admin_menu)


@dp.message(F.text == "🗑 Adminni o‘chirish")
async def admin_remove_prompt(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return
    await state.set_state(AdminManage.REMOVE)
    await message.answer("O‘chirish uchun admin ID yuboring:", reply_markup=back_kb)


@dp.message(StateFilter(AdminManage.REMOVE))
async def admin_remove_exec(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return
    if (message.text or "").strip() == "⬅️ Orqaga":
        await state.clear()
        return await message.answer("❎ Bekor qilindi.", reply_markup=admin_menu)
    if not (message.text or "").isdigit():
        return await message.answer("ID faqat raqamlardan iborat bo‘lishi kerak.")
    uid = int(message.text)
    ok = await remove_admin(uid)
    await state.clear()
    await message.answer("✅ Admin o‘chirildi." if ok else "ℹ️ Bunday admin topilmadi.", reply_markup=admin_menu)


# --- Qo'lda almaz berish ---
@dp.message(F.text == "💎 Qo‘lda almaz berish")
async def give_almaz_prompt(message: Message, state: FSMContext):
    if not await is_owner_or_admin(message.from_user.id):
        return
    await state.set_state(GiveAlmaz.WAIT)
    await message.answer(
        "Bu bo‘limda foydalanuvchilarga qo‘lda Almaz berishingiz mumkin.\n\n"
        "ID va Almaz miqdorini quyidagi ko‘rinishda yuboring:\n"
        "<code>123456789 10</code>  (ID + Almaz miqdori)",
        parse_mode="HTML",
        reply_markup=back_kb
    )


@dp.message(StateFilter(GiveAlmaz.WAIT))
async def give_almaz_exec(message: Message, state: FSMContext):
    if not await is_owner_or_admin(message.from_user.id):
        return
    text = (message.text or "").strip()
    if text == "⬅️ Orqaga":
        await state.clear()
        return await message.answer("❎ Amaliyot bekor qilindi.", reply_markup=admin_menu)

    parts = text.split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return await message.answer(
            "⚠️ Iltimos, formatga rioya qiling:\n"
            "<code>ID MIQDOR</code>\nMasalan: <code>123456789 10</code>",
            parse_mode="HTML"
        )

    user_id = int(parts[0])
    amount = int(parts[1])

    from database import DB_NAME
    import aiosqlite
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT username, almaz FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()

    if not row:
        return await message.answer("❌ Bunday foydalanuvchi bazada topilmadi.")

    await add_almaz(user_id, amount)
    await state.clear()

    try:
        await bot.send_message(
            user_id,
            f"🎁 Sizga bot administratori tomonidan <b>{amount} Almaz</b> taqdim qilindi! Tabriklaymiz 🎉",
            parse_mode="HTML"
        )
    except Exception:
        pass

    await message.answer(
        f"✅ <code>{user_id}</code> foydalanuvchi hisobiga {amount} Almaz qo‘shildi.",
        parse_mode="HTML",
        reply_markup=admin_menu
    )

@dp.message(F.text == "🔧 Referal almaz qiymati")
async def change_ref_reward(message: Message, state: FSMContext):
    if not await is_owner_or_admin(message.from_user.id):
        return
    current = await get_referral_reward()
    await state.set_state("WAITING_REF_REWARD")
    await message.answer(
        f"🔧 Hozirgi referal mukofoti: <b>{current} Almaz</b>\n\n"
        "Yangi qiymatni kiriting (faqat raqam):",
        parse_mode="HTML",
        reply_markup=back_kb
    )

@dp.message(StateFilter("WAITING_REF_REWARD"))
async def save_new_ref_reward(message: Message, state: FSMContext):
    if not await is_owner_or_admin(message.from_user.id):
        return

    if message.text.strip() == "⬅️ Orqaga":
        await state.clear()
        return await message.answer("❎ Bekor qilindi.", reply_markup=admin_menu)

    if not message.text.isdigit():
        return await message.answer("❗ Iltimos, faqat raqam kiriting.")

    value = message.text.strip()
    await set_setting("referral_reward", value)

    await state.clear()
    await message.answer(
        f"✅ Referal mukofoti yangilandi: <b>{value} Almaz</b>",
        parse_mode="HTML",
        reply_markup=admin_menu
    )


# --- Statistika bo'limi ---
@dp.message(F.text == "📈 Statistika")
async def show_stats(message: Message):
    if not await is_owner_or_admin(message.from_user.id):
        return
    top = await get_top_referrers_today(limit=10)
    total, pending, approved, edited, rejected = await get_withdraw_stats()

    text = "📈 <b>Statistika</b>\n\n"

    text += "🏆 Bugungi TOP-10 taklif qiluvchilar (tasdiqlangan referallar):\n"
    if not top:
        text += "— Hozircha ma’lumot yo‘q.\n"
    else:
        for i, (uid, username, cnt) in enumerate(top, 1):
            uname = f"@{username}" if username else f"ID:{uid}"
            text += f"{i}. {uname} — {cnt} ta tasdiqlangan referal\n"

    text += "\n💳 <b>Almaz yechish so‘rovlari</b>:\n"
    text += f"• Umumiy so‘rovlar: <b>{total}</b>\n"
    text += f"• Tasdiqlangan: <b>{approved}</b>\n"
    text += f"• Tahrirlangan: <b>{edited}</b>\n"
    text += f"• Rad etilgan: <b>{rejected}</b>\n"
    text += f"• Hozirda kutilayotgan: <b>{pending}</b>\n"

    await message.answer(text, parse_mode="HTML")

    if top:
        buttons = []
        for uid, username, cnt in top:
            label = f"{'@'+username if username else str(uid)} — {cnt} ta"
            buttons.append([InlineKeyboardButton(text=label, callback_data=f"topuser:{uid}")])
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        await message.answer(
            "🔍 TOP-10 ichidan foydalanuvchi profilini ko‘rish uchun tanlang:",
            reply_markup=kb
        )


@dp.callback_query(F.data.startswith("topuser:"))
async def top_user_profile(cb: CallbackQuery):
    if not await is_owner_or_admin(cb.from_user.id):
        await cb.answer("Siz admin emassiz.", show_alert=True)
        return
    try:
        uid = int(cb.data.split(":")[1])
    except ValueError:
        await cb.answer("Noto‘g‘ri ID.", show_alert=True)
        return

    user = await get_user(uid)
    if not user:
        await cb.answer("Foydalanuvchi topilmadi.", show_alert=True)
        return

    almaz = user[3] if len(user) > 3 and user[3] is not None else 0
    phone = user[5] if len(user) > 5 else None
    total_refs_verified = await count_verified_referrals(uid)
    remain = await get_suspension_remaining(uid)

    # Admin uchun ham ligani hisoblab ko'rsatamiz
    rank_data = await update_user_rank(uid, with_notification=False)

    txt = (
        "🔎 <b>Foydalanuvchi profili</b>\n\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"👤 Username: @{user[1] or 'Anonim'}\n"
        f"📞 Telefon: {phone or '—'}\n"
        f"💎 Almaz: {almaz}\n"
        f"🤝 Tasdiqlangan takliflar: {total_refs_verified}\n"
        f"🏅 Liga: {rank_data['emoji']} {rank_data['level']} (ball: {rank_data['score']})\n"
        f"⏳ Tanaffus (qolgan): {remain} s\n"
    )
    await cb.message.answer(txt, parse_mode="HTML")
    await cb.answer()

@dp.callback_query(F.data == "rank_how_to")
async def rank_how_to_handler(cb: CallbackQuery):
    text = (
        "🏅 <b>Daraja qanday oshadi?</b>\n\n"
        "Botdagi darajangiz (Liga) sizning faolligingizga qarab avtomatik oshadi.\n\n"
        "Liga quyidagi 2 mezon bo‘yicha hisoblanadi:\n\n"
        "1️⃣ <b>Umumiy takliflar</b> — har bir foydalanuvchi uchun <b>+2 ball</b>\n"
        "2️⃣ <b>Tasdiqlangan takliflar</b> — har biri uchun <b>+8 ball</b>\n\n"
        "🧮 <b>Reyting formulasi:</b>\n"
        "<b>Reyting = (Umumiy × 2) + (Tasdiqlangan × 8)</b>\n\n"
        "🏆 <b>Liga darajalari:</b>\n"
        "🥉 Bronze — 0 ball\n"
        "🥈 Silver — 50 ball\n"
        "🥇 Gold — 150 ball\n"
        "💎 Diamond — 350 ball\n"
        "🔱 Master — 750 ball\n"
        "👑🔥 GrandMaster — 1500+ ball\n\n"
        "🚀 <b>Darajani tez oshirish usullari:</b>\n"
        "• Ko‘proq do‘st taklif qiling\n"
        "• Ularning to‘liq ro‘yxatdan o‘tishiga yordam bering\n"
        "• Telefon tasdiqlashi muhim — eng ko‘p ball shundan beriladi\n"
        "• Har kuni faol bo‘ling\n"
        "• Yangiliklarni kuzatib boring — bonuslar qo‘shiladi!\n\n"
        "🎁 <b>Yaqinda qo‘shiladi:</b>\n"
        "• Har liga uchun bonuslar\n"
        "• Haftalik va oylik TOP sovrinlar\n"
        "• Ligaga qarab alohida imtiyozlar\n"
    )

    await cb.message.answer(text, parse_mode="HTML")
    await cb.answer()

# --- Tanaffus berish ---
@dp.message(F.text == "⏳ Tanaffus berish")
async def suspend_prompt(message: Message, state: FSMContext):
    if not await is_owner_or_admin(message.from_user.id):
        return
    await state.set_state(SuspensionInput.WAIT)
    await message.answer(
        "🕒 Tanaffus berish: <code>user_id soat</code> ko‘rinishida yuboring. Masalan:\n"
        "<code>123456789 2</code>  (2 soatga tanaffus)",
        parse_mode="HTML", reply_markup=back_kb
    )


@dp.message(
    StateFilter(SuspensionInput.WAIT),
    lambda m: (m.text or "").strip().count(" ") == 1 and all(p.isdigit() for p in (m.text or "").split())
)
async def simple_two_ints_handler(message: Message, state: FSMContext):
    if not await is_owner_or_admin(message.from_user.id):
        return
    uid_str, hour_str = (message.text or "").split()
    uid, hours = int(uid_str), int(hour_str)
    seconds = hours * 3600
    await set_suspension(uid, seconds)
    remain = await get_suspension_remaining(uid)
    try:
        await bot.send_message(
            uid,
            "😴 Profilingiz vaqtincha tanaffusda.\n"
            f"⏰ Tanaffus {hours} soatga belgilandi. Taxminan {remain} soniyadan so‘ng bot qayta faollashadi.\n"
            "Ushbu muddatda faqat <b>🧠 AI bilan suhbat</b> bo‘limi ochiq.",
            parse_mode="HTML"
        )
    except Exception:
        pass
    await state.clear()
    await message.answer(
        f"✅ <code>{uid}</code> foydalanuvchi {hours} soatga tanaffusga chiqarildi.",
        parse_mode="HTML",
        reply_markup=admin_menu
    )


# --- 🔎 Foydalanuvchini topish ---
@dp.message(F.text == "🔎 Foydalanuvchini topish")
async def search_user_prompt(message: Message, state: FSMContext):
    if not await is_owner_or_admin(message.from_user.id):
        return
    await state.set_state(SearchUser.WAIT)
    await message.answer("ID yoki @username yuboring:", reply_markup=back_kb)


@dp.message(F.text == "⬅️ Orqaga", StateFilter(SearchUser.WAIT))
async def search_user_cancel(message: Message, state: FSMContext):
    if not await is_owner_or_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer("❎ Qidiruv bekor qilindi.", reply_markup=admin_menu)


@dp.message(StateFilter(SearchUser.WAIT))
async def search_user_exec(message: Message, state: FSMContext):
    if not await is_owner_or_admin(message.from_user.id):
        return
    from database import DB_NAME
    import aiosqlite
    q = (message.text or "").strip()

    row = None
    async with aiosqlite.connect(DB_NAME) as db:
        if q.isdigit():
            cur = await db.execute(
                "SELECT user_id, username, almaz, ref_by, verified, phone, created_at FROM users WHERE user_id=?",
                (int(q),)
            )
            row = await cur.fetchone()
        elif q.startswith("@"):
            cur = await db.execute(
                "SELECT user_id, username, almaz, ref_by, verified, phone, created_at FROM users WHERE username=?",
                (q.lstrip("@"),)
            )
            row = await cur.fetchone()

        if not row:
            await state.clear()
            return await message.answer("❌ Foydalanuvchi topilmadi.", reply_markup=admin_menu)

        user_id, username, almaz, ref_by, verified, phone, created_at = row
        # referal soni
        cur = await db.execute("SELECT COUNT(*) FROM users WHERE ref_by=?", (user_id,))
        ref_cnt = (await cur.fetchone())[0]
    remain = await get_suspension_remaining(user_id)
    first_seen = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(created_at or 0)) if created_at else "—"

    # Rankni ham yangilab, admin uchun ko'rsatamiz
    rank_data = await update_user_rank(user_id, with_notification=False)

    txt = (
        "🔎 <b>Foydalanuvchi ma’lumoti</b>\n\n"
        f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
        f"👤 <b>Username:</b> @{username or 'Anonim'}\n"
        f"📞 <b>Telefon:</b> {phone or '—'}\n"
        f"✅ <b>Verified:</b> {'Ha' if (verified or 0) else 'Yo‘q'}\n"
        f"💎 <b>Almaz:</b> {almaz or 0}\n"
        f"🤝 <b>Referal soni (ref_by bilan):</b> {ref_cnt}\n"
        f"🏅 <b>Liga:</b> {rank_data['emoji']} {rank_data['level']} (ball: {rank_data['score']})\n"
        f"🕒 <b>Ro‘yxatdan o‘tgan:</b> {first_seen}\n"
        f"⏳ <b>Tanaffus (qolgan):</b> {remain} s\n"
    )
    await state.clear()
    await message.answer(txt, parse_mode="HTML", reply_markup=admin_menu)


# --- Admin paneldan chiqish / umumiy back ---
@dp.message(F.text == "⬅️ Chiqish")
async def admin_exit_to_main(message: Message, state: FSMContext):
    # Admin paneldan to'liq chiqib, asosiy menyuga qaytish
    if message.from_user.id == OWNER_ID or await is_admin(message.from_user.id):
        await state.clear()
        return await message.answer("🏠 Asosiy menyuga qaytdingiz.", reply_markup=main_menu)
    # oddiy foydalanuvchi uchun ham xuddi shu
    await state.clear()
    await message.answer("🏠 Asosiy menyuga qaytdingiz.", reply_markup=main_menu)

# --- Guruhga qo'shilganda log ---
@dp.my_chat_member(F.new_chat_member.status == "member")
async def bot_added_to_group(event):
    chat = event.chat
    try:
        await add_group(chat.id, chat.title or "Noma’lum")
    except Exception as e:
        log.warning("add_group failed: %s", e)


# --- Bootstrap ---
async def main():
    await init_db()
    await setup_bot_commands()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
