#!/usr/bin/env python3
"""
Carry Forward v5.4 - self-tuning intelligence layer for Hermes session continuity.
Transport, not comprehension. Packages what happened; agents do the interpretation.
v5: Decisions are logged, outcomes are tracked, thresholds are calibrated from data.

Usage:
    carry_forward.py context [--include-cron]     # Full context from last session (recommended)
    carry_forward.py status                       # Git-aware project state for detected projects
    carry_forward.py summary [SESSION_ID]         # Smart summary of session progress
    carry_forward.py last [--depth N]             # Last N non-trivial sessions
    carry_forward.py messages SESSION_ID [--last N]  # Messages from a session
    carry_forward.py last-id                      # Just the last session ID
    carry_forward.py chain [SESSION_ID]           # Trace the continuation chain
    carry_forward.py blockers                     # Show unresolved blockers
    carry_forward.py block <message>              # Record a blocker
    carry_forward.py unblock <pattern>            # Remove blockers matching pattern
    carry_forward.py should-continue              # Exit 0 if safe to chain, 1 if not
    carry_forward.py check-can-continue [SESSION] # JSON: full continuation decision
    carry_forward.py record-git-heads SESSION_ID  # Snapshot git HEADs for thrash detection
    carry_forward.py learn                        # Analyze session history, record patterns
    carry_forward.py record-outcome [SESSION_ID]  # Record outcome of a past decision
    carry_forward.py calibrate [--project DIR]       # Auto-tune thresholds (global or per-project)
    carry_forward.py show-config [--project DIR]     # Show current threshold values
    carry_forward.py health [--json]                 # Session health dashboard for today
    carry_forward.py roadmap                      # Show roadmap progress for detected projects
    carry_forward.py analyze-patterns [DIR]       # Extract file-level patterns from session history
"""
import sqlite3
import subprocess
import re
import sys
import os
import json
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from roadmap_integration import (
    scan_project_roadmaps, format_roadmap_context, roadmap_completion_signal,
    HAS_ROADMAP_BUILDER,
)

DB_PATH = "/home/jericho/.hermes/state.db"
CARRY_DB_PATH = os.path.expanduser("~/.hermes/carry_forward.db")

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

# Decision thresholds (defaults -- can be overridden via config table)
DEAD_SESSION_THRESHOLD = 3
DEAD_LOOKBACK = 5
ORPHAN_CHILD_THRESHOLD = 10
BLOCKER_HALT_HOURS = 4.0
GIT_MIN_SESSIONS = 3
STAGNATION_STALL_LIMIT = 3  # consecutive no-commit ticks before hard halt
HALLUCINATION_LOOP_LIMIT = 3  # same files in N consecutive ticks = hallucination loop
TEST_REGRESSION_THRESHOLD = 2  # drop of N+ tests in single tick = regression halt
NOOP_LIMIT = 3  # consecutive no-op ticks before hard halt

# ---------------------------------------------------------------------------
# Config helpers -- read/write tunable thresholds from DB
# ---------------------------------------------------------------------------

# Mapping of threshold constants to their config keys and types
THRESHOLD_DEFS = {
    "dead_session_threshold": {"default": DEAD_SESSION_THRESHOLD, "type": int, "min": 1, "max": 10},
    "dead_lookback": {"default": DEAD_LOOKBACK, "type": int, "min": 3, "max": 20},
    "orphan_child_threshold": {"default": ORPHAN_CHILD_THRESHOLD, "type": int, "min": 3, "max": 30},
    "blocker_halt_hours": {"default": BLOCKER_HALT_HOURS, "type": float, "min": 1.0, "max": 24.0},
    "git_min_sessions": {"default": GIT_MIN_SESSIONS, "type": int, "min": 2, "max": 10},
    "stagnation_stall_limit": {"default": STAGNATION_STALL_LIMIT, "type": int, "min": 2, "max": 10},
    "hallucination_loop_limit": {"default": HALLUCINATION_LOOP_LIMIT, "type": int, "min": 2, "max": 10},
    "test_regression_threshold": {"default": TEST_REGRESSION_THRESHOLD, "type": int, "min": 1, "max": 20},
    "noop_limit": {"default": NOOP_LIMIT, "type": int, "min": 2, "max": 10},
}

# Thresholds that are eligible for per-project overrides (stall/noop are the ones
# that vary most by project type -- Rust's cargo test is slower than Python's pytest).
PROJECT_OVERRIDABLE = {"stagnation_stall_limit", "noop_limit", "hallucination_loop_limit"}

# ---------------------------------------------------------------------------
# Project type detection
# ---------------------------------------------------------------------------

# Maps filenames to project type labels
PROJECT_TYPE_MARKERS = {
    "Cargo.toml": "rust",
    "pyproject.toml": "python",
    "setup.py": "python",
    "setup.cfg": "python",
    "package.json": "node",
    "go.mod": "go",
    "Makefile": "make",
    "pom.xml": "java",
    "build.gradle": "java",
}

# Default threshold multipliers by project type.
# Rust builds are slower (compilation), so stall/noop limits should be higher.
# Python cycles are fast, so defaults are fine.
PROJECT_TYPE_DEFAULTS = {
    "rust": {"stagnation_stall_limit": 5, "noop_limit": 4, "hallucination_loop_limit": 4},
    "python": {},  # uses global defaults
    "node": {"stagnation_stall_limit": 4, "noop_limit": 4},
    "go": {"stagnation_stall_limit": 4, "noop_limit": 4},
    "make": {"stagnation_stall_limit": 4, "noop_limit": 4},
    "java": {"stagnation_stall_limit": 5, "noop_limit": 4, "hallucination_loop_limit": 4},
}

# Test commands by project type (Phase 8).
# Maps detected project type to the test command the agent should run.
TEST_COMMAND_MAP: Dict[str, str] = {
    "rust": "cargo test",
    "python": "pytest",
    "node": "npm test",
    "go": "go test ./...",
    "make": "make test",
    "java": "mvn test",
}


def _read_config(key: str) -> Optional[str]:
    """Read a config value from the carry_forward DB. Returns None if not set."""
    conn = get_carry_conn()
    row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row[0] if row else None


def _write_config(key: str, value: str, source: str = "calibration") -> None:
    """Write a config value to the carry_forward DB."""
    conn = get_carry_conn()
    conn.execute(
        "INSERT OR REPLACE INTO config (key, value, source, updated_at) VALUES (?, ?, ?, ?)",
        (key, value, source, time.time())
    )
    conn.commit()
    conn.close()


def get_threshold(key: str) -> Any:
    """Get a threshold value. Checks config table first, falls back to constant."""
    if key not in THRESHOLD_DEFS:
        raise ValueError(f"Unknown threshold: {key}")
    defn = THRESHOLD_DEFS[key]
    val_str = _read_config(key)
    if val_str is not None:
        return defn["type"](val_str)
    return defn["default"]


def get_all_thresholds() -> Dict[str, Any]:
    """Return all threshold values (from config or defaults)."""
    result = {}
    for key, defn in THRESHOLD_DEFS.items():
        val_str = _read_config(key)
        if val_str is not None:
            result[key] = defn["type"](val_str)
        else:
            result[key] = defn["default"]
    return result


# ---------------------------------------------------------------------------
# Project type detection & per-project thresholds (Phase 7)
# ---------------------------------------------------------------------------

def detect_project_type(project_dir: str) -> Optional[str]:
    """Detect the project type by looking for marker files.

    Walks up from project_dir to find the git root, then checks for
    known marker files. Returns a type string like 'rust', 'python', etc.
    """
    # Find the actual project root (git root or the dir itself)
    check_dir = project_dir
    while check_dir and check_dir != "/":
        if os.path.isdir(os.path.join(check_dir, ".git")):
            break
        parent = os.path.dirname(check_dir)
        if parent == check_dir:
            break
        check_dir = parent

    if not os.path.isdir(check_dir):
        return None

    # Check for marker files (order matters -- first match wins)
    for marker, ptype in PROJECT_TYPE_MARKERS.items():
        if os.path.isfile(os.path.join(check_dir, marker)):
            return ptype

    return None


def detect_test_command(project_dir: str) -> Optional[str]:
    """Detect the test command for a project directory.

    Uses detect_project_type() to identify the project type, then looks up
    the corresponding test command in TEST_COMMAND_MAP.

    Returns the test command string (e.g. "cargo test", "pytest"), or None
    if the project type cannot be determined or has no known test command.
    """
    ptype = detect_project_type(project_dir)
    if not ptype:
        return None
    return TEST_COMMAND_MAP.get(ptype)


