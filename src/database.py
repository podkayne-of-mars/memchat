"""SQLite database setup and query functions for memchat."""

import sqlite3
from pathlib import Path
from contextlib import contextmanager

from src.config import get_config

_db_path: str | None = None


def _get_db_path() -> str:
    global _db_path
    if _db_path is None:
        _db_path = get_config().database.path
    return _db_path


def set_db_path(path: str) -> None:
    """Override DB path (useful for testing)."""
    global _db_path
    _db_path = path


@contextmanager
def get_connection():
    """Context manager for SQLite connections with WAL mode and foreign keys."""
    conn = sqlite3.connect(_get_db_path())
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA_SQL = """
-- Users
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Personas (one active per user, history preserved)
CREATE TABLE IF NOT EXISTS personas (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    persona_text TEXT NOT NULL,
    active BOOLEAN DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Conversation log (every message ever sent/received)
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system')),
    content TEXT NOT NULL,
    session_id TEXT NOT NULL,
    token_estimate INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Knowledge store
CREATE TABLE IF NOT EXISTS knowledge (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    type TEXT NOT NULL CHECK(type IN ('fact', 'preference', 'decision', 'correction', 'rejected', 'event', 'project', 'action')),
    topic TEXT NOT NULL,
    content TEXT NOT NULL,
    continuity TEXT DEFAULT 'low' CHECK(continuity IN ('high', 'low')),
    durable TEXT DEFAULT 'low' CHECK(durable IN ('high', 'low')),
    event_date TEXT,
    status TEXT DEFAULT 'active' CHECK(status IN ('active', 'superseded', 'retired')),
    supersedes_id INTEGER REFERENCES knowledge(id),
    source_session_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Checkpoints (one active per user)
CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    summary TEXT NOT NULL,
    active_topics TEXT,
    active BOOLEAN DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Session metadata
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ended_at TIMESTAMP,
    end_reason TEXT CHECK(end_reason IN ('token_limit', 'manual', 'timeout', 'error')),
    tokens_used INTEGER
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_messages_user_session ON messages(user_id, session_id);
CREATE INDEX IF NOT EXISTS idx_messages_user_created ON messages(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_knowledge_user_status ON knowledge(user_id, status);
CREATE INDEX IF NOT EXISTS idx_personas_user_active ON personas(user_id, active);
CREATE INDEX IF NOT EXISTS idx_checkpoints_user_active ON checkpoints(user_id, active);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
"""

FTS_SCHEMA_SQL = """
-- Full-text search index on knowledge (legacy — ChromaDB vector search is the
-- primary retrieval mechanism; FTS5 is maintained but not used for context assembly)
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    topic, content, content=knowledge, content_rowid=id
);
"""

# Triggers to keep FTS index in sync with the knowledge table (legacy — see above)
FTS_TRIGGERS_SQL = """
CREATE TRIGGER IF NOT EXISTS knowledge_ai AFTER INSERT ON knowledge BEGIN
    INSERT INTO knowledge_fts(rowid, topic, content)
    VALUES (new.id, new.topic, new.content);
END;

CREATE TRIGGER IF NOT EXISTS knowledge_ad AFTER DELETE ON knowledge BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, topic, content)
    VALUES ('delete', old.id, old.topic, old.content);
END;

CREATE TRIGGER IF NOT EXISTS knowledge_au AFTER UPDATE ON knowledge BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, topic, content)
    VALUES ('delete', old.id, old.topic, old.content);
    INSERT INTO knowledge_fts(rowid, topic, content)
    VALUES (new.id, new.topic, new.content);
END;
"""


