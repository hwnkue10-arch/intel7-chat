"""SQLite persistence for users, sessions, messages, and attachments."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid as _uuid
from difflib import SequenceMatcher
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

_DEFAULT_DATA = Path(__file__).resolve().parent.parent / "data"
DB_PATH = _DEFAULT_DATA / "chat.db"
DB_MAX_BYTES = 3 * 1024**3

def configure_storage(data_dir: Path | str, max_db_bytes: int) -> None:
    global DB_PATH, DB_MAX_BYTES
    root = Path(data_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    DB_PATH = root / "chat.db"
    DB_MAX_BYTES = int(max_db_bytes)

def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

def init_db() -> None:
    with get_connection() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        max_pages = max(1024, DB_MAX_BYTES // page_size)
        conn.execute(f"PRAGMA max_page_count={max_pages}")
        conn.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            normalized_username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'student',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_login TEXT
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nickname TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            ip TEXT NOT NULL DEFAULT '',
            reply_nickname TEXT,
            reply_content TEXT,
            attachment_id TEXT,
            user_id INTEGER
        )""")
        for column, definition in (
            ("ip", "TEXT NOT NULL DEFAULT ''"),
            ("reply_nickname", "TEXT"),
            ("reply_content", "TEXT"),
            ("attachment_id", "TEXT"),
            ("user_id", "INTEGER"),
            ("edited_at", "TEXT"),
            ("is_hidden", "INTEGER NOT NULL DEFAULT 0"),
            ("moved_from_channel_id", "INTEGER"),
        ):
            _add_column_if_missing(conn, "messages", column, definition)
        conn.execute("""CREATE TABLE IF NOT EXISTS attachments (
            id TEXT PRIMARY KEY,
            original_name TEXT NOT NULL,
            stored_name TEXT NOT NULL UNIQUE,
            size INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            content_type TEXT NOT NULL,
            previewable INTEGER NOT NULL DEFAULT 0,
            uploader_nickname TEXT NOT NULL,
            ip TEXT NOT NULL,
            owner_token_hash TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            claimed INTEGER NOT NULL DEFAULT 0,
            uploader_user_id INTEGER
        )""")
        _add_column_if_missing(conn, "attachments", "owner_token_hash", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(conn, "attachments", "uploader_user_id", "INTEGER")
        conn.execute("""CREATE TABLE IF NOT EXISTS message_attachments (
            message_id INTEGER NOT NULL,
            attachment_id TEXT NOT NULL,
            original_name TEXT NOT NULL,
            position INTEGER NOT NULL,
            PRIMARY KEY (message_id, attachment_id)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS direct_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_user_id INTEGER NOT NULL,
            recipient_user_id INTEGER NOT NULL,
            sender_nickname TEXT NOT NULL,
            recipient_nickname TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            reply_nickname TEXT,
            reply_content TEXT,
            FOREIGN KEY(sender_user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(recipient_user_id) REFERENCES users(id) ON DELETE CASCADE
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS direct_message_attachments (
            direct_message_id INTEGER NOT NULL,
            attachment_id TEXT NOT NULL,
            original_name TEXT NOT NULL,
            position INTEGER NOT NULL,
            PRIMARY KEY (direct_message_id, attachment_id),
            FOREIGN KEY(direct_message_id) REFERENCES direct_messages(id) ON DELETE CASCADE
        )""")
        conn.execute("""INSERT OR IGNORE INTO message_attachments
            (message_id, attachment_id, original_name, position)
            SELECT m.id, m.attachment_id, COALESCE(a.original_name, '파일'), 0
            FROM messages m LEFT JOIN attachments a ON a.id=m.attachment_id
            WHERE m.attachment_id IS NOT NULL AND m.attachment_id != ''""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dm_sender ON direct_messages(sender_user_id, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dm_recipient ON direct_messages(recipient_user_id, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dm_attachments_file ON direct_message_attachments(attachment_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_attachments_user ON attachments(uploader_user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at)")
        conn.execute("""CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER NOT NULL
        )""")
        conn.commit()
        _run_migrations(conn)
        seed_default_quizzes(conn)
        # Community/reference samples are added after the empty-database seed so
        # they do not suppress the original PLC/electrical sample set.
        _migrate_v15(conn)
        conn.commit()

def _get_schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return int(row[0]) if row and row[0] is not None else 0

def _set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))

def _migrate_v1(conn: sqlite3.Connection) -> None:
    """Add display_name and uuid columns to users."""
    _add_column_if_missing(conn, "users", "display_name", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "users", "uuid", "TEXT NOT NULL DEFAULT ''")
    conn.execute("UPDATE users SET display_name = username WHERE display_name = ''")
    rows = conn.execute("SELECT id FROM users WHERE uuid = ''").fetchall()
    for row in rows:
        conn.execute("UPDATE users SET uuid = ? WHERE id = ?", (_uuid.uuid4().hex, row[0]))
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_uuid ON users(uuid)")

def _migrate_v2(conn: sqlite3.Connection) -> None:
    """Create channels table, seed default permanent general channel, and add channel_id to messages."""
    conn.execute("""CREATE TABLE IF NOT EXISTS channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        uuid TEXT NOT NULL UNIQUE,
        created_by_user_id INTEGER,
        is_default INTEGER NOT NULL DEFAULT 0,
        archived INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        FOREIGN KEY(created_by_user_id) REFERENCES users(id)
    )""")
    _add_column_if_missing(conn, "channels", "archived", "INTEGER NOT NULL DEFAULT 0")
    now = utc_now()
    default_uuid = _uuid.uuid4().hex
    conn.execute("""INSERT OR IGNORE INTO channels
        (id, name, display_name, description, uuid, is_default, archived, created_at)
        VALUES (1, 'general', '전체 채팅', '기본 전체 공개 대화방', ?, 1, 0, ?)""",
        (default_uuid, now))
    _add_column_if_missing(conn, "messages", "channel_id", "INTEGER NOT NULL DEFAULT 1")
    conn.execute("UPDATE messages SET channel_id = 1 WHERE channel_id IS NULL OR channel_id = 0")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_channel_created ON messages(channel_id, id)")

def _migrate_v3(conn: sqlite3.Connection) -> None:
    """Add archived column to channels; add edited_at, is_hidden, moved_from_channel_id to messages."""
    _add_column_if_missing(conn, "channels", "archived", "INTEGER NOT NULL DEFAULT 0")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_channels_archived ON channels(archived)")
    _add_column_if_missing(conn, "messages", "edited_at", "TEXT")
    _add_column_if_missing(conn, "messages", "is_hidden", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "messages", "moved_from_channel_id", "INTEGER")

ALLOWED_REACTION_EMOJIS = {"👍", "❤️", "😂", "😮", "😢", "👏", "✅", "❌", "👀"}

def _migrate_v4(conn: sqlite3.Connection) -> None:
    """Create user_conversation_state table for read tracking and notification muting."""
    conn.execute("""CREATE TABLE IF NOT EXISTS user_conversation_state (
        user_id INTEGER NOT NULL,
        conversation_type TEXT NOT NULL,
        conversation_id TEXT NOT NULL,
        last_read_message_id INTEGER NOT NULL DEFAULT 0,
        muted INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (user_id, conversation_type, conversation_id),
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ucs_user ON user_conversation_state(user_id)")

def _migrate_v5(conn: sqlite3.Connection) -> None:
    """Create message_reactions table for channel and DM message reactions."""
    conn.execute("""CREATE TABLE IF NOT EXISTS message_reactions (
        message_type TEXT NOT NULL,
        message_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        emoji TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (message_type, message_id, user_id, emoji),
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reactions_target ON message_reactions(message_type, message_id)")

def _migrate_v6(conn: sqlite3.Connection) -> None:
    """Add edited_at column to direct_messages."""
    _add_column_if_missing(conn, "direct_messages", "edited_at", "TEXT")

def _migrate_v7(conn: sqlite3.Connection) -> None:
    """Create pinned_messages table for channel and DM message pins."""
    conn.execute("""CREATE TABLE IF NOT EXISTS pinned_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_type TEXT NOT NULL,
        conversation_id TEXT NOT NULL,
        message_id INTEGER NOT NULL,
        pinned_by_user_id INTEGER NOT NULL,
        pinned_at TEXT NOT NULL,
        UNIQUE(conversation_type, conversation_id, message_id),
        FOREIGN KEY(pinned_by_user_id) REFERENCES users(id) ON DELETE CASCADE
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pinned_conv ON pinned_messages(conversation_type, conversation_id)")

def _migrate_v8(conn: sqlite3.Connection) -> None:
    """Add last_login_ip column to users table."""
    _add_column_if_missing(conn, "users", "last_login_ip", "TEXT")

def _migrate_v9(conn: sqlite3.Connection) -> None:
    """Create tables for educational quizzes, submissions, stats/streaks, and source documents."""
    conn.execute("""CREATE TABLE IF NOT EXISTS quiz_source_documents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        filename TEXT NOT NULL,
        stored_filename TEXT NOT NULL UNIQUE,
        file_type TEXT NOT NULL DEFAULT 'pdf',
        sha256 TEXT NOT NULL,
        size INTEGER NOT NULL DEFAULT 0,
        uploaded_by_user_id INTEGER,
        created_at TEXT NOT NULL,
        FOREIGN KEY(uploaded_by_user_id) REFERENCES users(id) ON DELETE SET NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS quizzes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT NOT NULL DEFAULT 'PLC',
        difficulty TEXT NOT NULL DEFAULT 'medium',
        question_type TEXT NOT NULL DEFAULT 'multiple_choice',
        question TEXT NOT NULL,
        image_filename TEXT,
        options_json TEXT,
        correct_answers_json TEXT NOT NULL,
        explanation TEXT NOT NULL DEFAULT '',
        source_doc_id INTEGER,
        source_ref TEXT NOT NULL DEFAULT '',
        daily_date TEXT,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        FOREIGN KEY(source_doc_id) REFERENCES quiz_source_documents(id) ON DELETE SET NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS quiz_submissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        quiz_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        user_answer TEXT NOT NULL,
        is_correct INTEGER NOT NULL,
        score_earned INTEGER NOT NULL DEFAULT 0,
        submitted_at TEXT NOT NULL,
        submitted_date TEXT NOT NULL,
        UNIQUE(quiz_id, user_id),
        FOREIGN KEY(quiz_id) REFERENCES quizzes(id) ON DELETE CASCADE,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS user_quiz_stats (
        user_id INTEGER PRIMARY KEY,
        total_score INTEGER NOT NULL DEFAULT 0,
        total_solved INTEGER NOT NULL DEFAULT 0,
        total_correct INTEGER NOT NULL DEFAULT 0,
        current_streak INTEGER NOT NULL DEFAULT 0,
        max_streak INTEGER NOT NULL DEFAULT 0,
        weekly_score INTEGER NOT NULL DEFAULT 0,
        last_solved_date TEXT,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_quizzes_active ON quizzes(is_active, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_quizzes_daily ON quizzes(daily_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_quiz_sub_user ON quiz_submissions(user_id, quiz_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_quiz_sub_date ON quiz_submissions(submitted_date)")
def _migrate_v10(conn: sqlite3.Connection) -> None:
    """Add hint to quizzes and create quiz_bookmarks table."""
    _add_column_if_missing(conn, "quizzes", "hint", "TEXT DEFAULT ''")
    conn.execute("""CREATE TABLE IF NOT EXISTS quiz_bookmarks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        quiz_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(user_id, quiz_id),
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
        FOREIGN KEY(quiz_id) REFERENCES quizzes(id) ON DELETE CASCADE
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_quiz_bm_user ON quiz_bookmarks(user_id, quiz_id)")

def _migrate_v11(conn: sqlite3.Connection) -> None:
    """Add had_wrong_attempt column to quiz_submissions to retain quizzes in wrong list even after retry success."""
    _add_column_if_missing(conn, "quiz_submissions", "had_wrong_attempt", "INTEGER NOT NULL DEFAULT 0")
    conn.execute("UPDATE quiz_submissions SET had_wrong_attempt = 1 WHERE is_correct = 0")

def _migrate_v12(conn: sqlite3.Connection) -> None:
    """Add personal quiz-set review workflow and immutable daily assignments."""
    conn.execute("""CREATE TABLE IF NOT EXISTS user_quiz_sets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_user_id INTEGER NOT NULL,
        expertise TEXT NOT NULL,
        title TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'draft',
        quizzes_json TEXT NOT NULL,
        review_note TEXT NOT NULL DEFAULT '',
        approved_by_user_id INTEGER,
        submitted_at TEXT,
        approved_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(owner_user_id) REFERENCES users(id) ON DELETE CASCADE,
        FOREIGN KEY(approved_by_user_id) REFERENCES users(id) ON DELETE SET NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_quiz_sets_owner ON user_quiz_sets(owner_user_id, status)")
    conn.execute("""CREATE TABLE IF NOT EXISTS daily_quiz_sets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        assigned_date TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'published',
        created_by_user_id INTEGER,
        created_at TEXT NOT NULL,
        FOREIGN KEY(created_by_user_id) REFERENCES users(id) ON DELETE SET NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS daily_quiz_set_items (
        set_id INTEGER NOT NULL,
        quiz_id INTEGER NOT NULL,
        position INTEGER NOT NULL,
        points INTEGER NOT NULL DEFAULT 20,
        PRIMARY KEY(set_id, quiz_id),
        UNIQUE(set_id, position),
        FOREIGN KEY(set_id) REFERENCES daily_quiz_sets(id) ON DELETE CASCADE,
        FOREIGN KEY(quiz_id) REFERENCES quizzes(id) ON DELETE CASCADE
    )""")

def _migrate_v13(conn: sqlite3.Connection) -> None:
    """Normalize seeded quiz categories to the user-facing subject names."""
    conn.execute("UPDATE quizzes SET category='PLC' WHERE category='PLC/시퀀스'")
    conn.execute("UPDATE quizzes SET category='전기' WHERE category='CBT/전기기초'")
    conn.execute("UPDATE quizzes SET category='전자' WHERE category='CBT/디지털공학'")
    conn.execute("UPDATE quizzes SET category='자동화설비' WHERE category IN ('공압/유압', '생산자동화')")

def _migrate_v14(conn: sqlite3.Connection) -> None:
    """Apply subject-name normalization to databases seeded before v13."""
    conn.execute("UPDATE quizzes SET category='PLC' WHERE category='PLC/시퀀스'")
    conn.execute("UPDATE quizzes SET category='전기' WHERE category='CBT/전기기초'")
    conn.execute("UPDATE quizzes SET category='전자' WHERE category='CBT/디지털공학'")

def _migrate_v15(conn: sqlite3.Connection) -> None:
    """Seed beginner position-module error-code questions once."""
    if conn.execute("SELECT COUNT(*) FROM quizzes").fetchone()[0] == 0:
        return
    for quiz in POSITION_MODULE_SAMPLE_QUIZZES:
        exists = conn.execute("SELECT 1 FROM quizzes WHERE category=? AND question=?", (quiz["category"], quiz["question"])).fetchone()
        if exists:
            continue
        conn.execute("""INSERT INTO quizzes
            (category,difficulty,question_type,question,image_filename,options_json,
             correct_answers_json,hint,explanation,source_ref,is_active,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,1,?)""", (
            quiz["category"], quiz["difficulty"], quiz["question_type"], quiz["question"], "",
            json.dumps(quiz["options"], ensure_ascii=False), json.dumps(quiz["correct_answers"], ensure_ascii=False),
            quiz["hint"], quiz["explanation"], quiz["source_ref"], utc_now(),
        ))

def _migrate_v16(conn: sqlite3.Connection) -> None:
    """Keep approved pool questions traceable to their community submission."""
    _add_column_if_missing(conn, "quizzes", "source_submission_set_id",
                           "INTEGER REFERENCES user_quiz_sets(id) ON DELETE SET NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_quizzes_submission_source ON quizzes(source_submission_set_id)")

def _migrate_v17(conn: sqlite3.Connection) -> None:
    """Allow users to choose which earned quiz title appears by their nickname."""
    _add_column_if_missing(conn, "users", "quiz_badge_selection", "TEXT NOT NULL DEFAULT 'score'")

def _migrate_v18(conn: sqlite3.Connection) -> None:
    """Use accumulated score until a user explicitly selects an earned title."""
    conn.execute("UPDATE users SET quiz_badge_selection='score' WHERE quiz_badge_selection='auto'")

def _migrate_v19(conn: sqlite3.Connection) -> None:
    """Create persistent chess win/draw/loss totals."""
    conn.execute("""CREATE TABLE IF NOT EXISTS chess_player_stats (
        user_id INTEGER PRIMARY KEY,
        wins INTEGER NOT NULL DEFAULT 0,
        draws INTEGER NOT NULL DEFAULT 0,
        losses INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )""")

_MIGRATIONS = [
    _migrate_v1,
    _migrate_v2,
    _migrate_v3,
    _migrate_v4,
    _migrate_v5,
    _migrate_v6,
    _migrate_v7,
    _migrate_v8,
    _migrate_v9,
    _migrate_v10,
    _migrate_v11,
    _migrate_v12,
    _migrate_v13,
    _migrate_v14,
    _migrate_v15,
    _migrate_v16,
    _migrate_v17,
    _migrate_v18,
    _migrate_v19,
]


def _run_migrations(conn: sqlite3.Connection) -> None:
    current = _get_schema_version(conn)
    for index, migrate in enumerate(_MIGRATIONS, start=1):
        if index <= current:
            continue
        try:
            conn.execute("BEGIN IMMEDIATE")
            migrate(conn)
            _set_schema_version(conn, index)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

def create_user(username: str, password_hash: str, role: str = "student") -> Dict[str, Any]:
    from app.auth import normalize_username
    now = utc_now()
    user_uuid = _uuid.uuid4().hex
    with get_connection() as conn:
        cur = conn.execute("""INSERT INTO users
            (username, normalized_username, password_hash, role, created_at,
             display_name, uuid)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (username, normalize_username(username), password_hash, role, now,
             username, user_uuid))
        conn.commit()
        return get_user_by_id(cur.lastrowid)

def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None

def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    from app.auth import normalize_username
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE normalized_username=?",
                           (normalize_username(username),)).fetchone()
    return dict(row) if row else None

def update_password_hash(user_id: int, password_hash: str) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, user_id))
        conn.commit()

