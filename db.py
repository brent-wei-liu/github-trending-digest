"""Shared database setup for GitHub Trending Digest."""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = os.environ.get(
    "GH_TRENDING_DB_PATH",
    str(Path(__file__).resolve().parent / "data" / "gh_trending.db"),
)

DEFAULT_FOCUS_PROFILES = [
    ("default", "All languages, no filter", json.dumps({
        "languages": [],
        "keywords": [],
        "instructions": "",
        "top_n": 15
    })),
    ("ai-ml", "AI/ML focused", json.dumps({
        "languages": ["Python", "Jupyter Notebook"],
        "keywords": ["ai", "ml", "llm", "agent", "model", "neural", "transformer", "diffusion", "rag", "embedding", "fine-tune", "training", "inference"],
        "instructions": "重点分析 AI/ML 相关项目的技术特点和应用场景",
        "top_n": 15
    })),
    ("rust-go", "Rust and Go systems", json.dumps({
        "languages": ["Rust", "Go"],
        "keywords": [],
        "instructions": "重点分析系统编程和基础设施项目",
        "top_n": 15
    })),
    ("frontend", "Frontend/Web focused", json.dumps({
        "languages": ["TypeScript", "JavaScript"],
        "keywords": ["react", "vue", "svelte", "next", "css", "ui", "component", "design"],
        "instructions": "重点分析前端框架、UI 库和设计工具",
        "top_n": 15
    })),
]


def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # 5s wait when a concurrent writer holds the lock — default 0 is the
    # most plausible contributor to SQLite WAL corruption under always-on
    # api.py + scheduled fetch. See ai-leaders-digest for prior-art.
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS repos (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name   TEXT UNIQUE NOT NULL,
            description TEXT,
            language    TEXT,
            url         TEXT,
            first_seen  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS trending_entries (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            repo        TEXT NOT NULL,
            period      TEXT NOT NULL,
            rank        INTEGER,
            stars       INTEGER,
            stars_delta INTEGER,
            forks       INTEGER,
            fetched_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS summaries (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT NOT NULL,
            period      TEXT NOT NULL,
            focus       TEXT DEFAULT 'default',
            content     TEXT NOT NULL,
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS focus_profiles (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT UNIQUE NOT NULL,
            description TEXT,
            rules       TEXT NOT NULL,
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS subscribers (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT,
            email       TEXT UNIQUE,
            focus       TEXT DEFAULT 'default',
            enabled     INTEGER DEFAULT 1,
            created_at  TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_entries_repo_date ON trending_entries(repo, fetched_at);
        CREATE INDEX IF NOT EXISTS idx_entries_period ON trending_entries(period, fetched_at);
        CREATE INDEX IF NOT EXISTS idx_summaries_date ON summaries(date);
    """)

    if conn.execute("SELECT COUNT(*) FROM focus_profiles").fetchone()[0] == 0:
        now = datetime.now(timezone.utc).isoformat()
        conn.executemany(
            "INSERT INTO focus_profiles (name, description, rules, created_at) VALUES (?, ?, ?, ?)",
            [(n, d, r, now) for n, d, r in DEFAULT_FOCUS_PROFILES],
        )

    # Migration: starred columns on repos (additive ALTER TABLE)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(repos)").fetchall()}
    for col, typedef in [
        ("starred", "INTEGER DEFAULT 0"),
        ("starred_at", "TEXT DEFAULT NULL"),
    ]:
        if col not in cols:
            conn.execute(f"ALTER TABLE repos ADD COLUMN {col} {typedef}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_repos_starred ON repos(starred) WHERE starred = 1"
    )

    # Migration: read/unread tracking on summaries (additive ALTER TABLE)
    s_cols = {r[1] for r in conn.execute("PRAGMA table_info(summaries)").fetchall()}
    for col, typedef in [
        ("is_read", "INTEGER NOT NULL DEFAULT 0"),
        ("read_at", "TEXT DEFAULT NULL"),
    ]:
        if col not in s_cols:
            conn.execute(f"ALTER TABLE summaries ADD COLUMN {col} {typedef}")

    conn.commit()
