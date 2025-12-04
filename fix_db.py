import aiosqlite
import asyncio

async def fix_existing_db():
    async with aiosqlite.connect("bot_data.db") as db:
        # --- Users jadvalini to‘g‘rilaymiz ---
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            almaz       INTEGER DEFAULT 0,
            ref_by      INTEGER,
            verified    INTEGER DEFAULT 0,
            phone       TEXT,
            created_at  INTEGER
        );
        """)

        # --- Required channels jadvalini to‘g‘rilaymiz ---
        await db.execute("DROP TABLE IF EXISTS required_channels;")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS required_channels (
            username TEXT PRIMARY KEY
        );
        """)

        # --- Admins jadvali ---
        await db.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id  INTEGER PRIMARY KEY,
            username TEXT
        );
        """)

        # --- Groups jadvali ---
        await db.execute("""
        CREATE TABLE IF NOT EXISTS groups (
            chat_id INTEGER PRIMARY KEY,
            title   TEXT
        );
        """)

        # --- Dynamic text jadvali ---
        await db.execute("""
        CREATE TABLE IF NOT EXISTS dynamic_texts (
            key     TEXT PRIMARY KEY,
            content TEXT
        );
        """)

        # --- Suspensions jadvali ---
        await db.execute("""
        CREATE TABLE IF NOT EXISTS suspensions (
            user_id  INTEGER PRIMARY KEY,
            until_ts INTEGER
        );
        """)

        await db.commit()
        print("✅ Barcha jadvallar tuzatildi yoki yaratildi.")

asyncio.run(fix_existing_db())