def init_db() -> None:
    """Create all tables, indexes, FTS, and triggers if they don't exist."""
    db_path = Path(_get_db_path())
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with get_connection() as conn:
        conn.executescript(SCHEMA_SQL)
        conn.executescript(FTS_SCHEMA_SQL)
        conn.executescript(FTS_TRIGGERS_SQL)

        # Migration: add password_hash to existing databases
        cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "password_hash" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN password_hash TEXT NOT NULL DEFAULT ''")

        # Migration: add image columns to messages table
        msg_cols = [r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()]
        if "image_data" not in msg_cols:
            conn.execute("ALTER TABLE messages ADD COLUMN image_data TEXT")
            conn.execute("ALTER TABLE messages ADD COLUMN image_media_type TEXT")

        # Migration: knowledge table — replace salience with continuity + durable,
        # add 'action' type
        k_cols = [r[1] for r in conn.execute("PRAGMA table_info(knowledge)").fetchall()]
        if "continuity" not in k_cols and "salience" in k_cols:
            conn.executescript("""
                -- Drop FTS triggers and table first (they reference knowledge)
                DROP TRIGGER IF EXISTS knowledge_ai;
                DROP TRIGGER IF EXISTS knowledge_ad;
                DROP TRIGGER IF EXISTS knowledge_au;
                DROP TABLE IF EXISTS knowledge_fts;

                -- Recreate with continuity + durable columns
                CREATE TABLE knowledge_new (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    type TEXT NOT NULL CHECK(type IN ('fact', 'preference', 'decision', 'correction', 'rejected', 'event', 'project', 'action')),
                    topic TEXT NOT NULL,
                    content TEXT NOT NULL,
                    continuity TEXT DEFAULT 'low' CHECK(continuity IN ('high', 'low')),
                    durable TEXT DEFAULT 'low' CHECK(durable IN ('high', 'low')),
                    event_date TEXT,
                    status TEXT DEFAULT 'active' CHECK(status IN ('active', 'superseded', 'retired')),
                    supersedes_id INTEGER REFERENCES knowledge(id),
                    source_session_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                INSERT INTO knowledge_new (id, user_id, type, topic, content, continuity, durable, event_date, status, supersedes_id, source_session_id, created_at)
                SELECT id, user_id, type, topic, content,
                       CASE WHEN salience = 'high' THEN 'low' ELSE 'low' END,
                       CASE WHEN salience = 'high' THEN 'high' ELSE 'low' END,
                       event_date, status, supersedes_id, source_session_id, created_at
                FROM knowledge;

                DROP TABLE knowledge;
                ALTER TABLE knowledge_new RENAME TO knowledge;

                CREATE INDEX idx_knowledge_user_status ON knowledge(user_id, status);
            """)
            # Recreate FTS table and triggers (they were dropped above)
            conn.executescript(FTS_SCHEMA_SQL)
            conn.executescript(FTS_TRIGGERS_SQL)

        # Migration: knowledge table — replace confidence with continuity + durable,
        # update type taxonomy (opinion→preference, failed_approach→rejected, add event/project/action)
        # (legacy migration — only runs on very old databases that predate salience)
        if "salience" not in k_cols and "continuity" not in k_cols:
            conn.executescript("""
                -- Drop FTS triggers and table first (they reference knowledge)
                DROP TRIGGER IF EXISTS knowledge_ai;
                DROP TRIGGER IF EXISTS knowledge_ad;
                DROP TRIGGER IF EXISTS knowledge_au;
                DROP TABLE IF EXISTS knowledge_fts;

                -- Recreate with new schema (skip salience, go straight to continuity/durable)
                CREATE TABLE knowledge_new (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    type TEXT NOT NULL CHECK(type IN ('fact', 'preference', 'decision', 'correction', 'rejected', 'event', 'project', 'action')),
                    topic TEXT NOT NULL,
                    content TEXT NOT NULL,
                    continuity TEXT DEFAULT 'low' CHECK(continuity IN ('high', 'low')),
                    durable TEXT DEFAULT 'low' CHECK(durable IN ('high', 'low')),
                    event_date TEXT,
                    status TEXT DEFAULT 'active' CHECK(status IN ('active', 'superseded', 'retired')),
                    supersedes_id INTEGER REFERENCES knowledge(id),
                    source_session_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                INSERT INTO knowledge_new (id, user_id, type, topic, content, continuity, durable, event_date, status, supersedes_id, source_session_id, created_at)
                SELECT id, user_id,
                       CASE type
                           WHEN 'opinion' THEN 'preference'
                           WHEN 'failed_approach' THEN 'rejected'
                           ELSE type
                       END,
                       topic, content,
                       'low',
                       CASE type
                           WHEN 'opinion' THEN 'high'
                           WHEN 'decision' THEN 'high'
                           WHEN 'correction' THEN 'high'
                           WHEN 'failed_approach' THEN 'high'
                           ELSE 'low'
                       END,
                       NULL,
                       status, supersedes_id, source_session_id, created_at
                FROM knowledge;

                DROP TABLE knowledge;
                ALTER TABLE knowledge_new RENAME TO knowledge;

                CREATE INDEX idx_knowledge_user_status ON knowledge(user_id, status);
            """)
            # Recreate FTS table and triggers (they were dropped above)
            conn.executescript(FTS_SCHEMA_SQL)
            conn.executescript(FTS_TRIGGERS_SQL)

        # Migration: add source_ref column to knowledge table
        # (re-check columns since earlier migrations may have rebuilt the table)
        k_cols_now = [r[1] for r in conn.execute("PRAGMA table_info(knowledge)").fetchall()]
        if "source_ref" not in k_cols_now:
            conn.execute("ALTER TABLE knowledge ADD COLUMN source_ref TEXT")


# ---------------------------------------------------------------------------
# Query helpers — thin wrappers, one per operation
# ---------------------------------------------------------------------------

def create_user(username: str, display_name: str, password_hash: str = "") -> int:
    """Create a new user, return their id."""
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO users (username, display_name, password_hash) VALUES (?, ?, ?)",
            (username, display_name, password_hash),
        )
        return cursor.lastrowid