def _read_project_config(project_dir: str, key: str) -> Optional[str]:
    """Read a project-specific threshold value from the project_thresholds table."""
    conn = get_carry_conn()
    row = conn.execute(
        "SELECT value FROM project_thresholds WHERE project_dir = ? AND key = ?",
        (project_dir, key)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def _write_project_config(project_dir: str, key: str, value: str,
                          source: str = "calibration") -> None:
    """Write a project-specific threshold value."""
    conn = get_carry_conn()
    conn.execute(
        """INSERT OR REPLACE INTO project_thresholds
           (project_dir, key, value, source, project_type, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (project_dir, key, value, source, detect_project_type(project_dir) or "unknown", time.time())
    )
    conn.commit()
    conn.close()


def get_threshold_for_project(key: str, project_dir: Optional[str] = None) -> Any:
    """Get a threshold value with project-specific overrides.

    Resolution order:
    1. project_thresholds table (explicit per-project override)
    2. PROJECT_TYPE_DEFAULTS (defaults for this project type, auto-detected)
    3. config table (global override)
    4. THRESHOLD_DEFS default constant

    Only keys in PROJECT_OVERRIDABLE are eligible for per-project overrides.
    """
    if key not in THRESHOLD_DEFS:
        raise ValueError(f"Unknown threshold: {key}")

    defn = THRESHOLD_DEFS[key]

    # Step 1: Check explicit per-project override
    if project_dir and key in PROJECT_OVERRIDABLE:
        val_str = _read_project_config(project_dir, key)
        if val_str is not None:
            return defn["type"](val_str)

    # Step 2: Check project type defaults (only for overridable keys)
    if project_dir and key in PROJECT_OVERRIDABLE:
        ptype = detect_project_type(project_dir)
        if ptype and ptype in PROJECT_TYPE_DEFAULTS:
            type_defaults = PROJECT_TYPE_DEFAULTS[ptype]
            if key in type_defaults:
                return type_defaults[key]

    # Step 3: Fall back to global config / default
    return get_threshold(key)


def get_all_thresholds_for_project(project_dir: Optional[str] = None) -> Dict[str, Any]:
    """Return all threshold values with project-specific overrides applied."""
    result = {}
    for key in THRESHOLD_DEFS:
        result[key] = get_threshold_for_project(key, project_dir)
    return result


def _resolve_project_dir(session_id: Optional[str]) -> Optional[str]:
    """Resolve the primary project directory for a session.

    Checks chain_meta.project_dir first, then falls back to the
    most common project_dir in chain_git_heads for this session.
    """
    carry_conn = get_carry_conn()

    # Try chain_meta first
    if session_id:
        row = carry_conn.execute(
            "SELECT project_dir FROM chain_meta WHERE session_id = ? AND project_dir IS NOT NULL",
            (session_id,)
        ).fetchone()
        if row and row[0]:
            carry_conn.close()
            return row[0]

        # Fall back to chain_git_heads -- pick the most recent entry
        row = carry_conn.execute(
            "SELECT project_dir FROM chain_git_heads WHERE session_id = ? ORDER BY recorded_at DESC LIMIT 1",
            (session_id,)
        ).fetchone()
        if row and row[0]:
            carry_conn.close()
            return row[0]

    carry_conn.close()
    return None


def _get_chain_stalls(session_id: str) -> int:
    """Walk the parent chain and count consecutive sessions with no git progress.

    Returns the number of consecutive stalls (0 if latest session had a commit).
    """
    conn = get_conn()
    carry_conn = get_carry_conn()

    # Walk chain to get ordered list (oldest first)
    chain = []
    current = session_id
    visited = set()
    while current and current not in visited and len(chain) < 20:
        visited.add(current)
        conn_row = conn.execute(
            "SELECT id, parent_session_id FROM sessions WHERE id = ?", (current,)
        ).fetchone()
        if not conn_row:
            break
        chain.append(conn_row[0])
        current = conn_row[1]
    conn.close()

    if not chain:
        carry_conn.close()
        return 0

    # Walk from newest to oldest, counting consecutive stalls
    stalls = 0
    for i, sid in enumerate(chain):
        heads = carry_conn.execute(
            "SELECT project_dir, git_head FROM chain_git_heads WHERE session_id = ?",
            (sid,)
        ).fetchall()
        if not heads:
            # No git heads recorded -- can't determine, assume ok
            break

        # Check if any project moved compared to parent (chain[i+1] is older/parent)
        parent_idx = i + 1
        if parent_idx < len(chain):
            parent_heads = carry_conn.execute(
                "SELECT project_dir, git_head FROM chain_git_heads WHERE session_id = ?",
                (chain[parent_idx],)
            ).fetchall()
            parent_map = {d: h for d, h in parent_heads}
            moved = False
            for d, h in heads:
                if d in parent_map and parent_map[d] != h:
                    moved = True
                    break
            if moved:
                break  # Commit found -- stop counting

        stalls += 1

    carry_conn.close()
    return stalls


def _update_stall_counter(session_id: str, stalled: bool) -> None:
    """Update the consecutive_stalls counter for a session in chain_meta."""
    carry_conn = get_carry_conn()
    existing = carry_conn.execute(
        "SELECT consecutive_stalls FROM chain_meta WHERE session_id = ?",
        (session_id,)
    ).fetchone()

    if existing is not None:
        new_count = (existing[0] + 1) if stalled else 0
        carry_conn.execute(
            "UPDATE chain_meta SET consecutive_stalls = ? WHERE session_id = ?",
            (new_count, session_id)
        )
    else:
        new_count = 1 if stalled else 0
        carry_conn.execute(
            """INSERT INTO chain_meta (session_id, continuation_count, created_at, consecutive_stalls)
               VALUES (?, 0, ?, ?)""",
            (session_id, time.time(), new_count)
        )
    carry_conn.commit()
    carry_conn.close()


def record_tick_changes(session_id: str, tick_number: int, files_changed: List[str],
                        committed: bool) -> None:
    """Record which files changed in a tick for hallucination loop detection.

    Called after each tick completes. files_changed is a list of file paths
    relative to the project root. committed is True if the tick produced a commit.
    """
    carry_conn = get_carry_conn()
    carry_conn.execute("""
        INSERT INTO tick_file_changes (session_id, tick_number, files_changed_json, committed, recorded_at)
        VALUES (?, ?, ?, ?, ?)
    """, (session_id, tick_number, json.dumps(sorted(files_changed)),
          1 if committed else 0, time.time()))
    carry_conn.commit()
    carry_conn.close()


def _detect_hallucination_loop(session_id: str, lookback: int = 3) -> Tuple[bool, List[str], str]:
    """Check if the agent is editing the same files repeatedly without progress.

    Walks the parent chain backwards, looking at the last `lookback` ticks.
    If the same file(s) appear in all of them with no commit, it's a hallucination loop.

    Returns:
        is_loop: True if hallucination loop detected.
        common_files: The files that appear in every tick.
        details: Human-readable description.
    """
    conn = get_conn()
    carry_conn = get_carry_conn()

    # Walk chain to get recent sessions
    chain = []
    current = session_id
    visited = set()
    while current and current not in visited and len(chain) < 20:
        visited.add(current)
        row = conn.execute(
            "SELECT id, parent_session_id FROM sessions WHERE id = ?", (current,)
        ).fetchone()
        if not row:
            break
        chain.append(row[0])
        current = row[1]
    conn.close()

    if len(chain) < lookback:
        carry_conn.close()
        return False, [], f"chain too short ({len(chain)}) for hallucination check"

    # Get file changes for the most recent `lookback` sessions
    recent_sessions = chain[:lookback]
    tick_data = []
    for sid in recent_sessions:
        rows = carry_conn.execute(
            "SELECT files_changed_json, committed FROM tick_file_changes WHERE session_id = ? ORDER BY tick_number DESC LIMIT 1",
            (sid,)
        ).fetchall()
        if rows:
            files = json.loads(rows[0][0])
            committed = bool(rows[0][1])
            tick_data.append((files, committed))
        else:
            # No data for this session -- can't be part of a loop
            carry_conn.close()
            return False, [], f"no file change data for session {sid}"

    carry_conn.close()

    # If any tick in the window committed, it's not a hallucination loop
    if any(committed for _, committed in tick_data):
        return False, [], "at least one tick in window committed"

    # Find files that appear in ALL ticks
    file_sets = [set(files) for files, _ in tick_data]
    if not file_sets:
        return False, [], "no file data"

    common = file_sets[0]
    for fs in file_sets[1:]:
        common = common & fs

    if not common:
        return False, [], "different files in each tick (no loop)"

    return True, sorted(common), f"same {len(common)} file(s) edited in {lookback} consecutive ticks with no commit"


def record_test_count(session_id: str, tick_number: int, test_count: int,
                      source: str = "unknown") -> None:
    """Record the test count at the end of a tick.

    Called after each tick. test_count is the total number of tests found.
    source indicates how the count was obtained (e.g. 'pytest', 'cargo', 'grep').
    """
    carry_conn = get_carry_conn()
    carry_conn.execute("""
        INSERT INTO tick_test_counts (session_id, tick_number, test_count, source, recorded_at)
        VALUES (?, ?, ?, ?, ?)
    """, (session_id, tick_number, test_count, source, time.time()))
    carry_conn.commit()
    carry_conn.close()


def _detect_test_regression(session_id: str) -> Tuple[bool, int, int, str]:
    """Check if test count dropped significantly between the last two ticks.

    Returns:
        is_regression: True if test count dropped by more than threshold.
        prev_count: Test count from the previous tick.
        curr_count: Test count from the current (latest) tick.
        details: Human-readable description.
    """
    carry_conn = get_carry_conn()

    # Get the two most recent test counts for sessions in this chain
    # Walk chain to find parent
    conn = get_conn()
    chain = []
    current = session_id
    visited = set()
    while current and current not in visited and len(chain) < 20:
        visited.add(current)
        row = conn.execute(
            "SELECT id, parent_session_id FROM sessions WHERE id = ?", (current,)
        ).fetchone()
        if not row:
            break
        chain.append(row[0])
        current = row[1]
    conn.close()

    if len(chain) < 2:
        carry_conn.close()
        return False, 0, 0, "need at least 2 sessions for test regression check"

    # Get test counts for the two most recent sessions
    rows = []
    for sid in chain[:2]:
        r = carry_conn.execute(
            "SELECT test_count, source FROM tick_test_counts WHERE session_id = ? ORDER BY tick_number DESC LIMIT 1",
            (sid,)
        ).fetchone()
        if r:
            rows.append((r[0], r[1]))

    carry_conn.close()

    if len(rows) < 2:
        return False, 0, 0, "need test counts for at least 2 sessions"

    curr_count, curr_source = rows[0]
    prev_count, prev_source = rows[1]
    delta = prev_count - curr_count  # positive means tests were removed

    if delta > get_threshold("test_regression_threshold"):
        return True, prev_count, curr_count, (
            f"test count dropped by {delta} ({prev_count} -> {curr_count}) "
            f"in single tick [{curr_source}]"
        )

    return False, prev_count, curr_count, f"test count: {prev_count} -> {curr_count} (delta={delta})"


def _count_consecutive_noops(session_id: str) -> int:
    """Walk the parent chain and count consecutive no-op ticks.

    A tick is a "no-op" if:
    - No commit was made (git HEAD unchanged), AND
    - No test count increase, AND
    - Same files were edited as the previous tick (or no file changes recorded)

    Returns the number of consecutive no-ops ending at the current session.
    """
    conn = get_conn()
    carry_conn = get_carry_conn()

    # Walk chain
    chain = []
    current = session_id
    visited = set()
    while current and current not in visited and len(chain) < 20:
        visited.add(current)
        row = conn.execute(
            "SELECT id, parent_session_id FROM sessions WHERE id = ?", (current,)
        ).fetchone()
        if not row:
            break
        chain.append(row[0])
        current = row[1]
    conn.close()

    if len(chain) < 2:
        carry_conn.close()
        return 0

    noops = 0
    for i, sid in enumerate(chain):
        # Check git: did this session's commit differ from its parent?
        # chain is [newest, ..., oldest]. Parent of chain[i] is chain[i+1].
        git_heads = carry_conn.execute(
            "SELECT project_dir, git_head FROM chain_git_heads WHERE session_id = ?",
            (sid,)
        ).fetchall()
        parent_idx = i + 1
        if parent_idx < len(chain):
            parent_heads = carry_conn.execute(
                "SELECT project_dir, git_head FROM chain_git_heads WHERE session_id = ?",
                (chain[parent_idx],)
            ).fetchall()
            parent_map = {d: h for d, h in parent_heads}
            git_moved = any(d in parent_map and parent_map[d] != h for d, h in git_heads)
        else:
            git_moved = False  # oldest session, no parent to compare

        # Check test count: did it increase?
        tc = carry_conn.execute(
            "SELECT test_count FROM tick_test_counts WHERE session_id = ? ORDER BY tick_number DESC LIMIT 1",
            (sid,)
        ).fetchone()
        if parent_idx < len(chain) and tc:
            parent_tc = carry_conn.execute(
                "SELECT test_count FROM tick_test_counts WHERE session_id = ? ORDER BY tick_number DESC LIMIT 1",
                (chain[parent_idx],)
            ).fetchone()
            test_increased = (parent_tc and tc[0] > parent_tc[0])
        else:
            test_increased = False

        # Productive if git moved OR tests increased
        if git_moved or test_increased:
            break  # Productive tick resets the counter

        noops += 1

    carry_conn.close()
    return noops


def _update_noop_counter(session_id: str, is_noop: bool) -> None:
    """Update the consecutive_noops counter for a session in chain_meta."""
    carry_conn = get_carry_conn()
    existing = carry_conn.execute(
        "SELECT consecutive_noops FROM chain_meta WHERE session_id = ?",
        (session_id,)
    ).fetchone()

    if existing is not None:
        new_count = (existing[0] + 1) if is_noop else 0
        carry_conn.execute(
            "UPDATE chain_meta SET consecutive_noops = ? WHERE session_id = ?",
            (new_count, session_id)
        )
    else:
        new_count = 1 if is_noop else 0
        carry_conn.execute(
            """INSERT INTO chain_meta (session_id, continuation_count, created_at, consecutive_stalls, consecutive_noops)
               VALUES (?, 0, ?, 0, ?)""",
            (session_id, time.time(), new_count)
        )
    carry_conn.commit()
    carry_conn.close()


def get_conn() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def get_carry_conn() -> sqlite3.Connection:
    """Connect to carry_forward's own metadata DB (creates if needed)."""
    conn = sqlite3.connect(CARRY_DB_PATH)
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
            created_at REAL NOT NULL,
            consecutive_stalls INTEGER DEFAULT 0,
            consecutive_noops INTEGER DEFAULT 0
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
        CREATE INDEX IF NOT EXISTS idx_chain_git_session
        ON chain_git_heads(session_id)
    """)
    # v7: Tick file changes -- track which files changed per tick for hallucination loop detection
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tick_file_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            tick_number INTEGER NOT NULL,
            files_changed_json TEXT NOT NULL,
            committed INTEGER NOT NULL DEFAULT 0,
            recorded_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_tick_files_session
        ON tick_file_changes(session_id)
    """)
    # v7: Test count tracking -- detect test deletion/suppression
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tick_test_counts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            tick_number INTEGER NOT NULL,
            test_count INTEGER NOT NULL,
            source TEXT NOT NULL DEFAULT 'unknown',
            recorded_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_test_counts_session
        ON tick_test_counts(session_id)
    """)
    # v5: Decision logging -- every check_can_continue call gets recorded
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
        CREATE INDEX IF NOT EXISTS idx_decision_session
        ON decision_log(session_id)
    """)
    # v5: Outcome tracking -- what actually happened after a decision
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
    # v5: Tunable config -- thresholds read from here instead of hardcoded
    conn.execute("""
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'default',
            updated_at REAL NOT NULL
        )
    """)
    # v6: Learned lessons -- extracted from outcome history for context enrichment
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lesson TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'general',
            evidence TEXT,
            hit_count INTEGER DEFAULT 1,
            last_hit REAL NOT NULL,
            created_at REAL NOT NULL
        )
    """)
    # v7: Project-specific thresholds -- per-project overrides for stall/noop limits
    conn.execute("""
        CREATE TABLE IF NOT EXISTS project_thresholds (
            project_dir TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'auto-detect',
            project_type TEXT,
            updated_at REAL NOT NULL,
            PRIMARY KEY (project_dir, key)
        )
    """)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Command: last
# ---------------------------------------------------------------------------

def cmd_last(depth: int = 3, include_cron: bool = False) -> None:
    """Show last N non-trivial sessions."""
    conn = get_conn()
    cur = conn.cursor()
    sources = "('cli', 'telegram', 'whatsapp')" if not include_cron else "('cli', 'telegram', 'whatsapp', 'cron')"
    cur.execute(f"""
        SELECT id, source, model, message_count, tool_call_count, title, started_at
        FROM sessions
        WHERE message_count > 5 AND source IN {sources}
        ORDER BY started_at DESC LIMIT ?
    """, (depth,))
    rows = cur.fetchall()
    for r in rows:
        title = (r[5] or "---")[:60]
        ts = datetime.fromtimestamp(r[6]).strftime("%Y-%m-%d %H:%M") if r[6] else "?"
        print(f"{r[0]} | {r[1]} | msgs={r[3]} tools={r[4]} | {ts} | {title}")
    conn.close()


# ---------------------------------------------------------------------------
# Command: last-id
# ---------------------------------------------------------------------------

def cmd_last_id(include_cron: bool = False) -> None:
    conn = get_conn()
    cur = conn.cursor()
    sources = "('cli', 'telegram', 'whatsapp')" if not include_cron else "('cli', 'telegram', 'whatsapp', 'cron')"
    cur.execute(f"""
        SELECT id FROM sessions
        WHERE message_count > 5 AND source IN {sources}
        ORDER BY started_at DESC LIMIT 1
    """)
    row = cur.fetchone()
    if row:
        print(row[0])
    conn.close()


# ---------------------------------------------------------------------------
# Command: messages
# ---------------------------------------------------------------------------

def cmd_messages(session_id: str, last_n: int = 20) -> None:
    """Show messages from a specific session."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT role, content FROM messages
        WHERE session_id = ? AND role IN ('user', 'assistant')
        ORDER BY timestamp DESC LIMIT ?
    """, (session_id, last_n))
    rows = cur.fetchall()
    for r in reversed(rows):
        role = r[0]
        content = (r[1] or "(empty)")[:600]
        print(f"[{role}] {content}")
        print()
    conn.close()


# ---------------------------------------------------------------------------
# Command: chain
# ---------------------------------------------------------------------------

def cmd_chain(session_id: Optional[str] = None) -> None:
    """Trace the parent/child continuation chain for a session."""
    conn = get_conn()
    cur = conn.cursor()

    if not session_id:
        # Use last non-trivial session
        cur.execute("""
            SELECT id FROM sessions
            WHERE message_count > 5 AND source IN ('cli', 'telegram', 'whatsapp')
            ORDER BY started_at DESC LIMIT 1
        """)
        row = cur.fetchone()
        if not row:
            print("No sessions found.")
            conn.close()
            return
        session_id = row[0]

    # Build the chain by walking parent_session_id backwards
    chain = []
    current = session_id
    while current:
        cur.execute("SELECT id, parent_session_id, source, title, started_at, message_count FROM sessions WHERE id = ?", (current,))
        row = cur.fetchone()
        if not row:
            break
        chain.append(row)
        current = row[1]  # parent

    chain.reverse()  # oldest first

    print(f"=== SESSION CHAIN ({len(chain)} sessions) ===")
    for i, (sid, parent, source, title, ts, mc) in enumerate(chain):
        ts_str = datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "?"
        title_str = (title or "(untitled)")[:50]
        marker = " <-- you are here" if sid == session_id else ""
        print(f"  [{i+1}] {sid} | {source} | {ts_str} | msgs={mc} | {title_str}{marker}")

    # Also check carry_forward metadata
    carry_conn = get_carry_conn()
    for (sid, _, _, _, _, _) in chain:
        row = carry_conn.execute("SELECT outcome FROM chain_meta WHERE session_id = ?", (sid,)).fetchone()
        if row and row[0]:
            print(f"       outcome: {row[0]}")

    carry_conn.close()
    conn.close()

    if len(chain) >= 10:
        print()
        print("WARNING: Chain depth >= 10. Consider stopping and asking for human input.")


# ---------------------------------------------------------------------------
# Git HEAD tracking for cross-session diff
# ---------------------------------------------------------------------------

def record_git_heads(session_id: str) -> int:
    """Record current git HEAD for all detected projects in a session.
    Called at session start to establish baseline for thrash detection."""
    conn = get_conn()
    cur = conn.cursor()

    # Find project paths from session messages
    cur.execute("""
        SELECT content FROM messages WHERE session_id = ? AND role IN ('user', 'assistant', 'tool')
    """, (session_id,))
    all_text = " ".join(r[0] or "" for r in cur.fetchall())
    conn.close()

    paths = set(re.findall(r"/home/jericho/[a-zA-Z0-9_/.-]+\.[a-z]{1,4}", all_text))
    dirs = sorted(set(p.rsplit("/", 1)[0] for p in paths))[:10]

    now = time.time()
    carry_conn = get_carry_conn()
    recorded = 0
    for d in dirs:
        gs = git_status(d)
        git_root = gs.get("git_root")
        if not git_root or gs.get("error"):
            continue
        try:
            r = subprocess.run(["git", "rev-parse", "HEAD"],
                               capture_output=True, text=True, cwd=git_root, timeout=5)
            if r.returncode == 0:
                head = r.stdout.strip()
                carry_conn.execute("""
                    INSERT INTO chain_git_heads (session_id, project_dir, git_head, recorded_at)
                    VALUES (?, ?, ?, ?)
                """, (session_id, git_root, head, now))
                recorded += 1
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    carry_conn.commit()
    carry_conn.close()
    return recorded


def check_git_progress(session_id: str, min_sessions: Optional[int] = None) -> Tuple[bool, str]:
    """Check if git HEAD has actually moved across the chain.
    Returns (progress_made, details_str).
    This catches 'busy but unproductive' -- sessions that log work but never commit."""
    if min_sessions is None:
        min_sessions = get_threshold("git_min_sessions")
    # Walk the chain to find the oldest session with recorded git heads
    conn = get_conn()
    cur = conn.cursor()
    chain = []
    current = session_id
    visited = set()
    while current and current not in visited and len(chain) < 15:
        visited.add(current)
        cur.execute("SELECT id, parent_session_id FROM sessions WHERE id = ?", (current,))
        row = cur.fetchone()
        if not row:
            break
        chain.append(row[0])
        current = row[1]
    conn.close()

    if len(chain) < min_sessions:
        return True, f"chain too short ({len(chain)} sessions) for git progress check"

    carry_conn = get_carry_conn()
    # Get git heads from earliest and latest sessions in chain
    earliest = chain[-1]
    latest = chain[0]

    early_heads = carry_conn.execute("""
        SELECT project_dir, git_head FROM chain_git_heads WHERE session_id = ?
    """, (earliest,)).fetchall()

    late_heads = carry_conn.execute("""
        SELECT project_dir, git_head FROM chain_git_heads WHERE session_id = ?
    """, (latest,)).fetchall()
    carry_conn.close()

    if not early_heads:
        return True, "no git heads recorded at chain start (first run?)"

    # Compare heads for matching projects
    early_map = {d: h for d, h in early_heads}
    late_map = {d: h for d, h in late_heads}

    moved = 0
    stuck = 0
    for proj_dir in early_map:
        if proj_dir in late_map:
            if early_map[proj_dir] != late_map[proj_dir]:
                moved += 1
            else:
                stuck += 1

    total = moved + stuck
    if total == 0:
        return True, "no matching projects across chain endpoints"

    if moved == 0 and stuck > 0 and len(chain) >= min_sessions:
        return False, f"git HEAD unchanged across {len(chain)} sessions for {stuck} project(s)"

    return True, f"git moved in {moved}/{total} tracked projects across {len(chain)} sessions"


# ---------------------------------------------------------------------------
# Thrash detection
# ---------------------------------------------------------------------------

def detect_thrash(session_id: Optional[str] = None, lookback: Optional[int] = None) -> Tuple[bool, int, List[Dict[str, Any]], str]:
    """
    Detect whether recent sessions in the continuation chain are productive.

    Walks the parent chain backwards, counting "dead" sessions (0 messages
    and 0 tool calls). Also checks for orphan child sessions (runaway loop
    detection).

    Returns:
        is_thrashing: True if dead_count >= threshold or too many orphan children.
        dead_count: Number of dead sessions in the recent lookback window.
        chain_sessions: List of dicts with id, parent, source, msgs, tools, alive.
        details: Human-readable summary string.
    """
    if lookback is None:
        lookback = get_threshold("dead_lookback")
    dead_thresh = get_threshold("dead_session_threshold")
    orphan_thresh = get_threshold("orphan_child_threshold")
    conn = get_conn()
    cur = conn.cursor()

    if not session_id:
        cur.execute("""
            SELECT id FROM sessions
            WHERE message_count > 5 AND source IN ('cli', 'telegram', 'whatsapp')
            ORDER BY started_at DESC LIMIT 1
        """)
        row = cur.fetchone()
        session_id = row[0] if row else None

    if not session_id:
        conn.close()
        return False, 0, [], "no session"

    # Walk the chain backwards, collecting session stats
    chain_sessions = []
    current = session_id
    visited = set()
    while current and len(chain_sessions) < lookback + 5:
        if current in visited:
            break
        visited.add(current)
        cur.execute("""
            SELECT id, parent_session_id, source, message_count, tool_call_count, started_at
            FROM sessions WHERE id = ?
        """, (current,))
        row = cur.fetchone()
        if not row:
            break
        chain_sessions.append({
            "id": row[0],
            "parent": row[1],
            "source": row[2],
            "msgs": row[3],
            "tools": row[4],
            "ts": row[5],
            "alive": row[3] > 0 or row[4] > 0,
        })
        current = row[1]

    conn.close()

    # Count dead sessions (no messages, no tool calls) in the recent chain
    recent = chain_sessions[:lookback]
    dead_count = sum(1 for s in recent if not s["alive"])

    # Also check: are there child sessions of this session that are all dead?
    # (This catches the case where we're the origin of a runaway loop)
    conn = get_conn()
    child_count = conn.execute("""
        SELECT COUNT(*) FROM sessions WHERE parent_session_id = ? AND message_count = 0
    """, (session_id,)).fetchone()[0]
    conn.close()

    details = f"chain={len(chain_sessions)} recent_dead={dead_count}/{len(recent)} orphan_children={child_count}"

    # Thrashing if: dead_count >= threshold of last lookback sessions in chain, OR
    # this session already has orphan_thresh+ dead children (runaway loop detection)
    is_thrashing = dead_count >= dead_thresh or child_count >= orphan_thresh

    # Git stall is informational only -- it adds a reason but does NOT force
    # is_thrashing=True.  Sessions can be productive (many tool calls) without
    # committing, and we should not kill them for that.
    git_ok, git_details = check_git_progress(session_id)
    if not git_ok:
        details += f" | {git_details}"

    return is_thrashing, dead_count, chain_sessions, details


def check_can_continue(session_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Core decision engine for continuation decisions.

    Returns a dict with keys:
        can_continue (bool): Final go/no-go decision.
        reasons (list[str]): Halt reasons (empty if continuing).
        guard_rails (list[str]): Informational warnings (non-blocking).
        thrashing (bool): Whether the chain is thrashing.
        dead_count (int): Dead sessions in recent chain.
        git_progress (str): Git HEAD movement description.
        blocker_halt (bool): Whether a stale blocker forced halt.
        session_dead (bool): Whether current session has 0 tools + <=2 msgs.
        parent_dead (bool): Whether parent session was dead.
        thrash_details (str): Raw thrash detection details.
        decision_id (int): Auto-increment ID for outcome tracking.

    v5.2 Decision Pipeline:
        Each check independently gates continuation:
        1. Dead session thrash (chain-level, overridden if current session is active)
        2. Git stalled (informational only -- does NOT force halt)
        3. Learned pattern guard rails (low continuation rate, large parent)
        4. Stale blockers (older than blocker_halt_hours = halt)
        5. Session dead (0 tools AND <=2 msgs = halt)
        5b. Parent dead + current session also dead = halt
        6. Git stall + dead session combo (informational)
        7. Stagnation circuit breaker: consecutive_stalls >= limit and no active tools = halt
        8. Hallucination loop: same files edited in N consecutive ticks with no commit = halt
        9. Test regression: test count drops by more than threshold in single tick = halt
        10. No-op loop: consecutive no-ops (no commit, no test increase) >= limit = halt

        Final: can_continue = not thrashing AND not blocker_halt
               AND not session_dead AND not (parent_dead AND own_tools==0)
               AND not stagnation_halt AND not hallucination_halt
               AND not test_regression_halt AND not noop_halt
    """
    thrashing, dead_count, chain_sessions, details = detect_thrash(session_id)
    git_ok, git_details = check_git_progress(session_id or "")

    reasons = []
    guard_rails = []

    # Resolve session_id if not provided (cron context)
    conn = get_conn()
    resolved_session_id = session_id
    if not resolved_session_id:
        row = conn.execute("""
            SELECT id FROM sessions
            WHERE message_count > 0 AND source IN ('cli', 'telegram', 'whatsapp')
            ORDER BY started_at DESC LIMIT 1
        """).fetchone()
        if row:
            resolved_session_id = row[0]
    conn.close()

    # Fetch current session activity early (needed by multiple checks)
    own_tools = 0
    own_msgs = 0
    conn_early = get_conn()
    if resolved_session_id:
        early_row = conn_early.execute(
            "SELECT tool_call_count, message_count FROM sessions WHERE id = ?",
            (resolved_session_id,)
        ).fetchone()
        if early_row:
            own_tools = early_row[0]
            own_msgs = early_row[1]
    conn_early.close()

    # Phase 7: Resolve project directory for project-aware thresholds
    project_dir = _resolve_project_dir(resolved_session_id)

    # Check 1: Dead session thrash (chain has too many dead sessions)
    # But don't halt if the current session is already active -- a productive
    # session shouldn't be killed because its ancestors were dead.
    if thrashing and own_tools == 0:
        reasons.append(f"Thrash: {dead_count} dead sessions in recent chain ({details})")
    elif thrashing:
        guard_rails.append(f"Thrash detected but session is active: {details}")
        thrashing = False  # Active session overrides chain thrash

    # Check 2: Git stalled (informational -- does NOT force halt on its own.
    # A productive session with tool calls should not be killed just because
    # it hasn't committed yet. But combined with a dead session, it's fatal.)
    git_stalled = False
    if not git_ok:
        git_stalled = True
        # Only add as halt reason if session is also dead (see check 5)
        guard_rails.append(f"Git stalled: {git_details}")

    carry_conn = get_carry_conn()

    # Check 3: Blocker age threshold
    blocker_halt = False
    now = time.time()
    stale_blockers = carry_conn.execute("""
        SELECT message, created_at FROM blockers
        WHERE resolved_at IS NULL AND created_at < ?
    """, (now - (get_threshold("blocker_halt_hours") * 3600),)).fetchall()
    carry_conn.close()

    if stale_blockers:
        for msg, ts in stale_blockers:
            age_h = (now - ts) / 3600
            reasons.append(f"Stale blocker ({age_h:.1f}h old): {msg}")
        blocker_halt = True

    # Check 5: Session activity (v5.2 -- expanded)
    # 5a: If this session itself has 0 tool calls and <=2 messages, it's dead.
    # (own_tools/own_msgs already fetched before Check 1)
    session_dead = False
    parent_dead = False
    if own_tools == 0 and own_msgs <= 2:
        session_dead = True
        reasons.append(f"Session dead: 0 tool calls, {own_msgs} messages")

    # 5b: Parent dead check -- if parent had 0 tools and <=2 msgs, continuation is pointless
    conn_check = get_conn()
    if not session_dead and resolved_session_id:
        parent_row = conn_check.execute(
            "SELECT parent_session_id FROM sessions WHERE id = ?",
            (resolved_session_id,)
        ).fetchone()
        if parent_row and parent_row[0]:
            p_row = conn_check.execute(
                "SELECT tool_call_count, message_count FROM sessions WHERE id = ?",
                (parent_row[0],)
            ).fetchone()
            if p_row and p_row[0] == 0 and p_row[1] <= 2:
                parent_dead = True
                reasons.append(f"Parent session dead: 0 tool calls, {p_row[1]} messages")
    conn_check.close()

    # Check 6: Git stall + dead session combo
    # If git is stalled AND the current session has no tools, it's a double signal
    if git_stalled and session_dead:
        reasons.append("Git stalled + dead session: no progress and no activity")

    # Check 7: Stagnation circuit breaker (v7)
    # Count consecutive ticks with no commits across the chain. If >= limit, hard halt.
    # Active session override: if this session has tool calls, don't halt (might be mid-commit).
    # Phase 7: Uses project-aware stagnation_stall_limit.
    stagnation_halt = False
    consecutive_stalls = _get_chain_stalls(resolved_session_id or "")
    stag_limit = get_threshold_for_project("stagnation_stall_limit", project_dir)
    if git_stalled and consecutive_stalls >= stag_limit and own_tools == 0:
        stagnation_halt = True
        reasons.append(
            f"Stagnation: {consecutive_stalls} consecutive ticks with no commits"
        )
    elif git_stalled and consecutive_stalls >= stag_limit:
        guard_rails.append(
            f"Stagnation detected ({consecutive_stalls} stalls) but session is active"
        )
    # Update the stall counter in chain_meta
    _update_stall_counter(resolved_session_id or "", git_stalled)

    # Check 8: Hallucination loop detection (v7)
    # If the same files are being edited across multiple ticks with no commits,
    # the agent is stuck in a loop. Hard halt if limit reached, guard rail if 2 ticks.
    # Phase 7: Uses project-aware hallucination_loop_limit.
    hallucination_halt = False
    hallucination_files = []
    hlimit = get_threshold_for_project("hallucination_loop_limit", project_dir)
    if resolved_session_id:
        h_loop, h_files, h_details = _detect_hallucination_loop(resolved_session_id)
        if h_loop and own_tools == 0:
            hallucination_halt = True
            hallucination_files = h_files
            reasons.append(
                f"Hallucination loop: same {len(h_files)} file(s) in {hlimit}+ ticks "
                f"with no commit ({', '.join(h_files[:3])})"
            )
        elif h_loop:
            hallucination_files = h_files
            guard_rails.append(
                f"Hallucination loop detected ({h_details}) but session is active"
            )

    # Final decision: continue only if ALL halt checks pass.
    # Exception: if the current session itself has tool calls, it's productive
    # regardless of parent state -- don't kill a live session for a dead parent.

    # Check 9: Test count regression (v7)
    # If test count dropped by more than threshold in a single tick, hard halt.
    # This catches agents deleting tests to make suites pass.
    test_regression_halt = False
    test_prev_count = 0
    test_curr_count = 0
    if resolved_session_id:
        t_reg, t_prev, t_curr, t_details = _detect_test_regression(resolved_session_id)
        test_prev_count = t_prev
        test_curr_count = t_curr
        if t_reg:
            test_regression_halt = True
            reasons.append(f"Test regression: {t_details}")

    # Check 10: Consecutive no-op counter (v7)
    # Unified metric: no commit AND no test increase = no-op.
    # Hard halt after noop_limit consecutive no-ops with no active tools.
    # Phase 7: Uses project-aware noop_limit.
    noop_halt = False
    noop_limit = get_threshold_for_project("noop_limit", project_dir)
    consecutive_noops = _count_consecutive_noops(resolved_session_id or "")
    is_noop = (git_stalled and not test_regression_halt)  # no commit and no test crash
    _update_noop_counter(resolved_session_id or "", is_noop)
    if consecutive_noops >= noop_limit and own_tools == 0:
        noop_halt = True
        reasons.append(
            f"No-op loop: {consecutive_noops} consecutive ticks with no commit or test increase"
        )
    elif consecutive_noops >= noop_limit:
        guard_rails.append(
            f"No-op loop detected ({consecutive_noops} no-ops) but session is active"
        )

    # Check 11: Roadmap completion signal (informational)
    # If all roadmaps for tracked projects are complete, that's a natural stop signal.
    # This is NOT a hard halt -- just a guard rail suggesting the loop has achieved its goal.
    roadmap_signal = {"all_complete": False, "any_in_progress": False, "details": "no roadmaps found"}
    if HAS_ROADMAP_BUILDER and resolved_session_id:
        # Get project dirs from session
        conn_road = get_conn()
        road_texts = conn_road.execute("""
            SELECT content FROM messages WHERE session_id = ? AND role IN ('user', 'assistant', 'tool')
        """, (resolved_session_id,)).fetchall()
        conn_road.close()
        all_text_road = " ".join(r[0] or "" for r in road_texts)
        road_paths = set(re.findall(r"/home/jericho/[a-zA-Z0-9_/.-]+\.[a-z]{1,4}", all_text_road))
        road_dirs = sorted(set(p.rsplit("/", 1)[0] for p in road_paths))[:10]
        if road_dirs:
            roadmaps_found = scan_project_roadmaps(road_dirs)
            roadmap_signal = roadmap_completion_signal(roadmaps_found)
            if roadmap_signal["all_complete"]:
                guard_rails.append(
                    f"Roadmap complete: {roadmap_signal['details']} -- natural stopping point"
                )
            elif roadmap_signal["any_in_progress"]:
                # Positive signal -- there's still work to do
                pass  # silently continue

    can_continue = (not thrashing and not blocker_halt and not session_dead
                    and not (parent_dead and own_tools == 0)
                    and not stagnation_halt
                    and not hallucination_halt
                    and not test_regression_halt
                    and not noop_halt)

    # v5: Log the decision for later outcome tracking
    # Dedup: don't log if we already logged this exact decision for this session recently (<300s)
    decision_str = "continue" if can_continue else "halt"
    carry_conn2 = get_carry_conn()
    recent = carry_conn2.execute("""
        SELECT id FROM decision_log 
        WHERE session_id = ? AND decision = ? AND created_at > ?
        LIMIT 1
    """, (resolved_session_id, decision_str, time.time() - 300)).fetchone()
    if not recent:
        carry_conn2.execute("""
            INSERT INTO decision_log (session_id, decision, reasons_json, thresholds_json, can_continue, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            resolved_session_id,
            decision_str,
            json.dumps(reasons),
            json.dumps({}),
            1 if can_continue else 0,
            time.time(),
        ))
        decision_id = carry_conn2.execute("SELECT last_insert_rowid()").fetchone()[0]
        carry_conn2.commit()
    else:
        decision_id = recent[0]
    carry_conn2.close()

    return {
        "can_continue": can_continue,
        "reasons": reasons,
        "guard_rails": guard_rails,
        "thrashing": thrashing,
        "dead_count": dead_count,
        "git_progress": git_details,
        "blocker_halt": blocker_halt,
        "session_dead": session_dead,
        "parent_dead": parent_dead,
        "thrash_details": details,
        "decision_id": decision_id,
        "stagnation_halt": stagnation_halt,
        "consecutive_stalls": consecutive_stalls,
        "hallucination_halt": hallucination_halt,
        "hallucination_files": hallucination_files,
        "test_regression_halt": test_regression_halt,
        "test_prev_count": test_prev_count,
        "test_curr_count": test_curr_count,
        "noop_halt": noop_halt,
        "consecutive_noops": consecutive_noops,
        "roadmap_signal": roadmap_signal,
        "project_dir": project_dir,
    }


def cmd_should_continue(session_id: Optional[str] = None) -> None:
    """
    Exit code interface for cron scripting.
    0 = safe to chain, 1 = thrashing / blockers / should stop.
    Also prints human-readable reasoning.
    """
    # Auto-record outcome from previous session before making a decision
    auto_record_outcomes()

    result = check_can_continue(session_id)

    if not result["can_continue"]:
        print(f"STOP: {', '.join(result['reasons'])}")
        if result["guard_rails"]:
            print("Guard rail context:")
            for gr in result["guard_rails"]:
                print(f"  - {gr}")
        sys.exit(1)
    else:
        print(f"OK: No blockers detected")
        print(f"  Thrash: {result['thrash_details']}")
        print(f"  Git: {result['git_progress']}")
        if result["guard_rails"]:
            print("  Notes:")
            for gr in result["guard_rails"]:
                print(f"    - {gr}")
        sys.exit(0)


def cmd_run(session_id: Optional[str] = None, json_output: bool = False) -> None:
    """
    Single entry point for automated loops.

    Does three things in order:
    1. Record outcome from previous session (if any)
    2. Check can_continue
    3. Print context for the next session

    Exit codes:
      0 = safe to continue, context printed
      1 = halt (thrashing, blocker, dead session)
      2 = halt with reason printed to stderr

    Use --json for machine-readable output.
    """
    # Step 1: Record outcome from previous session
    outcome = record_outcome(session_id)

    # Step 2: Gate check
    result = check_can_continue(session_id)

    if not result["can_continue"]:
        reason = ", ".join(result["reasons"])
        if json_output:
            print(json.dumps({
                "action": "halt",
                "reasons": result["reasons"],
                "guard_rails": result.get("guard_rails", []),
                "outcome": outcome,
            }))
        else:
            print(f"HALT: {reason}")
        sys.exit(1)

    # Step 3: Print context for the next session
    if json_output:
        ctx = get_context_data(session_id, include_cron=False)
        print(json.dumps({
            "action": "continue",
            "context": ctx,
            "outcome": outcome,
            "session_id": result.get("session_id"),
        }, indent=2))
    else:
        print("CONTINUE")
        print()
        cmd_context(include_cron=False)

    sys.exit(0)


# ---------------------------------------------------------------------------
# v5: Outcome recording
# ---------------------------------------------------------------------------

def record_outcome(session_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Check what actually happened after a continuation decision was made.

    Compares the decision (continue/halt) against the actual session outcome:
    - Was the session productive (had tool calls)?
    - Did git HEAD move?
    - Did the chain continue (child session exists)?

    Records the outcome in decision_outcomes for later calibration.
    If session_id is None, checks the most recent decision.

    Returns:
        Dict with decision_id, session_id, decision, and outcome sub-dict.
        If no decisions are logged, returns {\"error\": \"no decisions logged yet\"}.
    """
    now = time.time()
    carry_conn = get_carry_conn()

    # Find the most recent decision for this session (or any session)
    if session_id:
        row = carry_conn.execute("""
            SELECT id, session_id, decision, can_continue, created_at
            FROM decision_log WHERE session_id = ?
            ORDER BY created_at DESC LIMIT 1
        """, (session_id,)).fetchone()
    else:
        row = carry_conn.execute("""
            SELECT id, session_id, decision, can_continue, created_at
            FROM decision_log
            ORDER BY created_at DESC LIMIT 1
        """).fetchone()

    if not row:
        carry_conn.close()
        return {"error": "no decisions logged yet"}

    decision_id, dec_session_id, decision, can_continue, decided_at = row

    # Can't record outcomes without a session
    if not dec_session_id:
        carry_conn.close()
        return {"error": "decision has no session_id, cannot determine outcome"}

    # Check if already recorded
    existing = carry_conn.execute("""
        SELECT decision_id FROM decision_outcomes WHERE decision_id = ?
    """, (decision_id,)).fetchone()
    if existing:
        carry_conn.close()
        return {"status": "already_recorded", "decision_id": decision_id}

    # Determine actual outcome
    conn = get_conn()

    # 1. Was the session productive? (had tool calls)
    sess = conn.execute("""
        SELECT message_count, tool_call_count FROM sessions WHERE id = ?
    """, (dec_session_id,)).fetchone()
    msg_count = sess[0] if sess else 0
    tool_calls = sess[1] if sess else 0
    productive = 1 if tool_calls > 0 else 0

    # 2. Did the chain continue? (does this session have a child?)
    child = conn.execute("""
        SELECT id FROM sessions WHERE parent_session_id = ? LIMIT 1
    """, (dec_session_id,)).fetchone()
    chain_continued = 1 if child else 0

    # 3. Did git move? (check git heads at decision time vs now)
    git_moved = 0
    # Get the session's recorded git heads
    early_heads = carry_conn.execute("""
        SELECT project_dir, git_head FROM chain_git_heads WHERE session_id = ?
    """, (dec_session_id,)).fetchone()
    if early_heads:
        # Check if current HEAD differs -- walk all sessions in the chain after this one
        child_sess = child[0] if child else None
        if child_sess:
            late_heads = carry_conn.execute("""
                SELECT project_dir, git_head FROM chain_git_heads WHERE session_id = ?
            """, (child_sess,)).fetchone()
            if late_heads and early_heads[1] != late_heads[1]:
                git_moved = 1
    conn.close()

    # Record the outcome
    carry_conn.execute("""
        INSERT INTO decision_outcomes
        (decision_id, session_id, outcome_productive, outcome_git_moved,
         outcome_chain_continued, outcome_tool_calls, outcome_message_count, checked_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        decision_id, dec_session_id, productive, git_moved,
        chain_continued, tool_calls, msg_count, now
    ))
    carry_conn.commit()
    carry_conn.close()

    return {
        "decision_id": decision_id,
        "session_id": dec_session_id,
        "decision": decision,
        "outcome": {
            "productive": bool(productive),
            "git_moved": bool(git_moved),
            "chain_continued": bool(chain_continued),
            "tool_calls": tool_calls,
            "messages": msg_count,
        }
    }


def auto_record_outcomes() -> None:
    """
    Called from context command -- checks if previous session had a logged decision
    and records the outcome if not yet done. Silent, no output.
    """
    conn = get_conn()
    last = conn.execute("""
        SELECT id FROM sessions
        WHERE message_count > 5 AND source IN ('cli', 'telegram', 'whatsapp')
        ORDER BY started_at DESC LIMIT 1
    """).fetchone()
    conn.close()
    if last:
        record_outcome(last[0])

    # Auto-calibrate every 10 outcomes
    try:
        carry_conn = get_carry_conn()
        count = carry_conn.execute("SELECT COUNT(*) FROM decision_outcomes").fetchone()[0]
        carry_conn.close()
        if count > 0 and count % 10 == 0:
            # Check if we already calibrated at this count
            carry_conn2 = get_carry_conn()
            last_cal = carry_conn2.execute(
                "SELECT value FROM config WHERE key = 'last_calibration_outcome_count'"
            ).fetchone()
            last_cal_count = int(last_cal[0]) if last_cal else 0
            carry_conn2.close()
            if count != last_cal_count:
                calibrate_thresholds()
                carry_conn3 = get_carry_conn()
                carry_conn3.execute(
                    "INSERT OR REPLACE INTO config (key, value, source, updated_at) VALUES (?, ?, ?, ?)",
                    ("last_calibration_outcome_count", str(count), "auto", time.time())
                )
                carry_conn3.commit()
                carry_conn3.close()
    except Exception:
        pass  # calibration is best-effort


# ---------------------------------------------------------------------------
# v6: Learned lessons from outcome history
# ---------------------------------------------------------------------------


def extract_lessons() -> List[Dict[str, Any]]:
    """Analyze outcome history and extract actionable lessons.

    Lessons are patterns like "this project fails on vm.rs changes" or
    "halts on hallucination loops are usually wrong". These get surfaced
    in the context command so the next session can learn from past failures.

    Returns list of lesson dicts with 'lesson', 'category', 'evidence' keys.
    """
    carry_conn = get_carry_conn()

    # Join decisions with outcomes
    rows = carry_conn.execute("""
        SELECT dl.session_id, dl.decision, dl.can_continue,
               dl.reasons_json,
               do_out.outcome_productive, do_out.outcome_git_moved,
               do_out.outcome_tool_calls, do_out.outcome_message_count,
               do_out.checked_at
        FROM decision_outcomes do_out
        JOIN decision_log dl ON do_out.decision_id = dl.id
        ORDER BY do_out.checked_at DESC
        LIMIT 200
    """).fetchall()
    carry_conn.close()

    if len(rows) < 3:
        return []

    new_lessons = []

    # --- Lesson type 1: Continue decisions that were unproductive ---
    bad_continues = [(r[0], r[3], r[7]) for r in rows
                     if r[2] == 1 and r[4] == 0]  # continued but not productive
    if len(bad_continues) >= 3:
        # Look for common halt reasons in these
        reason_counts: Dict[str, int] = {}
        for _, reasons_json, tool_calls in bad_continues:
            try:
                reasons = json.loads(reasons_json) if reasons_json else []
            except (json.JSONDecodeError, TypeError):
                reasons = []
            for reason in reasons:
                # Normalize: extract key phrase
                tag = reason.split(":")[0].strip() if ":" in reason else reason.strip()
                reason_counts[tag] = reason_counts.get(tag, 0) + 1

        if reason_counts:
            top_reason = max(reason_counts, key=reason_counts.get)
            count = reason_counts[top_reason]
            lesson_text = (f"Continue decisions often lead to unproductive sessions "
                           f"when {top_reason.lower()} is flagged -- consider tightening")
            new_lessons.append({
                "lesson": lesson_text,
                "category": "continue_accuracy",
                "evidence": f"{count}/{len(bad_continues)} bad continues with {top_reason}",
            })

    # --- Lesson type 2: Halt decisions that were wrong (session was productive) ---
    wrong_halts = [(r[0], r[3], r[7]) for r in rows
                   if r[2] == 0 and r[4] == 1]  # halted but was productive
    if len(wrong_halts) >= 3:
        reason_counts2: Dict[str, int] = {}
        for _, reasons_json, _ in wrong_halts:
            try:
                reasons = json.loads(reasons_json) if reasons_json else []
            except (json.JSONDecodeError, TypeError):
                reasons = []
            for reason in reasons:
                tag = reason.split(":")[0].strip() if ":" in reason else reason.strip()
                reason_counts2[tag] = reason_counts2.get(tag, 0) + 1

        if reason_counts2:
            top_reason2 = max(reason_counts2, key=reason_counts2.get)
            count2 = reason_counts2[top_reason2]
            lesson_text2 = (f"Halt decisions for {top_reason2.lower()} are often wrong "
                            f"-- the session was actually productive")
            new_lessons.append({
                "lesson": lesson_text2,
                "category": "halt_accuracy",
                "evidence": f"{count2}/{len(wrong_halts)} wrong halts triggered by {top_reason2}",
            })

    # --- Lesson type 3: Overall productivity pattern ---
    total = len(rows)
    productive = sum(1 for r in rows if r[4])
    unproductive = total - productive
    if total >= 10:
        if unproductive / total > 0.6:
            new_lessons.append({
                "lesson": f"Only {productive}/{total} sessions are productive -- "
                          f"use smaller steps and verify before committing",
                "category": "overall_productivity",
                "evidence": f"{unproductive} unproductive out of {total} recent sessions",
            })
        elif productive / total > 0.85:
            new_lessons.append({
                "lesson": f"High productivity rate ({productive}/{total}) -- "
                          f"current thresholds are working well",
                "category": "overall_productivity",
                "evidence": f"{productive} productive out of {total} recent sessions",
            })

    # --- Lesson type 4: Git-specific pattern ---
    no_git_move = sum(1 for r in rows if r[4] == 1 and r[5] == 0)  # productive but no git move
    if no_git_move >= 5:
        new_lessons.append({
            "lesson": "Productive sessions often don't commit -- "
                      "check for uncommitted work before assuming failure",
            "category": "git_pattern",
            "evidence": f"{no_git_move} productive sessions with no git HEAD movement",
        })

    # Store/update lessons in DB
    now = time.time()
    carry_conn2 = get_carry_conn()
    for lesson in new_lessons:
        # Check if similar lesson already exists (by category + first 50 chars)
        existing = carry_conn2.execute(
            "SELECT id, hit_count FROM lessons WHERE category = ? AND lesson LIKE ?",
            (lesson["category"], f"{lesson['lesson'][:50]}%")
        ).fetchone()

        if existing:
            # Update hit count and last_hit
            carry_conn2.execute(
                "UPDATE lessons SET hit_count = ?, last_hit = ?, evidence = ? WHERE id = ?",
                (existing[1] + 1, now, lesson.get("evidence", ""), existing[0])
            )
        else:
            carry_conn2.execute(
                "INSERT INTO lessons (lesson, category, evidence, hit_count, last_hit, created_at) "
                "VALUES (?, ?, ?, 1, ?, ?)",
                (lesson["lesson"], lesson["category"], lesson.get("evidence", ""), now, now)
            )

    carry_conn2.commit()
    carry_conn2.close()

    return new_lessons


def get_top_lessons(n: int = 3) -> List[Dict[str, Any]]:
    """Return the top N lessons from the lessons table, ranked by hit_count and recency.

    Args:
        n: Max number of lessons to return (default 3).

    Returns:
        List of dicts with 'lesson', 'category', 'evidence', 'hit_count' keys.
    """
    carry_conn = get_carry_conn()

    # Check if lessons table exists (handles fresh DBs)
    try:
        rows = carry_conn.execute("""
            SELECT lesson, category, evidence, hit_count, last_hit
            FROM lessons
            ORDER BY hit_count DESC, last_hit DESC
            LIMIT ?
        """, (n,)).fetchall()
    except sqlite3.OperationalError:
        rows = []

    carry_conn.close()

    return [
        {
            "lesson": r[0],
            "category": r[1],
            "evidence": r[2],
            "hit_count": r[3],
        }
        for r in rows
    ]


def cmd_learn() -> None:
    """Analyze outcome history and print extracted lessons."""
    lessons = extract_lessons()

    if not lessons:
        print("No lessons extracted -- need at least 3 outcomes with patterns.")
        return

    print(f"=== LEARNED LESSONS ({len(lessons)} new/updated) ===\n")
    for i, lesson in enumerate(lessons, 1):
        print(f"  [{i}] {lesson['lesson']}")
        print(f"      category: {lesson['category']}")
        if lesson.get("evidence"):
            print(f"      evidence: {lesson['evidence']}")
        print()

    # Also show top stored lessons
    top = get_top_lessons(n=5)
    if top:
        print("TOP STORED LESSONS:")
        for i, lesson in enumerate(top, 1):
            print(f"  {i}. {lesson['lesson']} (hits: {lesson['hit_count']})")


# ---------------------------------------------------------------------------
# v5: Calibration
# ---------------------------------------------------------------------------


def calibrate_thresholds(dry_run: bool = False) -> Dict[str, Any]:
    """Analyze decision outcomes and adjust thresholds.

    Calibration rules:
    - If continue decisions are mostly productive (>80%), loosen stall/noop limits by 1.
    - If continue decisions are mostly unproductive (<50%), tighten stall/noop limits by 1.
    - If halt decisions correctly caught bad sessions (>80% unproductive), tighten by 1.
    - If halt decisions were wrong (>50% were actually productive), loosen by 1.
    - Clamped to min/max bounds from THRESHOLD_DEFS.

    Returns:
        Dict with 'changes' list and 'summary' stats.
    """
    conn = get_carry_conn()

    # Gather outcomes
    rows = conn.execute("""
        SELECT dl.decision, dl.can_continue,
               do_out.outcome_productive, do_out.outcome_git_moved,
               do_out.outcome_chain_continued, do_out.outcome_tool_calls
        FROM decision_outcomes do_out
        JOIN decision_log dl ON do_out.decision_id = dl.id
        ORDER BY do_out.checked_at DESC
        LIMIT 100
    """).fetchall()
    conn.close()

    if len(rows) < 5:
        return {"changes": [], "summary": {"message": "Not enough outcomes to calibrate (need 5+)", "outcome_count": len(rows)}}

    # Analyze by decision type
    continue_outcomes = [(r[2], r[3], r[4], r[5]) for r in rows if r[1] == 1]  # can_continue=1
    halt_outcomes = [(r[2], r[3], r[4], r[5]) for r in rows if r[1] == 0]     # can_continue=0

    # "correct" means: continue -> productive OR halt -> unproductive
    continue_productive = sum(1 for o in continue_outcomes if o[0])  # productive
    continue_count = len(continue_outcomes)
    halt_unproductive = sum(1 for o in halt_outcomes if not o[0])  # unproductive
    halt_count = len(halt_outcomes)

    changes = []

    def _adjust(key: str, delta: int, reason: str) -> None:
        current = get_threshold(key)
        defn = THRESHOLD_DEFS[key]
        new_val = current + delta
        new_val = max(defn["min"], min(defn["max"], new_val))
        if new_val != current:
            if not dry_run:
                _write_config(key, str(new_val), "calibration")
            changes.append({
                "key": key,
                "old": current,
                "new": new_val,
                "reason": reason,
            })

    # Continue decision accuracy
    if continue_count >= 3:
        productive_rate = continue_productive / continue_count
        if productive_rate > 0.8:
            # Good continue rate -> can afford to be more lenient
            _adjust("stagnation_stall_limit", 1,
                    f"continue accuracy {productive_rate:.0%} (>{0.8:.0%}), loosening")
            _adjust("noop_limit", 1,
                    f"continue accuracy {productive_rate:.0%} (>{0.8:.0%}), loosening")
        elif productive_rate < 0.5:
            # Bad continue rate -> tighten up
            _adjust("stagnation_stall_limit", -1,
                    f"continue accuracy {productive_rate:.0%} (<{0.5:.0%}), tightening")
            _adjust("noop_limit", -1,
                    f"continue accuracy {productive_rate:.0%} (<{0.5:.0%}), tightening")
            _adjust("hallucination_loop_limit", -1,
                    f"continue accuracy {productive_rate:.0%} (<{0.5:.0%}), tightening")

    # Halt decision accuracy
    if halt_count >= 3:
        halt_correct_rate = halt_unproductive / halt_count
        if halt_correct_rate > 0.8:
            # Halts are accurate -> can tighten more aggressively
            _adjust("dead_session_threshold", -1,
                    f"halt accuracy {halt_correct_rate:.0%} (>{0.8:.0%}), tightening dead detection")
        elif halt_correct_rate < 0.5:
            # Halts are too aggressive -> loosen
            _adjust("stagnation_stall_limit", 1,
                    f"halt accuracy {halt_correct_rate:.0%} (<{0.5:.0%}), loosening")
            _adjust("noop_limit", 1,
                    f"halt accuracy {halt_correct_rate:.0%} (<{0.5:.0%}), loosening")

    return {
        "changes": changes,
        "summary": {
            "outcome_count": len(rows),
            "continue_count": continue_count,
            "continue_productive_rate": f"{continue_productive / continue_count:.0%}" if continue_count else "N/A",
            "halt_count": halt_count,
            "halt_correct_rate": f"{halt_unproductive / halt_count:.0%}" if halt_count else "N/A",
        }
    }


def cmd_calibrate(dry_run: bool = False) -> None:
    """Run calibration and print results."""
    result = calibrate_thresholds(dry_run=dry_run)

    summary = result["summary"]
    print(f"Outcome count: {summary.get('outcome_count', 0)}")
    if "message" in summary:
        print(summary["message"])
        return

    print(f"Continue decisions: {summary['continue_count']} (productive: {summary['continue_productive_rate']})")
    print(f"Halt decisions: {summary['halt_count']} (correct: {summary['halt_correct_rate']})")

    if result["changes"]:
        print(f"\nThreshold changes ({'dry run' if dry_run else 'applied'}):")
        for c in result["changes"]:
            print(f"  {c['key']}: {c['old']} -> {c['new']} ({c['reason']})")
    else:
        print("\nNo threshold changes needed.")


def cmd_show_config(project_dir: Optional[str] = None) -> None:
    """Show all current threshold values and their source.

    If project_dir is given, show the effective thresholds for that project
    (including per-project overrides and type defaults).
    """
    if project_dir:
        ptype = detect_project_type(project_dir)
        print(f"=== Carry Forward Thresholds (project: {project_dir}) ===")
        if ptype:
            print(f"  Detected type: {ptype}")
        print()
        for key, defn in THRESHOLD_DEFS.items():
            effective = get_threshold_for_project(key, project_dir)
            global_val = get_threshold(key)
            if effective != global_val:
                print(f"  {key} = {effective}  (project override, global={global_val})")
            elif global_val != defn["default"]:
                print(f"  {key} = {effective}  (global override, default={defn['default']})")
            else:
                print(f"  {key} = {effective}  (default)")
    else:
        print("=== Carry Forward Thresholds ===\n")
        for key, defn in THRESHOLD_DEFS.items():
            val_str = _read_config(key)
            if val_str is not None:
                source_row = None
                conn = get_carry_conn()
                row = conn.execute("SELECT source, updated_at FROM config WHERE key = ?", (key,)).fetchone()
                conn.close()
                source = row[0] if row else "unknown"
                ts = datetime.fromtimestamp(row[1]).strftime("%Y-%m-%d %H:%M") if row and row[1] else "?"
                print(f"  {key} = {val_str}  (source: {source}, since {ts})")
            else:
                print(f"  {key} = {defn['default']}  (source: default)")

    # Show project-specific overrides
    carry_conn = get_carry_conn()
    try:
        proj_rows = carry_conn.execute("""
            SELECT project_dir, key, value, source, project_type, updated_at
            FROM project_thresholds
            ORDER BY project_dir, key
        """).fetchall()
    except sqlite3.OperationalError:
        proj_rows = []
    carry_conn.close()

    if proj_rows:
        print("\n=== Project-Specific Thresholds ===\n")
        current_proj = None
        for pdir, pkey, pval, psrc, pttype, pts in proj_rows:
            if pdir != current_proj:
                current_proj = pdir
                print(f"  [{pdir}] (type: {pttype or 'unknown'})")
            ts_str = datetime.fromtimestamp(pts).strftime("%Y-%m-%d %H:%M") if pts else "?"
            print(f"    {pkey} = {pval}  (source: {psrc}, since {ts_str})")



# ---------------------------------------------------------------------------
# Phase 10: Technical pattern extraction
# ---------------------------------------------------------------------------


def extract_technical_patterns(project_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """Extract file-level patterns from session history, correlated with outcomes.

    Scans recent session tool messages for file paths, then joins with
    decision_outcomes to identify which files/directories correlate with
    productive vs unproductive sessions.

    Returns a list of pattern dicts with keys:
        path, total_sessions, productive, unproductive, success_rate, category
    """
    conn = get_conn()
    carry_conn = get_carry_conn()

    # Get recent sessions with outcomes
    rows = carry_conn.execute("""
        SELECT dl.session_id, do_out.outcome_productive, do_out.outcome_tool_calls
        FROM decision_outcomes do_out
        JOIN decision_log dl ON do_out.decision_id = dl.id
        ORDER BY do_out.checked_at DESC
        LIMIT 100
    """).fetchall()
    carry_conn.close()

    if len(rows) < 5:
        return []

    # For each session with an outcome, extract file paths from its messages
    file_stats: Dict[str, Dict[str, int]] = {}  # path -> {productive, unproductive, total}
    path_pattern = re.compile(r"/home/jericho/[a-zA-Z0-9_/.]+\.[a-z]{1,4}")

    for session_id, productive, tool_calls in rows:
        if tool_calls == 0:
            continue  # skip sessions with no tool calls

        # Get tool messages for this session
        tool_rows = conn.execute("""
            SELECT content FROM messages
            WHERE session_id = ? AND role = 'tool'
        """, (session_id,)).fetchall()
        # Also get assistant messages (file paths often appear there)
        asst_rows = conn.execute("""
            SELECT content FROM messages
            WHERE session_id = ? AND role = 'assistant'
        """, (session_id,)).fetchall()

        all_text = " ".join(r[0] or "" for r in tool_rows + asst_rows)
        paths = set(path_pattern.findall(all_text))

        # Filter by project if specified
        if project_dir:
            paths = {p for p in paths if p.startswith(project_dir)}

        # Track per-directory stats (more stable than per-file)
        dirs = set(p.rsplit("/", 1)[0] for p in paths if "/" in p)
        # Also track specific files that appear frequently
        files = paths

        for entity in dirs | files:
            if entity not in file_stats:
                file_stats[entity] = {"productive": 0, "unproductive": 0, "total": 0}
            file_stats[entity]["total"] += 1
            if productive:
                file_stats[entity]["productive"] += 1
            else:
                file_stats[entity]["unproductive"] += 1

    conn.close()

    # Filter to patterns with enough data points and interesting correlations
    patterns = []
    for path, stats in file_stats.items():
        if stats["total"] < 3:
            continue
        success_rate = stats["productive"] / stats["total"]
        # Only surface patterns that deviate significantly from baseline
        if stats["unproductive"] >= 3 and success_rate < 0.4:
            patterns.append({
                "path": path,
                "total_sessions": stats["total"],
                "productive": stats["productive"],
                "unproductive": stats["unproductive"],
                "success_rate": round(success_rate, 2),
                "category": "failure_hotspot",
            })
        elif stats["productive"] >= 3 and success_rate > 0.8:
            patterns.append({
                "path": path,
                "total_sessions": stats["total"],
                "productive": stats["productive"],
                "unproductive": stats["unproductive"],
                "success_rate": round(success_rate, 2),
                "category": "reliable_area",
            })

    # Sort by total sessions (most evidence first)
    patterns.sort(key=lambda p: p["total_sessions"], reverse=True)
    return patterns[:20]


def store_technical_patterns(patterns: List[Dict[str, Any]]) -> int:
    """Store extracted patterns as lessons in the lessons table.

    Returns the number of new/updated patterns stored.
    """
    if not patterns:
        return 0

    now = time.time()
    carry_conn = get_carry_conn()
    stored = 0

    for p in patterns:
        if p["category"] == "failure_hotspot":
            lesson_text = (
                f"{p['path']} appears in {p['total_sessions']} sessions with "
                f"only {p['success_rate']:.0%} success rate -- consider smaller steps"
            )
        else:
            lesson_text = (
                f"{p['path']} is a reliable area ({p['success_rate']:.0%} success "
                f"across {p['total_sessions']} sessions)"
            )

        # Upsert: check if similar lesson exists
        existing = carry_conn.execute(
            "SELECT id, hit_count FROM lessons WHERE category = ? AND lesson LIKE ?",
            (p["category"], f"{p['path']}%")
        ).fetchone()

        if existing:
            carry_conn.execute(
                "UPDATE lessons SET hit_count = ?, last_hit = ?, evidence = ? WHERE id = ?",
                (existing[1] + 1, now,
                 f"{p['productive']}/{p['total_sessions']} productive",
                 existing[0])
            )
        else:
            carry_conn.execute(
                "INSERT INTO lessons (lesson, category, evidence, hit_count, last_hit, created_at) "
                "VALUES (?, ?, ?, 1, ?, ?)",
                (lesson_text, p["category"],
                 f"{p['productive']}/{p['total_sessions']} productive",
                 now, now)
            )
            stored += 1

    carry_conn.commit()
    carry_conn.close()
    return stored


def cmd_analyze_patterns(project_dir: Optional[str] = None) -> None:
    """CLI command: analyze technical patterns from session history."""
    patterns = extract_technical_patterns(project_dir)

    if not patterns:
        print("No technical patterns found -- need at least 5 sessions with outcomes.")
        return

    # Categorize
    hotspots = [p for p in patterns if p["category"] == "failure_hotspot"]
    reliable = [p for p in patterns if p["category"] == "reliable_area"]

    if hotspots:
        print("=== FAILURE HOTSPOTS (low success rate) ===\n")
        for p in hotspots[:10]:
            print(f"  {p['path']}")
            print(f"    {p['productive']}/{p['total_sessions']} productive ({p['success_rate']:.0%})")
            print()

    if reliable:
        print("=== RELIABLE AREAS (high success rate) ===\n")
        for p in reliable[:10]:
            print(f"  {p['path']}")
            print(f"    {p['productive']}/{p['total_sessions']} productive ({p['success_rate']:.0%})")
            print()

    if not hotspots and not reliable:
        print("No strong patterns detected.")

    # Store as lessons
    stored = store_technical_patterns(patterns)
    if stored:
        print(f"Stored {stored} new technical patterns as lessons.")


def get_top_technical_patterns(n: int = 3) -> List[Dict[str, Any]]:
    """Get the top N technical patterns from the lessons table.

    These are lessons with category 'failure_hotspot' or 'reliable_area'.
    """
    carry_conn = get_carry_conn()
    try:
        rows = carry_conn.execute("""
            SELECT lesson, category, evidence, hit_count, last_hit
            FROM lessons
            WHERE category IN ('failure_hotspot', 'reliable_area')
            ORDER BY hit_count DESC, last_hit DESC
            LIMIT ?
        """, (n,)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    carry_conn.close()

    return [
        {"lesson": r[0], "category": r[1], "evidence": r[2], "hit_count": r[3]}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Phase 7: Per-project calibration from outcome data
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Phase 11: Next-task suggestion
# ---------------------------------------------------------------------------

def suggest_next(project_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """Suggest the next task to work on based on three signals.

    Signals (in priority order):
    1. Last session context -- what the most recent session was working on
    2. Uncommitted changes -- files modified but not committed (abandoned work)
    3. Roadmap gaps -- unchecked deliverables in project roadmap.yaml

    Returns a ranked list of up to 3 candidate task dicts, each with:
        source, confidence, description, details
    """
    candidates = []
    conn = get_conn()

    # Signal 1: Last session context
    cur = conn.execute("""
        SELECT id, title, parent_session_id
        FROM sessions
        WHERE tool_call_count > 0 AND source IN ('cli', 'telegram', 'whatsapp', 'cron')
        ORDER BY started_at DESC LIMIT 3
    """)
    recent_sessions = cur.fetchall()

    if recent_sessions:
        # Look at the most recent session's last assistant messages
        latest_id = recent_sessions[0][0]
        last_msgs = get_last_assistant_messages(latest_id, count=3, max_chars=2000)

        if last_msgs:
            # Extract file paths from last messages to identify what was being worked on
            all_last_text = " ".join(last_msgs)
            path_pattern = re.compile(r"/home/jericho/[a-zA-Z0-9_/.]+\.[a-z]{1,4}")
            paths = set(path_pattern.findall(all_last_text))

            # Filter by project if specified
            if project_dir:
                paths = {p for p in paths if p.startswith(project_dir)}

            if paths:
                # Check if this session was productive (has an outcome)
                carry_conn = get_carry_conn()
                outcome = carry_conn.execute("""
                    SELECT do_out.outcome_productive
                    FROM decision_outcomes do_out
                    JOIN decision_log dl ON do_out.decision_id = dl.id
                    WHERE dl.session_id = ?
                    ORDER BY do_out.checked_at DESC LIMIT 1
                """, (latest_id,)).fetchone()
                carry_conn.close()

                was_productive = bool(outcome[0]) if outcome else None

                # Summarize the files for the description
                file_names = [p.rsplit("/", 1)[-1] for p in sorted(paths)[:5]]
                dirs = sorted(set(p.rsplit("/", 1)[0] for p in paths))[:3]
                dir_names = [d.rsplit("/", 1)[-1] for d in dirs]

                if was_productive is False:
                    desc = f"Resume work on {', '.join(dir_names)} -- last session was unproductive"
                    confidence = 0.9
                elif was_productive is True:
                    desc = f"Continue successful work on {', '.join(dir_names)}"
                    confidence = 0.7
                else:
                    desc = f"Pick up where last session left off: {', '.join(dir_names)}"
                    confidence = 0.6

                candidates.append({
                    "source": "last_session",
                    "confidence": confidence,
                    "description": desc,
                    "details": {
                        "session_id": latest_id,
                        "files": sorted(paths)[:10],
                        "productive": was_productive,
                    },
                })
            else:
                # No files found, but still has context from messages
                snippet = last_msgs[-1][:200].strip() if last_msgs else ""
                if snippet:
                    candidates.append({
                        "source": "last_session",
                        "confidence": 0.4,
                        "description": f"Resume last session's work: {snippet}",
                        "details": {"session_id": latest_id},
                    })

    # Signal 2: Uncommitted changes (dirty working copy)
    if project_dir:
        gs = git_status(project_dir)
        dirty_files = gs.get("dirty_files", [])
        if dirty_files:
            # These files have changes that haven't been committed
            file_names = [f.split("/")[-1] for f in sorted(dirty_files)[:5]]
            candidates.append({
                "source": "uncommitted_changes",
                "confidence": 0.8,
                "description": f"Address {len(dirty_files)} uncommitted file(s): {', '.join(file_names)}",
                "details": {
                    "files": sorted(dirty_files)[:10],
                    "total_dirty": len(dirty_files),
                },
            })
    elif recent_sessions:
        # No project specified -- check all detected project dirs from recent sessions
        cur2 = conn.execute("""
            SELECT content FROM messages
            WHERE session_id = ? AND role IN ('tool', 'assistant')
        """, (recent_sessions[0][0],))
        rows = cur2.fetchall()
        all_text = " ".join(r[0] or "" for r in rows)
        all_paths = set(re.findall(r"/home/jericho/[a-zA-Z0-9_/.]+\.[a-z]{1,4}", all_text))
        all_dirs = sorted(set(p.rsplit("/", 1)[0] for p in all_paths))[:10]

        for d in all_dirs:
            gs = git_status(d)
            if not gs.get("error") and gs.get("git_root") and gs.get("dirty"):
                dirty_files = gs.get("dirty_files", [])
                if dirty_files:
                    root = gs["git_root"]
                    file_names = [f.split("/")[-1] for f in sorted(dirty_files)[:5]]
                    candidates.append({
                        "source": "uncommitted_changes",
                        "confidence": 0.8,
                        "description": f"Commit or fix {len(dirty_files)} dirty file(s) in {root}: {', '.join(file_names)}",
                        "details": {
                            "project": root,
                            "files": sorted(dirty_files)[:10],
                        },
                    })
                    break  # just report the first dirty project

    # Signal 3: Roadmap gaps (next unchecked deliverable)
    target_dirs = [project_dir] if project_dir else []
    if not target_dirs and recent_sessions:
        # Auto-detect project dirs from recent sessions
        cur3 = conn.execute("""
            SELECT content FROM messages
            WHERE session_id = ? AND role IN ('tool', 'assistant')
        """, (recent_sessions[0][0],))
        rows3 = cur3.fetchall()
        text3 = " ".join(r[0] or "" for r in rows3)
        paths3 = set(re.findall(r"/home/jericho/[a-zA-Z0-9_/.]+\.[a-z]{1,4}", text3))
        target_dirs = sorted(set(p.rsplit("/", 1)[0] for p in paths3))[:5]

    if HAS_ROADMAP_BUILDER and target_dirs:
        roadmaps = scan_project_roadmaps(target_dirs)
        for rm in roadmaps:
            if rm.get("next_deliverables"):
                first_next = rm["next_deliverables"][0]
                phase_name = rm.get("current_phase", "unknown phase")
                candidates.append({
                    "source": "roadmap",
                    "confidence": 0.6,
                    "description": f"Roadmap next: {first_next.get('name', 'unnamed')} (phase: {phase_name})",
                    "details": {
                        "project": rm.get("project_dir", ""),
                        "phase": phase_name,
                        "deliverable": first_next,
                    },
                })
                break  # one roadmap suggestion is enough

    # Fallback: if no candidates, check for failure hotspots
    if not candidates:
        hotspots = [p for p in extract_technical_patterns(project_dir)
                    if p["category"] == "failure_hotspot"]
        if hotspots:
            top = hotspots[0]
            candidates.append({
                "source": "failure_hotspot",
                "confidence": 0.5,
                "description": f"Investigate failure hotspot: {top['path']} ({top['success_rate']:.0%} success)",
                "details": top,
            })

    # Sort by confidence (descending) and return top 3
    candidates.sort(key=lambda c: c["confidence"], reverse=True)
    conn.close()
    return candidates[:3]


def cmd_suggest_next(project_dir: Optional[str] = None) -> None:
    """CLI command: suggest the next task to work on."""
    suggestions = suggest_next(project_dir)

    if not suggestions:
        print("No task suggestions available -- need more session history or project context.")
        return

    print("=== SUGGESTED NEXT TASKS ===\n")
    for i, s in enumerate(suggestions, 1):
        conf_bar = "=" * int(s["confidence"] * 10)
        print(f"  {i}. [{s['source']}] (confidence: {s['confidence']:.0%})")
        print(f"     {s['description']}")
        if s.get("details", {}).get("files"):
            print(f"     Files: {', '.join(s['details']['files'][:5])}")
        print()



def calibrate_project_thresholds(project_dir: str, dry_run: bool = False) -> Dict[str, Any]:
    """Calibrate thresholds for a specific project based on its outcome data.

     Uses the same calibration logic as calibrate_thresholds() but scoped to
    decisions that involved this project (matched via chain_git_heads.project_dir).

    If there are fewer than 5 outcomes for this project, seeds from
    PROJECT_TYPE_DEFAULTS instead.
    """
    ptype = detect_project_type(project_dir)
    carry_conn = get_carry_conn()

    # Find decision sessions that involved this project
    project_sessions = carry_conn.execute("""
        SELECT DISTINCT session_id FROM chain_git_heads WHERE project_dir = ?
    """, (project_dir,)).fetchall()
    project_session_ids = [r[0] for r in project_sessions]

    if not project_session_ids:
        carry_conn.close()
        # Seed from project type defaults
        return _seed_project_type_defaults(project_dir, ptype, dry_run)

    # Gather outcomes scoped to this project's sessions
    placeholders = ",".join("?" * len(project_session_ids))
    rows = carry_conn.execute(f"""
        SELECT dl.decision, dl.can_continue,
               do_out.outcome_productive, do_out.outcome_git_moved,
               do_out.outcome_chain_continued, do_out.outcome_tool_calls
        FROM decision_outcomes do_out
        JOIN decision_log dl ON do_out.decision_id = dl.id
        WHERE do_out.session_id IN ({placeholders})
        ORDER BY do_out.checked_at DESC
        LIMIT 100
    """, project_session_ids).fetchall()
    carry_conn.close()

    if len(rows) < 5:
        # Not enough data -- seed from project type defaults
        return _seed_project_type_defaults(project_dir, ptype, dry_run)

    # Same calibration logic as global, but writes to project_thresholds
    continue_outcomes = [(r[2], r[3], r[4], r[5]) for r in rows if r[1] == 1]
    halt_outcomes = [(r[2], r[3], r[4], r[5]) for r in rows if r[1] == 0]

    continue_productive = sum(1 for o in continue_outcomes if o[0])
    continue_count = len(continue_outcomes)
    halt_unproductive = sum(1 for o in halt_outcomes if not o[0])
    halt_count = len(halt_outcomes)

    changes = []

    def _adjust_project(key: str, delta: int, reason: str) -> None:
        current = get_threshold_for_project(key, project_dir)
        defn = THRESHOLD_DEFS[key]
        new_val = current + delta
        new_val = max(defn["min"], min(defn["max"], new_val))
        if new_val != current:
            if not dry_run:
                _write_project_config(project_dir, key, str(new_val), "project_calibration")
            changes.append({
                "key": key,
                "old": current,
                "new": new_val,
                "reason": reason,
            })

    # Continue decision accuracy
    if continue_count >= 3:
        productive_rate = continue_productive / continue_count
        if productive_rate > 0.8:
            _adjust_project("stagnation_stall_limit", 1,
                            f"project continue accuracy {productive_rate:.0%} (>{0.8:.0%}), loosening")
            _adjust_project("noop_limit", 1,
                            f"project continue accuracy {productive_rate:.0%} (>{0.8:.0%}), loosening")
        elif productive_rate < 0.5:
            _adjust_project("stagnation_stall_limit", -1,
                            f"project continue accuracy {productive_rate:.0%} (<{0.5:.0%}), tightening")
            _adjust_project("noop_limit", -1,
                            f"project continue accuracy {productive_rate:.0%} (<{0.5:.0%}), tightening")
            _adjust_project("hallucination_loop_limit", -1,
                            f"project continue accuracy {productive_rate:.0%} (<{0.5:.0%}), tightening")

    # Halt decision accuracy
    if halt_count >= 3:
        halt_correct_rate = halt_unproductive / halt_count
        if halt_correct_rate > 0.8:
            _adjust_project("stagnation_stall_limit", -1,
                            f"project halt accuracy {halt_correct_rate:.0%} (>{0.8:.0%}), tightening")
        elif halt_correct_rate < 0.5:
            _adjust_project("stagnation_stall_limit", 1,
                            f"project halt accuracy {halt_correct_rate:.0%} (<{0.5:.0%}), loosening")
            _adjust_project("noop_limit", 1,
                            f"project halt accuracy {halt_correct_rate:.0%} (<{0.5:.0%}), loosening")

    return {
        "project_dir": project_dir,
        "project_type": ptype,
        "changes": changes,
        "summary": {
            "outcome_count": len(rows),
            "continue_count": continue_count,
            "continue_productive_rate": f"{continue_productive / continue_count:.0%}" if continue_count else "N/A",
            "halt_count": halt_count,
            "halt_correct_rate": f"{halt_unproductive / halt_count:.0%}" if halt_count else "N/A",
        }
    }


def _seed_project_type_defaults(project_dir: str, ptype: Optional[str],
                                dry_run: bool = False) -> Dict[str, Any]:
    """Seed per-project thresholds from PROJECT_TYPE_DEFAULTS for a new project."""
    if not ptype or ptype not in PROJECT_TYPE_DEFAULTS:
        return {
            "project_dir": project_dir,
            "project_type": ptype,
            "changes": [],
            "summary": {"message": f"No type defaults for '{ptype}' -- using global thresholds"}
        }

    changes = []
    type_defaults = PROJECT_TYPE_DEFAULTS[ptype]

    for key, value in type_defaults.items():
        current = get_threshold(key)  # global value
        if value != current:
            if not dry_run:
                _write_project_config(project_dir, key, str(value), "auto-detect")
            changes.append({
                "key": key,
                "old": current,
                "new": value,
                "reason": f"seeded from {ptype} type defaults",
            })

    return {
        "project_dir": project_dir,
        "project_type": ptype,
        "changes": changes,
        "summary": {"message": f"Seeded {len(changes)} thresholds from {ptype} type defaults"}
    }


# ---------------------------------------------------------------------------
# Command: blockers
# ---------------------------------------------------------------------------

def cmd_blockers() -> None:
    """Show unresolved blockers."""
    conn = get_carry_conn()
    rows = conn.execute("""
        SELECT id, session_id, message, created_at FROM blockers
        WHERE resolved_at IS NULL
        ORDER BY created_at DESC
    """).fetchall()
    conn.close()

    if not rows:
        print("No unresolved blockers.")
        return

    print("=== UNRESOLVED BLOCKERS ===")
    for (bid, sid, msg, ts) in rows:
        ts_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "?"
        print(f"  #{bid} | {ts_str} | {msg}")
        if sid:
            print(f"         session: {sid}")


def cmd_block(message: str) -> None:
    """Record a new blocker."""
    conn = get_carry_conn()
    # Get last session ID for context
    sconn = get_conn()
    row = sconn.execute("""
        SELECT id FROM sessions
        WHERE message_count > 5 AND source IN ('cli', 'telegram', 'whatsapp')
        ORDER BY started_at DESC LIMIT 1
    """).fetchone()
    sconn.close()

    session_id = row[0] if row else None
    conn.execute("INSERT INTO blockers (session_id, message, created_at) VALUES (?, ?, ?)",
                 (session_id, message, time.time()))
    conn.commit()
    conn.close()
    print(f"Blocked: {message}")


def cmd_unblock(pattern: str) -> None:
    """Resolve blockers matching a pattern."""
    conn = get_carry_conn()
    rows = conn.execute("""
        SELECT id, message FROM blockers WHERE resolved_at IS NULL AND message LIKE ?
    """, (f"%{pattern}%",)).fetchall()

    if not rows:
        print(f"No blockers matching '{pattern}'.")
        conn.close()
        return

    for (bid, msg) in rows:
        conn.execute("UPDATE blockers SET resolved_at = ? WHERE id = ?", (time.time(), bid))
        print(f"Resolved #{bid}: {msg}")

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Git-aware project state
# ---------------------------------------------------------------------------

def git_status(project_dir: str) -> Dict[str, Any]:
    """Run git commands in a project dir and return structured state."""
    result = {"dir": project_dir, "branch": None, "last_commits": [], "dirty": False, "error": None}

    # Check if it's actually a git repo (might need to go up)
    git_dir = project_dir
    while git_dir and git_dir != "/":
        if os.path.isdir(os.path.join(git_dir, ".git")):
            break
        git_dir = os.path.dirname(git_dir)
    else:
        result["error"] = "not a git repo"
        return result

    try:
        # Branch
        r = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                           capture_output=True, text=True, cwd=git_dir, timeout=5)
        if r.returncode == 0:
            result["branch"] = r.stdout.strip()
            result["git_root"] = git_dir

        # Last commits
        r = subprocess.run(["git", "log", "--oneline", "-5"],
                           capture_output=True, text=True, cwd=git_dir, timeout=5)
        if r.returncode == 0:
            result["last_commits"] = r.stdout.strip().split("\n") if r.stdout.strip() else []

        # Dirty state
        r = subprocess.run(["git", "status", "--porcelain"],
                           capture_output=True, text=True, cwd=git_dir, timeout=5)
        if r.returncode == 0:
            dirty_files = r.stdout.strip().split("\n") if r.stdout.strip() else []
            result["dirty"] = len(dirty_files) > 0
            result["dirty_files"] = dirty_files[:10]  # cap at 10

    except (subprocess.TimeoutExpired, FileNotFoundError):
        result["error"] = "git command failed"

    return result


# Project state files to look for (in order of priority)
PROJECT_STATE_FILES = ["ROADMAP.md", "NORTH_STAR.md", "TODO.md", "NEXT.md", "PLAN.md"]


def read_project_state(project_dir: str) -> Dict[str, Any]:
    """
    Read project state files (ROADMAP.md, etc.) from a project directory.
    Returns dict with file contents and git diff stat.
    """
    state = {"dir": project_dir, "files": {}, "git_diff_stat": None}

    # Read state files
    for filename in PROJECT_STATE_FILES:
        filepath = os.path.join(project_dir, filename)
        if os.path.isfile(filepath):
            try:
                with open(filepath, "r") as f:
                    content = f.read()
                if content.strip():
                    state["files"][filename] = content[:3000]  # cap at 3K chars
            except (PermissionError, OSError):
                pass

    # Check parent dirs too (project root may be above the detected dir)
    check_dir = project_dir
    for _ in range(3):
        parent = os.path.dirname(check_dir)
        if parent == check_dir or not parent:
            break
        for filename in PROJECT_STATE_FILES:
            if filename not in state["files"]:
                filepath = os.path.join(parent, filename)
                if os.path.isfile(filepath):
                    try:
                        with open(filepath, "r") as f:
                            content = f.read()
                        if content.strip():
                            state["files"][filename] = content[:3000]
                    except (PermissionError, OSError):
                        pass
        check_dir = parent

    # Git diff stat (what changed recently)
    git_dir = project_dir
    while git_dir and git_dir != "/":
        if os.path.isdir(os.path.join(git_dir, ".git")):
            break
        git_dir = os.path.dirname(git_dir)
    
    if git_dir and git_dir != "/":
        try:
            r = subprocess.run(
                ["git", "diff", "--stat", "HEAD~5..HEAD"],
                capture_output=True, text=True, cwd=git_dir, timeout=5
            )
            if r.returncode == 0 and r.stdout.strip():
                state["git_diff_stat"] = r.stdout.strip()[:2000]
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    return state


def cmd_status(session_id: Optional[str] = None) -> None:
    """Show git-aware project state for projects detected from a session."""
    conn = get_conn()
    cur = conn.cursor()

    if not session_id:
        cur.execute("""
            SELECT id FROM sessions
            WHERE message_count > 5 AND source IN ('cli', 'telegram', 'whatsapp')
            ORDER BY started_at DESC LIMIT 1
        """)
        row = cur.fetchone()
        session_id = row[0] if row else None

    if not session_id:
        print("No session found.")
        conn.close()
        return

    # Extract all file paths from the session
    cur.execute("""
        SELECT content FROM messages WHERE session_id = ? AND role IN ('user', 'assistant', 'tool')
    """, (session_id,))
    all_text = " ".join(r[0] or "" for r in cur.fetchall())
    conn.close()

    # Find project directories
    paths = set(re.findall(r"/home/jericho/[a-zA-Z0-9_/.-]+\.[a-z]{1,4}", all_text))
    dirs = sorted(set(p.rsplit("/", 1)[0] for p in paths))[:10]

    if not dirs:
        print("No project directories detected in session.")
        return

    print("=== PROJECT STATUS ===")
    for d in dirs:
        gs = git_status(d)
        if gs.get("error"):
            continue  # skip non-git dirs silently

        print(f"\n  {d}")
        if gs.get("branch"):
            print(f"    branch: {gs['branch']}")
        if gs.get("last_commits"):
            print(f"    recent commits:")
            for c in gs["last_commits"]:
                print(f"      {c}")
        if gs.get("dirty"):
            print(f"    DIRTY: {len(gs.get('dirty_files', []))} uncommitted changes")
            for f in gs.get("dirty_files", [])[:5]:
                print(f"      {f}")


# ---------------------------------------------------------------------------
# Smart summary extraction
# ---------------------------------------------------------------------------

def _extract_progress_fallback(text: str) -> Dict[str, Any]:
    """Fallback regex extraction for sessions without structured markers.
    Only used when no agent-written summary is available."""
    lines = text.split("\n")
    results = {
        "completed": [],
        "in_progress": [],
        "next": [],
        "errors": [],
        "key_facts": [],
    }

    for line in lines:
        stripped = line.strip()
        # Completed items (checkmarks, "done", "finished")
        if re.match(r'^[-*]\s*\[x\]|✓|✅|done:|finished:|completed:', stripped, re.I):
            results["completed"].append(stripped[:120])
        # In-progress items
        elif re.match(r'^[-*]\s*\[~\]|🔄|working on:|in progress:', stripped, re.I):
            results["in_progress"].append(stripped[:120])
        # Next steps / TODO
        elif re.match(r'^[-*]\s*\[\s\]|next:|todo:|remaining:|still need', stripped, re.I):
            results["next"].append(stripped[:120])
        # Errors / failures
        elif re.match(r'error:|failed|failure|panic!|unwrap.*err', stripped, re.I):
            results["errors"].append(stripped[:120])

    # Extract key facts (sentences with "committed", "merged", "test.*pass", "build.*success")
    sentences = re.split(r'[.!?\n]', text)
    for s in sentences:
        s = s.strip()
        if len(s) > 20 and len(s) < 200:
            if re.search(r'committed|merged|test.*pass|build.*success|all.*pass|(\d+) test', s, re.I):
                results["key_facts"].append(s)

    return results


def get_last_assistant_messages(session_id: str, count: int = 3, max_chars: int = 2000) -> List[str]:
    """Get the last N assistant messages verbatim. This is the primary
    handoff mechanism -- agents interpret, carry_forward transports."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT content FROM messages
        WHERE session_id = ? AND role = 'assistant'
        ORDER BY timestamp DESC LIMIT ?
    """, (session_id, count))
    msgs = [r[0] for r in cur.fetchall() if r[0]]
    conn.close()

    # Return in chronological order, capped at max_chars total
    msgs.reverse()
    total = 0
    result = []
    for m in msgs:
        if total + len(m) > max_chars:
            # Truncate the last one to fit
            remaining = max_chars - total
            if remaining > 100:
                result.append(m[:remaining] + "\n... (truncated)")
            break
        result.append(m)
        total += len(m)
    return result


def cmd_summary(session_id: Optional[str] = None) -> None:
    """Smart summary of session progress. Primary: raw assistant messages.
    Fallback: regex extraction if no messages available."""
    conn = get_conn()
    cur = conn.cursor()

    if not session_id:
        cur.execute("""
            SELECT id FROM sessions
            WHERE message_count > 5 AND source IN ('cli', 'telegram', 'whatsapp')
            ORDER BY started_at DESC LIMIT 1
        """)
        row = cur.fetchone()
        session_id = row[0] if row else None

    if not session_id:
        print("No session found.")
        conn.close()
        return

    # Get all assistant messages for fallback extraction
    cur.execute("""
        SELECT content FROM messages
        WHERE session_id = ? AND role = 'assistant'
        ORDER BY timestamp ASC
    """, (session_id,))
    asst_msgs = [r[0] for r in cur.fetchall() if r[0]]
    conn.close()

    if not asst_msgs:
        print("No assistant messages in session.")
        return

    print(f"=== SESSION SUMMARY ({session_id}) ===\n")

    # Primary: last assistant messages verbatim (the intelligence layer)
    last_msgs = get_last_assistant_messages(session_id, count=2, max_chars=1500)
    if last_msgs:
        print("LAST ASSISTANT MESSAGES (raw):")
        for msg in last_msgs:
            print(f"  {msg[:800]}")
            print()
        print()

    # Supplemental: regex fallback for structured markers
    all_progress = {"completed": [], "in_progress": [], "next": [], "errors": [], "key_facts": []}
    for msg in asst_msgs:
        prog = _extract_progress_fallback(msg)
        for key in all_progress:
            all_progress[key].extend(prog[key])

    # Deduplicate
    for key in all_progress:
        seen = set()
        deduped = []
        for item in all_progress[key]:
            if item not in seen:
                seen.add(item)
                deduped.append(item)
        all_progress[key] = deduped[:15]

    if all_progress["key_facts"]:
        print("KEY FACTS (auto-extracted):")
        for f in all_progress["key_facts"][:8]:
            print(f"  - {f}")
        print()

    if all_progress["completed"]:
        print("COMPLETED (auto-extracted):")
        for item in all_progress["completed"][:10]:
            print(f"  {item}")
        print()

    if all_progress["errors"]:
        print("ERRORS ENCOUNTERED:")
        for item in all_progress["errors"][:8]:
            print(f"  {item}")
        print()


# ---------------------------------------------------------------------------
# Command: context (the main one - integrates everything)
# ---------------------------------------------------------------------------

def cmd_context(include_cron: bool = False) -> None:
    """Full context from last session, with git state, summary, chain, and blockers."""
    # v5: Auto-record outcome from previous session before showing context
    auto_record_outcomes()

    conn = get_conn()
    cur = conn.cursor()

    sources = "('cli', 'telegram', 'whatsapp')" if not include_cron else "('cli', 'telegram', 'whatsapp', 'cron')"
    cur.execute(f"""
        SELECT id, message_count, title, parent_session_id, started_at
        FROM sessions
        WHERE message_count > 5 AND source IN {sources}
        ORDER BY started_at DESC LIMIT 1
    """)
    row = cur.fetchone()
    if not row:
        print("No sessions with real content found.")
        conn.close()
        return

    session_id, msg_count, title, parent_id, started_at = row
    ts = datetime.fromtimestamp(started_at).strftime("%Y-%m-%d %H:%M") if started_at else "?"

    print(f"SESSION: {session_id}")
    print(f"TITLE: {title or '(untitled)'}")
    print(f"STARTED: {ts}")
    if parent_id:
        print(f"PARENT: {parent_id}")
    print()

    # Get all user/assistant messages
    cur.execute("""
        SELECT role, content FROM messages
        WHERE session_id = ? AND role IN ('user', 'assistant')
        ORDER BY timestamp ASC
    """, (session_id,))
    rows = cur.fetchall()

    user_msgs = [r[1] for r in rows if r[0] == "user"]
    asst_msgs = [r[1] for r in rows if r[0] == "assistant"]
    conn.close()

    if user_msgs:
        print("=== WHAT WAS REQUESTED ===")
        print((user_msgs[0] or "")[:500])
        print()

    if user_msgs and len(user_msgs) > 1:
        print("=== LAST USER MESSAGE ===")
        print((user_msgs[-1] or "")[:500])
        print()

    # v4: Raw assistant messages (primary handoff mechanism)
    if asst_msgs:
        print("=== LAST ASSISTANT MESSAGES (raw) ===")
        last_raw = get_last_assistant_messages(session_id, count=2, max_chars=1500)
        for msg in last_raw:
            print(f"  {msg[:800]}")
            print()
        print()

    # Supplemental: regex-extracted markers
    if asst_msgs:
        print("=== EXTRACTED MARKERS ===")
        all_progress = {"completed": [], "in_progress": [], "next": [], "errors": [], "key_facts": []}
        for msg in asst_msgs:
            prog = _extract_progress_fallback(msg)
            for key in all_progress:
                all_progress[key].extend(prog[key])

        found_structured = False
        for key in ["key_facts", "completed", "errors"]:
            items = all_progress[key]
            if items:
                seen = set()
                deduped = []
                for item in items:
                    if item not in seen:
                        seen.add(item)
                        deduped.append(item)
                deduped = deduped[:8]
                if deduped:
                    found_structured = True
                    label = key.replace("_", " ").upper()
                    print(f"  {label}:")
                    for item in deduped:
                        print(f"    {item}")
        if not found_structured:
            print("  (no structured markers found in session)")
        print()

    # Detect project paths and show git status
    all_text = " ".join(r[1] or "" for r in rows)
    # Also include tool messages for better path detection
    conn_tool = get_conn()
    tool_rows = conn_tool.execute("""
        SELECT content FROM messages
        WHERE session_id = ? AND role = 'tool'
    """, (session_id,)).fetchall()
    conn_tool.close()
    tool_text = " ".join(r[0] or "" for r in tool_rows)
    combined_text = all_text + " " + tool_text
    paths = set(re.findall(r"/home/jericho/[a-zA-Z0-9_/.-]+\.[a-z]{1,4}", combined_text))
    dirs = sorted(set(p.rsplit("/", 1)[0] for p in paths))[:10]

    if dirs:
        print("=== PROJECT STATUS ===")
        for d in dirs:
            gs = git_status(d)
            if gs.get("error"):
                continue
            if gs.get("git_root"):
                print(f"\n  {gs['git_root']} (branch: {gs.get('branch', '?')})")
                if gs.get("last_commits"):
                    for c in gs["last_commits"][:3]:
                        print(f"    {c}")
                if gs.get("dirty"):
                    print(f"    DIRTY: {len(gs.get('dirty_files', []))} uncommitted changes")

                # Phase 8: Show detected test command
                test_cmd = detect_test_command(gs.get("git_root", d))
                if test_cmd:
                    print(f"    TEST: {test_cmd}")

            # Read project state files (ROADMAP.md, etc.)
            ps = read_project_state(d)
            if ps.get("files"):
                print(f"  PROJECT STATE:")
                for fname, content in ps["files"].items():
                    # Show first 20 lines of each file
                    lines = content.splitlines()[:20]
                    print(f"    --- {fname} ---")
                    for line in lines:
                        print(f"    {line}")
                    if len(content.splitlines()) > 20:
                        print(f"    ... ({len(content.splitlines())} lines total)")
            if ps.get("git_diff_stat"):
                print(f"  RECENT CHANGES (HEAD~5..HEAD):")
                for line in ps["git_diff_stat"].splitlines()[:10]:
                    print(f"    {line}")
        print()

    # Roadmap progress (if roadmap_builder is available)
    if HAS_ROADMAP_BUILDER:
        roadmaps = scan_project_roadmaps(dirs)
        roadmap_text = format_roadmap_context(roadmaps)
        if roadmap_text:
            print("=== ROADMAP PROGRESS ===")
            print(roadmap_text)

    # Show session chain depth
    carry_conn = get_carry_conn()
    chain_count = 0
    current = parent_id
    while current:
        carry_conn_row = cur  # reusing var name is fine, we're in a different scope
        conn2 = get_conn()
        r = conn2.execute("SELECT parent_session_id FROM sessions WHERE id = ?", (current,)).fetchone()
        conn2.close()
        if not r:
            break
        chain_count += 1
        current = r[0]
        if chain_count > 20:
            break
    if chain_count > 0:
        print(f"=== CHAIN DEPTH: {chain_count} continuation(s) ===")
        if chain_count >= 8:
            print("  WARNING: Deep chain. Consider stopping for human review.")
        print()

    # Thrash detection
    thrashing, dead_count, _, thrash_details = detect_thrash(session_id)
    if thrashing or dead_count > 0:
        print("=== THRASH CHECK ===")
        if thrashing:
            print(f"  THRASHING: {thrash_details}")
            print("  RECOMMENDATION: Do NOT schedule another continuation. Stop for human review.")
        elif dead_count > 0:
            print(f"  CAUTION: {dead_count} dead session(s) in recent chain ({thrash_details})")
        print()

    # Show unresolved blockers
    carry_conn = get_carry_conn()
    blockers = carry_conn.execute("""
        SELECT message, created_at FROM blockers WHERE resolved_at IS NULL
        ORDER BY created_at DESC LIMIT 5
    """).fetchall()
    carry_conn.close()
    if blockers:
        print("=== UNRESOLVED BLOCKERS ===")
        for (msg, ts) in blockers:
            ts_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "?"
            print(f"  [{ts_str}] {msg}")
        print()

    # v6: Learned lessons from outcome history
    lessons = get_top_lessons(n=3)
    if lessons:
        print("=== LEARNED LESSONS ===")
        for lesson in lessons:
            print(f"  - {lesson['lesson']}")
            if lesson.get("evidence"):
                print(f"    ({lesson['evidence']})")
        print()

    # v10: Technical patterns from file-outcome correlation
    tech_patterns = get_top_technical_patterns(n=3)
    if tech_patterns:
        print("=== TECHNICAL PATTERNS ===")
        for tp in tech_patterns:
            label = "WARNING" if tp["category"] == "failure_hotspot" else "OK"
            print(f"  [{label}] {tp['lesson']}")
            if tp.get("evidence"):
                print(f"    ({tp['evidence']})")
        print()

    # v11: Next-task suggestions
    suggestions = suggest_next()
    if suggestions:
        print("=== SUGGESTED NEXT TASKS ===")
        for i, s in enumerate(suggestions, 1):
            print(f"  {i}. [{s['source']}] ({s['confidence']:.0%}) {s['description']}")
        print()


# ---------------------------------------------------------------------------
# Command: health (Phase 9: Session health dashboard)
# ---------------------------------------------------------------------------

def _today_bounds() -> Tuple[float, float]:
    """Return (start_of_day, now) as unix timestamps for today in local time."""
    now = time.time()
    # Use datetime to get start of today in local time
    local_now = datetime.fromtimestamp(now)
    start_of_day = datetime(local_now.year, local_now.month, local_now.day).timestamp()
    return start_of_day, now


def _compute_git_commits_today(carry_conn) -> int:
    """Count distinct git HEAD changes recorded today across all projects.

    For each project, compare the HEAD recorded before today (if any) to the
    HEADs recorded today. Each distinct new HEAD = 1 commit landed.
    """
    start_of_day, _ = _today_bounds()
    cur = carry_conn.cursor()

    # Get all git heads recorded today, grouped by project
    cur.execute("""
        SELECT project_dir, git_head FROM chain_git_heads
        WHERE recorded_at >= ? AND recorded_at < ?
    """, (start_of_day, start_of_day + 86400))
    today_heads = cur.fetchall()

    if not today_heads:
        return 0

    # Get the latest HEAD before today for each project
    projects_today = set(h[0] for h in today_heads)
    before_heads = {}
    for proj in projects_today:
        cur.execute("""
            SELECT git_head FROM chain_git_heads
            WHERE project_dir = ? AND recorded_at < ?
            ORDER BY recorded_at DESC LIMIT 1
        """, (proj, start_of_day))
        row = cur.fetchone()
        if row:
            before_heads[proj] = row[0]

    # Count distinct new HEADs per project
    commits = 0
    for proj in projects_today:
        proj_heads = set(h[1] for h in today_heads if h[0] == proj)
        before = before_heads.get(proj)
        # All HEADs that differ from the pre-today HEAD count as new commits
        if before:
            commits += len(proj_heads - {before})
        else:
            commits += len(proj_heads)

    return commits


def _compute_wasted_time(state_conn) -> float:
    """Estimate minutes wasted on failed (zero-tool, non-cron) sessions today.

    A session with 0 tool calls that isn't a cron job is considered wasted.
    Uses (ended_at - started_at) when available, otherwise estimates 60s.
    """
    start_of_day, now = _today_bounds()
    cur = state_conn.cursor()

    # Check if ended_at column exists (may not in older/test schemas)
    has_ended_at = False
    try:
        cur.execute("SELECT ended_at FROM sessions LIMIT 0")
        has_ended_at = True
    except sqlite3.OperationalError:
        pass

    if has_ended_at:
        cur.execute("""
            SELECT started_at, ended_at FROM sessions
            WHERE tool_call_count = 0
              AND source NOT IN ('cron')
              AND started_at >= ? AND started_at < ?
        """, (start_of_day, start_of_day + 86400))
    else:
        cur.execute("""
            SELECT started_at, NULL FROM sessions
            WHERE tool_call_count = 0
              AND source NOT IN ('cron')
              AND started_at >= ? AND started_at < ?
        """, (start_of_day, start_of_day + 86400))

    wasted_secs = 0.0
    for started, ended in cur.fetchall():
        if ended and ended > started:
            wasted_secs += (ended - started)
        else:
            wasted_secs += 60  # estimate 1 min for sessions without end time

    return wasted_secs / 60.0  # return minutes


def session_health_data() -> Dict[str, Any]:
    """Gather today's session health metrics.

    Returns dict with:
        sessions_total: all sessions started today
        sessions_active: sessions with tool_call_count > 0
        sessions_dead: sessions with 0 tool calls (non-cron)
        decisions_continue: decisions to continue today
        decisions_halt: decisions to halt today
        decisions_total: total decisions today
        commits_landed: distinct git HEAD changes today
        test_counts: dict of {project: latest_test_count} for today
        wasted_minutes: estimated minutes on dead (non-cron) sessions
        wasted_sessions: count of dead sessions
    """
    start_of_day, now = _today_bounds()
    day_end = start_of_day + 86400

    conn = get_conn()
    carry = get_carry_conn()

    # --- Session counts ---
    cur = conn.cursor()
    cur.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN tool_call_count > 0 THEN 1 ELSE 0 END) as active,
            SUM(CASE WHEN tool_call_count = 0 AND source NOT IN ('cron') THEN 1 ELSE 0 END) as dead
        FROM sessions
        WHERE started_at >= ? AND started_at < ?
    """, (start_of_day, day_end))
    row = cur.fetchone()
    sessions_total = row[0] or 0
    sessions_active = int(row[1] or 0)
    sessions_dead = int(row[2] or 0)

    # --- Decision counts ---
    ccur = carry.cursor()
    ccur.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN can_continue = 1 THEN 1 ELSE 0 END) as continues,
            SUM(CASE WHEN can_continue = 0 THEN 1 ELSE 0 END) as halts
        FROM decision_log
        WHERE created_at >= ? AND created_at < ?
          AND session_id NOT LIKE '--%'
    """, (start_of_day, day_end))
    drow = ccur.fetchone()
    decisions_total = drow[0] or 0
    decisions_continue = int(drow[1] or 0)
    decisions_halt = int(drow[2] or 0)

    # --- Commits landed ---
    commits_landed = _compute_git_commits_today(carry)

    # --- Test counts (latest per session today) ---
    ccur.execute("""
        SELECT session_id, test_count, source FROM tick_test_counts
        WHERE recorded_at >= ? AND recorded_at < ?
        ORDER BY recorded_at DESC
    """, (start_of_day, day_end))
    test_entries = ccur.fetchall()
    # Just track latest test counts we've seen
    test_counts = {}
    for sid, count, source in test_entries:
        if source not in test_counts:
            test_counts[source] = count

    # --- Wasted time ---
    wasted_minutes = _compute_wasted_time(conn)

    # --- Decision accuracy (from outcomes) ---
    ccur.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN outcome_productive = 1 THEN 1 ELSE 0 END) as productive,
            SUM(CASE WHEN outcome_productive = 0 AND session_id NOT LIKE '--%' THEN 1 ELSE 0 END) as wasted
        FROM decision_outcomes
        WHERE checked_at >= ? AND checked_at < ?
          AND session_id NOT LIKE '--%'
    """, (start_of_day, day_end))
    orow = ccur.fetchone()
    outcomes_total = orow[0] or 0
    outcomes_productive = int(orow[1] or 0)
    outcomes_wasted = int(orow[2] or 0)

    conn.close()
    carry.close()

    return {
        "sessions_total": sessions_total,
        "sessions_active": sessions_active,
        "sessions_dead": sessions_dead,
        "decisions_total": decisions_total,
        "decisions_continue": decisions_continue,
        "decisions_halt": decisions_halt,
        "commits_landed": commits_landed,
        "test_counts": test_counts,
        "wasted_minutes": round(wasted_minutes, 1),
        "wasted_sessions": sessions_dead,
        "outcomes_total": outcomes_total,
        "outcomes_productive": outcomes_productive,
        "outcomes_wasted": outcomes_wasted,
    }


