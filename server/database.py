# -*- coding: utf-8 -*-
"""
server/database.py
===================
שכבת בסיס הנתונים (SQLite). אחראית על:
  - משתמשים (הרשמה, אימות, חיפוש עם דפדוף)
  - קבוצות וחברי-קבוצה
  - היסטוריית הודעות (קבוצתיות ופרטיות) - כדי שגם משתמש שהיה offline יוכל לראות מה פספס

הסיסמאות **לא** נשמרות כטקסט גלוי (Plaintext). נשמר Hash מבוסס PBKDF2-HMAC-SHA256
עם salt אקראי וייחודי לכל משתמש (ראו get_password_hash / verify_password).
"""

import sqlite3
import threading
import hashlib
import os
import time

DB_PATH = os.path.join(os.path.dirname(__file__), "cybercomm.db")

_PBKDF2_ITERATIONS = 200_000


def _hash_password(password: str, salt: bytes) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return dk.hex()


class Database:
    """
    עוטף את חיבור ה-SQLite. משתמשים ב-Lock גלובלי כדי לאפשר גישה בטוחה
    מכמה Thread-ים בו-זמנית (השרת הוא Multi-threaded - כל לקוח מטופל בתהליכון משלו).
    """

    def __init__(self, path: str = DB_PATH):
        # RLock (ולא Lock רגיל) כי חלק מהמתודות (למשל create_group) קוראות
        # למתודות אחרות שגם הן נועלות את אותו lock (למשל get_user_id) -
        # עם Lock רגיל (לא-רקורסיבי) זה היה גורם ל-Deadlock.
        self._lock = threading.RLock()
        # check_same_thread=False כי אנחנו מנהלים סנכרון בעצמנו עם ה-Lock
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self):
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    username      TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    salt          TEXT NOT NULL,
                    created_at    REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS groups (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    name     TEXT NOT NULL,
                    owner_id INTEGER NOT NULL,
                    FOREIGN KEY (owner_id) REFERENCES users(id)
                );

                CREATE TABLE IF NOT EXISTS group_members (
                    group_id INTEGER NOT NULL,
                    user_id  INTEGER NOT NULL,
                    PRIMARY KEY (group_id, user_id),
                    FOREIGN KEY (group_id) REFERENCES groups(id),
                    FOREIGN KEY (user_id)  REFERENCES users(id)
                );

                CREATE TABLE IF NOT EXISTS group_messages (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id  INTEGER NOT NULL,
                    sender_id INTEGER NOT NULL,
                    text      TEXT NOT NULL,
                    ts        REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS private_messages (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender_id   INTEGER NOT NULL,
                    receiver_id INTEGER NOT NULL,
                    text        TEXT NOT NULL,
                    ts          REAL NOT NULL
                );
                """
            )

    # ------------------------------------------------------------------
    # משתמשים
    # ------------------------------------------------------------------

    def create_user(self, username: str, password: str) -> bool:
        """מנסה ליצור משתמש חדש. מחזיר False אם השם כבר תפוס."""
        salt = os.urandom(16)
        pwd_hash = _hash_password(password, salt)
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT INTO users (username, password_hash, salt, created_at) VALUES (?, ?, ?, ?)",
                    (username, pwd_hash, salt.hex(), time.time()),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def verify_user(self, username: str, password: str):
        """מאמת שם משתמש/סיסמה. מחזיר את שורת המשתמש אם תקין, אחרת None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()
        if row is None:
            return None
        salt = bytes.fromhex(row["salt"])
        if _hash_password(password, salt) == row["password_hash"]:
            return row
        return None

    def get_user_id(self, username: str):
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM users WHERE username = ?", (username,)
            ).fetchone()
        return row["id"] if row else None

    def search_users(self, query: str, page: int, page_size: int = 50):
        """
        חיפוש דינמי + Paging: פותר את "אתגר ה-Scale" (מיליוני משתמשים).
        השרת לעולם לא מחזיר את כל טבלת המשתמשים בבת אחת - רק את העמוד המבוקש
        מתוך התוצאות שתואמות את מחרוזת החיפוש (LIKE).
        """
        offset = page * page_size
        like_query = f"%{query}%"
        with self._lock:
            rows = self._conn.execute(
                "SELECT username FROM users WHERE username LIKE ? "
                "ORDER BY username LIMIT ? OFFSET ?",
                (like_query, page_size + 1, offset),  # +1 כדי לדעת אם יש עוד עמוד
            ).fetchall()
        has_more = len(rows) > page_size
        usernames = [r["username"] for r in rows[:page_size]]
        return usernames, has_more

    # ------------------------------------------------------------------
    # קבוצות
    # ------------------------------------------------------------------

    def create_group(self, name: str, owner_username: str, member_usernames: list[str]) -> int:
        owner_id = self.get_user_id(owner_username)
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO groups (name, owner_id) VALUES (?, ?)", (name, owner_id)
            )
            group_id = cur.lastrowid
            self._conn.execute(
                "INSERT OR IGNORE INTO group_members (group_id, user_id) VALUES (?, ?)",
                (group_id, owner_id),
            )
            for uname in member_usernames:
                uid = self.get_user_id(uname)
                if uid:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO group_members (group_id, user_id) VALUES (?, ?)",
                        (group_id, uid),
                    )
        return group_id

    def add_group_member(self, group_id: int, username: str) -> bool:
        uid = self.get_user_id(username)
        if uid is None:
            return False
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO group_members (group_id, user_id) VALUES (?, ?)",
                (group_id, uid),
            )
        return True

    def get_user_groups(self, username: str):
        uid = self.get_user_id(username)
        if uid is None:
            return []
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT g.id AS id, g.name AS name FROM groups g
                JOIN group_members gm ON gm.group_id = g.id
                WHERE gm.user_id = ?
                ORDER BY g.name
                """,
                (uid,),
            ).fetchall()
        return [{"id": r["id"], "name": r["name"]} for r in rows]

    def get_group_members(self, group_id: int):
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT u.username AS username FROM users u
                JOIN group_members gm ON gm.user_id = u.id
                WHERE gm.group_id = ?
                """,
                (group_id,),
            ).fetchall()
        return [r["username"] for r in rows]

    # ------------------------------------------------------------------
    # הודעות (נשמרות גם לצורך היסטוריה למשתמשים שהתנתקו)
    # ------------------------------------------------------------------

    def save_group_message(self, group_id: int, sender_username: str, text: str) -> float:
        sender_id = self.get_user_id(sender_username)
        ts = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO group_messages (group_id, sender_id, text, ts) VALUES (?, ?, ?, ?)",
                (group_id, sender_id, text, ts),
            )
        return ts

    def get_group_history(self, group_id: int, limit: int = 100):
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT u.username AS username, gm_msg.text AS text, gm_msg.ts AS ts
                FROM group_messages gm_msg
                JOIN users u ON u.id = gm_msg.sender_id
                WHERE gm_msg.group_id = ?
                ORDER BY gm_msg.ts DESC LIMIT ?
                """,
                (group_id, limit),
            ).fetchall()
        return [{"from": r["username"], "text": r["text"], "ts": r["ts"]} for r in reversed(rows)]

    def save_private_message(self, sender_username: str, receiver_username: str, text: str) -> float:
        sender_id = self.get_user_id(sender_username)
        receiver_id = self.get_user_id(receiver_username)
        ts = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO private_messages (sender_id, receiver_id, text, ts) VALUES (?, ?, ?, ?)",
                (sender_id, receiver_id, text, ts),
            )
        return ts

    def get_private_history(self, user_a: str, user_b: str, limit: int = 100):
        uid_a = self.get_user_id(user_a)
        uid_b = self.get_user_id(user_b)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT ua.username AS sender, ub.username AS receiver, pm.text AS text, pm.ts AS ts
                FROM private_messages pm
                JOIN users ua ON ua.id = pm.sender_id
                JOIN users ub ON ub.id = pm.receiver_id
                WHERE (pm.sender_id = ? AND pm.receiver_id = ?)
                   OR (pm.sender_id = ? AND pm.receiver_id = ?)
                ORDER BY pm.ts DESC LIMIT ?
                """,
                (uid_a, uid_b, uid_b, uid_a, limit),
            ).fetchall()
        return [{"from": r["sender"], "to": r["receiver"], "text": r["text"], "ts": r["ts"]} for r in reversed(rows)]