"""A small stand-in for ~/Library/Messages/chat.db with the tables bench-reader reads.

Column names and conventions follow the real database: dates are nanoseconds
since 2001-01-01, 0 means unset, and 1:1 chats use the other person's handle
as chat_identifier while group chats use "chat…".
"""

import sqlite3
import time
import uuid
from pathlib import Path

APPLE_EPOCH = 978_307_200

SCHEMA = """
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL, service TEXT NOT NULL);
CREATE TABLE chat (
    ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE NOT NULL, style INTEGER,
    chat_identifier TEXT, service_name TEXT);
CREATE TABLE message (
    ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE NOT NULL, text TEXT,
    handle_id INTEGER DEFAULT 0, service TEXT, date INTEGER, date_read INTEGER DEFAULT 0,
    date_delivered INTEGER DEFAULT 0, is_from_me INTEGER DEFAULT 0,
    cache_has_attachments INTEGER DEFAULT 0, attributedBody BLOB,
    associated_message_guid TEXT, associated_message_type INTEGER DEFAULT 0,
    balloon_bundle_id TEXT, date_edited INTEGER DEFAULT 0, date_retracted INTEGER DEFAULT 0);
CREATE TABLE chat_message_join (
    chat_id INTEGER, message_id INTEGER, message_date INTEGER DEFAULT 0, PRIMARY KEY (chat_id, message_id));
CREATE TABLE attachment (
    ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE NOT NULL, filename TEXT,
    mime_type TEXT, transfer_name TEXT, total_bytes INTEGER DEFAULT 0);
CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
"""


def apple_ns(unix: float | None = None) -> int:
    return int(((time.time() if unix is None else unix) - APPLE_EPOCH) * 1e9)


class FakeChatDB:
    def __init__(self, path: Path):
        self.path = path
        # Kept open in WAL mode while bench-reader reads, like Messages does.
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def chat(self, identifier: str, group: bool = False) -> int:
        cur = self.conn.execute(
            "INSERT INTO chat (guid, style, chat_identifier, service_name) VALUES (?, ?, ?, 'iMessage')",
            (f"any;{'+' if group else '-'};{identifier}", 43 if group else 45, identifier),
        )
        return cur.lastrowid

    def message(self, chat_id: int, text: str | None = None, *, body: bytes | None = None,
                from_me: bool = False, date: int | None = None, **columns) -> int:
        values = {
            "guid": str(uuid.uuid4()).upper(), "text": text, "attributedBody": body, "service": "iMessage",
            "date": apple_ns() if date is None else date, "is_from_me": int(from_me), **columns,
        }
        cur = self.conn.execute(
            f"INSERT INTO message ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
            list(values.values()),
        )
        self.conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)", (chat_id, cur.lastrowid))
        return cur.lastrowid

    def attachment(self, message_id: int, filename: str, transfer_name: str, mime_type: str = "image/jpeg") -> None:
        cur = self.conn.execute(
            "INSERT INTO attachment (guid, filename, mime_type, transfer_name, total_bytes) VALUES (?, ?, ?, ?, 3)",
            (str(uuid.uuid4()).upper(), filename, mime_type, transfer_name),
        )
        self.conn.execute(
            "INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (?, ?)", (message_id, cur.lastrowid)
        )
        self.conn.execute("UPDATE message SET cache_has_attachments = 1 WHERE ROWID = ?", (message_id,))

    def update(self, rowid: int, **columns) -> None:
        sets = ", ".join(f"{name} = ?" for name in columns)
        self.conn.execute(f"UPDATE message SET {sets} WHERE ROWID = ?", [*columns.values(), rowid])
