import aiosqlite
import time
from typing import Optional, List, Tuple

DB_NAME = "bot_data.db"  # sendagi haqiqiy baza nomi shu ekan

CREATE_SQL = [
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id     INTEGER PRIMARY KEY,
        username    TEXT,
        almaz       INTEGER DEFAULT 0,
        ref_by      INTEGER,
        verified    INTEGER DEFAULT 0,
        phone       TEXT,
        created_at  INTEGER,
        rank_score  INTEGER DEFAULT 0,
        rank_level  TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS admins (
        user_id  INTEGER PRIMARY KEY,
        username TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS groups (
        chat_id INTEGER PRIMARY KEY,
        title   TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS dynamic_texts (
        key     TEXT PRIMARY KEY,
        content TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS required_channels (
        username TEXT PRIMARY KEY
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS suspensions (
        user_id  INTEGER PRIMARY KEY,
        until_ts INTEGER
    );
    """,
    # --- Referral jadvali ---
    """
    CREATE TABLE IF NOT EXISTS referrals (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        inviter_id  INTEGER NOT NULL,
        invited_id  INTEGER NOT NULL UNIQUE,
        status      TEXT DEFAULT 'joined',   -- joined / verified / rejected
        created_at  INTEGER,
        verified_at INTEGER
    );
    """,
    # --- Almaz yechish so'rovlari ---
    """
    CREATE TABLE IF NOT EXISTS withdraw_requests (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id      INTEGER NOT NULL,
        amount       INTEGER NOT NULL,
        ff_id        TEXT,
        status       TEXT DEFAULT 'pending', -- pending / approved / edited / rejected
        created_at   INTEGER,
        processed_at INTEGER,
        processed_by INTEGER,
        note         TEXT
    );
    """,
    # --- Adminlarga yuborilgan so'rov xabarlari (hammasini sync tahrirlash uchun) ---
   """
    CREATE TABLE IF NOT EXISTS withdraw_notifications (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id INTEGER NOT NULL,
        chat_id    INTEGER NOT NULL,
        message_id INTEGER NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT
    );
    """
]


async def _ensure_rank_columns(db: aiosqlite.Connection):
    """
    Eski bazalarda rank_score va rank_level ustunlari yo'q bo'lsa,
    ALTER TABLE orqali qo'shib qo'yamiz.
    """
    cur = await db.execute("PRAGMA table_info(users)")
    cols = [r[1] for r in await cur.fetchall()]
    changed = False

    if "rank_score" not in cols:
        await db.execute("ALTER TABLE users ADD COLUMN rank_score INTEGER DEFAULT 0")
        changed = True
    if "rank_level" not in cols:
        await db.execute("ALTER TABLE users ADD COLUMN rank_level TEXT")
        changed = True

    if changed:
        await db.commit()


async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        for sql in CREATE_SQL:
            await db.execute(sql)
        await db.commit()
        # Eski bazani ham rank ustunlari bilan yangilab qo'yamiz
        await _ensure_rank_columns(db)


# ---------------- Users ----------------
async def add_user(user_id: int, username: Optional[str] = None, ref_by: Optional[int] = None):
    now = int(time.time())
    async with aiosqlite.connect(DB_NAME) as db:

        # 1) User mavjud bo'lmasa — yaratamiz
        await db.execute("""
            INSERT INTO users(user_id, username, created_at)
            VALUES(?, ?, ?)
            ON CONFLICT(user_id) DO NOTHING
        """, (user_id, username, now))

        # 2) Username har doim yangilanishi mumkin
        if username:
            await db.execute("UPDATE users SET username=? WHERE user_id=?", (username, user_id))

        # 3) Referral yozish — faqat agar bo'sh bo'lsa va self-referral bo'lmasa
        if ref_by and ref_by != user_id:
            cur = await db.execute("SELECT ref_by FROM users WHERE user_id=?", (user_id,))
            row = await cur.fetchone()

            if row and row[0] is None:
                await db.execute("UPDATE users SET ref_by=? WHERE user_id=?", (ref_by, user_id))

        await db.commit()



async def get_user(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT user_id, username, ref_by, almaz, verified, phone, rank_score, rank_level FROM users WHERE user_id=?",
            (user_id,)
        )
        return await cur.fetchone()


async def add_almaz(user_id: int, amount: int):
    """
    amount musbat bo'lsa qo'shadi, manfiy bo'lsa ayradi (balans <=0 bo'lib qolsa ham xato emas).
    """
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE users SET almaz = COALESCE(almaz,0) + ? WHERE user_id=?",
            (amount, user_id)
        )
        await db.commit()


async def get_leaderboard(limit: int = 15) -> List[Tuple[str, int]]:
    """
    Top foydalanuvchilarni Almaz bo'yicha qaytaradi (GLOBAL reyting bo'limi uchun).
    """
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("""
            SELECT username, almaz FROM users
            ORDER BY almaz DESC, user_id ASC
            LIMIT ?
        """, (limit,))
        rows = await cur.fetchall()
        return [(r[0], r[1]) for r in rows]


async def get_ref_by(user_id: int) -> Optional[int]:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT ref_by FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        return row[0] if row and row[0] is not None else None


async def set_ref_by_if_empty(user_id: int, ref_by: Optional[int]):
    """
    ref_by faqat birinchi marta yoziladi.
    self-referral, 0, None, yoki mavjud referrer bo'lsa — o'zgarmaydi.
    """
    if not ref_by or ref_by == user_id:
        return

    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT ref_by FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()

        # faqat bo'sh bo'lsa yoziladi
        if row and row[0] is None:
            await db.execute("UPDATE users SET ref_by=? WHERE user_id=?", (ref_by, user_id))
            await db.commit()



# ✅ Foydalanuvchini verified deb belgilaydi
async def set_verified(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET verified = 1 WHERE user_id=?", (user_id,))
        await db.commit()


# ✅ Telefon raqamini saqlaydi
async def set_phone_verified(user_id: int, phone: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE users SET phone = ?, verified = 1 WHERE user_id=?",
            (phone, user_id)
        )
        await db.commit()


# ✅ Foydalanuvchi verified yoki yo‘qligini tekshiradi
async def is_verified(user_id: int) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT verified FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        if not row:
            return False
        value = row[0]
        try:
            return bool(int(value))
        except Exception:
            return False


# ---------------- Admins ----------------
async def list_admins() -> List[Tuple[int, Optional[str]]]:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT user_id, username FROM admins ORDER BY user_id ASC")
        rows = await cur.fetchall()
        return [(r[0], r[1]) for r in rows]


async def add_admin(user_id: int, username: Optional[str]):
    async with aiosqlite.connect(DB_NAME) as db:
        try:
            await db.execute("INSERT INTO admins(user_id, username) VALUES(?, ?)", (user_id, username))
            await db.commit()
            return True
        except Exception:
            return False


async def remove_admin(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
        await db.commit()
        return cur.rowcount > 0


async def is_admin(user_id: int) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,))
        return await cur.fetchone() is not None


# ---------------- Groups ----------------
async def list_groups() -> List[Tuple[int, str]]:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT chat_id, title FROM groups ORDER BY chat_id ASC")
        rows = await cur.fetchall()
        return [(r[0], r[1]) for r in rows]


async def add_group(chat_id: int, title: Optional[str]):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            INSERT INTO groups(chat_id, title)
            VALUES(?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title
        """, (chat_id, title))
        await db.commit()


# ---------------- Dynamic texts ----------------
async def get_dynamic_text(key: str) -> str:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT content FROM dynamic_texts WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else ""


async def update_dynamic_text(key: str, content: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            INSERT INTO dynamic_texts(key, content)
            VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET content=excluded.content
        """, (key, content))
        await db.commit()


# ---------------- Required channels ----------------
async def list_required_channels() -> list[str]:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT username FROM required_channels ORDER BY username ASC")
        rows = await cur.fetchall()
        return [r[0] for r in rows]


async def add_required_channel(username: str) -> bool:
    username = username.strip()
    if not username.startswith("@"):
        username = "@" + username
    async with aiosqlite.connect(DB_NAME) as db:
        try:
            await db.execute("INSERT INTO required_channels(username) VALUES(?)", (username,))
            await db.commit()
            return True
        except Exception:
            return False


async def remove_required_channel(username: str) -> bool:
    username = username.strip()
    if not username.startswith("@"):
        username = "@" + username
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("DELETE FROM required_channels WHERE username=?", (username,))
        await db.commit()
        return cur.rowcount > 0


async def required_channels_count() -> int:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT COUNT(*) FROM required_channels")
        row = await cur.fetchone()
        return int(row[0]) if row else 0


# ---------------- Suspensions ----------------
async def set_suspension(user_id: int, seconds: int):
    until_ts = int(time.time()) + max(0, int(seconds))
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            INSERT INTO suspensions(user_id, until_ts)
            VALUES(?, ?)
            ON CONFLICT(user_id) DO UPDATE SET until_ts=excluded.until_ts
        """, (user_id, until_ts))
        await db.commit()


async def get_suspension_remaining(user_id: int) -> int:
    now = int(time.time())
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT until_ts FROM suspensions WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        if not row:
            return 0
        remain = row[0] - now
        if remain <= 0:
            await db.execute("DELETE FROM suspensions WHERE user_id=?", (user_id,))
            await db.commit()
            return 0
        return remain


# ---------------- Referrals (yangi) ----------------
async def create_referral(inviter_id: int, invited_id: int):
    """
    Yangi referral yozuvi. invited_id bo'yicha UNIQUE, shuning uchun faqat birinchi taklifchi hisobga olinadi.
    """
    now = int(time.time())
    async with aiosqlite.connect(DB_NAME) as db:
        try:
            await db.execute(
                "INSERT INTO referrals(inviter_id, invited_id, status, created_at) VALUES(?, ?, 'joined', ?)",
                (inviter_id, invited_id, now)
            )
            await db.commit()
        except Exception:
            # allaqachon yozilgan bo'lishi mumkin — jim o'tamiz
            pass


async def mark_referral_verified(invited_id: int):
    now = int(time.time())
    async with aiosqlite.connect(DB_NAME) as db:

        # Agar allaqachon verified bo'lsa — qaytamiz (double reward yo‘q!)
        cur = await db.execute("SELECT status FROM referrals WHERE invited_id=?", (invited_id,))
        row = await cur.fetchone()
        if row and row[0] == "verified":
            return

        # Verified ga o‘tkazamiz
        await db.execute(
            "UPDATE referrals SET status='verified', verified_at=? WHERE invited_id=?",
            (now, invited_id)
        )
        await db.commit()



async def count_verified_referrals(inviter_id: int) -> int:
    """
    Taklifchi uchun tasdiqlangan (verified) referallar soni.
    """
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM referrals WHERE inviter_id=? AND status='verified'",
            (inviter_id,)
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0


async def count_all_referrals(inviter_id: int) -> int:
    """
    Taklifchi tomonidan taklif qilinganlarning umumiy soni (joined + verified + rejected).
    """
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM referrals WHERE inviter_id=?",
            (inviter_id,)
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0


async def get_top_referrers_today(limit: int = 10) -> List[Tuple[int, Optional[str], int]]:
    """
    Bugungi (server vaqti bo'yicha) tasdiqlangan referallar soniga ko'ra TOP taklifchilar.
    """
    now = time.time()
    lt = time.localtime(now)
    # kun boshini hisoblaymiz
    start_of_day = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, lt.tm_wday, lt.tm_yday, lt.tm_isdst)))

    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            """
            SELECT r.inviter_id, u.username, COUNT(*) AS cnt
            FROM referrals r
            JOIN users u ON u.user_id = r.inviter_id
            WHERE r.status='verified' AND r.verified_at >= ?
            GROUP BY r.inviter_id, u.username
            ORDER BY cnt DESC, r.inviter_id ASC
            LIMIT ?
            """,
            (start_of_day, limit)
        )
        rows = await cur.fetchall()
        return [(r[0], r[1], r[2]) for r in rows]


