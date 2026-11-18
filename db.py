import time

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    phone TEXT,
    created_at INTEGER,
    banned_until INTEGER DEFAULT 0,   -- 0 = нет бана, -1 = навсегда, иначе unix-время окончания
    ban_reason TEXT
);
CREATE TABLE IF NOT EXISTS requests(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    service TEXT,
    description TEXT,
    budget TEXT,
    status TEXT DEFAULT 'new',         -- new / accepted / rejected
    reject_reason TEXT,
    created_at INTEGER,
    decided_at INTEGER
);
CREATE TABLE IF NOT EXISTS cards(      -- какое сообщение у админа к какому клиенту относится
    chat_id INTEGER,
    message_id INTEGER,
    user_id INTEGER,
    request_id INTEGER,
    PRIMARY KEY(chat_id, message_id)
);
"""


class DB:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def init(self):
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def _one(self, sql, params=()):
        async with self.conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def _all(self, sql, params=()):
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def _run(self, sql, params=()):
        cur = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cur

    # ---------- users ----------
    async def upsert_user(self, u):
        await self._run(
            """INSERT INTO users(user_id, username, first_name, created_at)
               VALUES(?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
                                                  first_name=excluded.first_name""",
            (u.id, u.username, u.first_name, int(time.time())),
        )

    async def get_user(self, user_id: int):
        return await self._one("SELECT * FROM users WHERE user_id=?", (user_id,))

    async def find_username(self, username: str):
        return await self._one(
            "SELECT * FROM users WHERE lower(username)=?", (username.lstrip("@").lower(),)
        )

    async def set_phone(self, user_id: int, phone: str):
        await self._run("UPDATE users SET phone=? WHERE user_id=?", (phone, user_id))

    async def ban(self, user_id: int, until: int, reason: str | None):
        await self._run(
            "UPDATE users SET banned_until=?, ban_reason=? WHERE user_id=?",
            (until, reason, user_id),
        )

    async def unban(self, user_id: int):
        await self._run(
            "UPDATE users SET banned_until=0, ban_reason=NULL WHERE user_id=?", (user_id,)
        )

    async def check_ban(self, user_id: int):
        """-> (banned, until, reason). Истёкший бан снимается автоматически."""
        u = await self.get_user(user_id)
        if not u or not u["banned_until"]:
            return False, 0, None
        until = u["banned_until"]
        if until == -1 or until > time.time():
            return True, until, u["ban_reason"]
        await self.unban(user_id)
        return False, 0, None

    async def list_users(self, mode: str = "all", query: str = ""):
        sql = "SELECT * FROM users"
        params: list = []
        if mode == "banned":
            sql += " WHERE banned_until=-1 OR banned_until>?"
            params.append(int(time.time()))
        sql += " ORDER BY created_at DESC"
        rows = await self._all(sql, params)
        q = query.strip().lstrip("@").lstrip("+").casefold()
        if q:
            rows = [
                r for r in rows
                if q in (r["username"] or "").casefold()
                or q in (r["first_name"] or "").casefold()
                or q in (r["phone"] or "").lstrip("+")
                or q == str(r["user_id"])
            ]
        return rows

    # ---------- requests ----------
    async def create_request(self, user_id, service, description, budget) -> int:
        cur = await self._run(
            """INSERT INTO requests(user_id, service, description, budget, created_at)
               VALUES(?,?,?,?,?)""",
            (user_id, service, description, budget, int(time.time())),
        )
        return cur.lastrowid

    async def get_request(self, req_id: int):
        return await self._one("SELECT * FROM requests WHERE id=?", (req_id,))

    async def decide(self, req_id: int, status: str, reason: str | None = None) -> bool:
        """Возвращает False, если заявка уже обработана."""
        cur = await self._run(
            """UPDATE requests SET status=?, reject_reason=?, decided_at=?
               WHERE id=? AND status='new'""",
            (status, reason, int(time.time()), req_id),
        )
        return cur.rowcount > 0

    async def user_requests(self, user_id: int, limit: int = 10):
        return await self._all(
            "SELECT * FROM requests WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit)
        )

    async def new_requests(self, limit: int = 10):
        return await self._all(
            "SELECT * FROM requests WHERE status='new' ORDER BY id LIMIT ?", (limit,)
        )

    async def count_open(self, user_id: int) -> int:
        r = await self._one(
            "SELECT COUNT(*) c FROM requests WHERE user_id=? AND status='new'", (user_id,)
        )
        return r["c"]

    async def last_accepted(self, user_id: int):
        return await self._one(
            "SELECT * FROM requests WHERE user_id=? AND status='accepted' ORDER BY id DESC LIMIT 1",
            (user_id,),
        )

    # ---------- cards ----------
    async def add_card(self, chat_id, message_id, user_id, request_id):
        await self._run(
            "INSERT OR REPLACE INTO cards VALUES(?,?,?,?)",
            (chat_id, message_id, user_id, request_id),
        )

    async def get_card(self, chat_id, message_id):
        return await self._one(
            "SELECT * FROM cards WHERE chat_id=? AND message_id=?", (chat_id, message_id)
        )

    # ---------- stats ----------
    async def stats(self):
        now = int(time.time())
        u = await self._one(
            """SELECT COUNT(*) total,
                      SUM(banned_until=-1 OR banned_until>?) banned FROM users""",
            (now,),
        )
        r = await self._one(
            """SELECT COUNT(*) total,
                      SUM(status='new') new,
                      SUM(status='accepted') accepted,
                      SUM(status='rejected') rejected FROM requests"""
        )
        return u, r
