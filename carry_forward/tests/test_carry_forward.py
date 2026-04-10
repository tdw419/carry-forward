"""Tests for carry_forward v5.2 decision pipeline.

Uses temporary SQLite databases to avoid touching real state.
Mocks get_conn() and get_carry_conn() to point to test DBs.
"""
import pytest
import sqlite3
import os
import sys
import tempfile
from unittest.mock import patch
from datetime import datetime

# Ensure carry_forward module is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


# ---------------------------------------------------------------------------
# Fixtures: temp databases
# ---------------------------------------------------------------------------

@pytest.fixture
def state_db(tmp_path):
    """Create a temporary state.db with the sessions table."""
    db_path = str(tmp_path / "state.db")
    conn = sqlite3.connect(db_path)
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
    return db_path


@pytest.fixture
def carry_db(tmp_path):
    """Create a temporary carry_forward.db with all required tables."""
    db_path = str(tmp_path / "carry_forward.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS blockers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            message TEXT NOT NULL,
            created_at REAL NOT NULL,
            resolved_at REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chain_meta (
            session_id TEXT PRIMARY KEY,
            parent_session_id TEXT,
            continuation_count INTEGER DEFAULT 0,
            outcome TEXT,
            project_dir TEXT,
            created_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS learned_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT NOT NULL,
            pattern_key TEXT NOT NULL,
            observation TEXT NOT NULL,
            sample_size INTEGER DEFAULT 1,
            last_seen REAL NOT NULL,
            created_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chain_git_heads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            project_dir TEXT NOT NULL,
            git_head TEXT NOT NULL,
            recorded_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS decision_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            decision TEXT NOT NULL,
            reasons_json TEXT,
            thresholds_json TEXT,
            can_continue INTEGER NOT NULL,
            created_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS decision_outcomes (
            decision_id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            outcome_productive INTEGER,
            outcome_git_moved INTEGER,
            outcome_chain_continued INTEGER,
            outcome_tool_calls INTEGER DEFAULT 0,
            outcome_message_count INTEGER DEFAULT 0,
            checked_at REAL NOT NULL,
            FOREIGN KEY (decision_id) REFERENCES decision_log(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'default',
            updated_at REAL NOT NULL
        )
    """)
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def patched_env(state_db, carry_db):
    """Patch get_conn and get_carry_conn to use temp databases."""
    import carry_forward as cf
    with patch.object(cf, 'DB_PATH', state_db), \
         patch.object(cf, 'CARRY_DB_PATH', carry_db), \
         patch.object(cf, 'get_conn', lambda: sqlite3.connect(state_db)), \
         patch.object(cf, 'get_carry_conn', lambda: sqlite3.connect(carry_db)):
        yield cf


def insert_session(state_db, sid, parent=None, source='cli', msgs=0, tools=0):
    """Helper to insert a session into state.db."""
    conn = sqlite3.connect(state_db)
    conn.execute(
        "INSERT INTO sessions (id, parent_session_id, source, message_count, tool_call_count, started_at) VALUES (?, ?, ?, ?, ?, ?)",
        (sid, parent, source, msgs, tools, datetime.now().timestamp())
    )
    conn.commit()
    conn.close()


def insert_blocker(carry_db, message, age_hours=5, resolved=False):
    """Helper to insert a blocker."""
    conn = sqlite3.connect(carry_db)
    import time
    ts = time.time() - (age_hours * 3600)
    conn.execute(
        "INSERT INTO blockers (session_id, message, created_at, resolved_at) VALUES (?, ?, ?, ?)",
        ("test_session", message, ts, time.time() if resolved else None)
    )
    conn.commit()
    conn.close()


def insert_git_heads(carry_db, session_id, project_dir, git_head):
    """Helper to insert git head snapshots."""
    conn = sqlite3.connect(carry_db)
    import time
    conn.execute(
        "INSERT INTO chain_git_heads (session_id, project_dir, git_head, recorded_at) VALUES (?, ?, ?, ?)",
        (session_id, project_dir, git_head, time.time())
    )
    conn.commit()
    conn.close()


def insert_config(carry_db, key, value, source='default'):
    """Helper to set a config value."""
    import time
    conn = sqlite3.connect(carry_db)
    conn.execute(
        "INSERT INTO config (key, value, source, updated_at) VALUES (?, ?, ?, ?)",
        (key, str(value), source, time.time())
    )
    conn.commit()
    conn.close()


# ===========================================================================
# Tests: check_can_continue
# ===========================================================================

class TestCheckCanContinue:
    """Tests for the v5.2 decision pipeline."""

    def test_active_session_continues(self, state_db, carry_db, patched_env):
        """Session with tools > 0 should always continue."""
        insert_session(state_db, "active_1", msgs=10, tools=5)
        result = patched_env.check_can_continue("active_1")
        assert result["can_continue"] is True

    def test_dead_session_halts(self, state_db, carry_db, patched_env):
        """Session with 0 tools and <=2 messages should halt."""
        insert_session(state_db, "dead_1", msgs=1, tools=0)
        result = patched_env.check_can_continue("dead_1")
        assert result["can_continue"] is False
        assert result["session_dead"] is True
        assert any("Session dead" in r for r in result["reasons"])

    def test_dead_session_0_msgs_halts(self, state_db, carry_db, patched_env):
        """Session with 0 tools and 0 messages should halt."""
        insert_session(state_db, "dead_0", msgs=0, tools=0)
        result = patched_env.check_can_continue("dead_0")
        assert result["can_continue"] is False
        assert result["session_dead"] is True

    def test_zero_tools_3_msgs_continues(self, state_db, carry_db, patched_env):
        """Session with 0 tools but 3+ messages is not dead (>2 threshold)."""
        insert_session(state_db, "msgs_3", msgs=3, tools=0)
        result = patched_env.check_can_continue("msgs_3")
        # Not dead (msgs > 2), not thrashing (only 1 session)
        assert result["can_continue"] is True
        assert result["session_dead"] is False

    def test_parent_dead_session_active_continues(self, state_db, carry_db, patched_env):
        """Active session should continue even if parent was dead."""
        insert_session(state_db, "parent_dead", msgs=0, tools=0)
        insert_session(state_db, "child_active", parent="parent_dead", msgs=10, tools=15)
        result = patched_env.check_can_continue("child_active")
        assert result["can_continue"] is True

    def test_parent_dead_session_dead_halts(self, state_db, carry_db, patched_env):
        """Dead session with dead parent should halt."""
        insert_session(state_db, "parent_dead_2", msgs=0, tools=0)
        insert_session(state_db, "child_dead", parent="parent_dead_2", msgs=1, tools=0)
        result = patched_env.check_can_continue("child_dead")
        assert result["can_continue"] is False
        assert result["session_dead"] is True

    def test_parent_active_continues(self, state_db, carry_db, patched_env):
        """Session with active parent should continue normally."""
        insert_session(state_db, "parent_ok", msgs=20, tools=10)
        insert_session(state_db, "child_ok", parent="parent_ok", msgs=5, tools=3)
        result = patched_env.check_can_continue("child_ok")
        assert result["can_continue"] is True

    def test_stale_blocker_halts(self, state_db, carry_db, patched_env):
        """Stale blocker (>4h old) should halt."""
        insert_session(state_db, "blocked_1", msgs=10, tools=5)
        insert_blocker(carry_db, "waiting for API key", age_hours=6)
        result = patched_env.check_can_continue("blocked_1")
        assert result["can_continue"] is False
        assert result["blocker_halt"] is True
        assert any("Stale blocker" in r for r in result["reasons"])

    def test_recent_blocker_no_halt(self, state_db, carry_db, patched_env):
        """Recent blocker (<4h old) should NOT halt."""
        insert_session(state_db, "blocked_recent", msgs=10, tools=5)
        insert_blocker(carry_db, "waiting for review", age_hours=1)
        result = patched_env.check_can_continue("blocked_recent")
        assert result["can_continue"] is True
        assert result["blocker_halt"] is False

    def test_resolved_blocker_no_halt(self, state_db, carry_db, patched_env):
        """Resolved blocker should not affect continuation."""
        insert_session(state_db, "resolved_1", msgs=10, tools=5)
        insert_blocker(carry_db, "was blocked", age_hours=10, resolved=True)
        result = patched_env.check_can_continue("resolved_1")
        assert result["can_continue"] is True

    def test_thrashing_active_session_continues(self, state_db, carry_db, patched_env):
        """Active session should continue even in a thrashing chain."""
        # Set low thresholds to trigger thrashing
        insert_config(carry_db, "dead_session_threshold", "1")
        insert_config(carry_db, "dead_lookback", "3")
        insert_config(carry_db, "orphan_child_threshold", "10")

        # Create chain: grandparent -> parent(dead) -> current(active)
        insert_session(state_db, "gp_1", msgs=5, tools=2)
        insert_session(state_db, "p_dead", parent="gp_1", msgs=0, tools=0)
        insert_session(state_db, "active_in_thrash", parent="p_dead", msgs=20, tools=15)

        result = patched_env.check_can_continue("active_in_thrash")
        assert result["can_continue"] is True
        # Thrash should be overridden
        assert result["thrashing"] is False

    def test_thrashing_dead_session_halts(self, state_db, carry_db, patched_env):
        """Dead session in thrashing chain should halt."""
        insert_config(carry_db, "dead_session_threshold", "1")
        insert_config(carry_db, "dead_lookback", "3")

        insert_session(state_db, "gp_2", msgs=0, tools=0)
        insert_session(state_db, "p_dead_2", parent="gp_2", msgs=0, tools=0)
        insert_session(state_db, "dead_in_thrash", parent="p_dead_2", msgs=0, tools=0)

        result = patched_env.check_can_continue("dead_in_thrash")
        assert result["can_continue"] is False

    def test_no_session_id_resolves(self, state_db, carry_db, patched_env):
        """When no session_id given, should resolve to latest session."""
        insert_session(state_db, "old_one", msgs=5, tools=2, source='cli')
        import time
        conn = sqlite3.connect(state_db)
        conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?",
                     (time.time() - 100, "old_one"))
        conn.commit()
        conn.close()

        insert_session(state_db, "latest_one", msgs=10, tools=3, source='cli')
        conn = sqlite3.connect(state_db)
        conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?",
                     (time.time(), "latest_one"))
        conn.commit()
        conn.close()

        result = patched_env.check_can_continue()
        assert result["can_continue"] is True

    def test_git_stall_active_session_continues(self, state_db, carry_db, patched_env):
        """Git stalled but session has tools -> should continue (v5.2 fix)."""
        insert_config(carry_db, "git_min_sessions", "2")

        # Create chain of 3 sessions
        insert_session(state_db, "g1", msgs=5, tools=2)
        insert_session(state_db, "g2", parent="g1", msgs=5, tools=2)
        insert_session(state_db, "g3", parent="g2", msgs=10, tools=8)

        # Same git head at start and end = stalled
        insert_git_heads(carry_db, "g1", "/project", "abc123")
        insert_git_heads(carry_db, "g3", "/project", "abc123")

        result = patched_env.check_can_continue("g3")
        # v5.2: git stall does NOT force halt
        assert result["can_continue"] is True
        # Should note git stall in guard_rails
        assert any("Git stalled" in gr for gr in result["guard_rails"])

    def test_returns_decision_id(self, state_db, carry_db, patched_env):
        """Each call should return a decision_id for outcome tracking."""
        insert_session(state_db, "dec_test", msgs=5, tools=2)
        result = patched_env.check_can_continue("dec_test")
        assert "decision_id" in result
        assert result["decision_id"] is not None

    def test_decision_logged(self, state_db, carry_db, patched_env):
        """Decision should be logged to decision_log table."""
        insert_session(state_db, "log_test", msgs=5, tools=2)
        result = patched_env.check_can_continue("log_test")

        conn = sqlite3.connect(carry_db)
        row = conn.execute(
            "SELECT decision, session_id FROM decision_log WHERE id = ?",
            (result["decision_id"],)
        ).fetchone()
        conn.close()

        assert row is not None
        assert row[0] == "continue"
        assert row[1] == "log_test"


# ===========================================================================
# Tests: get_threshold / set_threshold
# ===========================================================================

class TestThresholds:
    def test_default_threshold(self, carry_db, patched_env):
        """Should return default when no config entry."""
        val = patched_env.get_threshold("dead_session_threshold")
        assert val == "3"

    def test_set_and_get_threshold(self, carry_db, patched_env):
        """set_threshold should persist and get_threshold should read it."""
        patched_env.set_threshold("dead_session_threshold", 1, source="test")
        val = patched_env.get_threshold("dead_session_threshold")
        assert val == "1"

    def test_unknown_threshold_returns_none(self, carry_db, patched_env):
        """Unknown threshold key should return None."""
        val = patched_env.get_threshold("nonexistent_key")
        assert val is None


# ===========================================================================
# Tests: detect_thrash
# ===========================================================================

class TestDetectThrash:
    def test_short_chain_not_thrashing(self, state_db, carry_db, patched_env):
        """Single session chain should not be thrashing."""
        insert_session(state_db, "single", msgs=5, tools=2)
        thrashing, dead_count, chain, details = patched_env.detect_thrash("single")
        assert thrashing is False

    def test_chain_with_dead_sessions_thrashing(self, state_db, carry_db, patched_env):
        """Chain of all dead sessions should be thrashing."""
        insert_config(carry_db, "dead_session_threshold", "2")
        insert_config(carry_db, "dead_lookback", "5")

        insert_session(state_db, "anc1", msgs=0, tools=0)
        insert_session(state_db, "anc2", parent="anc1", msgs=0, tools=0)
        insert_session(state_db, "anc3", parent="anc2", msgs=0, tools=0)

        thrashing, dead_count, chain, details = patched_env.detect_thrash("anc3")
        assert dead_count >= 2
        assert thrashing is True

    def test_chain_with_mixed_sessions(self, state_db, carry_db, patched_env):
        """Chain with some dead, some alive should not be thrashing with default threshold."""
        insert_config(carry_db, "dead_session_threshold", "3")

        insert_session(state_db, "mix1", msgs=10, tools=5)
        insert_session(state_db, "mix2", parent="mix1", msgs=0, tools=0)
        insert_session(state_db, "mix3", parent="mix2", msgs=10, tools=5)

        thrashing, dead_count, chain, details = patched_env.detect_thrash("mix3")
        assert dead_count < 3  # Only 1 dead in the chain


# ===========================================================================
# Tests: blockers
# ===========================================================================

class TestBlockers:
    def test_block_and_list(self, carry_db, patched_env):
        """block() should create a blocker in the DB."""
        patched_env.cmd_block("test blocker reason")
        import sqlite3
        conn = sqlite3.connect(carry_db)
        row = conn.execute("SELECT COUNT(*) FROM blockers WHERE message LIKE '%test blocker reason%'").fetchone()
        conn.close()
        assert row[0] >= 1

    def test_unblock(self, carry_db, patched_env):
        """unblock() should resolve matching blockers."""
        patched_env.cmd_block("will be removed")
        patched_env.cmd_unblock("will be removed")
        import sqlite3
        conn = sqlite3.connect(carry_db)
        row = conn.execute("SELECT COUNT(*) FROM blockers WHERE message LIKE '%will be removed%' AND resolved_at IS NULL").fetchone()
        conn.close()
        assert row[0] == 0


# ===========================================================================
# Tests: check_git_progress
# ===========================================================================

class TestGitProgress:
    def test_short_chain_passes(self, state_db, carry_db, patched_env):
        """Short chain should always pass git progress check."""
        insert_session(state_db, "short1", msgs=5, tools=2)
        ok, details = patched_env.check_git_progress("short1")
        assert ok is True

    def test_no_git_heads_passes(self, state_db, carry_db, patched_env):
        """No recorded git heads should pass (first run)."""
        insert_session(state_db, "gh1", msgs=5, tools=2)
        insert_session(state_db, "gh2", parent="gh1", msgs=5, tools=2)
        insert_session(state_db, "gh3", parent="gh2", msgs=5, tools=2)
        ok, details = patched_env.check_git_progress("gh3")
        assert ok is True  # no heads recorded = first run

    def test_git_moved_passes(self, state_db, carry_db, patched_env):
        """Different git HEADs across chain should pass."""
        insert_session(state_db, "gm1", msgs=5, tools=2)
        insert_session(state_db, "gm2", parent="gm1", msgs=5, tools=2)
        insert_session(state_db, "gm3", parent="gm2", msgs=5, tools=2)

        insert_git_heads(carry_db, "gm1", "/project", "aaa111")
        insert_git_heads(carry_db, "gm3", "/project", "bbb222")

        ok, details = patched_env.check_git_progress("gm3")
        assert ok is True

    def test_git_stalled_detected(self, state_db, carry_db, patched_env):
        """Same git HEADs across long chain should detect stall."""
        insert_config(carry_db, "git_min_sessions", "2")

        insert_session(state_db, "gs1", msgs=5, tools=2)
        insert_session(state_db, "gs2", parent="gs1", msgs=5, tools=2)
        insert_session(state_db, "gs3", parent="gs2", msgs=5, tools=2)

        insert_git_heads(carry_db, "gs1", "/project", "same123")
        insert_git_heads(carry_db, "gs3", "/project", "same123")

        ok, details = patched_env.check_git_progress("gs3")
        assert ok is False
        assert "unchanged" in details


# ===========================================================================
# Tests: record_outcome
# ===========================================================================

class TestRecordOutcome:
    def test_record_outcome_for_productive(self, state_db, carry_db, patched_env):
        """Should record productive outcome for a session with tool calls."""
        insert_session(state_db, "prod_1", msgs=10, tools=5)

        # First, make a decision
        result = patched_env.check_can_continue("prod_1")
        dec_id = result["decision_id"]

        # Record outcome
        outcome = patched_env.record_outcome("prod_1")
        assert outcome["outcome"]["productive"] is True

    def test_record_outcome_for_unproductive(self, state_db, carry_db, patched_env):
        """Should record unproductive outcome for a session with no tool calls."""
        insert_session(state_db, "unprod_1", msgs=1, tools=0)

        result = patched_env.check_can_continue("unprod_1")
        outcome = patched_env.record_outcome("unprod_1")
        assert outcome["outcome"]["productive"] is False


# ===========================================================================
# Tests: cmd_should_continue exit codes
# ===========================================================================

class TestShouldContinue:
    def test_active_exits_zero(self, state_db, carry_db, patched_env):
        """Active session should cause exit code 0."""
        insert_session(state_db, "sc_active", msgs=10, tools=5)
        with pytest.raises(SystemExit) as exc_info:
            patched_env.cmd_should_continue("sc_active")
        assert exc_info.value.code == 0

    def test_dead_exits_one(self, state_db, carry_db, patched_env):
        """Dead session should cause exit code 1."""
        insert_session(state_db, "sc_dead", msgs=0, tools=0)
        with pytest.raises(SystemExit) as exc_info:
            patched_env.cmd_should_continue("sc_dead")
        assert exc_info.value.code == 1