def cmd_health(json_output: bool = False) -> None:
    """Print a session health dashboard for today.

    One command to answer: "how's the loop doing?"
    """
    data = session_health_data()

    if json_output:
        print(json.dumps(data, indent=2))
        return

    # ASCII dashboard
    total = data["sessions_total"]
    active = data["sessions_active"]
    dead = data["sessions_dead"]
    pct_active = (active / total * 100) if total else 0

    # Decision stats
    d_total = data["decisions_total"]
    d_cont = data["decisions_continue"]
    d_halt = data["decisions_halt"]

    # Outcome accuracy
    o_total = data["outcomes_total"]
    o_prod = data["outcomes_productive"]
    o_waste = data["outcomes_wasted"]

    today_str = datetime.now().strftime("%Y-%m-%d")

    print(f"=== SESSION HEALTH: {today_str} ===")
    print()
    print(f"  Sessions: {total} total | {active} active ({pct_active:.0f}%) | {dead} dead")
    print(f"  Decisions: {d_total} total | {d_cont} continue | {d_halt} halt")
    print(f"  Commits: {data['commits_landed']} landed")

    if o_total:
        acc = o_prod / o_total * 100
        print(f"  Accuracy: {o_prod}/{o_total} productive ({acc:.0f}%) | {o_waste} wasted cycles")

    if data["wasted_minutes"] > 0:
        print(f"  Time wasted: {data['wasted_minutes']:.0f} min on {data['wasted_sessions']} dead sessions")

    if data["test_counts"]:
        parts = [f"{src}: {cnt}" for src, cnt in data["test_counts"].items()]
        print(f"  Tests: {' | '.join(parts)}")

    # Health verdict
    print()
    if total == 0:
        print("  Verdict: NO DATA -- no sessions today")
    elif pct_active >= 60 and data["commits_landed"] > 0:
        print("  Verdict: HEALTHY -- active loop, commits landing")
    elif pct_active >= 40:
        print("  Verdict: OK -- some activity, check commits")
    elif pct_active > 0:
        print("  Verdict: SLOW -- mostly dead sessions")
    else:
        print("  Verdict: STALLED -- no active sessions")


