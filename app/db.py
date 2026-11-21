"""SQLite storage. One connection, used only from the asyncio event loop thread."""
import json
import sqlite3
import time
from pathlib import Path

from .config import DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    token_hash  TEXT NOT NULL UNIQUE,
    created_at  REAL NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS submissions (
    id          INTEGER PRIMARY KEY,
    player_id   INTEGER NOT NULL REFERENCES players(id),
    version     INTEGER NOT NULL,
    created_at  REAL NOT NULL,
    finished_at REAL,
    status      TEXT NOT NULL,          -- queued | building | testing | ready | failed | disabled
    image       TEXT,
    log         TEXT NOT NULL DEFAULT '',
    engine_name TEXT,                   -- from "id name" during smoke test
    size_bytes  INTEGER,
    UNIQUE(player_id, version)
);
CREATE TABLE IF NOT EXISTS rounds (
    id          INTEGER PRIMARY KEY,
    created_at  REAL NOT NULL,
    started_at  REAL,
    finished_at REAL,
    status      TEXT NOT NULL,          -- running | done | cancelled | failed
    config      TEXT NOT NULL,          -- json snapshot of settings
    total_games INTEGER NOT NULL DEFAULT 0,
    note        TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS round_entries (
    round_id      INTEGER NOT NULL REFERENCES rounds(id),
    player_id     INTEGER NOT NULL REFERENCES players(id),
    submission_id INTEGER NOT NULL REFERENCES submissions(id),
    PRIMARY KEY (round_id, player_id)
);
CREATE TABLE IF NOT EXISTS games (
    id          INTEGER PRIMARY KEY,
    round_id    INTEGER NOT NULL REFERENCES rounds(id),
    game_no     INTEGER NOT NULL,
    white_id    INTEGER NOT NULL REFERENCES players(id),
    black_id    INTEGER NOT NULL REFERENCES players(id),
    status      TEXT NOT NULL,          -- live | done | aborted
    result      TEXT NOT NULL DEFAULT '*',
    reason      TEXT NOT NULL DEFAULT '',
    termination TEXT NOT NULL DEFAULT '',
    start_fen   TEXT NOT NULL,
    book_plies  INTEGER NOT NULL DEFAULT 0,
    moves       TEXT NOT NULL DEFAULT '[]',   -- json: list of uci moves (book included)
    evals       TEXT NOT NULL DEFAULT '[]',   -- json: per non-book ply {cp|mate, depth, ms}
    started_at  REAL,
    finished_at REAL,
    pgn         TEXT,
    UNIQUE(round_id, game_no)
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_conn: sqlite3.Connection | None = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(Path(DATA_DIR) / "scacc.db", check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.executescript(SCHEMA)
    return _conn


def q(sql: str, *args) -> list[sqlite3.Row]:
    return conn().execute(sql, args).fetchall()


def q1(sql: str, *args) -> sqlite3.Row | None:
    return conn().execute(sql, args).fetchone()


def ex(sql: str, *args) -> int:
    cur = conn().execute(sql, args)
    return cur.lastrowid


def now() -> float:
    return time.time()


# --- settings ------------------------------------------------------------

DEFAULT_SETTINGS = {
    "submissions_open": False,
    "tc": "10+0.1",
    "rounds": 10,              # openings per pairing; each played twice (colours swapped)
    "concurrency": 0,          # 0 = auto (cpus // 2)
    "timemargin": 100,         # ms an engine may overrun its clock before losing
    "engine_cpus": "1",
    "engine_memory": "512m",
    "adjudicate": True,        # draw/resign adjudication based on engine scores
    "maxmoves": 200,           # draw after this many full moves
    "event_name": "Scacchackathon",
    "public_url": "",          # address players use; empty = PUBLIC_URL env or the request's host
}


def get_setting(key: str):
    row = q1("SELECT value FROM settings WHERE key=?", key)
    if row is None:
        return DEFAULT_SETTINGS[key]
    return json.loads(row["value"])


def set_setting(key: str, value) -> None:
    ex("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
       key, json.dumps(value))


def all_settings() -> dict:
    return {k: get_setting(k) for k in DEFAULT_SETTINGS}