def update_display_name(user_id: int, display_name: str) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        conn.execute("UPDATE users SET display_name=? WHERE id=?", (display_name, user_id))
        conn.commit()
    return get_user_by_id(user_id)

def update_quiz_badge_selection(user_id: int, selection: str) -> Optional[Dict[str, Any]]:
    """Persist score/none or an earned subject title selection."""
    clean = selection.strip()
    if clean not in {"score", "none"}:
        earned = {item["selection"] for item in get_user_quiz_title_options(user_id)}
        if clean not in earned:
            raise ValueError("현재 획득한 칭호만 선택할 수 있습니다.")
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE users SET quiz_badge_selection=? WHERE id=?", (clean, user_id)
        )
        conn.commit()
    return get_user_by_id(user_id) if cur.rowcount else None

def list_users() -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute("""SELECT u.id, u.username, u.display_name, u.uuid,
            u.role, u.active, u.created_at, u.last_login, u.last_login_ip,
            (SELECT COUNT(*) FROM messages m WHERE m.user_id=u.id) AS message_count,
            (SELECT COALESCE(SUM(a.size), 0) FROM attachments a
             WHERE a.uploader_user_id=u.id) AS attachment_bytes
            FROM users u
            ORDER BY u.role='admin' DESC, u.normalized_username""").fetchall()
        return [dict(row) for row in rows]

def list_mentionable_users() -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, username, display_name FROM users WHERE active=1 ORDER BY normalized_username"
        ).fetchall()
        return [dict(row) for row in rows]

def count_active_admins() -> int:
    with get_connection() as conn:
        return int(conn.execute(
            "SELECT COUNT(*) FROM users WHERE role='admin' AND active=1"
        ).fetchone()[0])

def set_user_active(user_id: int, active: bool) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE users SET active=? WHERE id=?", (int(active), user_id))
        if not active:
            conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        conn.commit()

def set_user_role(user_id: int, role: str) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
        conn.commit()

def delete_user_sessions(user_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        conn.commit()

def create_session(session_hash: str, user_id: int, expires_at: str, client_ip: Optional[str] = None) -> None:
    now = utc_now()
    with get_connection() as conn:
        conn.execute("DELETE FROM sessions WHERE user_id=? OR expires_at<=?", (user_id, now))
        conn.execute("INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",
                     (session_hash, user_id, now, expires_at))
        if client_ip is not None:
            conn.execute("UPDATE users SET last_login=?, last_login_ip=? WHERE id=?", (now, client_ip, user_id))
        else:
            conn.execute("UPDATE users SET last_login=? WHERE id=?", (now, user_id))
        conn.commit()

def get_session_user(session_hash: str) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("""SELECT u.*, s.expires_at AS session_expires_at
            FROM sessions s JOIN users u ON u.id=s.user_id
            WHERE s.token_hash=? AND s.expires_at>? AND u.active=1""",
            (session_hash, utc_now())).fetchone()
    return dict(row) if row else None

def delete_session(session_hash: str) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (session_hash,))
        conn.commit()

def prune_expired_sessions() -> int:
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE expires_at<=?", (utc_now(),))
        conn.commit()
        return cur.rowcount

def create_channel(name: str, display_name: str, description: str = "",
                   created_by_user_id: Optional[int] = None) -> Dict[str, Any]:
    from app.auth import validate_channel_description, validate_channel_display_name, validate_channel_name
    clean_name = validate_channel_name(name)
    clean_display = validate_channel_display_name(display_name)
    clean_desc = validate_channel_description(description)
    now = utc_now()
    chan_uuid = _uuid.uuid4().hex
    with get_connection() as conn:
        cur = conn.execute("""INSERT INTO channels
            (name, display_name, description, uuid, created_by_user_id, is_default, created_at)
            VALUES (?, ?, ?, ?, ?, 0, ?)""",
            (clean_name, clean_display, clean_desc, chan_uuid, created_by_user_id, now))
        conn.commit()
        return get_channel_by_id(cur.lastrowid)

