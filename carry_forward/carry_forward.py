#!/usr/bin/env python3
"""
Carry Forward v4 - intelligence layer for Hermes session continuity.
Transport, not comprehension. Packages what happened; agents do the interpretation.

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
"""
import sqlite3
import subprocess
import re
import sys
import os
import json
import time
from datetime import datetime

DB_PATH = "/home/jericho/.hermes/state.db"
CARRY_DB_PATH = os.path.expanduser("~/.hermes/carry_forward.db")

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_conn():
    return sqlite3.connect(DB_PATH)


def get_carry_conn():
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
        CREATE INDEX IF NOT EXISTS idx_patterns_type_key
        ON learned_patterns(pattern_type, pattern_key)
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
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Command: last
# ---------------------------------------------------------------------------

def cmd_last(depth=3, include_cron=False):
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

def cmd_last_id(include_cron=False):
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

def cmd_messages(session_id, last_n=20):
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

def cmd_chain(session_id=None):
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

def record_git_heads(session_id):
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

    import time
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


def check_git_progress(session_id, min_sessions=3):
    """Check if git HEAD has actually moved across the chain.
    Returns (progress_made, details_str).
    This catches 'busy but unproductive' -- sessions that log work but never commit."""
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

def detect_thrash(session_id=None, lookback=5):
    """
    Check if recent sessions in the chain are productive.
    Returns (is_thrashing, dead_count, chain_sessions, details).
    """
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

    # Thrashing if: 3+ of last 5 sessions in chain are dead, OR
    # this session already has 10+ dead children (runaway loop detection)
    is_thrashing = dead_count >= 3 or child_count >= 10

    # v4: Also check git progress -- catches "busy but unproductive"
    git_ok, git_details = check_git_progress(session_id)
    if not git_ok:
        is_thrashing = True
        details += f" | {git_details}"

    return is_thrashing, dead_count, chain_sessions, details