# ---------------------------------------------------------------------------
# Command: roadmap
# ---------------------------------------------------------------------------

def cmd_roadmap(session_id: Optional[str] = None) -> None:
    """Show roadmap progress for projects detected from a session."""
    if not HAS_ROADMAP_BUILDER:
        print("roadmap_builder not installed. pip install -e ~/zion/projects/roadmap_builder/roadmap_builder/")
        return

    conn = get_conn()
    cur = conn.cursor()

    if not session_id:
        cur.execute("""
            SELECT id FROM sessions
            WHERE message_count > 5 AND source IN ('cli', 'telegram', 'whatsapp')
            ORDER BY started_at DESC LIMIT 1
        """)
        row = cur.fetchone()
        session_id = row[0] if row else None

    if not session_id:
        print("No session found.")
        conn.close()
        return

    # Extract project dirs from session
    cur.execute("""
        SELECT content FROM messages WHERE session_id = ? AND role IN ('user', 'assistant', 'tool')
    """, (session_id,))
    all_text = " ".join(r[0] or "" for r in cur.fetchall())
    conn.close()

    paths = set(re.findall(r"/home/jericho/[a-zA-Z0-9_/.-]+\.[a-z]{1,4}", all_text))
    dirs = sorted(set(p.rsplit("/", 1)[0] for p in paths))[:10]

    roadmaps = scan_project_roadmaps(dirs)
    if not roadmaps:
        print("No roadmaps found for tracked projects.")
        print(f"  Scanned: {', '.join(dirs[:5])}")
        return

    print("=== ROADMAP PROGRESS ===")
    print()
    for r in roadmaps:
        bar_len = 30
        filled = int(bar_len * r["deliverables_done"] / max(r["deliverables_total"], 1))
        bar = "#" * filled + "-" * (bar_len - filled)
        print(f"  {r['title']}")
        print(f"  [{bar}] {r['progress_pct']}%")
        print(f"  Phases: {r['phases_complete']}/{r['phases_total']} complete | "
              f"Deliverables: {r['deliverables_done']}/{r['deliverables_total']} done")
        print(f"  File: {r['roadmap_file']}")

        cp = r.get("current_phase")
        if cp:
            print(f"  Current: {cp['id']} -- {cp['title']} [{cp['status']}]")
            if cp.get("goal"):
                print(f"  Goal: {cp['goal'][:150]}")

        nd = r.get("next_deliverables", [])
        if nd:
            print("  Next deliverables:")
            for d in nd:
                status_mark = "~" if d["status"] == "in_progress" else " "
                print(f"    [{status_mark}] {d['name']}: {d['description'][:80]}")
        print()


