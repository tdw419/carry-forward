"""Shared test fixtures for carry_forward tests."""
import pytest
import sqlite3
import os
import sys
import time
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def _create_state_db(path):
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            parent_session_id TEXT,
            source TEXT,
            message_count INTEGER DEFAULT 0,
            tool_call_count INTEGER DEFAULT 0,
            started_at REAL,
            model TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT
        )
    """)
    conn.commit()
    conn.close()


def _create_carry_db(path):
    conn = sqlite3.connect(path)
    for ddl in [
        """CREATE TABLE IF NOT EXISTS blockers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, message TEXT NOT NULL,
            created_at REAL NOT NULL, resolved_at REAL)""",
        """CREATE TABLE IF NOT EXISTS chain_meta (
            session_id TEXT PRIMARY KEY, parent_session_id TEXT,
            continuation_count INTEGER DEFAULT 0, outcome TEXT,
            project_dir TEXT, created_at REAL NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS learned_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT NOT NULL, pattern_key TEXT NOT NULL,
            observation TEXT NOT NULL, sample_size INTEGER DEFAULT 1,
            last_seen REAL NOT NULL, created_at REAL NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS chain_git_heads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL, project_dir TEXT NOT NULL,
            git_head TEXT NOT NULL, recorded_at REAL NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS decision_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, decision TEXT NOT NULL,
            reasons_json TEXT, thresholds_json TEXT,
            can_continue INTEGER NOT NULL, created_at REAL NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS decision_outcomes (
            decision_id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            outcome_productive INTEGER, outcome_git_moved INTEGER,
            outcome_chain_continued INTEGER,
            outcome_tool_calls INTEGER DEFAULT 0,
            outcome_message_count INTEGER DEFAULT 0,
            checked_at REAL NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY, value TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'default',
            updated_at REAL NOT NULL)""",
    ]:
        conn.execute(ddl)
    conn.commit()
    conn.close()


@pytest.fixture
def state_db(tmp_path):
    path = str(tmp_path / "state.db")
    _create_state_db(path)
    return path


@pytest.fixture
def carry_db(tmp_path):
    path = str(tmp_path / "carry_forward.db")
    _create_carry_db(path)
    return path


@pytest.fixture
def patched_env(state_db, carry_db):
    import carry_forward as cf
    with patch.object(cf, 'DB_PATH', state_db), \
         patch.object(cf, 'CARRY_DB_PATH', carry_db), \
         patch.object(cf, 'get_conn', lambda: sqlite3.connect(state_db)), \
         patch.object(cf, 'get_carry_conn', lambda: sqlite3.connect(carry_db)):
        yield cf


def insert_session(state_db, sid, parent=None, source='cli', msgs=0, tools=0, ts=None):
    conn = sqlite3.connect(state_db)
    conn.execute(
        "INSERT INTO sessions (id, parent_session_id, source, message_count, tool_call_count, started_at) VALUES (?, ?, ?, ?, ?, ?)",
        (sid, parent, source, msgs, tools, ts or time.time())
    )
    conn.commit()
    conn.close()


def insert_blocker(carry_db, message, age_hours=5, resolved=False):
    ts = time.time() - (age_hours * 3600)
    conn = sqlite3.connect(carry_db)
    conn.execute(
        "INSERT INTO blockers (session_id, message, created_at, resolved_at) VALUES (?, ?, ?, ?)",
        ("test_session", message, ts, time.time() if resolved else None)
    )
    conn.commit()
    conn.close()


def insert_git_heads(carry_db, session_id, project_dir, git_head):
    conn = sqlite3.connect(carry_db)
    conn.execute(
        "INSERT INTO chain_git_heads (session_id, project_dir, git_head, recorded_at) VALUES (?, ?, ?, ?)",
        (session_id, project_dir, git_head, time.time())
    )
    conn.commit()
    conn.close()


def insert_config(carry_db, key, value, source='default'):
    conn = sqlite3.connect(carry_db)
    conn.execute(
        "INSERT INTO config (key, value, source, updated_at) VALUES (?, ?, ?, ?)",
        (key, str(value), source, time.time())
    )
    conn.commit()
    conn.close()


def insert_chain(state_db, n, alive=True):
    """Insert a chain of *n* sessions, each parented to the previous one.

    If *alive* is True each session gets 5 tools / 10 msgs; otherwise 0/0.
    Returns the list of session IDs (earliest first).
    """
    ids = []
    for i in range(n):
        sid = f"chain_{i}"
        parent = ids[-1] if ids else None
        msgs, tools = (10, 5) if alive else (0, 0)
        insert_session(state_db, sid, parent=parent, msgs=msgs, tools=tools)
        ids.append(sid)
    return ids
