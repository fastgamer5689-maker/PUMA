"""Async SQLite layer + full schema (messages, snapshots, blacklist, quarantine, tickets...)."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS guild_config (
    guild_id            INTEGER PRIMARY KEY,
    modlog_channel_id   INTEGER,
    joins_channel_id    INTEGER,
    dnt_channel_id      INTEGER,
    quarantine_role_id  INTEGER,   -- new human members (10-minute vetting)
    isolation_role_id   INTEGER,   -- new bots + "inside men": sees nothing
    mod_role_id         INTEGER,
    ticket_category_id  INTEGER
);

-- "Photographic memory": append-only. `content` is the ORIGINAL text and is never overwritten.
CREATE TABLE IF NOT EXISTS messages (
    message_id       INTEGER PRIMARY KEY,
    guild_id         INTEGER NOT NULL,
    channel_id       INTEGER NOT NULL,
    channel_name     TEXT,
    author_id        INTEGER NOT NULL,
    author_tag       TEXT,
    is_bot           INTEGER DEFAULT 0,
    content          TEXT,
    latest_content   TEXT,
    attachments      TEXT,            -- JSON list of URLs
    mention_ids      TEXT,            -- JSON list of mentioned (non-bot) user IDs at send time
    mention_everyone INTEGER DEFAULT 0,
    ghost_reported   INTEGER DEFAULT 0,
    created_at       REAL NOT NULL,
    edited_at        REAL,
    deleted_at       REAL
);
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages (guild_id, channel_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_author  ON messages (guild_id, author_id);

CREATE TABLE IF NOT EXISTS message_edits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  INTEGER NOT NULL,
    old_content TEXT,
    new_content TEXT,
    edited_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_edits_message ON message_edits (message_id);

CREATE TRIGGER IF NOT EXISTS messages_no_delete BEFORE DELETE ON messages
BEGIN SELECT RAISE(ABORT, 'message log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS edits_no_delete BEFORE DELETE ON message_edits
BEGIN SELECT RAISE(ABORT, 'message log is append-only'); END;

-- Server structure cache used for rollback.
CREATE TABLE IF NOT EXISTS channel_snapshots (
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    name        TEXT,
    type        INTEGER,            -- discord.ChannelType value
    parent_id   INTEGER,            -- category id
    position    INTEGER,
    topic       TEXT,
    nsfw        INTEGER DEFAULT 0,
    slowmode    INTEGER DEFAULT 0,
    bitrate     INTEGER,
    user_limit  INTEGER,
    overwrites  TEXT,               -- JSON [{id,type,allow,deny}]
    created_at  REAL,               -- when the channel was created on Discord
    snapshot_at REAL,
    created_by  INTEGER,            -- audit-log executor of the creation (if known)
    deleted_at  REAL,               -- soft delete: set when the channel disappears
    PRIMARY KEY (guild_id, channel_id)
);

-- Original @everyone send-permission state captured by the emergency freeze (for /unfreeze).
CREATE TABLE IF NOT EXISTS lockdown_state (
    guild_id     INTEGER NOT NULL,
    channel_id   INTEGER NOT NULL,
    prev_send    INTEGER,           -- NULL = inherit, 1 = allow, 0 = deny
    prev_threads INTEGER,
    PRIMARY KEY (guild_id, channel_id)
);

-- Inside-man permanent blacklist: these users may never hold a staff role again
-- unless the owner authorizes an override with the owner key.
CREATE TABLE IF NOT EXISTS staff_blacklist (
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    reason     TEXT,
    created_at REAL NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS incidents (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    kind       TEXT,
    details    TEXT,
    status     TEXT DEFAULT 'pending_owner_trial',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS infractions (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    kind     TEXT NOT NULL,
    detail   TEXT,
    ts       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_infractions_user ON infractions (guild_id, user_id, ts);

CREATE TABLE IF NOT EXISTS quarantine (
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    joined_at  REAL NOT NULL,
    release_at REAL,
    is_bot     INTEGER DEFAULT 0,
    released   INTEGER DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS tickets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL UNIQUE,
    user_id    INTEGER NOT NULL,
    issue      TEXT,
    status     TEXT DEFAULT 'open',
    escalated  INTEGER DEFAULT 0,
    reported   INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    closed_at  REAL
);

CREATE TABLE IF NOT EXISTS faq (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    question TEXT NOT NULL,
    answer   TEXT NOT NULL,
    keywords TEXT DEFAULT ''
);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    # -- primitives ---------------------------------------------------------
    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        async with self.conn.execute(sql, tuple(params)) as cur:
            await self.conn.commit()
            return cur.lastrowid

    async def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        await self.conn.executemany(sql, [tuple(r) for r in rows])
        await self.conn.commit()

    async def fetchone(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        async with self.conn.execute(sql, tuple(params)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        async with self.conn.execute(sql, tuple(params)) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # -- guild config -------------------------------------------------------
    async def get_config(self, guild_id: int) -> dict:
        return await self.fetchone("SELECT * FROM guild_config WHERE guild_id=?", (guild_id,)) or {}

    async def set_config(self, guild_id: int, **fields: Any) -> None:
        # Column names come from our own code, never from user input.
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        updates = ", ".join(f"{k}=excluded.{k}" for k in fields)
        await self.execute(
            f"INSERT INTO guild_config (guild_id, {cols}) VALUES (?, {marks}) "
            f"ON CONFLICT(guild_id) DO UPDATE SET {updates}",
            (guild_id, *fields.values()),
        )

    # -- infractions --------------------------------------------------------
    async def add_infraction(self, guild_id: int, user_id: int, kind: str, detail: str = "") -> None:
        await self.execute(
            "INSERT INTO infractions (guild_id, user_id, kind, detail, ts) VALUES (?,?,?,?,?)",
            (guild_id, user_id, kind, detail[:500], time.time()),
        )

    async def count_infractions(self, guild_id: int, user_id: int, kind: str, within: float) -> int:
        row = await self.fetchone(
            "SELECT COUNT(*) AS n FROM infractions WHERE guild_id=? AND user_id=? AND kind=? AND ts>=?",
            (guild_id, user_id, kind, time.time() - within),
        )
        return row["n"] if row else 0
