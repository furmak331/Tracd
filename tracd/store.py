"""Watches, their history and the settings, kept in one small SQLite file."""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

DEFAULT_SETTINGS: dict[str, Any] = {
    "ntfy_enabled": True,
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": "",  # generated on first run: the topic name is the only secret
    "ntfy_token": "",  # only for a protected topic on your own ntfy server
    "telegram_enabled": False,
    "telegram_token": "",
    "telegram_chat_id": "",
    "repeat_alerts": 5,  # how many times to alert while a size stays in stock
    "repeat_every": 20,  # seconds between those alerts
    "auto_open": True,  # open the size's page on this computer on a hit
    "beep": True,  # beep on this computer on a hit
    "notify_sold_out_again": True,
    "blind_after": 180,  # seconds a shop may be unreadable before you are told
    "default_interval": 15,
    "onboarded": False,
    "alerts_verified": False,  # a test alert has gone through at least once
    "lan_key": "",  # the key for phones on your Wi-Fi (--lan), kept so saved links keep working
}

# Never sent to the dashboard and never changed from it.
INTERNAL_SETTINGS = ("lan_key",)

SECRET_SETTINGS = ("telegram_token", "ntfy_token")


@dataclass
class Watch:
    id: int
    shop: str
    product_url: str
    poll_url: str
    title: str
    size_id: str
    size_label: str
    brand: str | None = None
    image: str | None = None
    size_url: str | None = None
    interval: int = 15
    enabled: bool = True
    created_at: float = 0.0
    state: str | None = None  # last reading: IN_STOCK / OUT / UNKNOWN / BLOCKED
    detail: str = ""
    price: float | None = None
    qty: int | None = None
    last_checked: float | None = None
    last_change: float | None = None  # when it last flipped between in stock and sold out
    last_in_stock: float | None = None
    checks: int = 0
    last_ms: int | None = None
    acked_at: float | None = None  # when "Got it" was tapped for the current restock

    @property
    def open_url(self) -> str:
        for u in (self.size_url, self.poll_url, self.product_url):
            if u and u.lower().startswith(("https://", "http://")):
                return u
        return self.product_url

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["open_url"] = self.open_url
        return d


WATCH_COLUMNS = [f.name for f in fields(Watch)]

SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop TEXT NOT NULL,
    product_url TEXT NOT NULL,
    poll_url TEXT NOT NULL,
    title TEXT NOT NULL,
    size_id TEXT NOT NULL,
    size_label TEXT NOT NULL,
    brand TEXT,
    image TEXT,
    size_url TEXT,
    interval INTEGER NOT NULL DEFAULT 15,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    state TEXT,
    detail TEXT NOT NULL DEFAULT '',
    price REAL,
    qty INTEGER,
    last_checked REAL,
    last_change REAL,
    last_in_stock REAL,
    checks INTEGER NOT NULL DEFAULT 0,
    last_ms INTEGER,
    acked_at REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id INTEGER NOT NULL,
    at REAL NOT NULL,
    state TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    price REAL
);
CREATE INDEX IF NOT EXISTS events_watch ON events (watch_id, at);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# Columns added after the first release, with their SQL type. Older databases
# get them on start.
ADDED_COLUMNS = {"acked_at": "REAL"}


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL") if self.path != ":memory:" else None
            self._db.executescript(SCHEMA)
            have = {r[1] for r in self._db.execute("PRAGMA table_info(watches)")}
            for col, typ in ADDED_COLUMNS.items():
                if col not in have:
                    self._db.execute(f"ALTER TABLE watches ADD COLUMN {col} {typ}")
        self._settings_cache: dict[str, Any] | None = None

    def close(self) -> None:
        self._db.close()

    def _exec(self, sql: str, args: tuple | list = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, args)

    # -- watches ---------------------------------------------------------------

    @staticmethod
    def _watch(row: sqlite3.Row) -> Watch:
        d = dict(row)
        d["enabled"] = bool(d["enabled"])
        return Watch(**d)

    def watches(self, enabled_only: bool = False) -> list[Watch]:
        sql = "SELECT * FROM watches" + (" WHERE enabled = 1" if enabled_only else "") + " ORDER BY id"
        return [self._watch(r) for r in self._exec(sql).fetchall()]

    def watch(self, watch_id: int) -> Watch | None:
        r = self._exec("SELECT * FROM watches WHERE id = ?", (watch_id,)).fetchone()
        return self._watch(r) if r else None

    def find_watch(self, shop: str, poll_url: str, size_id: str) -> Watch | None:
        r = self._exec(
            "SELECT * FROM watches WHERE shop = ? AND poll_url = ? AND size_id = ?", (shop, poll_url, size_id)
        ).fetchone()
        return self._watch(r) if r else None

    def add_watch(self, **kw: Any) -> Watch:
        kw.setdefault("created_at", time.time())
        cols = [c for c in kw if c in WATCH_COLUMNS and c != "id"]
        cur = self._exec(
            f"INSERT INTO watches ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
            [int(kw[c]) if isinstance(kw[c], bool) else kw[c] for c in cols],
        )
        return self.watch(cur.lastrowid)  # type: ignore[return-value]

    def update_watch(self, watch_id: int, **kw: Any) -> Watch | None:
        cols = [c for c in kw if c in WATCH_COLUMNS and c != "id"]
        if cols:
            self._exec(
                f"UPDATE watches SET {', '.join(c + ' = ?' for c in cols)} WHERE id = ?",
                [int(kw[c]) if isinstance(kw[c], bool) else kw[c] for c in cols] + [watch_id],
            )
        return self.watch(watch_id)

    def delete_watch(self, watch_id: int) -> bool:
        cur = self._exec("DELETE FROM watches WHERE id = ?", (watch_id,))
        self._exec("DELETE FROM events WHERE watch_id = ?", (watch_id,))
        return cur.rowcount > 0

    # -- history -----------------------------------------------------------------

    def add_event(self, watch_id: int, state: str, detail: str = "", price: float | None = None, at: float | None = None) -> None:
        self._exec(
            "INSERT INTO events (watch_id, at, state, detail, price) VALUES (?, ?, ?, ?, ?)",
            (watch_id, at or time.time(), state, detail, price),
        )
        # Keep the newest 500 per watch.
        self._exec(
            "DELETE FROM events WHERE watch_id = ? AND id NOT IN "
            "(SELECT id FROM events WHERE watch_id = ? ORDER BY id DESC LIMIT 500)",
            (watch_id, watch_id),
        )

    def events(self, watch_id: int, limit: int = 50) -> list[dict]:
        rows = self._exec(
            "SELECT at, state, detail, price FROM events WHERE watch_id = ? ORDER BY id DESC LIMIT ?", (watch_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]

    # -- settings ----------------------------------------------------------------

    def settings(self) -> dict[str, Any]:
        if self._settings_cache is None:
            s = dict(DEFAULT_SETTINGS)
            for r in self._exec("SELECT key, value FROM settings").fetchall():
                try:
                    s[r["key"]] = json.loads(r["value"])
                except ValueError:
                    pass
            if not s.get("ntfy_topic"):
                s["ntfy_topic"] = "tracd-" + secrets.token_hex(8)
                self._exec("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", ("ntfy_topic", json.dumps(s["ntfy_topic"])))
            self._settings_cache = s
        return dict(self._settings_cache)

    def update_settings(self, **kw: Any) -> dict[str, Any]:
        cur = self.settings()
        for k, v in kw.items():
            if k not in DEFAULT_SETTINGS:
                continue
            default = DEFAULT_SETTINGS[k]
            if isinstance(default, bool):
                v = bool(v)
            elif isinstance(default, int):
                try:
                    v = int(float(v))
                except (TypeError, ValueError, OverflowError):
                    continue
            elif isinstance(default, str):
                v = str(v or "").strip()
            cur[k] = v
            self._exec("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, json.dumps(v)))
        self._settings_cache = cur
        return dict(cur)