def get_user(user_id: int) -> dict | None:
    """Get a user by id."""
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def get_user_by_username(username: str) -> dict | None:
    """Get a user by username."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        return dict(row) if row else None


def list_users() -> list[dict]:
    """Return all users."""
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY username").fetchall()
        return [dict(r) for r in rows]


def save_message(
    user_id: int,
    role: str,
    content: str,
    session_id: str,
    token_estimate: int | None = None,
    image_data: str | None = None,
    image_media_type: str | None = None,
) -> int:
    """Persist a message, return its id."""
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO messages (user_id, role, content, session_id, token_estimate,
               image_data, image_media_type)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, role, content, session_id, token_estimate,
             image_data, image_media_type),
        )
        return cursor.lastrowid


def get_recent_messages(user_id: int, limit: int = 20) -> list[dict]:
    """Return the most recent messages for a user, oldest-first."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM messages WHERE user_id = ?
               ORDER BY created_at DESC, id DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]


def get_session_messages(session_id: str) -> list[dict]:
    """Return all messages for a specific session, in chronological order."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM messages WHERE session_id = ?
               ORDER BY created_at, id""",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def create_session(session_id: str, user_id: int) -> None:
    """Record a new API session."""
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO sessions (id, user_id) VALUES (?, ?)",
            (session_id, user_id),
        )


def get_active_session(user_id: int) -> dict | None:
    """Get the current open session for a user (no ended_at)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE user_id = ? AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


def end_session(
    session_id: str, end_reason: str, tokens_used: int | None = None
) -> None:
    """Mark a session as ended."""
    with get_connection() as conn:
        conn.execute(
            """UPDATE sessions SET ended_at = CURRENT_TIMESTAMP,
               end_reason = ?, tokens_used = ? WHERE id = ?""",
            (end_reason, tokens_used, session_id),
        )


def get_active_persona(user_id: int) -> dict | None:
    """Get the active persona for a user."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM personas WHERE user_id = ? AND active = 1", (user_id,)
        ).fetchone()
        return dict(row) if row else None


def set_persona(user_id: int, persona_text: str) -> int:
    """Deactivate existing personas and create a new active one. Returns new id."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE personas SET active = 0 WHERE user_id = ? AND active = 1",
            (user_id,),
        )
        cursor = conn.execute(
            "INSERT INTO personas (user_id, persona_text) VALUES (?, ?)",
            (user_id, persona_text),
        )
        return cursor.lastrowid


def get_active_checkpoint(user_id: int) -> dict | None:
    """Get the active checkpoint for a user."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM checkpoints WHERE user_id = ? AND active = 1",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


def save_checkpoint(user_id: int, summary: str, active_topics: str) -> int:
    """Deactivate old checkpoints and save a new one. Returns new id."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE checkpoints SET active = 0 WHERE user_id = ? AND active = 1",
            (user_id,),
        )
        cursor = conn.execute(
            """INSERT INTO checkpoints (user_id, summary, active_topics)
               VALUES (?, ?, ?)""",
            (user_id, summary, active_topics),
        )
        return cursor.lastrowid


def save_knowledge(
    user_id: int,
    entry_type: str,
    topic: str,
    content: str,
    continuity: str = "low",
    durable: str = "low",
    event_date: str | None = None,
    source_session_id: str | None = None,
    supersedes_id: int | None = None,
    source_ref: str | None = None,
) -> int:
    """Insert a knowledge entry. If it supersedes another, mark the old one."""
    from src.vector_store import add_knowledge as vector_add

    with get_connection() as conn:
        if supersedes_id is not None:
            conn.execute(
                "UPDATE knowledge SET status = 'superseded' WHERE id = ?",
                (supersedes_id,),
            )
        cursor = conn.execute(
            """INSERT INTO knowledge
               (user_id, type, topic, content, continuity, durable, event_date, source_session_id, supersedes_id, source_ref)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, entry_type, topic, content, continuity, durable, event_date, source_session_id, supersedes_id, source_ref),
        )
        entry_id = cursor.lastrowid

    vector_add(entry_id, user_id, topic, content, continuity, durable)
    return entry_id


def search_knowledge(user_id: int, query: str, limit: int = 30) -> list[dict]:
    """Full-text search across knowledge entries for a user (legacy — not used
    for context assembly; ChromaDB vector search in knowledge.py is primary)."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT k.* FROM knowledge k
               JOIN knowledge_fts fts ON k.id = fts.rowid
               WHERE knowledge_fts MATCH ? AND k.user_id = ? AND k.status = 'active'
               ORDER BY rank
               LIMIT ?""",
            (query, user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_knowledge_by_ids(ids: list[int]) -> list[dict]:
    """Fetch specific knowledge entries by ID list, preserving the input order."""
    if not ids:
        return []
    with get_connection() as conn:
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT * FROM knowledge WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
    by_id = {r["id"]: dict(r) for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def get_all_active_knowledge(user_id: int) -> list[dict]:
    """Return all active knowledge entries for a user (fallback when no search query)."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM knowledge
               WHERE user_id = ? AND status = 'active'
               ORDER BY created_at DESC""",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