def check_can_continue(session_id=None):
    """
    Logic core for continuation decisions. Returns dict:
      {can_continue, reasons[], thrashing, dead_count, git_progress, blocker_halt, guard_rails}
    """
    thrashing, dead_count, chain_sessions, details = detect_thrash(session_id)
    git_ok, git_details = check_git_progress(session_id or "")

    reasons = []
    guard_rails = []

    # Check 1: Dead session thrash
    if thrashing:
        if dead_count >= 3:
            reasons.append(f"Thrash: {dead_count} dead sessions in recent chain ({details})")
        if not git_ok:
            reasons.append(f"Git stalled: {git_details}")

    # Check 2: Learned pattern guard rails
    carry_conn = get_carry_conn()

    # 2a: Continuation rates by source -- if current source has <20% rate, warn
    conn = get_conn()
    if session_id:
        src = conn.execute("SELECT source FROM sessions WHERE id = ?", (session_id,)).fetchone()
        source = src[0] if src else None
    else:
        src = conn.execute("""
            SELECT source FROM sessions
            WHERE message_count > 5 AND source IN ('cli', 'telegram', 'whatsapp')
            ORDER BY started_at DESC LIMIT 1
        """).fetchone()
        source = src[0] if src else None
    conn.close()

    if source:
        rate_row = carry_conn.execute("""
            SELECT observation FROM learned_patterns
            WHERE pattern_type = 'continuation_rate' AND pattern_key = ?
            ORDER BY last_seen DESC LIMIT 1
        """, (source,)).fetchone()
        if rate_row:
            # Parse "XX.X% productive" from observation
            m = re.match(r'([\d.]+)%\s+productive', rate_row[0])
            if m and float(m.group(1)) < 15:
                guard_rails.append(f"Low continuation rate for {source}: {rate_row[0]}")
                reasons.append(f"Source {source} has historically low continuation success")

    # 2b: Session size warning -- if parent session was massive, continuations tend to die
    conn = get_conn()
    if session_id:
        parent = conn.execute("SELECT parent_session_id FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if parent and parent[0]:
            parent_msgs = conn.execute("SELECT message_count FROM sessions WHERE id = ?", (parent[0],)).fetchone()
            if parent_msgs and parent_msgs[0] > 200:
                size_row = carry_conn.execute("""
                    SELECT observation FROM learned_patterns
                    WHERE pattern_type = 'size_success' AND pattern_key = 'massive'
                    ORDER BY last_seen DESC LIMIT 1
                """).fetchone()
                if size_row:
                    guard_rails.append(f"Large parent session: {size_row[0]}")
    conn.close()

    # Check 3: Blocker age threshold
    blocker_halt = False
    BLOCKER_HALT_HOURS = 4
    import time
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

    can_continue = not thrashing and not blocker_halt

    return {
        "can_continue": can_continue,
        "reasons": reasons,
        "guard_rails": guard_rails,
        "thrashing": thrashing,
        "dead_count": dead_count,
        "git_progress": git_details,
        "blocker_halt": blocker_halt,
        "thrash_details": details,
    }


def cmd_should_continue(session_id=None):
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


# ---------------------------------------------------------------------------
# Command: learn
# ---------------------------------------------------------------------------

def cmd_learn():
    """
    Analyze the full session history and record patterns about what works.
    Stores findings in carry_forward.db learned_patterns table.
    """
    import time
    now = time.time()
    conn = get_conn()
    cur = conn.cursor()

    carry_conn = get_carry_conn()

    # --- Pattern 1: Continuation success rate by source ---
    print("Analyzing continuation success rates...")
    cur.execute("""
        SELECT s.source,
               COUNT(*) as total,
               SUM(CASE WHEN s.tool_call_count > 0 THEN 1 ELSE 0 END) as productive,
               SUM(CASE WHEN s.tool_call_count = 0 AND s.message_count = 0 THEN 1 ELSE 0 END) as dead
        FROM sessions s
        WHERE s.parent_session_id IS NOT NULL
        GROUP BY s.source
    """)
    for source, total, productive, dead in cur.fetchall():
        productive = productive or 0
        dead = dead or 0
        rate = productive / total * 100 if total > 0 else 0
        obs = f"{rate:.1f}% productive ({productive}/{total}), {dead} dead"
        carry_conn.execute("""
            INSERT INTO learned_patterns (pattern_type, pattern_key, observation, sample_size, last_seen, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("continuation_rate", source, obs, total, now, now))

    # --- Pattern 2: Session chains that went too deep ---
    print("Analyzing deep chains...")
    # Find root sessions that spawned many continuations
    cur.execute("""
        SELECT parent_session_id, COUNT(*) as children,
               SUM(CASE WHEN message_count = 0 AND tool_call_count = 0 THEN 1 ELSE 0 END) as dead_children
        FROM sessions
        WHERE parent_session_id IS NOT NULL
        GROUP BY parent_session_id
        HAVING children > 5
        ORDER BY dead_children DESC
        LIMIT 20
    """)
    runaway_count = 0
    for parent_id, children, dead_children in cur.fetchall():
        dead_children = dead_children or 0
        if dead_children > children * 0.8:
            runaway_count += 1
    if runaway_count > 0:
        carry_conn.execute("""
            INSERT INTO learned_patterns (pattern_type, pattern_key, observation, sample_size, last_seen, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("runaway_chains", "total", f"{runaway_count} sessions spawned runaway chains (>80% dead children)", runaway_count, now, now))

    # --- Pattern 3: What session sizes lead to productive continuations? ---
    print("Analyzing session size vs continuation success...")
    cur.execute("""
        SELECT
            CASE
                WHEN s1.message_count < 20 THEN 'small'
                WHEN s1.message_count < 80 THEN 'medium'
                WHEN s1.message_count < 200 THEN 'large'
                ELSE 'massive'
            END as parent_size,
            COUNT(*) as total,
            SUM(CASE WHEN s2.tool_call_count > 0 THEN 1 ELSE 0 END) as productive
        FROM sessions s1
        JOIN sessions s2 ON s2.parent_session_id = s1.id
        GROUP BY parent_size
    """)
    for size, total, productive in cur.fetchall():
        productive = productive or 0
        rate = productive / total * 100 if total > 0 else 0
        obs = f"Parent sessions of {size} size: {rate:.1f}% of continuations are productive ({productive}/{total})"
        carry_conn.execute("""
            INSERT INTO learned_patterns (pattern_type, pattern_key, observation, sample_size, last_seen, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("size_success", size, obs, total, now, now))

    # --- Pattern 4: Time-of-day productivity ---
    print("Analyzing time-of-day patterns...")
    cur.execute("""
        SELECT
            CAST(strftime('%H', s.started_at, 'unixepoch') AS INTEGER) as hour,
            COUNT(*) as total,
            SUM(CASE WHEN s.tool_call_count > 10 THEN 1 ELSE 0 END) as productive
        FROM sessions s
        WHERE s.message_count > 5
        GROUP BY hour
        ORDER BY productive DESC
    """)
    hours = cur.fetchall()
    if hours:
        best_hours = sorted(hours, key=lambda h: (h[2] or 0) / max(h[1], 1), reverse=True)[:3]
        worst_hours = sorted(hours, key=lambda h: (h[2] or 0) / max(h[1], 1))[:3]
        best_str = ", ".join(f"{h[0]:02d}:00 ({(h[2] or 0)}/{h[1]})" for h in best_hours)
        worst_str = ", ".join(f"{h[0]:02d}:00 ({(h[2] or 0)}/{h[1]})" for h in worst_hours)
        carry_conn.execute("""
            INSERT INTO learned_patterns (pattern_type, pattern_key, observation, sample_size, last_seen, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("productive_hours", "best", best_str, sum(h[1] for h in hours), now, now))
        carry_conn.execute("""
            INSERT INTO learned_patterns (pattern_type, pattern_key, observation, sample_size, last_seen, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("productive_hours", "worst", worst_str, sum(h[1] for h in hours), now, now))

    # --- Pattern 5: Overall continuation stats ---
    print("Computing overall stats...")
    total_sessions = cur.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    substantial = cur.execute("SELECT COUNT(*) FROM sessions WHERE message_count > 5").fetchone()[0]
    with_parent = cur.execute("SELECT COUNT(*) FROM sessions WHERE parent_session_id IS NOT NULL").fetchone()[0]
    dead_continuations = cur.execute("""
        SELECT COUNT(*) FROM sessions
        WHERE parent_session_id IS NOT NULL AND message_count = 0 AND tool_call_count = 0
    """).fetchone()[0]

    carry_conn.execute("""
        INSERT INTO learned_patterns (pattern_type, pattern_key, observation, sample_size, last_seen, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, ("overview", "stats",
          f"{total_sessions} total, {substantial} substantial, {with_parent} continuations, {dead_continuations} dead continuations ({dead_continuations/max(with_parent,1)*100:.1f}%)",
          total_sessions, now, now))

    conn.close()

    # Print summary of what was learned
    print("\n=== LEARNED PATTERNS ===")
    rows = carry_conn.execute("""
        SELECT pattern_type, pattern_key, observation, sample_size
        FROM learned_patterns
        WHERE last_seen = ?
        ORDER BY pattern_type, pattern_key
    """, (now,)).fetchall()
    for ptype, pkey, obs, n in rows:
        print(f"  [{ptype}] {pkey}: {obs} (n={n})")

    carry_conn.commit()
    carry_conn.close()
    print(f"\nRecorded {len(rows)} patterns.")


# ---------------------------------------------------------------------------
# Command: blockers
# ---------------------------------------------------------------------------

def cmd_blockers():
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


def cmd_block(message):
    """Record a new blocker."""
    conn = get_carry_conn()
    import time
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


def cmd_unblock(pattern):
    """Resolve blockers matching a pattern."""
    conn = get_carry_conn()
    import time
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

def git_status(project_dir):
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


def cmd_status(session_id=None):
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

def _extract_progress_fallback(text):
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


def get_last_assistant_messages(session_id, count=3, max_chars=2000):
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


def cmd_summary(session_id=None):
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

def cmd_context(include_cron=False):
    """Full context from last session, with git state, summary, chain, and blockers."""
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

    # Show learned insights relevant to this session
    carry_conn2 = get_carry_conn()
    learnings = carry_conn2.execute("""
        SELECT pattern_type, observation FROM learned_patterns
        WHERE pattern_type IN ('continuation_rate', 'overview', 'runaway_chains')
        ORDER BY last_seen DESC LIMIT 5
    """).fetchall()
    carry_conn2.close()
    if learnings:
        print("=== LEARNED INSIGHTS ===")
        for ptype, obs in learnings:
            print(f"  [{ptype}] {obs}")
        print()

    carry_conn.close()

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

def get_context_data(session_id=None, include_cron=False):
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

    # Learned insights
    carry_conn = get_carry_conn()
    insights = carry_conn.execute("""
        SELECT pattern_type, observation FROM learned_patterns
        WHERE pattern_type IN ('continuation_rate', 'overview', 'runaway_chains')
        ORDER BY last_seen DESC LIMIT 5
    """).fetchall()

    # Blockers
    blockers = carry_conn.execute("""
        SELECT message, created_at FROM blockers WHERE resolved_at IS NULL
        ORDER BY created_at DESC LIMIT 5
    """).fetchall()
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
        "can_continue": check_can_continue(sid),
        "learned_insights": [{"type": t, "observation": o} for t, o in insights],
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
    elif cmd == "learn":
        cmd_learn()
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