# ---------------- Rank (Liga) maydonlari ----------------
async def get_rank_fields(user_id: int) -> Tuple[int, Optional[str]]:
    """
    Foydalanuvchi uchun rank_score va rank_level qiymatlari (agar yo'q bo'lsa 0, None qaytadi).
    """
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT rank_score, rank_level FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        if not row:
            return 0, None
        score = int(row[0]) if row[0] is not None else 0
        level = row[1]
        return score, level


async def update_rank_fields(user_id: int, score: int, level: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE users SET rank_score=?, rank_level=? WHERE user_id=?",
            (score, level, user_id)
        )
        await db.commit()


# ---------------- Withdraw (almaz yechish) ----------------
async def create_withdraw_request(user_id: int, amount: int, ff_id: str) -> int:
    now = int(time.time())
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            """
            INSERT INTO withdraw_requests(user_id, amount, ff_id, status, created_at)
            VALUES(?, ?, ?, 'pending', ?)
            """,
            (user_id, amount, ff_id, now)
        )
        await db.commit()
        return cur.lastrowid


async def get_withdraw_request(request_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            """
            SELECT id, user_id, amount, ff_id, status, created_at, processed_at, processed_by, note
            FROM withdraw_requests
            WHERE id=?
            """,
            (request_id,)
        )
        return await cur.fetchone()


