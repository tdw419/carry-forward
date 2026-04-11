#!/usr/bin/env python3
"""
Carry Forward v5 - self-tuning intelligence layer for Hermes session continuity.
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
    carry_forward.py calibrate                    # Auto-tune thresholds from decision history
    carry_forward.py show-config                  # Show current threshold values
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

DB_PATH = "/home/jericho/.hermes/state.db"
CARRY_DB_PATH = os.path.expanduser("~/.hermes/carry_forward.db")

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

# Decision thresholds (simple constants -- no calibration)
DEAD_SESSION_THRESHOLD = 3
DEAD_LOOKBACK = 5
ORPHAN_CHILD_THRESHOLD = 10
BLOCKER_HALT_HOURS = 4.0
GIT_MIN_SESSIONS = 3

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
        CREATE INDEX IF NOT EXISTS idx_chain_git_session
        ON chain_git_heads(session_id)
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
        min_sessions = GIT_MIN_SESSIONS
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
        lookback = DEAD_LOOKBACK
    dead_thresh = DEAD_SESSION_THRESHOLD
    orphan_thresh = ORPHAN_CHILD_THRESHOLD
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

        Final: can_continue = not thrashing AND not blocker_halt
               AND not session_dead AND not (parent_dead AND own_tools==0)
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
    """, (now - (BLOCKER_HALT_HOURS * 3600),)).fetchall()
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

    # Final decision: continue only if ALL halt checks pass.
    # Exception: if the current session itself has tool calls, it's productive
    # regardless of parent state -- don't kill a live session for a dead parent.
    can_continue = (not thrashing and not blocker_halt and not session_dead
                    and not (parent_dead and own_tools == 0))

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
    }


def cmd_should_continue(session_id: Optional[str] = None) -> None:
    """
    Exit code interface for cron scripting.
    0 = safe to chain, 1 = thrashing / blockers / should stop.
    Also prints human-readable reasoning.
    """
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
    paths = set(re.findall(r"/home/jericho/[a-zA-Z0-9_/.-]+\.[a-z]{1,4}", all_text))
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


# ---------------------------------------------------------------------------
# Main
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


    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)