# ---------------------------------------------------------------------------
# Main (CLI router)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Library API (for import by session_chain and other tools)
# ---------------------------------------------------------------------------

def get_context_data(session_id: Optional[str] = None, include_cron: bool = False) -> Dict[str, Any]:
    """
    Programmatic version of cmd_context(). Returns a dict with:
      session_id, title, what_requested, last_user_msg,
      summary (dict with completed/errors/next/key_facts),
      projects (list of git states),
      chain_depth, thrashing, thrash_details,
      learned_insights, blockers
    """
    conn = get_conn()
    cur = conn.cursor()

    if not session_id:
        sources = "('cli', 'telegram', 'whatsapp')" if not include_cron else "('cli', 'telegram', 'whatsapp', 'cron')"
        cur.execute(f"""
            SELECT id, message_count, title, parent_session_id, started_at
            FROM sessions
            WHERE message_count > 5 AND source IN {sources}
            ORDER BY started_at DESC LIMIT 1
        """)
        row = cur.fetchone()
        if not row:
            conn.close()
            return {"error": "no sessions found"}
        session_id = row[0]

    cur.execute("""
        SELECT id, message_count, title, parent_session_id, started_at
        FROM sessions WHERE id = ?
    """, (session_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return {"error": f"session {session_id} not found"}

    sid, msg_count, title, parent_id, started_at = row

    cur.execute("""
        SELECT role, content FROM messages
        WHERE session_id = ? AND role IN ('user', 'assistant')
        ORDER BY timestamp ASC
    """, (session_id,))
    rows = cur.fetchall()
    user_msgs = [r[1] for r in rows if r[0] == "user"]
    asst_msgs = [r[1] for r in rows if r[0] == "assistant"]
    conn.close()

    # v4: Primary handoff -- raw last assistant messages (agents interpret, not regex)
    last_asst_raw = get_last_assistant_messages(sid, count=3, max_chars=2000) if asst_msgs else []

    # Supplemental: regex fallback for structured markers
    summary = {"completed": [], "in_progress": [], "next": [], "errors": [], "key_facts": []}
    for msg in asst_msgs:
        prog = _extract_progress_fallback(msg)
        for key in summary:
            summary[key].extend(prog[key])
    for key in summary:
        seen = set()
        deduped = []
        for item in summary[key]:
            if item not in seen:
                seen.add(item)
                deduped.append(item)
        summary[key] = deduped[:10]

    # Project detection + git state
    all_text = " ".join(r[1] or "" for r in rows)
    paths = set(re.findall(r"/home/jericho/[a-zA-Z0-9_/.-]+\.[a-z]{1,4}", all_text))
    dirs = sorted(set(p.rsplit("/", 1)[0] for p in paths))[:10]
    projects = []
    for d in dirs:
        gs = git_status(d)
        if not gs.get("error") and gs.get("git_root"):
            projects.append(gs)

    # Chain depth
    chain_depth = 0
    current = parent_id
    visited = set()
    while current and current not in visited and chain_depth < 25:
        visited.add(current)
        conn2 = get_conn()
        r = conn2.execute("SELECT parent_session_id FROM sessions WHERE id = ?", (current,)).fetchone()
        conn2.close()
        if not r:
            break
        chain_depth += 1
        current = r[0]

    # Thrash detection
    thrashing, dead_count, _, thrash_details = detect_thrash(session_id)

    # Blockers
    carry_conn = get_carry_conn()
    blockers = [(r[0], r[1]) for r in carry_conn.execute(
        "SELECT message, created_at FROM blockers WHERE resolved_at IS NULL"
    ).fetchall()]
    carry_conn.close()

    # Roadmap data (if available)
    roadmap_data = []
    if HAS_ROADMAP_BUILDER:
        roadmap_data = scan_project_roadmaps(
            [p.get("git_root", "") for p in projects if p.get("git_root")]
        )

    return {
        "session_id": sid,
        "title": title,
        "what_requested": (user_msgs[0] or "")[:500] if user_msgs else None,
        "last_user_msg": (user_msgs[-1] or "")[:500] if user_msgs and len(user_msgs) > 1 else None,
        "last_assistant_raw": last_asst_raw,
        "summary": summary,
        "projects": [{
            "root": p.get("git_root"),
            "branch": p.get("branch"),
            "last_commits": p.get("last_commits", [])[:3],
            "dirty": p.get("dirty", False),
        } for p in projects],
        "chain_depth": chain_depth,
        "thrashing": thrashing,
        "thrash_details": thrash_details,
        "blockers": [{"message": m, "created": ts, "age_hours": (time.time() - ts) / 3600 if ts else None} for m, ts in blockers],
        "roadmaps": roadmap_data,
        "learned_lessons": get_top_lessons(n=3),
        "technical_patterns": get_top_technical_patterns(n=3),
        "suggested_next": suggest_next(),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "last":
        depth = 3
        if "--depth" in sys.argv and sys.argv.index("--depth") + 1 < len(sys.argv):
            depth = int(sys.argv[sys.argv.index("--depth") + 1])
        include_cron = "--include-cron" in sys.argv
        cmd_last(depth, include_cron)
    elif cmd == "messages":
        if len(sys.argv) < 3:
            print("Usage: carry_forward.py messages SESSION_ID [--last N]")
            sys.exit(1)
        sid = sys.argv[2]
        last_n = 20
        if "--last" in sys.argv and sys.argv.index("--last") + 1 < len(sys.argv):
            last_n = int(sys.argv[sys.argv.index("--last") + 1])
        cmd_messages(sid, last_n)
    elif cmd == "context":
        include_cron = "--include-cron" in sys.argv
        cmd_context(include_cron)
    elif cmd == "status":
        sid = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else None
        cmd_status(sid)
    elif cmd == "summary":
        sid = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else None
        cmd_summary(sid)
    elif cmd == "last-id":
        cmd_last_id("--include-cron" in sys.argv)
    elif cmd == "chain":
        sid = sys.argv[2] if len(sys.argv) > 2 else None
        cmd_chain(sid)
    elif cmd == "blockers":
        cmd_blockers()
    elif cmd == "block":
        if len(sys.argv) < 3:
            print("Usage: carry_forward.py block <message>")
            sys.exit(1)
        cmd_block(" ".join(sys.argv[2:]))
    elif cmd == "unblock":
        if len(sys.argv) < 3:
            print("Usage: carry_forward.py unblock <pattern>")
            sys.exit(1)
        cmd_unblock(" ".join(sys.argv[2:]))
    elif cmd == "run":
        sid = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else None
        json_out = "--json" in sys.argv
        cmd_run(sid, json_out)
    elif cmd == "should-continue":
        sid = sys.argv[2] if len(sys.argv) > 2 else None
        cmd_should_continue(sid)
    elif cmd == "check-can-continue":
        sid = sys.argv[2] if len(sys.argv) > 2 else None
        result = check_can_continue(sid)
        print(json.dumps(result, indent=2))
    elif cmd == "record-git-heads":
        if len(sys.argv) < 3:
            print("Usage: carry_forward.py record-git-heads SESSION_ID")
            sys.exit(1)
        n = record_git_heads(sys.argv[2])
        print(f"Recorded git HEADs for {n} projects in session {sys.argv[2]}")

    elif cmd == "record-outcome":
        sid = sys.argv[2] if len(sys.argv) > 2 else None
        result = record_outcome(sid)
        print(json.dumps(result, indent=2))

    elif cmd == "calibrate":
        dry_run = "--dry-run" in sys.argv
        project = None
        if "--project" in sys.argv:
            idx = sys.argv.index("--project")
            if idx + 1 < len(sys.argv):
                project = sys.argv[idx + 1]
        if project:
            result = calibrate_project_thresholds(project, dry_run=dry_run)
            print(f"Project: {result.get('project_dir', project)}")
            print(f"Type: {result.get('project_type', 'unknown')}")
            if "message" in result.get("summary", {}):
                print(result["summary"]["message"])
            elif result.get("changes"):
                print(f"\nThreshold changes ({'dry run' if dry_run else 'applied'}):")
                for c in result["changes"]:
                    print(f"  {c['key']}: {c['old']} -> {c['new']} ({c['reason']})")
            else:
                print("\nNo project threshold changes needed.")
        else:
            cmd_calibrate(dry_run)

    elif cmd == "show-config":
        project = None
        if "--project" in sys.argv:
            idx = sys.argv.index("--project")
            if idx + 1 < len(sys.argv):
                project = sys.argv[idx + 1]
        cmd_show_config(project)

    elif cmd == "learn":
        cmd_learn()

    elif cmd == "detect-test-command":
        if len(sys.argv) < 3:
            print("Usage: carry_forward.py detect-test-command <project_dir>")
            sys.exit(1)
        result = detect_test_command(sys.argv[2])
        if result:
            print(result)
        else:
            print("No test command detected for this project.")
            sys.exit(1)

    elif cmd == "health":
        cmd_health(json_output="--json" in sys.argv)

    elif cmd == "analyze-patterns":
        project = None
        if "--project" in sys.argv:
            idx = sys.argv.index("--project")
            if idx + 1 < len(sys.argv):
                project = sys.argv[idx + 1]
        elif len(sys.argv) > 2 and not sys.argv[2].startswith("--"):
            project = sys.argv[2]
        cmd_analyze_patterns(project)

    elif cmd == "roadmap":
        sid = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else None
        cmd_roadmap(sid)

    elif cmd == "suggest-next":
        project = None
        if len(sys.argv) > 2 and not sys.argv[2].startswith("--"):
            project = sys.argv[2]
        cmd_suggest_next(project)

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)