def get_channel_by_id(channel_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["is_default"] = bool(d.get("is_default"))
    d["archived"] = bool(d.get("archived"))
    return d

def get_channel_by_name(name: str) -> Optional[Dict[str, Any]]:
    clean_name = name.strip().casefold()
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM channels WHERE name=?", (clean_name,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["is_default"] = bool(d.get("is_default"))
    d["archived"] = bool(d.get("archived"))
    return d

def list_channels(include_archived: bool = False) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        where_clause = "" if include_archived else "WHERE c.archived = 0"
        rows = conn.execute(f"""SELECT c.*,
            (SELECT COUNT(*) FROM messages m WHERE m.channel_id=c.id) AS message_count
            FROM channels c
            {where_clause}
            ORDER BY c.is_default DESC, c.id ASC""").fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["is_default"] = bool(d.get("is_default"))
            d["archived"] = bool(d.get("archived"))
            result.append(d)
        return result

def channel_exists(channel_id: int) -> bool:
    with get_connection() as conn:
        row = conn.execute("SELECT 1 FROM channels WHERE id=?", (channel_id,)).fetchone()
        return bool(row)

def update_channel(channel_id: int, name: Optional[str] = None,
                   display_name: Optional[str] = None,
                   description: Optional[str] = None) -> Optional[Dict[str, Any]]:
    from app.auth import validate_channel_description, validate_channel_display_name, validate_channel_name
    current = get_channel_by_id(channel_id)
    if not current:
        return None
    new_name = validate_channel_name(name) if name is not None else current["name"]
    new_display = validate_channel_display_name(display_name) if display_name is not None else current["display_name"]
    new_desc = validate_channel_description(description) if description is not None else current["description"]
    with get_connection() as conn:
        conn.execute("""UPDATE channels
            SET name=?, display_name=?, description=?
            WHERE id=?""",
            (new_name, new_display, new_desc, channel_id))
        conn.commit()
    return get_channel_by_id(channel_id)

def archive_channel(channel_id: int, unarchive: bool = False) -> Optional[Dict[str, Any]]:
    current = get_channel_by_id(channel_id)
    if not current:
        return None
    if current.get("is_default") and not unarchive:
        raise ValueError("기본 채널은 보관할 수 없습니다.")
    val = 0 if unarchive else 1
    with get_connection() as conn:
        conn.execute("UPDATE channels SET archived=? WHERE id=?", (val, channel_id))
        conn.commit()
    return get_channel_by_id(channel_id)

def delete_channel(channel_id: int) -> Optional[List[str]]:
    current = get_channel_by_id(channel_id)
    if not current:
        return None
    if current.get("is_default"):
        raise ValueError("기본 채널은 삭제할 수 없습니다.")
    stored_files_to_remove: List[str] = []
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        msg_rows = conn.execute("SELECT id FROM messages WHERE channel_id=?", (channel_id,)).fetchall()
        msg_ids = [r[0] for r in msg_rows]
        if msg_ids:
            marks = ",".join("?" for _ in msg_ids)
            att_rows = conn.execute(f"SELECT DISTINCT attachment_id FROM message_attachments WHERE message_id IN ({marks})", msg_ids).fetchall()
            att_ids = [r[0] for r in att_rows if r[0]]
            conn.execute(f"DELETE FROM message_attachments WHERE message_id IN ({marks})", msg_ids)
            conn.execute(f"DELETE FROM message_reactions WHERE message_type='channel' AND message_id IN ({marks})", msg_ids)
            for att_id in att_ids:
                in_msgs = conn.execute("SELECT 1 FROM message_attachments WHERE attachment_id=?", (att_id,)).fetchone()
                in_dms = conn.execute("SELECT 1 FROM direct_message_attachments WHERE attachment_id=?", (att_id,)).fetchone()
                if not in_msgs and not in_dms:
                    rec = conn.execute("SELECT stored_name FROM attachments WHERE id=?", (att_id,)).fetchone()
                    if rec:
                        stored_files_to_remove.append(rec["stored_name"])
                        conn.execute("DELETE FROM attachments WHERE id=?", (att_id,))
            conn.execute(f"DELETE FROM messages WHERE id IN ({marks})", msg_ids)
        conn.execute("DELETE FROM user_conversation_state WHERE conversation_type='channel' AND conversation_id=?", (str(channel_id),))
        conn.execute("DELETE FROM pinned_messages WHERE conversation_type='channel' AND conversation_id=?", (str(channel_id),))
        conn.execute("DELETE FROM channels WHERE id=?", (channel_id,))
        conn.commit()
    return stored_files_to_remove

def _attachment_public(row: sqlite3.Row | Dict[str, Any] | None) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    return {
        "id": row["id"], "name": row["original_name"], "size": row["size"],
        "sha256": row["sha256"], "content_type": row["content_type"],
        "previewable": bool(row["previewable"]), "url": f"/api/files/{row['id']}",
        "removed": False, "owner_id": row["uploader_user_id"],
    }

def _message_attachments(conn: sqlite3.Connection, ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
    result = {message_id: [] for message_id in ids}
    if not ids:
        return result
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(f"""SELECT ma.*, a.id AS live_id, a.size, a.sha256,
        a.content_type, a.previewable, a.uploader_user_id
        FROM message_attachments ma LEFT JOIN attachments a ON a.id=ma.attachment_id
        WHERE ma.message_id IN ({marks}) ORDER BY ma.message_id, ma.position""", ids).fetchall()
    for row in rows:
        if row["live_id"]:
            item = {"id": row["attachment_id"], "name": row["original_name"], "size": row["size"],
                    "sha256": row["sha256"], "content_type": row["content_type"],
                    "previewable": bool(row["previewable"]), "url": f"/api/files/{row['attachment_id']}",
                    "removed": False, "owner_id": row["uploader_user_id"]}
        else:
            item = {"id": row["attachment_id"], "name": row["original_name"], "removed": True}
        result[row["message_id"]].append(item)
    return result

def get_reactions_for_messages(
    conn: sqlite3.Connection,
    message_type: str,
    message_ids: List[int],
    current_user_id: Optional[int] = None,
) -> Dict[int, List[Dict[str, Any]]]:
    result: Dict[int, List[Dict[str, Any]]] = {mid: [] for mid in message_ids}
    if not message_ids:
        return result
    marks = ",".join("?" for _ in message_ids)
    rows = conn.execute(f"""
        SELECT mr.message_id, mr.emoji, mr.user_id, mr.created_at,
               u.username, u.display_name
        FROM message_reactions mr
        JOIN users u ON u.id = mr.user_id
        WHERE mr.message_type = ? AND mr.message_id IN ({marks})
        ORDER BY mr.created_at ASC, mr.rowid ASC
    """, [message_type, *message_ids]).fetchall()

    grouped: Dict[int, Dict[str, Dict[str, Any]]] = {mid: {} for mid in message_ids}
    for row in rows:
        mid = row["message_id"]
        emoji = row["emoji"]
        uid = row["user_id"]
        user_info = {
            "id": uid,
            "username": row["username"],
            "display_name": row["display_name"] or row["username"],
        }
        if emoji not in grouped[mid]:
            grouped[mid][emoji] = {
                "emoji": emoji,
                "count": 0,
                "reacted_by_me": False,
                "users": [],
            }
        grouped[mid][emoji]["count"] += 1
        grouped[mid][emoji]["users"].append(user_info)
        if current_user_id is not None and uid == current_user_id:
            grouped[mid][emoji]["reacted_by_me"] = True

    for mid in message_ids:
        result[mid] = list(grouped[mid].values())
    return result

def toggle_message_reaction(
    message_type: str,
    message_id: int,
    user_id: int,
    emoji: str,
) -> bool:
    """Toggles reaction on a message. Returns True if added, False if removed."""
    if emoji not in ALLOWED_REACTION_EMOJIS:
        raise ValueError(f"Invalid reaction emoji: {emoji}")
    with get_connection() as conn:
        existing = conn.execute("""
            SELECT 1 FROM message_reactions
            WHERE message_type = ? AND message_id = ? AND user_id = ? AND emoji = ?
        """, (message_type, message_id, user_id, emoji)).fetchone()
        if existing:
            conn.execute("""
                DELETE FROM message_reactions
                WHERE message_type = ? AND message_id = ? AND user_id = ? AND emoji = ?
            """, (message_type, message_id, user_id, emoji))
            conn.commit()
            return False
        else:
            conn.execute("""
                INSERT INTO message_reactions (message_type, message_id, user_id, emoji, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (message_type, message_id, user_id, emoji, utc_now()))
            conn.commit()
            return True

def get_message_reactions(
    message_type: str,
    message_id: int,
    current_user_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        res = get_reactions_for_messages(conn, message_type, [message_id], current_user_id)
        return res.get(message_id, [])

def _message_public(row: sqlite3.Row, attachments: List[Dict[str, Any]], reactions: Optional[List[Dict[str, Any]]] = None, is_pinned: bool = False) -> Dict[str, Any]:
    reply = None
    if row["reply_nickname"] or row["reply_content"]:
        reply = {"nickname": row["reply_nickname"] or "", "content": row["reply_content"] or ""}
    live = next((item for item in attachments if not item.get("removed")), None)
    keys = row.keys() if hasattr(row, 'keys') else row
    channel_id = row["channel_id"] if "channel_id" in keys else 1
    edited_at = row["edited_at"] if "edited_at" in keys else None
    is_hidden = bool(row["is_hidden"]) if "is_hidden" in keys else False
    moved_from = row["moved_from_channel_id"] if "moved_from_channel_id" in keys else None
    return {"message_id": f"public:{row['id']}", "nickname": row["nickname"],
            "author_id": row["user_id"], "channel_id": channel_id, "content": row["content"],
            "created_at": row["created_at"], "edited_at": edited_at, "is_hidden": is_hidden,
            "moved_from_channel_id": moved_from, "reply": reply, "attachment": live,
            "attachments": attachments, "attachment_removed": bool(attachments and not live),
            "reactions": reactions if reactions is not None else [],
            "is_pinned": bool(is_pinned)}

def get_message_by_id(message_id: int, current_user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        if not row:
            return None
        attachments = _message_attachments(conn, [message_id]).get(message_id, [])
        reactions = get_reactions_for_messages(conn, "channel", [message_id], current_user_id).get(message_id, [])
        pinned = conn.execute(
            "SELECT 1 FROM pinned_messages WHERE conversation_type='channel' AND message_id=?",
            (message_id,)
        ).fetchone() is not None
        return _message_public(row, attachments, reactions, is_pinned=pinned)

def update_message_content(message_id: int, new_content: str, user_id: int, is_admin: bool = False) -> Optional[Dict[str, Any]]:
    now = utc_now()
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        if not row:
            return None
        if not is_admin and row["user_id"] != user_id:
            raise PermissionError("본인이 작성한 메시지만 수정할 수 있습니다.")
        conn.execute("UPDATE messages SET content=?, edited_at=? WHERE id=?",
                     (new_content, now, message_id))
        conn.commit()
    return get_message_by_id(message_id, user_id)

def set_message_hidden(message_id: int, is_hidden: bool) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        if not row:
            return None
        conn.execute("UPDATE messages SET is_hidden=? WHERE id=?",
                     (1 if is_hidden else 0, message_id))
        conn.commit()
    return get_message_by_id(message_id)

def move_message_channel(message_id: int, to_channel_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        if not row:
            return None
        from_channel_id = row["channel_id"] if "channel_id" in row.keys() else 1
        conn.execute("UPDATE messages SET channel_id=?, moved_from_channel_id=? WHERE id=?",
                     (to_channel_id, from_channel_id, message_id))
        conn.commit()
    return get_message_by_id(message_id)

def save_message(nickname: str, content: str, ip: str = "", reply: Optional[Dict[str,str]] = None,
                 attachment_id: Optional[str] = None, attachment_ids: Optional[List[str]] = None,
                 user_id: Optional[int] = None, channel_id: int = 1) -> Dict[str, Any]:
    selected = list(dict.fromkeys(attachment_ids or ([] if not attachment_id else [attachment_id])))
    now = utc_now()
    with get_connection() as conn:
        cur = conn.execute("""INSERT INTO messages
            (nickname,content,created_at,ip,reply_nickname,reply_content,attachment_id,user_id,channel_id)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (nickname, content, now, ip, reply.get("nickname","") if reply else None,
             reply.get("content","") if reply else None, selected[0] if selected else None,
             user_id, channel_id))
        message_id = cur.lastrowid
        if selected:
            marks = ",".join("?" for _ in selected)
            names = {r["id"]: r["original_name"] for r in conn.execute(
                f"SELECT id,original_name FROM attachments WHERE id IN ({marks})", selected)}
            conn.executemany("""INSERT INTO message_attachments
                (message_id,attachment_id,original_name,position) VALUES(?,?,?,?)""",
                [(message_id, item, names.get(item, "파일"), pos) for pos,item in enumerate(selected)])
        row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        items = _message_attachments(conn, [message_id])[message_id]
        conn.commit()
    result = _message_public(row, items, [])
    result["ip"] = ip
    return result

def get_recent_messages(limit: int = 100, before_id: Optional[int] = None,
                        channel_id: int = 1, current_user_id: Optional[int] = None) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        if before_id is None:
            rows = conn.execute("""SELECT * FROM
                (SELECT * FROM messages WHERE channel_id=? ORDER BY id DESC LIMIT ?) ORDER BY id ASC""",
                (channel_id, limit)).fetchall()
        else:
            rows = conn.execute("""SELECT * FROM
                (SELECT * FROM messages WHERE channel_id=? AND id<? ORDER BY id DESC LIMIT ?)
                ORDER BY id ASC""", (channel_id, before_id, limit)).fetchall()
        msg_ids = [row["id"] for row in rows]
        items = _message_attachments(conn, msg_ids)
        reactions = get_reactions_for_messages(conn, "channel", msg_ids, current_user_id)
        pinned_ids = {r[0] for r in conn.execute(
            "SELECT message_id FROM pinned_messages WHERE conversation_type='channel' AND conversation_id=?",
            (str(channel_id),)
        ).fetchall()}
    return [_message_public(row, items[row["id"]], reactions.get(row["id"], []), is_pinned=(row["id"] in pinned_ids)) for row in rows]

def search_conversation_history(user_id: int, query: str, *, is_admin: bool = False,
                                conversation_type: Optional[str] = None,
                                conversation_id: Optional[str] = None,
                                limit: int = 50) -> List[Dict[str, Any]]:
    """Search accessible message text and attachment filenames."""
    escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    needle = f"%{escaped}%"
    limit = max(1, min(int(limit), 100))
    results: List[Dict[str, Any]] = []
    with get_connection() as conn:
        if conversation_type in (None, "channel"):
            params: List[Any] = [needle, needle]
            clauses = ["(m.content LIKE ? ESCAPE '\\' COLLATE NOCASE OR EXISTS (SELECT 1 FROM message_attachments ma JOIN attachments a ON a.id=ma.attachment_id WHERE ma.message_id=m.id AND ma.original_name LIKE ? ESCAPE '\\' COLLATE NOCASE))"]
            if not is_admin:
                clauses.append("m.is_hidden=0")
            if conversation_type == "channel":
                clauses.append("m.channel_id=?")
                params.append(int(conversation_id or 0))
            params.append(limit)
            rows = conn.execute(f"""SELECT m.*, c.display_name AS channel_display_name
                FROM messages m JOIN channels c ON c.id=m.channel_id
                WHERE {' AND '.join(clauses)} ORDER BY m.id DESC LIMIT ?""", params).fetchall()
            attachment_map = _message_attachments(conn, [row["id"] for row in rows])
            for row in rows:
                results.append({"message_type": "channel", "message_id": f"public:{row['id']}",
                    "conversation_id": str(row["channel_id"]), "conversation_name": row["channel_display_name"],
                    "author": row["nickname"], "content": row["content"], "created_at": row["created_at"],
                    "attachments": attachment_map.get(row["id"], [])})

        if conversation_type in (None, "dm"):
            params = [user_id, user_id, needle, needle]
            clauses = ["(dm.sender_user_id=? OR dm.recipient_user_id=?)",
                       "(dm.content LIKE ? ESCAPE '\\' COLLATE NOCASE OR EXISTS (SELECT 1 FROM direct_message_attachments dma JOIN attachments a ON a.id=dma.attachment_id WHERE dma.direct_message_id=dm.id AND dma.original_name LIKE ? ESCAPE '\\' COLLATE NOCASE))"]
            if conversation_type == "dm":
                partner_id = int(conversation_id or 0)
                clauses.append("((dm.sender_user_id=? AND dm.recipient_user_id=?) OR (dm.sender_user_id=? AND dm.recipient_user_id=?))")
                params.extend([user_id, partner_id, partner_id, user_id])
            params.append(limit)
            rows = conn.execute(f"SELECT dm.* FROM direct_messages dm WHERE {' AND '.join(clauses)} ORDER BY dm.id DESC LIMIT ?", params).fetchall()
            attachment_map = _direct_message_attachments(conn, [row["id"] for row in rows])
            for row in rows:
                partner = row["recipient_nickname"] if row["sender_user_id"] == user_id else row["sender_nickname"]
                results.append({"message_type": "dm", "message_id": f"dm:{row['id']}",
                    "conversation_id": partner, "conversation_name": partner,
                    "author": row["sender_nickname"], "content": row["content"], "created_at": row["created_at"],
                    "attachments": attachment_map.get(row["id"], [])})
    results.sort(key=lambda item: (item["created_at"], item["message_id"]), reverse=True)
    return results[:limit]

def _direct_message_attachments(conn: sqlite3.Connection,
                                ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
    result = {message_id: [] for message_id in ids}
    if not ids:
        return result
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(f"""SELECT dma.*, a.id AS live_id, a.size, a.sha256,
        a.content_type, a.previewable, a.uploader_user_id
        FROM direct_message_attachments dma
        LEFT JOIN attachments a ON a.id=dma.attachment_id
        WHERE dma.direct_message_id IN ({marks})
        ORDER BY dma.direct_message_id, dma.position""", ids).fetchall()
    for row in rows:
        if row["live_id"]:
            item = {"id": row["attachment_id"], "name": row["original_name"],
                    "size": row["size"], "sha256": row["sha256"],
                    "content_type": row["content_type"],
                    "previewable": bool(row["previewable"]),
                    "url": f"/api/files/{row['attachment_id']}", "removed": False,
                    "owner_id": row["uploader_user_id"]}
        else:
            item = {"id": row["attachment_id"], "name": row["original_name"],
                    "removed": True}
        result[row["direct_message_id"]].append(item)
    return result

def _direct_message_public(row: sqlite3.Row,
                           attachments: List[Dict[str, Any]],
                           reactions: Optional[List[Dict[str, Any]]] = None,
                           is_pinned: bool = False) -> Dict[str, Any]:
    reply = None
    if row["reply_nickname"] or row["reply_content"]:
        reply = {"nickname": row["reply_nickname"] or "",
                 "content": row["reply_content"] or ""}
    live = next((item for item in attachments if not item.get("removed")), None)
    keys = row.keys() if hasattr(row, 'keys') else row
    edited_at = row["edited_at"] if "edited_at" in keys else None
    return {"message_id": f"dm:{row['id']}", "from_nick": row["sender_nickname"],
            "from_user_id": row["sender_user_id"], "to_nick": row["recipient_nickname"],
            "to_user_id": row["recipient_user_id"], "content": row["content"],
            "created_at": row["created_at"], "edited_at": edited_at, "reply": reply, "attachment": live,
            "attachments": attachments,
            "attachment_removed": bool(attachments and not live),
            "reactions": reactions if reactions is not None else [],
            "is_pinned": bool(is_pinned)}

def get_direct_message_by_id(dm_id: int, current_user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM direct_messages WHERE id=?", (dm_id,)).fetchone()
        if not row:
            return None
        items = _direct_message_attachments(conn, [dm_id]).get(dm_id, [])
        reactions = get_reactions_for_messages(conn, "dm", [dm_id], current_user_id).get(dm_id, [])
        pinned = conn.execute(
            "SELECT 1 FROM pinned_messages WHERE conversation_type='dm' AND message_id=?",
            (dm_id,)
        ).fetchone() is not None
        return _direct_message_public(row, items, reactions, is_pinned=pinned)

def update_direct_message_content(dm_id: int, new_content: str, user_id: int) -> Optional[Dict[str, Any]]:
    now = utc_now()
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM direct_messages WHERE id=?", (dm_id,)).fetchone()
        if not row:
            return None
        if row["sender_user_id"] != user_id:
            raise PermissionError("본인이 작성한 1:1 메시지만 수정할 수 있습니다.")
        conn.execute("UPDATE direct_messages SET content=?, edited_at=? WHERE id=?",
                     (new_content, now, dm_id))
        conn.commit()
    return get_direct_message_by_id(dm_id, user_id)

def save_direct_message(sender: Dict[str, Any], recipient: Dict[str, Any], content: str,
                        reply: Optional[Dict[str, str]] = None,
                        attachment_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    selected = list(dict.fromkeys(attachment_ids or []))
    with get_connection() as conn:
        cur = conn.execute("""INSERT INTO direct_messages
            (sender_user_id,recipient_user_id,sender_nickname,recipient_nickname,content,
             created_at,reply_nickname,reply_content) VALUES(?,?,?,?,?,?,?,?)""",
            (sender["id"], recipient["id"], sender["username"], recipient["username"],
             content, utc_now(), reply.get("nickname", "") if reply else None,
             reply.get("content", "") if reply else None))
        message_id = cur.lastrowid
        if selected:
            marks = ",".join("?" for _ in selected)
            names = {row["id"]: row["original_name"] for row in conn.execute(
                f"SELECT id,original_name FROM attachments WHERE id IN ({marks})", selected)}
            conn.executemany("""INSERT INTO direct_message_attachments
                (direct_message_id,attachment_id,original_name,position) VALUES(?,?,?,?)""",
                [(message_id, item, names.get(item, "파일"), position)
                 for position, item in enumerate(selected)])
        row = conn.execute("SELECT * FROM direct_messages WHERE id=?", (message_id,)).fetchone()
        items = _direct_message_attachments(conn, [message_id])[message_id]
        conn.commit()
    return _direct_message_public(row, items, [])

def get_recent_direct_messages(user_id: int, limit: int = 30) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute("""SELECT * FROM (
            SELECT dm.*, ROW_NUMBER() OVER (
                PARTITION BY CASE WHEN sender_user_id=? THEN recipient_user_id ELSE sender_user_id END
                ORDER BY id DESC
            ) AS conversation_row
            FROM direct_messages dm
            WHERE sender_user_id=? OR recipient_user_id=?
        ) WHERE conversation_row<=? ORDER BY id ASC""",
            (user_id, user_id, user_id, limit)).fetchall()
        msg_ids = [row["id"] for row in rows]
        items = _direct_message_attachments(conn, msg_ids)
        reactions = get_reactions_for_messages(conn, "dm", msg_ids, user_id)
    return [_direct_message_public(row, items[row["id"]], reactions.get(row["id"], [])) for row in rows]

def get_direct_messages_between(user_id: int, partner_user_id: int, limit: int = 50,
                                before_id: Optional[int] = None) -> List[Dict[str, Any]]:
    params: List[Any] = [user_id, partner_user_id, partner_user_id, user_id]
    before_clause = ""
    if before_id is not None:
        before_clause = "AND id<?"
        params.append(before_id)
    params.append(limit)
    with get_connection() as conn:
        rows = conn.execute(f"""SELECT * FROM (
            SELECT * FROM direct_messages WHERE
            ((sender_user_id=? AND recipient_user_id=?) OR
             (sender_user_id=? AND recipient_user_id=?))
            {before_clause} ORDER BY id DESC LIMIT ?
        ) ORDER BY id ASC""", params).fetchall()
        msg_ids = [row["id"] for row in rows]
        items = _direct_message_attachments(conn, msg_ids)
        reactions = get_reactions_for_messages(conn, "dm", msg_ids, user_id)
        norm_id = normalize_dm_conversation_id(user_id, partner_user_id)
        pinned_ids = {r[0] for r in conn.execute(
            "SELECT message_id FROM pinned_messages WHERE conversation_type='dm' AND conversation_id=?",
            (norm_id,)
        ).fetchall()}
    return [_direct_message_public(row, items[row["id"]], reactions.get(row["id"], []), is_pinned=(row["id"] in pinned_ids)) for row in rows]

def attachment_is_visible_to_user(attachment_id: str, user_id: int) -> bool:
    with get_connection() as conn:
        dm_rows = conn.execute("""SELECT dm.sender_user_id, dm.recipient_user_id
            FROM direct_message_attachments dma
            JOIN direct_messages dm ON dm.id=dma.direct_message_id
            WHERE dma.attachment_id=?""", (attachment_id,)).fetchall()
    if not dm_rows:
        return True
    return any(user_id in {row["sender_user_id"], row["recipient_user_id"]}
               for row in dm_rows)

def save_attachment(metadata: Dict[str, Any]) -> Dict[str, Any]:
    with get_connection() as conn:
        conn.execute("""INSERT INTO attachments
            (id,original_name,stored_name,size,sha256,content_type,previewable,
             uploader_nickname,ip,owner_token_hash,created_at,claimed,uploader_user_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?)""",
            (metadata["id"], metadata["original_name"], metadata["stored_name"], metadata["size"],
             metadata["sha256"], metadata["content_type"], int(metadata["previewable"]),
             metadata["uploader_nickname"], metadata["ip"], "", metadata.get("created_at", utc_now()),
             metadata["uploader_user_id"]))
        conn.commit()
    return get_attachment_public(metadata["id"])

def get_attachment_record(attachment_id: str) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()
    return dict(row) if row else None

def get_attachment_public(attachment_id: str) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()
    return _attachment_public(row)

def claim_attachments(attachment_ids: List[str], user_id: int) -> Optional[List[Dict[str, Any]]]:
    selected = list(dict.fromkeys(attachment_ids))
    if not selected:
        return []
    marks = ",".join("?" for _ in selected)
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(f"""SELECT * FROM attachments WHERE id IN ({marks})
            AND uploader_user_id=? AND claimed=0""", [*selected, user_id]).fetchall()
        by_id = {row["id"]: row for row in rows}
        if len(rows) != len(selected) or any(item not in by_id for item in selected):
            conn.rollback()
            return None
        conn.executemany("UPDATE attachments SET claimed=1 WHERE id=? AND claimed=0",
                         [(item,) for item in selected])
        conn.commit()
    return [_attachment_public(by_id[item]) for item in selected]

def delete_owned_attachment(attachment_id: str, user_id: int, is_admin: bool = False) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        if is_admin:
            row = conn.execute("SELECT stored_name,claimed FROM attachments WHERE id=?",
                               (attachment_id,)).fetchone()
        else:
            row = conn.execute("""SELECT stored_name,claimed FROM attachments
                WHERE id=? AND uploader_user_id=?""", (attachment_id, user_id)).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM attachments WHERE id=?", (attachment_id,))
        conn.commit()
    return dict(row)

def get_upload_usage(user_id: int) -> Tuple[int, int]:
    with get_connection() as conn:
        mine = conn.execute("SELECT COALESCE(SUM(size),0) FROM attachments WHERE uploader_user_id=?",
                            (user_id,)).fetchone()[0]
        total = conn.execute("SELECT COALESCE(SUM(size),0) FROM attachments").fetchone()[0]
    return int(mine), int(total)

def get_storage_status() -> Dict[str, int]:
    db_bytes = sum(path.stat().st_size for path in (
        DB_PATH, Path(str(DB_PATH)+"-wal"), Path(str(DB_PATH)+"-shm")) if path.exists())
    with get_connection() as conn:
        attachment_bytes = int(conn.execute("SELECT COALESCE(SUM(size),0) FROM attachments").fetchone()[0])
    return {"database_bytes": db_bytes, "database_limit_bytes": DB_MAX_BYTES,
            "attachment_bytes": attachment_bytes}

def get_user_conversation_states(user_id: int) -> Dict[str, Dict[str, Any]]:
    """Returns mapping of 'type:id' -> {last_read_message_id, muted, updated_at}."""
    with get_connection() as conn:
        rows = conn.execute("""SELECT conversation_type, conversation_id, last_read_message_id, muted, updated_at
            FROM user_conversation_state WHERE user_id = ?""", (user_id,)).fetchall()
        states = {}
        for row in rows:
            key = f"{row[0]}:{row[1]}"
            states[key] = {
                "conversation_type": row[0],
                "conversation_id": row[1],
                "last_read_message_id": int(row[2]),
                "muted": bool(row[3]),
                "updated_at": row[4]
            }
        return states

def update_user_read_state(user_id: int, conv_type: str, conv_id: str, last_read_id: int) -> Dict[str, Any]:
    """Updates last_read_message_id for a conversation, advancing it forward."""
    conv_id = str(conv_id)
    last_read_id = int(last_read_id)
    now = utc_now()
    with get_connection() as conn:
        row = conn.execute("""SELECT last_read_message_id, muted FROM user_conversation_state
            WHERE user_id = ? AND conversation_type = ? AND conversation_id = ?""",
            (user_id, conv_type, conv_id)).fetchone()
        if row:
            curr_last_read = int(row[0])
            muted = int(row[1])
            new_last_read = max(curr_last_read, last_read_id)
            conn.execute("""UPDATE user_conversation_state
                SET last_read_message_id = ?, updated_at = ?
                WHERE user_id = ? AND conversation_type = ? AND conversation_id = ?""",
                (new_last_read, now, user_id, conv_type, conv_id))
            conn.commit()
            return {
                "conversation_type": conv_type,
                "conversation_id": conv_id,
                "last_read_message_id": new_last_read,
                "muted": bool(muted),
                "updated_at": now
            }
        else:
            conn.execute("""INSERT INTO user_conversation_state
                (user_id, conversation_type, conversation_id, last_read_message_id, muted, updated_at)
                VALUES (?, ?, ?, ?, 0, ?)""",
                (user_id, conv_type, conv_id, last_read_id, now))
            conn.commit()
            return {
                "conversation_type": conv_type,
                "conversation_id": conv_id,
                "last_read_message_id": last_read_id,
                "muted": False,
                "updated_at": now
            }

def set_conversation_muted(user_id: int, conv_type: str, conv_id: str, muted: bool) -> Dict[str, Any]:
    """Sets muted flag for a conversation."""
    conv_id = str(conv_id)
    muted_int = 1 if muted else 0
    now = utc_now()
    with get_connection() as conn:
        row = conn.execute("""SELECT last_read_message_id FROM user_conversation_state
            WHERE user_id = ? AND conversation_type = ? AND conversation_id = ?""",
            (user_id, conv_type, conv_id)).fetchone()
        if row:
            last_read_id = int(row[0])
            conn.execute("""UPDATE user_conversation_state
                SET muted = ?, updated_at = ?
                WHERE user_id = ? AND conversation_type = ? AND conversation_id = ?""",
                (muted_int, now, user_id, conv_type, conv_id))
        else:
            last_read_id = 0
            conn.execute("""INSERT INTO user_conversation_state
                (user_id, conversation_type, conversation_id, last_read_message_id, muted, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (user_id, conv_type, conv_id, 0, muted_int, now))
        conn.commit()
        return {
            "conversation_type": conv_type,
            "conversation_id": conv_id,
            "last_read_message_id": last_read_id,
            "muted": bool(muted),
            "updated_at": now
        }

def get_user_unread_counts(user_id: int) -> Dict[str, int]:
    """Calculates unread message count for each channel and DM conversation of user."""
    unread_counts = {}
    with get_connection() as conn:
        states = {}
        for r in conn.execute("""SELECT conversation_type, conversation_id, last_read_message_id
            FROM user_conversation_state WHERE user_id = ?""", (user_id,)).fetchall():
            states[f"{r[0]}:{r[1]}"] = int(r[2])
        
        # Channels (exclude messages authored by user_id)
        chans = conn.execute("SELECT id FROM channels WHERE archived = 0").fetchall()
        for chan in chans:
            cid = chan[0]
            key = f"channel:{cid}"
            last_read = states.get(key, 0)
            cnt = conn.execute("""SELECT COUNT(*) FROM messages
                WHERE channel_id = ? AND id > ? AND (user_id IS NULL OR user_id != ?)""",
                (cid, last_read, user_id)).fetchone()[0]
            unread_counts[key] = cnt
            
        # DMs (conversations where this user is recipient and message is unread from sender_id)
        dm_rows = conn.execute("""SELECT sender_user_id, id FROM direct_messages
            WHERE recipient_user_id = ? AND sender_user_id != ? ORDER BY id ASC""", (user_id, user_id)).fetchall()
        
        # Group by partner (sender)
        for sender_id, msg_id in dm_rows:
            key = f"dm:{sender_id}"
            last_read = states.get(key, 0)
            if msg_id > last_read:
                unread_counts[key] = unread_counts.get(key, 0) + 1
        
    return unread_counts


# --- Message Pins (Iteration 5) ---

def normalize_dm_conversation_id(user_id_1: int, user_id_2: int) -> str:
    """Normalizes DM conversation key by sorting user IDs (e.g. '1:2')."""
    return f"{min(user_id_1, user_id_2)}:{max(user_id_1, user_id_2)}"

def pin_message(
    conversation_type: str,
    conversation_id: str,
    message_id: int,
    pinned_by_user_id: int,
) -> Dict[str, Any]:
    """Pins a channel or DM message. Returns pinned message details."""
    now = utc_now()
    with get_connection() as conn:
        if conversation_type == "channel":
            chan_id = int(conversation_id)
            row = conn.execute("SELECT * FROM messages WHERE id = ? AND channel_id = ? AND is_hidden = 0", (message_id, chan_id)).fetchone()
            if not row:
                raise ValueError("고정할 메시지를 찾을 수 없습니다.")
        elif conversation_type == "dm":
            row = conn.execute("SELECT * FROM direct_messages WHERE id = ?", (message_id,)).fetchone()
            if not row:
                raise ValueError("고정할 DM 메시지를 찾을 수 없습니다.")
            if row["sender_user_id"] != pinned_by_user_id and row["recipient_user_id"] != pinned_by_user_id:
                raise PermissionError("본인이 참여한 1:1 대화의 메시지만 고정할 수 있습니다.")
            norm_id = normalize_dm_conversation_id(row["sender_user_id"], row["recipient_user_id"])
            conversation_id = norm_id
        else:
            raise ValueError(f"Invalid conversation_type: {conversation_type}")

        conn.execute("""
            INSERT OR REPLACE INTO pinned_messages
            (conversation_type, conversation_id, message_id, pinned_by_user_id, pinned_at)
            VALUES (?, ?, ?, ?, ?)
        """, (conversation_type, str(conversation_id), message_id, pinned_by_user_id, now))
        conn.commit()

        p_user = conn.execute("SELECT id, username, display_name FROM users WHERE id = ?", (pinned_by_user_id,)).fetchone()
        pinned_by_info = {
            "id": p_user["id"],
            "username": p_user["username"],
            "display_name": p_user["display_name"] or p_user["username"],
        } if p_user else {"id": pinned_by_user_id, "username": "unknown", "display_name": "unknown"}

        if conversation_type == "channel":
            atts = _message_attachments(conn, [message_id]).get(message_id, [])
            reactions = get_reactions_for_messages(conn, "channel", [message_id], pinned_by_user_id).get(message_id, [])
            msg_obj = _message_public(row, atts, reactions, is_pinned=True)
        else:
            atts = _direct_message_attachments(conn, [message_id]).get(message_id, [])
            reactions = get_reactions_for_messages(conn, "dm", [message_id], pinned_by_user_id).get(message_id, [])
            msg_obj = _direct_message_public(row, atts, reactions, is_pinned=True)

        return {
            "conversation_type": conversation_type,
            "conversation_id": str(conversation_id),
            "message_id": message_id,
            "pinned_at": now,
            "pinned_by": pinned_by_info,
            "message": msg_obj,
        }

def unpin_message(
    conversation_type: str,
    conversation_id: str,
    message_id: int,
    user_id: int,
) -> bool:
    """Unpins a channel or DM message."""
    with get_connection() as conn:
        if conversation_type == "dm":
            row = conn.execute("SELECT * FROM direct_messages WHERE id = ?", (message_id,)).fetchone()
            if not row:
                return False
            if row["sender_user_id"] != user_id and row["recipient_user_id"] != user_id:
                raise PermissionError("본인이 참여한 1:1 대화의 메시지만 고정 해제할 수 있습니다.")
            conversation_id = normalize_dm_conversation_id(row["sender_user_id"], row["recipient_user_id"])

        cur = conn.execute("""
            DELETE FROM pinned_messages
            WHERE conversation_type = ? AND conversation_id = ? AND message_id = ?
        """, (conversation_type, str(conversation_id), message_id))
        conn.commit()
        return cur.rowcount > 0

def get_pinned_messages(
    conversation_type: str,
    conversation_id: str,
    current_user_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Lists all pinned messages in a channel or DM."""
    results = []
    with get_connection() as conn:
        if conversation_type == "channel":
            rows = conn.execute("""
                SELECT pm.message_id, pm.pinned_at, pm.pinned_by_user_id,
                       u.username as p_username, u.display_name as p_display_name,
                       m.*
                FROM pinned_messages pm
                JOIN messages m ON pm.message_id = m.id
                LEFT JOIN users u ON pm.pinned_by_user_id = u.id
                WHERE pm.conversation_type = 'channel' AND pm.conversation_id = ? AND m.is_hidden = 0
                ORDER BY pm.pinned_at DESC
            """, (str(conversation_id),)).fetchall()

            if not rows:
                return []

            msg_ids = [r["message_id"] for r in rows]
            atts_map = _message_attachments(conn, msg_ids)
            reactions_map = get_reactions_for_messages(conn, "channel", msg_ids, current_user_id)

            for r in rows:
                mid = r["message_id"]
                msg_atts = atts_map.get(mid, [])
                msg_reactions = reactions_map.get(mid, [])
                msg_obj = _message_public(r, msg_atts, msg_reactions, is_pinned=True)
                pinned_by = {
                    "id": r["pinned_by_user_id"],
                    "username": r["p_username"] or "unknown",
                    "display_name": r["p_display_name"] or r["p_username"] or "unknown",
                }
                results.append({
                    "conversation_type": "channel",
                    "conversation_id": str(conversation_id),
                    "message_id": mid,
                    "pinned_at": r["pinned_at"],
                    "pinned_by": pinned_by,
                    "message": msg_obj,
                })
        elif conversation_type == "dm":
            rows = conn.execute("""
                SELECT pm.message_id, pm.pinned_at, pm.pinned_by_user_id,
                       u.username as p_username, u.display_name as p_display_name,
                       dm.*
                FROM pinned_messages pm
                JOIN direct_messages dm ON pm.message_id = dm.id
                LEFT JOIN users u ON pm.pinned_by_user_id = u.id
                WHERE pm.conversation_type = 'dm' AND pm.conversation_id = ?
                ORDER BY pm.pinned_at DESC
            """, (str(conversation_id),)).fetchall()

            if not rows:
                return []

            msg_ids = [r["message_id"] for r in rows]
            atts_map = _direct_message_attachments(conn, msg_ids)
            reactions_map = get_reactions_for_messages(conn, "dm", msg_ids, current_user_id)

            for r in rows:
                mid = r["message_id"]
                msg_atts = atts_map.get(mid, [])
                msg_reactions = reactions_map.get(mid, [])
                msg_obj = _direct_message_public(r, msg_atts, msg_reactions, is_pinned=True)
                pinned_by = {
                    "id": r["pinned_by_user_id"],
                    "username": r["p_username"] or "unknown",
                    "display_name": r["p_display_name"] or r["p_username"] or "unknown",
                }
                results.append({
                    "conversation_type": "dm",
                    "conversation_id": str(conversation_id),
                    "message_id": mid,
                    "pinned_at": r["pinned_at"],
                    "pinned_by": pinned_by,
                    "message": msg_obj,
                })
    return results

def get_pinned_message_ids(
    conversation_type: str,
    conversation_id: str,
) -> Set[int]:
    """Returns set of pinned message IDs for a conversation."""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT message_id FROM pinned_messages
            WHERE conversation_type = ? AND conversation_id = ?
        """, (conversation_type, str(conversation_id))).fetchall()
        return {r[0] for r in rows}


# ==========================================
# Educational Quizzes & Leaderboard Functions
# ==========================================

DEFAULT_SAMPLE_QUIZZES = [
    {
        "category": "PLC",
        "difficulty": "medium",
        "question_type": "ladder_input",
        "question": "자기유지(Self-holding) 회로에서 출력 코일 Y0이 ON된 후 기동 스위치 X0을 떼어도 계속 전원이 공급되도록 X0과 병렬(OR)로 연결해야 하는 접점 디바이스 번호는 무엇인가?",
        "image_filename": "",
        "options": None,
        "correct_answers": ["Y0", "Y00", "Y0 a접점", "Y0 A접점", "Y000", "LD Y0", "OR Y0"],
        "hint": "출력 코일과 동일한 디바이스 번호의 a접점을 병렬로 연결합니다.",
        "explanation": "기동 스위치(X0)와 출력 릴레이의 a접점(Y0)을 병렬 연결하면, 스위치가 복귀해도 출력 Y0의 a접점이 닫혀 있어 전원이 유지됩니다.",
        "source_ref": "PLC 시퀀스 실습 4강 - 자기유지 회로 p.12"
    },
    {
        "category": "PLC",
        "difficulty": "easy",
        "question_type": "multiple_choice",
        "question": "PLC 래더 다이어그램에서 두 개 이상의 a접점을 직렬로 연결할 때 사용하는 기본 명령어는 무엇인가?",
        "options": ["1. AND", "2. OR", "3. OUT", "4. SET"],
        "correct_answers": ["1", "1번", "AND", "1. AND"],
        "hint": "직렬 접속에는 논리곱(Logical AND) 연산 명령어를 사용합니다.",
        "explanation": "직렬 접속에는 AND(a접점 직렬) 또는 ANI/AND NOT(b접점 직렬) 명령어를 사용합니다.",
        "source_ref": "PLC 기초 명령어 편람 p.5"
    },
    {
        "category": "PLC",
        "difficulty": "medium",
        "question_type": "short_answer",
        "question": "기본 단위가 100ms인 PLC 타이머(Timer)에서 3초를 지연 동작시키기 위해 입력해야 하는 설정값(K값)은 얼마인가?",
        "image_filename": "",
        "options": None,
        "correct_answers": ["30", "K30", "K 30"],
        "hint": "100ms는 0.1초입니다. 목표 시간(3초)을 0.1초로 나누어 보세요.",
        "explanation": "100ms(0.1초) 단위 타이머에서 3초는 3.0 / 0.1 = 30이므로 K30을 설정합니다.",
        "source_ref": "PLC 타이머/카운터 응용 p.23"
    },
    {
        "category": "전기",
        "difficulty": "medium",
        "question_type": "multiple_choice",
        "question": "3상 유도전동기의 회전 방향을 역회전으로 바꾸기 위한 가장 올바른 결선 변경 방법은?",
        "options": ["1. 3상 중 임의의 2선의 접속을 서로 바꾼다", "2. 3선의 접속을 모두 일제히 바꾼다", "3. 접지선(E)의 위치를 전원선으로 바꾼다", "4. 공급 전압을 2배로 승압한다"],
        "correct_answers": ["1", "1번", "1. 3상 중 임의의 2선의 접속을 서로 바꾼다"],
        "hint": "3개 선 중 임의의 2개 선 위치를 맞바꾸면 회전자계 방향이 반전됩니다.",
        "explanation": "3상 교류 전동기(R, S, T)는 3상 중 임의의 두 선의 접속을 서로 바꾸면 회전자계의 방향이 반대가 되어 전동기가 역회전합니다.",
        "source_ref": "Q-Net 전기 분야 출제범위 참고 재작성"
    },
    {
        "category": "PLC",
        "difficulty": "hard",
        "question_type": "short_answer",
        "question": "정회전 코일과 역회전 코일이 동시에 투입되어 선간 단락 사고가 발생하는 것을 막기 위해, 상대방 코일 전단에 자신의 b접점을 직렬 연결하는 제어 회로의 명칭은?",
        "image_filename": "",
        "options": None,
        "correct_answers": ["인터록", "인터록 회로", "인터록회로", "INTERLOCK", "Interlock"],
        "hint": "상대방의 동작을 서로 잠근다는 의미의 영단어(Inter-lock)입니다.",
        "explanation": "두 개의 상반된 동작이 동시에 일어나는 것을 방지하기 위해 상대 회로를 잠그는 회로를 인터록(Interlock) 회로라고 합니다.",
        "source_ref": "시퀀스 제어 핵심이론 p.31"
    },
    {
        "category": "전자",
        "difficulty": "easy",
        "question_type": "multiple_choice",
        "question": "2진수 10110(2)을 10진수로 올바르게 변환한 값은?",
        "options": ["1. 18", "2. 20", "3. 22", "4. 24"],
        "correct_answers": ["3", "3번", "22", "3. 22"],
        "hint": "각 자리수의 가중치(16, 8, 4, 2, 1) 중 1인 자리만 더해보세요: 16 + 4 + 2",
        "explanation": "16*1 + 8*0 + 4*1 + 2*1 + 1*0 = 16 + 4 + 2 = 22 입니다.",
        "source_ref": "Q-Net 전자·디지털공학 출제범위 참고 재작성"
    },
    {
        "category": "PLC",
        "difficulty": "medium",
        "question_type": "ladder_input",
        "question": "미쓰비시(MELSEC) PLC 래더 프로그래밍에서 모선(Bus bar)에서 b접점을 시작할 때 사용하는 니모닉(Mnemonic) 명령어는 무엇인가?",
        "image_filename": "",
        "options": None,
        "correct_answers": ["LDI", "LD NOT", "LDNOT", "LD I"],
        "hint": "Load Inverse의 약자 3글자입니다.",
        "explanation": "모선에서 a접점 시작은 LD(Load), b접점 시작은 LDI(Load Inverse) 명령어를 사용합니다.",
        "source_ref": "MELSEC 명령어 일람표"
    },
    {
        "category": "자동화설비",
        "difficulty": "medium",
        "question_type": "multiple_choice",
        "question": "공압 회로에서 방향제어밸브의 주된 역할은 무엇인가?",
        "options": ["1. 압축공기의 흐름 방향을 전환한다", "2. 전압을 정류한다", "3. 회전수를 측정한다", "4. 절연저항을 높인다"],
        "correct_answers": ["1", "1번", "압축공기의 흐름 방향을 전환한다"],
        "hint": "실린더의 전진·후진을 제어합니다.",
        "explanation": "방향제어밸브는 공급·배기 경로를 바꿔 공압 액추에이터의 움직임을 제어합니다.",
        "source_ref": "Q-Net 자동화설비산업기사 공개문제(공압) 범위 참고 재작성"
    },
    {
        "category": "자동화설비",
        "difficulty": "hard",
        "question_type": "multiple_choice",
        "question": "비상정지 회로에서 단선이 발생해도 안전 정지가 되도록 일반적으로 사용하는 접점은?",
        "options": ["1. 정상 시 닫힌 b접점", "2. 정상 시 열린 a접점", "3. 아날로그 출력", "4. 타이머 출력만 사용"],
        "correct_answers": ["1", "1번", "정상 시 닫힌 b접점"],
        "hint": "정상 상태에서 닫혀 있어야 단선 시 회로가 열립니다.",
        "explanation": "정상 시 닫힌 접점을 직렬로 사용하면 비상 입력이나 단선 때 회로가 열려 안전 정지합니다.",
        "source_ref": "Q-Net 자동화설비산업기사 공개문제(안전제어) 범위 참고 재작성"
    }
]

def _position_mc(code: str, question: str, answer: str, distractors: List[str], explanation: str) -> Dict[str, Any]:
    values = [answer, *distractors[:3]]
    offset = int(code) % 4
    options = values[offset:] + values[:offset]
    correct_no = options.index(answer) + 1
    return {
        "category": "PLC",
        "difficulty": "easy",
        "question_type": "multiple_choice",
        "question": question,
        "options": [f"{i}. {value}" for i, value in enumerate(options, 1)],
        "correct_answers": [str(correct_no), answer, f"{correct_no}. {answer}"],
        "hint": f"위치결정 모듈 오류 코드 {code}의 명칭을 확인하세요.",
        "explanation": explanation,
        "source_ref": "위치결정모듈 사용자 매뉴얼 MR-J2S/QD75, 부록 오류 코드 및 버퍼 메모리 표 참고 재작성",
    }

POSITION_MODULE_SAMPLE_QUIZZES = [
    _position_mc("507", "위치결정 모듈 오류 코드 507의 의미는?", "소프트웨어 스트로크 리미트(+)", ["소프트웨어 스트로크 리미트(-)", "지령속도 없음", "원점복귀 방식 에러"], "정방향(+) 소프트웨어 스트로크 리미트 초과를 나타냅니다."),
    _position_mc("508", "오류 코드 508이 발생했을 때 가장 가까운 설명은?", "소프트웨어 스트로크 리미트(-)", ["소프트웨어 스트로크 리미트(+)", "PLC CPU 에러", "반경범위 외"], "역방향(-) 소프트웨어 스트로크 리미트 초과입니다."),
    _position_mc("502", "오류 코드 502는 무엇을 의미하는가?", "데이터 No. 부정", ["축 Busy", "JOG 속도 제한값 에러", "원점 어드레스 설정 에러"], "위치결정 데이터 번호가 허용 범위를 벗어난 경우입니다."),
    _position_mc("503", "오류 코드 503의 원인은?", "지령속도 없음", ["직선 이동량 범위 외", "중심점 설정 에러", "M코드 On 신호 On기동"], "위치결정 기동에 필요한 지령속도가 설정되지 않은 상태입니다."),
    _position_mc("504", "오류 코드 504가 나타내는 것은?", "직선 이동량 범위 외", ["원호보간 불가", "단위그룹 불일치", "축 에러 리셋"], "직선 이동량이 모듈에서 허용하는 범위를 벗어난 경우입니다."),
    _position_mc("514", "오류 코드 514의 의미는?", "현재값 변경범위 외", ["현재값 변경불가", "속도 제한값 범위 외", "조건 데이터 에러"], "현재값 변경에 사용한 어드레스가 허용 범위를 벗어난 경우입니다."),
    _position_mc("518", "오류 코드 518은 어떤 설정과 관련되는가?", "운전패턴 범위 외", ["회전방향 설정 에러", "원점복귀 속도 에러", "보간모드 에러"], "설정한 운전 패턴 값이 허용 범위 밖일 때 발생합니다."),
    _position_mc("519", "오류 코드 519는 어떤 상태에서 발생하는가?", "상대축 Busy 보간", ["PLC Ready Off", "플래시 ROM 쓰기 에러", "반경범위 외"], "상대축이 운전 중인데 보간 기동을 지령한 경우입니다."),
    _position_mc("520", "오류 코드 520의 설명으로 옳은 것은?", "단위그룹 불일치", ["데이터 No. 부정", "원점복귀 방향 에러", "M코드 On 타이밍 에러"], "보간 속도 지정 방법과 합성 속도 등의 단위 그룹 설정이 맞지 않습니다."),
    _position_mc("521", "오류 코드 521은 무엇을 뜻하는가?", "보간기술 명령부정", ["지령속도 설정 에러", "PLC Ready Off 기동", "현재값 갱신요구 에러"], "보간 제어에 사용한 명령 조합이 올바르지 않은 경우입니다."),
    _position_mc("522", "오류 코드 522의 의미는?", "지령속도 설정 에러", ["보간모드 에러", "직선 이동량 범위 외", "JOG가속시간 선택 에러"], "보간 또는 위치결정에 지정한 지령속도 설정이 올바르지 않습니다."),
    _position_mc("523", "오류 코드 523은 어떤 에러인가?", "보간모드 에러", ["단위 설정 범위 외", "축 에러 번호 부정", "원호보간 오차 허용범위 외"], "보간 모드와 관련된 설정 또는 운전 조건이 맞지 않습니다."),
    _position_mc("530", "오류 코드 530의 의미는?", "어드레스 범위 외", ["속도 제한값 범위 외", "원점복귀 리트라이 에러", "데이터 No. 부정"], "이동 목표 어드레스가 허용 범위를 벗어난 경우입니다."),
    _position_mc("536", "오류 코드 536이 발생하는 조건은?", "M코드 On 신호 On기동", ["PLC Ready Off 기동", "준비완료 Off 기동", "동시기동 불가"], "M코드 On 신호가 이미 On인 상태에서 위치결정을 기동한 경우입니다."),
    _position_mc("538", "오류 코드 538의 의미는?", "준비완료 Off 기동", ["PLC Ready Off 기동", "M코드 On 신호 On기동", "원점복귀 방식 에러"], "QD75의 준비완료 신호가 Off인 상태에서 기동한 경우입니다."),
    _position_mc("1517", "1축 버퍼 메모리 K1517이 나타내는 데이터는?", "인칭 이동량", ["JOG 속도", "JOG 가속시간", "축 에러 번호"], "K1517(축별 1517)은 인칭 이동량을 설정·전달하는 버퍼 메모리입니다."),
]

QUIZ_EXPERTISES = (
    "PLC", "전기", "전자", "자동화설비",
    "전기기능사", "전기기사", "디지털공학", "공압/유압", "로봇 Python",
)

def normalize_quiz_expertise(value: Any) -> str:
    """Validate a suggested or user-defined quiz subject name."""
    expertise = " ".join(str(value or "").split())
    if not 2 <= len(expertise) <= 40:
        raise ValueError("주제는 2~40자로 입력해 주세요.")
    if any(ord(char) < 32 or ord(char) == 127 for char in expertise):
        raise ValueError("주제에는 제어문자를 사용할 수 없습니다.")
    return expertise

# Subject ranks use score, then the time that score was reached. Practice retries
# award no score, so they do not inflate rankings.
QUIZ_SUBJECT_TITLES = {
    "PLC": ("⚡", "접점 찍먹", "래더 좀 함", "PLC 고인물"),
    "전기": ("🔌", "회로 찍먹", "전류 좀 봄", "전기 고인물"),
    "전자": ("🔧", "저항 찍먹", "회로 좀 봄", "전자 고인물"),
    "자동화설비": ("🏭", "버튼 찍먹", "설비 좀 함", "공장 고인물"),
    "전기기능사": ("🧰", "공구 찍먹", "배선 좀 함", "기능사 고인물"),
    "전기기사": ("📐", "공식 찍먹", "설계 좀 함", "전기기사 고인물"),
    "디지털공학": ("⚙️", "비트 찍먹", "논리 좀 함", "디지털 고인물"),
    "공압/유압": ("💨", "압력 찍먹", "밸브 좀 함", "유공압 고인물"),
    "로봇 Python": ("🤖", "로봇 찍먹", "코드 좀 함", "로봇 고인물"),
}


def _subject_quiz_badge(category: Optional[str], subject_rank: int,
                        subject_score: int) -> Optional[Dict[str, Any]]:
    if not category or subject_rank not in {1, 2, 3}:
        return None
    subject = QUIZ_SUBJECT_TITLES.get(
        category, ("📚", "주제 찍먹", "주제 좀 함", "주제 고인물")
    )
    icon, beginner_label, skilled_label, master_label = subject
    if subject_rank == 1:
        label = master_label
    elif subject_rank == 2:
        label = skilled_label
    else:
        label = beginner_label
    return {
        "type": "subject",
        "icon": icon,
        "label": label,
        "title": f"{category} {subject_rank}위 · {subject_score}점",
    }

def normalize_quiz_import(items: Any, expertise: str) -> List[Dict[str, Any]]:
    expertise = normalize_quiz_expertise(expertise)
    if not isinstance(items, list) or not 1 <= len(items) <= 50:
        raise ValueError("퀴즈는 1~50문항의 JSON 배열이어야 합니다.")
    normalized = []
    seen_questions = set()
    for index, raw in enumerate(items, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"{index}번 문항은 JSON 객체여야 합니다.")
        question = str(raw.get("question", "")).strip()
        difficulty = str(raw.get("difficulty", "medium"))
        question_type = str(raw.get("question_type", "multiple_choice"))
        answers = raw.get("correct_answers")
        options = raw.get("options")
        if not question or len(question) > 4000:
            raise ValueError(f"{index}번 문항의 문제 본문이 비어 있거나 너무 깁니다.")
        question_key = " ".join(question.casefold().split())
        if question_key in seen_questions:
            raise ValueError(f"{index}번 문항은 문제집 안의 다른 문항과 중복됩니다.")
        seen_questions.add(question_key)
        if raw.get("image_filename") or raw.get("image_url") or "![" in question:
            raise ValueError(f"{index}번 문항은 이미지를 사용할 수 없습니다. ASCII 도면을 코드 블록으로 넣어 주세요.")
        if difficulty not in {"easy", "medium", "hard"}:
            raise ValueError(f"{index}번 문항의 난이도가 올바르지 않습니다.")
        if question_type not in {"multiple_choice", "short_answer", "ladder_input"}:
            raise ValueError(f"{index}번 문항의 유형이 올바르지 않습니다.")
        if not isinstance(answers, list) or not answers or any(not str(answer).strip() for answer in answers):
            raise ValueError(f"{index}번 문항에는 correct_answers 배열이 필요합니다.")
        clean_answers = [str(answer).strip() for answer in answers]
        clean_options = [str(option).strip() for option in options] if isinstance(options, list) else None
        if question_type == "multiple_choice":
            if not clean_options or len(clean_options) != 4:
                raise ValueError(f"{index}번 객관식 문항은 정확히 보기 4개가 필요합니다.")
            if any(not option for option in clean_options):
                raise ValueError(f"{index}번 객관식 문항에는 빈 보기가 없어야 합니다.")
            if len({option.casefold() for option in clean_options}) != 4:
                raise ValueError(f"{index}번 객관식 문항의 보기는 서로 달라야 합니다.")
            valid_answers = {str(number) for number in range(1, 5)} | {option.casefold() for option in clean_options}
            if not any(answer.casefold() in valid_answers for answer in clean_answers):
                raise ValueError(f"{index}번 객관식 문항의 정답이 보기 1~4 또는 보기 본문과 일치해야 합니다.")
        normalized.append({
            "category": expertise,
            "difficulty": difficulty,
            "question_type": question_type,
            "question": question,
            "options": clean_options,
            "correct_answers": clean_answers,
            "hint": str(raw.get("hint", "")).strip()[:1000],
            "explanation": str(raw.get("explanation", "")).strip()[:4000],
            "source_ref": str(raw.get("source_ref", "")).strip()[:500],
        })
    return normalized

def analyze_quiz_set(items: Any, expertise: str) -> Dict[str, Any]:
    """Run advisory duplicate-similarity and multiple-choice answer-bias checks."""
    quizzes = normalize_quiz_import(items, expertise)

    def similarity_text(value: str) -> str:
        value = re.sub(r"```[\s\S]*?```", " ", value.casefold())
        return re.sub(r"[^0-9a-z가-힣]+", "", value)

    with get_connection() as conn:
        existing_rows = conn.execute(
            "SELECT id, category, question FROM quizzes WHERE is_active=1"
        ).fetchall()
    existing = [
        (row["id"], row["category"], row["question"], similarity_text(row["question"]))
        for row in existing_rows
    ]
    similarities = []
    for index, quiz in enumerate(quizzes, start=1):
        candidate = similarity_text(quiz["question"])
        if not candidate:
            continue
        best = None
        for quiz_id, category, question, normalized in existing:
            if not normalized:
                continue
            ratio = SequenceMatcher(None, candidate, normalized).ratio()
            if best is None or ratio > best["similarity"]:
                best = {
                    "candidate_index": index,
                    "existing_quiz_id": quiz_id,
                    "existing_category": category,
                    "existing_question": question[:160],
                    "similarity": ratio,
                }
        if best and best["similarity"] >= 0.65:
            best["similarity"] = round(best["similarity"] * 100, 1)
            similarities.append(best)

    answer_counts = {str(number): 0 for number in range(1, 5)}
    multiple_choice_count = 0
    for quiz in quizzes:
        if quiz["question_type"] != "multiple_choice":
            continue
        multiple_choice_count += 1
        answer_number = None
        for answer in quiz["correct_answers"]:
            match = re.match(r"^([1-4])(?:번|[.)\s]|$)", answer)
            if match:
                answer_number = match.group(1)
                break
            for option_index, option in enumerate(quiz["options"] or [], start=1):
                if answer.casefold() == option.casefold():
                    answer_number = str(option_index)
                    break
            if answer_number:
                break
        if answer_number:
            answer_counts[answer_number] += 1

    dominant_option = max(answer_counts, key=answer_counts.get) if multiple_choice_count else None
    dominant_count = answer_counts.get(dominant_option, 0) if dominant_option else 0
    dominant_ratio = dominant_count / multiple_choice_count if multiple_choice_count else 0
    bias_warning = multiple_choice_count >= 4 and dominant_ratio >= 0.6
    warnings = []
    if similarities:
        warnings.append(f"기존 DB와 65% 이상 유사한 문항이 {len(similarities)}개 있습니다.")
    if bias_warning:
        warnings.append(
            f"객관식 정답의 {round(dominant_ratio * 100)}%가 {dominant_option}번에 몰려 있습니다."
        )
    return {
        "valid": True,
        "question_count": len(quizzes),
        "similarities": similarities,
        "answer_bias": {
            "multiple_choice_count": multiple_choice_count,
            "counts": answer_counts,
            "dominant_option": dominant_option,
            "dominant_ratio": round(dominant_ratio * 100, 1),
            "warning": bias_warning,
        },
        "warnings": warnings,
    }

def create_user_quiz_set(owner_user_id: int, expertise: str, title: str, items: Any) -> Dict[str, Any]:
    title = title.strip()
    if not 2 <= len(title) <= 80:
        raise ValueError("문제집 제목은 2~80자여야 합니다.")
    quizzes = normalize_quiz_import(items, expertise)
    expertise = quizzes[0]["category"]
    now = utc_now()
    with get_connection() as conn:
        cur = conn.execute("""INSERT INTO user_quiz_sets
            (owner_user_id, expertise, title, status, quizzes_json, created_at, updated_at)
            VALUES (?, ?, ?, 'draft', ?, ?, ?)""",
            (owner_user_id, expertise, title, json.dumps(quizzes, ensure_ascii=False), now, now))
        conn.commit()
        return get_user_quiz_set(int(cur.lastrowid), owner_user_id)

def update_user_quiz_set(set_id: int, owner_user_id: int, expertise: str, title: str, items: Any) -> Optional[Dict[str, Any]]:
    title = title.strip()
    if not 2 <= len(title) <= 80:
        raise ValueError("문제집 제목은 2~80자여야 합니다.")
    quizzes = normalize_quiz_import(items, expertise)
    expertise = quizzes[0]["category"]
    now = utc_now()
    with get_connection() as conn:
        cur = conn.execute("""UPDATE user_quiz_sets
            SET expertise=?, title=?, quizzes_json=?, status='draft', review_note='',
                submitted_at=NULL, updated_at=?
            WHERE id=? AND owner_user_id=? AND status IN ('draft','rejected')""",
            (expertise, title, json.dumps(quizzes, ensure_ascii=False), now, set_id, owner_user_id))
        conn.commit()
        return get_user_quiz_set(set_id, owner_user_id) if cur.rowcount else None

def get_user_quiz_set(set_id: int, owner_user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        params: List[Any] = [set_id]
        where = "id=?"
        if owner_user_id is not None:
            where += " AND owner_user_id=?"
            params.append(owner_user_id)
        row = conn.execute(f"SELECT * FROM user_quiz_sets WHERE {where}", params).fetchone()
        if not row: return None
        data = dict(row); data["quizzes"] = json.loads(data.pop("quizzes_json")); return data

def list_user_quiz_sets(owner_user_id: Optional[int] = None, status: Optional[str] = None) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        clauses, params = [], []
        if owner_user_id is not None: clauses.append("s.owner_user_id=?"); params.append(owner_user_id)
        if status: clauses.append("s.status=?"); params.append(status)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        rows = conn.execute(f"""SELECT s.*, u.username, u.display_name FROM user_quiz_sets s
            JOIN users u ON u.id=s.owner_user_id {where} ORDER BY s.id DESC""", params).fetchall()
        result=[]
        for row in rows:
            item=dict(row); item["quizzes"]=json.loads(item.pop("quizzes_json")); result.append(item)
        return result

def submit_user_quiz_set(set_id: int, owner_user_id: int) -> bool:
    now=utc_now()
    with get_connection() as conn:
        cur=conn.execute("UPDATE user_quiz_sets SET status='pending_review', submitted_at=?, updated_at=? WHERE id=? AND owner_user_id=? AND status IN ('draft','rejected')",
                         (now, now, set_id, owner_user_id)); conn.commit(); return cur.rowcount > 0

def review_user_quiz_set(set_id: int, admin_user_id: int, approve: bool, note: str = "",
                         items: Any = None, expertise: Optional[str] = None,
                         title: Optional[str] = None) -> List[int]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM user_quiz_sets WHERE id=?", (set_id,)).fetchone()
        if not row or row["status"] != "pending_review":
            raise ValueError("검토 대기 중인 문제집이 아닙니다.")
        quizzes = json.loads(row["quizzes_json"])
        if approve and items is not None:
            clean_title = str(title if title is not None else row["title"]).strip()
            if not 2 <= len(clean_title) <= 80:
                raise ValueError("문제집 제목은 2~80자여야 합니다.")
            clean_expertise = str(expertise if expertise is not None else row["expertise"])
            quizzes = normalize_quiz_import(items, clean_expertise)
            clean_expertise = quizzes[0]["category"]
            conn.execute("UPDATE user_quiz_sets SET expertise=?, title=?, quizzes_json=?, updated_at=? WHERE id=?",
                         (clean_expertise, clean_title, json.dumps(quizzes, ensure_ascii=False), utc_now(), set_id))
        created_ids: List[int] = []
        now=utc_now(); status="approved" if approve else "rejected"
        if approve:
            for quiz in quizzes:
                duplicate = conn.execute(
                    "SELECT 1 FROM quizzes WHERE category=? AND lower(trim(question))=lower(trim(?)) LIMIT 1",
                    (quiz["category"], quiz["question"]),
                ).fetchone()
                if duplicate:
                    raise ValueError(f"공용 풀에 이미 존재하는 문항입니다: {quiz['question'][:80]}")
                cur = conn.execute("""INSERT INTO quizzes
                    (category,difficulty,question_type,question,image_filename,options_json,
                     correct_answers_json,hint,explanation,source_ref,is_active,created_at,source_submission_set_id)
                    VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?)""", (
                    quiz["category"], quiz["difficulty"], quiz["question_type"], quiz["question"], "",
                    json.dumps(quiz.get("options"), ensure_ascii=False) if quiz.get("options") else None,
                    json.dumps(quiz["correct_answers"], ensure_ascii=False), quiz.get("hint", ""),
                    quiz.get("explanation", ""), quiz.get("source_ref", ""), now, set_id,
                ))
                created_ids.append(int(cur.lastrowid))
        conn.execute("UPDATE user_quiz_sets SET status=?, review_note=?, approved_by_user_id=?, approved_at=?, updated_at=? WHERE id=?",
                     (status, note[:1000], admin_user_id, now if approve else None, now, set_id))
        conn.commit()
    return created_ids

def update_pending_user_quiz_set(set_id: int, expertise: str, title: str, items: Any) -> Dict[str, Any]:
    """Allow an administrator to correct a submission before making a review decision."""
    title = title.strip()
    if not 2 <= len(title) <= 80:
        raise ValueError("문제집 제목은 2~80자여야 합니다.")
    quizzes = normalize_quiz_import(items, expertise)
    expertise = quizzes[0]["category"]
    with get_connection() as conn:
        cur = conn.execute("""UPDATE user_quiz_sets
            SET expertise=?, title=?, quizzes_json=?, updated_at=?
            WHERE id=? AND status='pending_review'""",
            (expertise, title, json.dumps(quizzes, ensure_ascii=False), utc_now(), set_id))
        conn.commit()
        if not cur.rowcount:
            raise ValueError("검토 대기 중인 문제집이 아닙니다.")
    updated = get_user_quiz_set(set_id)
    if not updated:
        raise ValueError("문제집을 찾을 수 없습니다.")
    return updated

def ensure_daily_quiz_set(assigned_date: str, count: int = 5, created_by_user_id: Optional[int] = None) -> int:
    with get_connection() as conn:
        row=conn.execute("SELECT id FROM daily_quiz_sets WHERE assigned_date=?", (assigned_date,)).fetchone()
        if row: return int(row["id"])
        quiz_rows=conn.execute("""SELECT q.id FROM quizzes q
            WHERE q.is_active=1 AND NOT EXISTS (
                SELECT 1 FROM daily_quiz_set_items used WHERE used.quiz_id=q.id
            ) ORDER BY q.id ASC LIMIT ?""", (count,)).fetchall()
        if not quiz_rows: return 0
        now=utc_now(); cur=conn.execute("INSERT INTO daily_quiz_sets (assigned_date,status,created_by_user_id,created_at) VALUES (?,'published',?,?)",
            (assigned_date, created_by_user_id, now)); set_id=int(cur.lastrowid)
        conn.executemany("INSERT INTO daily_quiz_set_items (set_id,quiz_id,position,points) VALUES (?,?,?,20)",
            [(set_id,row["id"],index) for index,row in enumerate(quiz_rows, start=1)])
        conn.commit(); return set_id

def assign_daily_quizzes(assigned_date: str, quiz_ids: List[int], admin_user_id: int) -> int:
    if not quiz_ids or len(quiz_ids) > 20: raise ValueError("1~20개의 문제를 배정하세요.")
    if assigned_date < datetime.now().strftime("%Y-%m-%d"):
        raise ValueError("과거 날짜에는 퀴즈를 게시할 수 없습니다.")
    with get_connection() as conn:
        if conn.execute("SELECT 1 FROM daily_quiz_sets WHERE assigned_date=?", (assigned_date,)).fetchone():
            raise ValueError("해당 날짜의 퀴즈 세트가 이미 게시되었습니다.")
    # Creation is immutable once published.
    with get_connection() as conn:
        valid={r["id"] for r in conn.execute(f"""SELECT q.id FROM quizzes q
            WHERE q.is_active=1 AND q.id IN ({','.join('?' for _ in quiz_ids)})
              AND NOT EXISTS (SELECT 1 FROM daily_quiz_set_items used WHERE used.quiz_id=q.id)""", quiz_ids)}
        if len(valid) != len(set(quiz_ids)): raise ValueError("유효하지 않은 퀴즈가 포함되어 있습니다.")
        now=utc_now(); cur=conn.execute("INSERT INTO daily_quiz_sets (assigned_date,status,created_by_user_id,created_at) VALUES (?,'published',?,?)", (assigned_date,admin_user_id,now)); set_id=int(cur.lastrowid)
        conn.executemany("INSERT INTO daily_quiz_set_items (set_id,quiz_id,position,points) VALUES (?,?,?,20)", [(set_id,qid,i) for i,qid in enumerate(quiz_ids,1)]); conn.commit(); return set_id


def seed_default_quizzes(conn: Optional[sqlite3.Connection] = None) -> None:
    """Seeds default educational quizzes if the quizzes table is empty."""
    def _seed(c: sqlite3.Connection) -> None:
        count = c.execute("SELECT COUNT(*) FROM quizzes").fetchone()[0]
        if count > 0:
            return
        now = utc_now()
        for q in DEFAULT_SAMPLE_QUIZZES:
            opts = json.dumps(q.get("options"), ensure_ascii=False) if q.get("options") else None
            corrects = json.dumps(q.get("correct_answers", []), ensure_ascii=False)
            c.execute("""INSERT INTO quizzes
                (category, difficulty, question_type, question, image_filename,
                 options_json, correct_answers_json, hint, explanation, source_ref,
                 is_active, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (
                    q.get("category", "PLC"),
                    q.get("difficulty", "medium"),
                    q.get("question_type", "multiple_choice"),
                    q["question"],
                    q.get("image_filename", ""),
                    opts,
                    corrects,
                    q.get("hint", ""),
                    q.get("explanation", ""),
                    q.get("source_ref", ""),
                    now,
                ))

    if conn is not None:
        _seed(conn)
    else:
        with get_connection() as c:
            _seed(c)


def save_quiz_source_document(
    filename: str,
    stored_filename: str,
    file_type: str,
    sha256: str,
    size: int,
    uploaded_by_user_id: Optional[int] = None,
) -> int:
    """Saves metadata for an uploaded lecture PDF or question document."""
    now = utc_now()
    with get_connection() as conn:
        cur = conn.execute("""INSERT INTO quiz_source_documents
            (filename, stored_filename, file_type, sha256, size, uploaded_by_user_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (filename, stored_filename, file_type, sha256, size, uploaded_by_user_id, now))
        return int(cur.lastrowid)


def get_quiz_source_documents() -> List[Dict[str, Any]]:
    """Returns a list of all uploaded quiz source documents."""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT d.*, u.username as uploader_username, u.display_name as uploader_display_name,
                   (SELECT COUNT(*) FROM quizzes q WHERE q.source_doc_id = d.id) as generated_quizzes_count
            FROM quiz_source_documents d
            LEFT JOIN users u ON d.uploaded_by_user_id = u.id
            ORDER BY d.id DESC
        """).fetchall()
        return [dict(r) for r in rows]


def create_quiz(
    category: str,
    difficulty: str,
    question_type: str,
    question: str,
    correct_answers: List[str],
    options: Optional[List[str]] = None,
    image_filename: Optional[str] = None,
    hint: str = "",
    explanation: str = "",
    source_doc_id: Optional[int] = None,
    source_ref: str = "",
    daily_date: Optional[str] = None,
) -> int:
    """Creates a new quiz item."""
    now = utc_now()
    opts_json = json.dumps(options, ensure_ascii=False) if options else None
    corrects_json = json.dumps(correct_answers, ensure_ascii=False)
    with get_connection() as conn:
        cur = conn.execute("""INSERT INTO quizzes
            (category, difficulty, question_type, question, image_filename,
             options_json, correct_answers_json, hint, explanation, source_doc_id,
             source_ref, daily_date, is_active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
            (
                category,
                difficulty,
                question_type,
                question,
                image_filename or "",
                opts_json,
                corrects_json,
                hint,
                explanation,
                source_doc_id,
                source_ref,
                daily_date,
                now,
            ))
        return int(cur.lastrowid)


def create_quiz_batch(
    quizzes_data: List[Dict[str, Any]],
    source_doc_id: Optional[int] = None,
) -> List[int]:
    """Bulk creates multiple quizzes from AI or JSON import."""
    now = utc_now()
    created_ids: List[int] = []
    with get_connection() as conn:
        for q in quizzes_data:
            opts = q.get("options")
            opts_json = json.dumps(opts, ensure_ascii=False) if opts else None
            corrects = q.get("correct_answers") or [q.get("answer", "")]
            if isinstance(corrects, str):
                corrects = [corrects]
            corrects_json = json.dumps(corrects, ensure_ascii=False)
            cur = conn.execute("""INSERT INTO quizzes
                (category, difficulty, question_type, question, image_filename,
                 options_json, correct_answers_json, hint, explanation, source_doc_id,
                 source_ref, daily_date, is_active, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (
                    q.get("category", "PLC"),
                    q.get("difficulty", "medium"),
                    q.get("question_type", "multiple_choice"),
                    q.get("question", ""),
                    q.get("image_filename", "") or q.get("image_url", ""),
                    opts_json,
                    corrects_json,
                    q.get("hint", ""),
                    q.get("explanation", ""),
                    source_doc_id,
                    q.get("source_ref", ""),
                    q.get("daily_date"),
                    now,
                ))
            created_ids.append(int(cur.lastrowid))
    return created_ids

def update_quiz(quiz_id: int, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Update an existing quiz while retaining its identity, attempts, and set membership."""
    category = str(data.get("category", "")).strip()
    if not category or len(category) > 80:
        raise ValueError("과목은 1~80자여야 합니다.")
    normalized = normalize_quiz_import([data], "PLC")[0]
    normalized["category"] = category
    image_filename = str(data.get("image_filename", "")).strip()[:500]
    is_active = 1 if data.get("is_active", True) else 0
    with get_connection() as conn:
        cur = conn.execute("""UPDATE quizzes SET
            category=?, difficulty=?, question_type=?, question=?, image_filename=?,
            options_json=?, correct_answers_json=?, hint=?, explanation=?, source_ref=?, is_active=?
            WHERE id=?""", (
            normalized["category"], normalized["difficulty"], normalized["question_type"],
            normalized["question"], image_filename,
            json.dumps(normalized.get("options"), ensure_ascii=False) if normalized.get("options") else None,
            json.dumps(normalized["correct_answers"], ensure_ascii=False), normalized.get("hint", ""),
            normalized.get("explanation", ""), normalized.get("source_ref", ""), is_active, quiz_id,
        ))
        conn.commit()
        if not cur.rowcount:
            return None
    return next((item for item in get_all_quizzes_admin() if item["id"] == quiz_id), None)


def get_all_quizzes_admin(limit: int = 200) -> List[Dict[str, Any]]:
    """Returns all active quizzes for management view."""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT q.*, d.filename as source_doc_filename
            FROM quizzes q
            LEFT JOIN quiz_source_documents d ON q.source_doc_id = d.id
            ORDER BY q.id DESC
            LIMIT ?
        """, (limit,)).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["options"] = json.loads(d["options_json"]) if d["options_json"] else None
            d["correct_answers"] = json.loads(d["correct_answers_json"]) if d["correct_answers_json"] else []
            results.append(d)
        return results


def delete_quiz(quiz_id: int) -> bool:
    """Deletes or archives a quiz."""
    with get_connection() as conn:
        if conn.execute("SELECT 1 FROM daily_quiz_set_items WHERE quiz_id=?", (quiz_id,)).fetchone():
            return False
        cur = conn.execute("DELETE FROM quizzes WHERE id = ?", (quiz_id,))
        return cur.rowcount > 0


def toggle_quiz_bookmark(user_id: int, quiz_id: int) -> bool:
    """Toggles a bookmark/star for a quiz by the given user. Returns True if now bookmarked."""
    now = utc_now()
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM quiz_bookmarks WHERE user_id = ? AND quiz_id = ?",
            (user_id, quiz_id)
        ).fetchone()
        if existing:
            conn.execute("DELETE FROM quiz_bookmarks WHERE id = ?", (existing["id"],))
            return False
        else:
            conn.execute(
                "INSERT INTO quiz_bookmarks (user_id, quiz_id, created_at) VALUES (?, ?, ?)",
                (user_id, quiz_id, now)
            )
            return True


def get_user_quiz_bookmarks_set(user_id: int) -> Set[int]:
    """Returns the set of quiz IDs bookmarked by the user."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT quiz_id FROM quiz_bookmarks WHERE user_id = ?", (user_id,)
        ).fetchall()
        return {r["quiz_id"] for r in rows}


def get_quiz_categories_summary() -> List[Dict[str, Any]]:
    """Returns unique categories and their active quiz counts."""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT category, COUNT(*) as count
            FROM quizzes
            WHERE is_active = 1
            GROUP BY category
            ORDER BY count DESC, category ASC
        """).fetchall()
        return [dict(r) for r in rows]


def get_quiz_sidebar_counts(user_id: int) -> Dict[str, int]:
    """Returns count stats for sidebar badges (wrong, starred, history/solved)."""
    with get_connection() as conn:
        total_quizzes = conn.execute("SELECT COUNT(*) FROM quizzes WHERE is_active = 1").fetchone()[0]
        wrong_count = conn.execute("""
            SELECT COUNT(DISTINCT q.id)
            FROM quizzes q
            JOIN quiz_submissions qs ON q.id = qs.quiz_id
            WHERE qs.user_id = ? AND (qs.is_correct = 0 OR qs.had_wrong_attempt = 1) AND q.is_active = 1
        """, (user_id,)).fetchone()[0]
        starred_count = conn.execute("""
            SELECT COUNT(DISTINCT q.id)
            FROM quizzes q
            JOIN quiz_bookmarks qb ON q.id = qb.quiz_id
            WHERE qb.user_id = ? AND q.is_active = 1
        """, (user_id,)).fetchone()[0]
        history_count = conn.execute("""
            SELECT COUNT(DISTINCT q.id)
            FROM quizzes q
            JOIN quiz_submissions qs ON q.id = qs.quiz_id
            WHERE qs.user_id = ? AND q.is_active = 1
        """, (user_id,)).fetchone()[0]
        return {
            "total_quizzes": total_quizzes,
            "wrong": wrong_count,
            "starred": starred_count,
            "history": history_count,
        }


def get_daily_quizzes(
    user_id: int,
    count: int = 5,
    category: Optional[str] = None,
    offset: int = 0,
    exclude_ids: Optional[List[int]] = None,
) -> List[Dict[str, Any]]:
    """Returns active educational quizzes with the current user's submission & bookmark state.
    Supports topic category filtering or 'random'/'all' modes.
    """
    daily_set_id = ensure_daily_quiz_set(datetime.now().strftime("%Y-%m-%d"), count=count) if not category or category == "daily" else None
    with get_connection() as conn:
        starred_set = get_user_quiz_bookmarks_set(user_id)
        where_clause = "WHERE q.is_active = 1"
        params: List[Any] = [user_id]

        is_random = category in ("random", "all_random", "all")

        if category and category not in ("all", "random", "all_random", "daily"):
            where_clause += " AND q.category = ?"
            params.append(category)

        excluded = sorted({int(value) for value in (exclude_ids or []) if int(value) > 0})
        if excluded:
            where_clause += f" AND q.id NOT IN ({','.join('?' for _ in excluded)})"
            params.extend(excluded)

        daily_join = ""
        if daily_set_id is not None:
            daily_join = "JOIN daily_quiz_set_items dqi ON dqi.quiz_id=q.id"
            where_clause += " AND dqi.set_id = ?"
            params.append(daily_set_id)
            order_by = "ORDER BY dqi.position ASC"
        else:
            if is_random:
                # Prefer unseen questions; once exhausted, fill the set with solved ones.
                order_by = "ORDER BY CASE WHEN qs.id IS NULL THEN 0 ELSE 1 END, RANDOM()"
            else:
                order_by = "ORDER BY CASE WHEN qs.id IS NULL THEN 0 ELSE 1 END, CASE q.difficulty WHEN 'easy' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, q.id ASC"
        params.extend([count, max(0, int(offset))])

        query = f"""
            SELECT q.id, q.category, q.difficulty, q.question_type, q.question,
                   q.image_filename, q.options_json, q.correct_answers_json,
                   q.hint, q.explanation, q.source_ref, q.daily_date,
                   qs.id as submission_id, qs.user_answer, qs.is_correct,
                   qs.score_earned, qs.submitted_at
            FROM quizzes q
            {daily_join}
            LEFT JOIN quiz_submissions qs ON q.id = qs.quiz_id AND qs.user_id = ?
            {where_clause}
            {order_by}
            LIMIT ? OFFSET ?
        """
        rows = conn.execute(query, tuple(params)).fetchall()

        quizzes = []
        for r in rows:
            is_solved = r["submission_id"] is not None
            options = json.loads(r["options_json"]) if r["options_json"] else None
            item: Dict[str, Any] = {
                "id": r["id"],
                "category": r["category"],
                "difficulty": r["difficulty"],
                "question_type": r["question_type"],
                "question": r["question"],
                "image_filename": r["image_filename"] or "",
                "options": options,
                "hint": r["hint"] or "",
                "source_ref": r["source_ref"] or "",
                "is_solved": is_solved,
                "is_starred": r["id"] in starred_set,
            }
            if is_solved:
                item["user_answer"] = r["user_answer"]
                item["is_correct"] = bool(r["is_correct"])
                item["score_earned"] = r["score_earned"]
                item["submitted_at"] = r["submitted_at"]
                item["correct_answers"] = json.loads(r["correct_answers_json"])
                item["explanation"] = r["explanation"]
            else:
                item["user_answer"] = None
                item["is_correct"] = None
                item["score_earned"] = 0
            quizzes.append(item)
        return quizzes


def get_quiz_review_list(user_id: int, mode: str = "wrong") -> List[Dict[str, Any]]:
    """Returns quizzes for review: 'wrong' (quizzes ever attempted incorrectly), 'starred' (bookmarks), or 'history' (all solved)."""
    with get_connection() as conn:
        starred_set = get_user_quiz_bookmarks_set(user_id)
        if mode == "wrong":
            rows = conn.execute("""
                SELECT q.id, q.category, q.difficulty, q.question_type, q.question,
                       q.image_filename, q.options_json, q.correct_answers_json,
                       q.hint, q.explanation, q.source_ref,
                       qs.id as submission_id, qs.user_answer, qs.is_correct,
                       qs.score_earned, qs.submitted_at, qs.had_wrong_attempt
                FROM quiz_submissions qs
                JOIN quizzes q ON qs.quiz_id = q.id
                WHERE qs.user_id = ? AND (qs.is_correct = 0 OR qs.had_wrong_attempt = 1)
                ORDER BY qs.id DESC
            """, (user_id,)).fetchall()
        elif mode == "starred":
            rows = conn.execute("""
                SELECT q.id, q.category, q.difficulty, q.question_type, q.question,
                       q.image_filename, q.options_json, q.correct_answers_json,
                       q.hint, q.explanation, q.source_ref,
                       qs.id as submission_id, qs.user_answer, qs.is_correct,
                       qs.score_earned, qs.submitted_at, qs.had_wrong_attempt
                FROM quiz_bookmarks qb
                JOIN quizzes q ON qb.quiz_id = q.id
                LEFT JOIN quiz_submissions qs ON q.id = qs.quiz_id AND qs.user_id = ?
                WHERE qb.user_id = ?
                ORDER BY qb.id DESC
            """, (user_id, user_id)).fetchall()
        else:  # 'history' / all
            rows = conn.execute("""
                SELECT q.id, q.category, q.difficulty, q.question_type, q.question,
                       q.image_filename, q.options_json, q.correct_answers_json,
                       q.hint, q.explanation, q.source_ref,
                       qs.id as submission_id, qs.user_answer, qs.is_correct,
                       qs.score_earned, qs.submitted_at, qs.had_wrong_attempt
                FROM quiz_submissions qs
                JOIN quizzes q ON qs.quiz_id = q.id
                WHERE qs.user_id = ?
                ORDER BY qs.id DESC
            """, (user_id,)).fetchall()

        results = []
        for r in rows:
            is_solved = r["submission_id"] is not None
            options = json.loads(r["options_json"]) if r["options_json"] else None
            had_wrong = bool(r["had_wrong_attempt"]) if "had_wrong_attempt" in r.keys() and r["had_wrong_attempt"] is not None else False
            item: Dict[str, Any] = {
                "id": r["id"],
                "category": r["category"],
                "difficulty": r["difficulty"],
                "question_type": r["question_type"],
                "question": r["question"],
                "image_filename": r["image_filename"] or "",
                "options": options,
                "hint": r["hint"] or "",
                "source_ref": r["source_ref"] or "",
                "is_solved": is_solved,
                "is_starred": r["id"] in starred_set,
                "had_wrong": had_wrong,
                "is_mastered": bool(r["is_correct"]) if is_solved and had_wrong else False,
                "user_answer": r["user_answer"] if is_solved else None,
                "is_correct": bool(r["is_correct"]) if is_solved else None,
                "score_earned": r["score_earned"] if is_solved else 0,
                "submitted_at": r["submitted_at"] if is_solved else None,
                "correct_answers": json.loads(r["correct_answers_json"]) if is_solved else [],
                "explanation": r["explanation"] if is_solved else "",
            }
            results.append(item)
        return results


def retry_quiz_answer(user_id: int, quiz_id: int, user_answer: str) -> Dict[str, Any]:
    """Allows repeating a wrong or saved quiz in practice mode and updates review state while preserving wrong history."""
    from app.quiz_ai import check_quiz_answer

    today_str = datetime.now().strftime("%Y-%m-%d")
    now = utc_now()

    with get_connection() as conn:
        quiz_row = conn.execute("SELECT * FROM quizzes WHERE id = ?", (quiz_id,)).fetchone()
        if not quiz_row:
            raise ValueError("존재하지 않는 퀴즈입니다.")

        correct_answers: List[str] = json.loads(quiz_row["correct_answers_json"])
        is_correct = 1 if check_quiz_answer(correct_answers, user_answer) else 0

        # Update or insert practice submission while preserving had_wrong_attempt
        existing = conn.execute(
            "SELECT id, had_wrong_attempt, is_correct FROM quiz_submissions WHERE quiz_id = ? AND user_id = ?",
            (quiz_id, user_id)
        ).fetchone()

        if existing:
            had_wrong = 1 if (existing["had_wrong_attempt"] or existing["is_correct"] == 0 or is_correct == 0) else 0
            conn.execute("""UPDATE quiz_submissions SET
                user_answer = ?, is_correct = ?, submitted_at = ?, had_wrong_attempt = ?
                WHERE id = ?""", (user_answer, is_correct, now, had_wrong, existing["id"]))
        else:
            conn.execute("""INSERT INTO quiz_submissions
                (quiz_id, user_id, user_answer, is_correct, score_earned, submitted_at, submitted_date, had_wrong_attempt)
                VALUES (?, ?, ?, ?, 0, ?, ?, ?)""",
                (quiz_id, user_id, user_answer, is_correct, now, today_str, 1 if is_correct == 0 else 0))

        stats = get_user_quiz_stats(user_id)
        return {
            "is_correct": bool(is_correct),
            "score_earned": 0,
            "correct_answers": correct_answers,
            "explanation": quiz_row["explanation"] or "",
            "source_ref": quiz_row["source_ref"] or "",
            "user_stats": stats,
        }


def submit_quiz_answer(user_id: int, quiz_id: int, user_answer: str) -> Dict[str, Any]:
    """Evaluates the submitted answer, updates streaks/scores, and returns detailed results."""
    from app.quiz_ai import check_quiz_answer

    today_str = datetime.now().strftime("%Y-%m-%d")
    yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    now = utc_now()

    with get_connection() as conn:
        quiz_row = conn.execute(
            "SELECT * FROM quizzes WHERE id = ? AND is_active = 1", (quiz_id,)
        ).fetchone()
        if not quiz_row:
            raise ValueError("존재하지 않거나 비활성화된 퀴즈입니다.")

        today_set = conn.execute("SELECT id FROM daily_quiz_sets WHERE assigned_date=? AND status='published'", (today_str,)).fetchone()
        assigned = today_set and conn.execute(
            "SELECT 1 FROM daily_quiz_set_items WHERE set_id=? AND quiz_id=?",
            (today_set["id"], quiz_id),
        ).fetchone()
        is_daily_quiz = bool(assigned)

        # Check existing submission
        existing = conn.execute(
            "SELECT id FROM quiz_submissions WHERE quiz_id = ? AND user_id = ?",
            (quiz_id, user_id)
        ).fetchone()
        if existing:
            raise ValueError("이미 제출 완료된 문제입니다.")

        correct_answers: List[str] = json.loads(quiz_row["correct_answers_json"])
        is_correct = 1 if check_quiz_answer(correct_answers, user_answer) else 0

        # Score calculation
        score_earned = ({"easy": 10, "medium": 20, "hard": 30}.get(quiz_row["difficulty"], 20)
                        if is_correct else 0)

        # Insert submission
        conn.execute("""INSERT INTO quiz_submissions
            (quiz_id, user_id, user_answer, is_correct, score_earned, submitted_at, submitted_date, had_wrong_attempt)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (quiz_id, user_id, user_answer, is_correct, score_earned, now, today_str, 1 if is_correct == 0 else 0))


        # Update or create user stats
        stats_row = conn.execute(
            "SELECT * FROM user_quiz_stats WHERE user_id = ?", (user_id,)
        ).fetchone()

        if stats_row:
            last_date = stats_row["last_solved_date"]
            curr_streak = stats_row["current_streak"]
            max_streak = stats_row["max_streak"]

            streak = curr_streak
            next_last_date = last_date
            if is_daily_quiz:
                if last_date == today_str:
                    streak = curr_streak
                elif last_date == yesterday_str:
                    streak = curr_streak + 1
                else:
                    streak = 1
                max_streak = max(max_streak, streak)
                next_last_date = today_str
            total_score = stats_row["total_score"] + score_earned
            weekly_score = stats_row["weekly_score"] + score_earned
            total_solved = stats_row["total_solved"] + 1
            total_correct = stats_row["total_correct"] + is_correct

            conn.execute("""UPDATE user_quiz_stats SET
                total_score = ?, total_solved = ?, total_correct = ?,
                current_streak = ?, max_streak = ?, weekly_score = ?,
                last_solved_date = ?
                WHERE user_id = ?""",
                (total_score, total_solved, total_correct, streak, max_streak,
                 weekly_score, next_last_date, user_id))
        else:
            streak = 1 if is_daily_quiz else 0
            max_streak = streak
            total_score = score_earned
            weekly_score = score_earned
            total_solved = 1
            total_correct = is_correct

            conn.execute("""INSERT INTO user_quiz_stats
                (user_id, total_score, total_solved, total_correct,
                 current_streak, max_streak, weekly_score, last_solved_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, total_score, total_solved, total_correct,
                 streak, max_streak, weekly_score, today_str if is_daily_quiz else None))

        return {
            "quiz_id": quiz_id,
            "is_correct": bool(is_correct),
            "score_earned": score_earned,
            "correct_answers": correct_answers,
            "explanation": quiz_row["explanation"],
            "source_ref": quiz_row["source_ref"],
            "user_stats": {
                "current_streak": streak,
                "max_streak": max_streak,
                "total_score": total_score,
                "weekly_score": weekly_score,
                "total_solved": total_solved,
                "total_correct": total_correct,
            }
        }


def get_user_quiz_stats(user_id: int) -> Dict[str, Any]:
    """Returns user quiz performance summary and rank."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM user_quiz_stats WHERE user_id = ?", (user_id,)
        ).fetchone()

        if not row:
            return {
                "user_id": user_id,
                "total_score": 0,
                "weekly_score": 0,
                "current_streak": 0,
                "max_streak": 0,
                "total_solved": 0,
                "total_correct": 0,
                "accuracy": 0.0,
                "badge": None,
            }

        data = dict(row)
        solved = data.get("total_solved", 0)
        correct = data.get("total_correct", 0)
        data["accuracy"] = round((correct / solved * 100), 1) if solved > 0 else 0.0
        data["badge"] = get_user_quiz_badge(user_id)
        return data


def record_chess_result(white_id: int, black_id: int, winner: Optional[str]) -> None:
    """Persist one completed chess result for both players."""
    columns = {"w": ("wins", "losses"), "b": ("losses", "wins")}
    with get_connection() as conn:
        for user_id in (white_id, black_id):
            conn.execute("INSERT OR IGNORE INTO chess_player_stats (user_id) VALUES (?)", (user_id,))
        if winner in columns:
            white_column, black_column = columns[winner]
            conn.execute(f"UPDATE chess_player_stats SET {white_column} = {white_column} + 1 WHERE user_id = ?", (white_id,))
            conn.execute(f"UPDATE chess_player_stats SET {black_column} = {black_column} + 1 WHERE user_id = ?", (black_id,))
        else:
            conn.execute("UPDATE chess_player_stats SET draws = draws + 1 WHERE user_id IN (?, ?)", (white_id, black_id))


def get_chess_stats(user_id: int) -> Dict[str, int]:
    with get_connection() as conn:
        row = conn.execute("SELECT wins, draws, losses FROM chess_player_stats WHERE user_id = ?", (user_id,)).fetchone()
    return dict(row) if row else {"wins": 0, "draws": 0, "losses": 0}


def get_quiz_leaderboard(period: str = "weekly", limit: int = 20) -> List[Dict[str, Any]]:
    """Returns top ranked users for daily, weekly, or all-time educational quizzes."""
    today_str = datetime.now().strftime("%Y-%m-%d")
    week_start = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime("%Y-%m-%d")
    if period == "streak":
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        with get_connection() as conn:
            rows = conn.execute("""SELECT st.user_id, u.username, u.display_name,
                       st.total_score AS score, st.total_correct AS correct_count,
                       st.total_solved AS solved_count, st.current_streak,
                       st.last_solved_date AS score_reached_at
                FROM user_quiz_stats st JOIN users u ON u.id=st.user_id
                WHERE st.current_streak >= 3 AND st.last_solved_date >= ?
                ORDER BY st.current_streak DESC, st.last_solved_date ASC, st.user_id ASC LIMIT ?""",
                (yesterday, limit)).fetchall()
        return [{**dict(row), "rank": rank} for rank, row in enumerate(rows, start=1)]
    with get_connection() as conn:
        date_clause, params = "", []
        if period == "daily": date_clause = "AND qs.submitted_date = ?"; params.append(today_str)
        elif period != "all": date_clause = "AND qs.submitted_date >= ?"; params.append(week_start)
        params.append(limit)
        rows = conn.execute(f"""
            SELECT qs.user_id, u.username, u.display_name,
                   SUM(qs.score_earned) as score,
                   SUM(CASE WHEN qs.score_earned > 0 THEN 1 ELSE 0 END) as correct_count,
                   COUNT(qs.id) as solved_count, COALESCE(st.current_streak,0) as current_streak,
                   COALESCE(MAX(CASE WHEN qs.score_earned > 0 THEN qs.submitted_at END), MIN(qs.submitted_at)) as score_reached_at
            FROM quiz_submissions qs
            JOIN users u ON u.id=qs.user_id
            LEFT JOIN user_quiz_stats st ON st.user_id=qs.user_id
            WHERE 1=1 {date_clause}
            GROUP BY qs.user_id
            ORDER BY score DESC, score_reached_at ASC, qs.user_id ASC LIMIT ?
        """, params).fetchall()

        results = []
        for rank, r in enumerate(rows, start=1):
            item = dict(r)
            item["rank"] = rank
            results.append(item)
        return results


def get_user_quiz_title_options(user_id: int) -> List[Dict[str, Any]]:
    """Return every subject title the user currently holds (top three by score)."""
    with get_connection() as conn:
        rows = conn.execute("""
            WITH subject_scores AS (
                SELECT qs.user_id, q.category,
                       SUM(qs.score_earned) AS subject_score,
                       MAX(qs.submitted_at) AS score_reached_at
                FROM quiz_submissions qs
                JOIN quizzes q ON q.id = qs.quiz_id
                WHERE qs.score_earned > 0
                GROUP BY qs.user_id, q.category
            ), ranked AS (
                SELECT user_id, category, subject_score, score_reached_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY category
                           ORDER BY subject_score DESC, score_reached_at ASC, user_id ASC
                       ) AS subject_rank
                FROM subject_scores
            )
            SELECT category, subject_score, subject_rank
            FROM ranked
            WHERE user_id = ? AND subject_rank <= 3
            ORDER BY subject_rank ASC, subject_score DESC, category ASC
        """, (user_id,)).fetchall()
    options = []
    for row in rows:
        badge = _subject_quiz_badge(
            row["category"], int(row["subject_rank"]), int(row["subject_score"])
        )
        if badge:
            options.append({
                **badge,
                "category": row["category"],
                "selection": f"subject:{row['category']}",
                "rank": int(row["subject_rank"]),
                "score": int(row["subject_score"]),
            })
    with get_connection() as conn:
        stats = conn.execute(
            "SELECT current_streak FROM user_quiz_stats WHERE user_id=?", (user_id,)
        ).fetchone()
    streak = int(stats["current_streak"]) if stats else 0
    if streak >= 3:
        options.append({
            "type": "streak",
            "icon": "🔥",
            "label": "꾸준러",
            "title": f"오늘의 퀴즈 {streak}일 연속",
            "category": "연속 학습",
            "selection": "streak",
            "rank": None,
            "score": None,
        })
    return options


def get_user_quiz_badge(user_id: int) -> Optional[Dict[str, Any]]:
    """Calculates user badge to display beside nickname in chat."""
    with get_connection() as conn:
        row = conn.execute(
            """SELECT u.id AS user_id,
                      COALESCE(st.total_score, 0) AS total_score,
                      COALESCE(st.current_streak, 0) AS current_streak,
                      COALESCE(u.quiz_badge_selection, 'score') AS badge_selection
               FROM users u LEFT JOIN user_quiz_stats st ON st.user_id=u.id
               WHERE u.id = ?""", (user_id,)
        ).fetchone()
        if not row:
            return None

        selection = row["badge_selection"]
        if selection == "none":
            return None
        if selection == "score":
            if row["total_score"] <= 0:
                return None
            return {
                "type": "score",
                "icon": "⚡",
                "label": f"{row['total_score']}점",
                "title": f"퀴즈 누적 {row['total_score']}점",
            }
        title_options = get_user_quiz_title_options(user_id)
        selected_badge = next(
            (item for item in title_options if item["selection"] == selection), None
        )
        if selected_badge:
            return {key: selected_badge[key] for key in ("type", "icon", "label", "title")}
        if selection not in {"score", "none"}:
            if row["total_score"] <= 0:
                return None
            return {
                "type": "score",
                "icon": "⚡",
                "label": f"{row['total_score']}점",
                "title": f"퀴즈 누적 {row['total_score']}점",
            }

        return None


def get_user_quiz_badges_map(user_ids: List[int]) -> Dict[int, Optional[Dict[str, Any]]]:
    """Bulk calculates badges for multiple users to optimize message rendering."""
    if not user_ids:
        return {}
    badges: Dict[int, Optional[Dict[str, Any]]] = {}
    with get_connection() as conn:
        placeholders = ",".join("?" for _ in user_ids)
        rows = conn.execute(
            f"""SELECT u.id AS user_id,
                       COALESCE(st.total_score, 0) AS total_score,
                       COALESCE(st.current_streak, 0) AS current_streak,
                       COALESCE(u.quiz_badge_selection, 'score') AS badge_selection
                FROM users u LEFT JOIN user_quiz_stats st ON st.user_id=u.id
                WHERE u.id IN ({placeholders})""", user_ids
        ).fetchall()
        stats_map = {r["user_id"]: dict(r) for r in rows}
        subject_rows = conn.execute(f"""
            WITH subject_scores AS (
                SELECT qs.user_id, q.category,
                       SUM(qs.score_earned) AS subject_score,
                       MAX(qs.submitted_at) AS score_reached_at
                FROM quiz_submissions qs
                JOIN quizzes q ON q.id = qs.quiz_id
                WHERE qs.score_earned > 0
                GROUP BY qs.user_id, q.category
            ), ranked AS (
                SELECT user_id, category, subject_score, score_reached_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY category
                           ORDER BY subject_score DESC, score_reached_at ASC, user_id ASC
                       ) AS subject_rank
                FROM subject_scores
            )
            SELECT user_id, category, subject_score, subject_rank
            FROM ranked
            WHERE user_id IN ({placeholders}) AND subject_rank <= 3
            ORDER BY user_id, subject_rank ASC, subject_score DESC, category ASC
        """, user_ids).fetchall()
        subject_map: Dict[int, Dict[str, Dict[str, Any]]] = {}
        for subject_row in subject_rows:
            subject_map.setdefault(subject_row["user_id"], {})[subject_row["category"]] = dict(subject_row)

        for uid in user_ids:
            st = stats_map.get(uid)
            if not st:
                badges[uid] = None
                continue
            selection = st.get("badge_selection", "score")
            subjects = subject_map.get(uid, {})
            subject = None
            if selection.startswith("subject:"):
                subject = subjects.get(selection.removeprefix("subject:"))
            if selection == "none":
                badges[uid] = None
            elif selection == "streak" and st.get("current_streak", 0) >= 3:
                badges[uid] = {
                    "type": "streak", "icon": "🔥", "label": "꾸준러",
                    "title": f"오늘의 퀴즈 {st['current_streak']}일 연속",
                }
            elif selection == "score" or not subject:
                badges[uid] = None if st["total_score"] <= 0 else {
                    "type": "score", "icon": "⚡",
                    "label": f"{st['total_score']}점",
                    "title": f"퀴즈 누적 {st['total_score']}점",
                }
            elif subject:
                badges[uid] = _subject_quiz_badge(
                    subject["category"], int(subject["subject_rank"]),
                    int(subject["subject_score"]),
                )
            else:
                badges[uid] = None if st["total_score"] <= 0 else {
                    "type": "score", "icon": "⚡", "label": f"{st['total_score']}점",
                    "title": f"퀴즈 누적 {st['total_score']}점",
                }

    return badges