async def update_withdraw_status(request_id: int, status: str, processed_by: Optional[int], note: Optional[str]):
    now = int(time.time())
    async with aiosqlite.connect(DB_NAME) as db:
        if note is not None:
            await db.execute(
                """
                UPDATE withdraw_requests
                SET status=?, processed_at=?, processed_by=?, note=?
                WHERE id=?
                """,
                (status, now, processed_by, note, request_id)
            )
        else:
            await db.execute(
                """
                UPDATE withdraw_requests
                SET status=?, processed_at=?, processed_by=?
                WHERE id=?
                """,
                (status, now, processed_by, request_id)
            )
        await db.commit()


async def get_withdraw_stats() -> Tuple[int, int, int, int, int]:
    """
    return: total, pending, approved, edited, rejected
    """
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT status, COUNT(*) FROM withdraw_requests GROUP BY status"
        )
        rows = await cur.fetchall()
    counts = {"pending": 0, "approved": 0, "edited": 0, "rejected": 0}
    total = 0
    for status, cnt in rows:
        total += cnt
        if status in counts:
            counts[status] = cnt
    return total, counts["pending"], counts["approved"], counts["edited"], counts["rejected"]


# --- Admin xabarlari uchun notification jadvallari ---
async def add_withdraw_notification(request_id: int, chat_id: int, message_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO withdraw_notifications(request_id, chat_id, message_id) VALUES(?, ?, ?)",
            (request_id, chat_id, message_id)
        )
        await db.commit()


async def get_withdraw_notifications(request_id: int) -> List[Tuple[int, int]]:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT chat_id, message_id FROM withdraw_notifications WHERE request_id=?",
            (request_id,)
        )
        rows = await cur.fetchall()
        return [(r[0], r[1]) for r in rows]

# ---------------- Settings (referal mukofoti) ----------------

async def get_setting(key: str) -> str | None:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else None


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            INSERT INTO settings(key, value)
            VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (key, value))
        await db.commit()
