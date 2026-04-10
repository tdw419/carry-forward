#!/usr/bin/env python3
"""
Carry Forward v2 - reads Hermes session history from the SQLite DB.
Understands git state, session chains, blockers, and extracts real progress.

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
"""
import sqlite3
import subprocess
import re
import sys
import os
import json
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

def extract_progress(text):
    """Extract structured progress info from assistant messages."""
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


def cmd_summary(session_id=None):
    """Smart summary of session progress."""
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

    # Get all assistant messages (they contain the work record)
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

    # Aggregate progress from ALL assistant messages
    all_progress = {"completed": [], "in_progress": [], "next": [], "errors": [], "key_facts": []}
    for msg in asst_msgs:
        prog = extract_progress(msg)
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
        all_progress[key] = deduped[:15]  # cap each section

    print(f"=== SESSION SUMMARY ({session_id}) ===\n")

    if all_progress["key_facts"]:
        print("KEY FACTS:")
        for f in all_progress["key_facts"][:8]:
            print(f"  - {f}")
        print()

    if all_progress["completed"]:
        print("COMPLETED:")
        for item in all_progress["completed"][:10]:
            print(f"  {item}")
        print()

    if all_progress["errors"]:
        print("ERRORS ENCOUNTERED:")
        for item in all_progress["errors"][:8]:
            print(f"  {item}")
        print()

    if all_progress["in_progress"]:
        print("IN PROGRESS (last reported):")
        for item in all_progress["in_progress"][-5:]:
            print(f"  {item}")
        print()

    if all_progress["next"]:
        print("NEXT / REMAINING:")
        for item in all_progress["next"][:10]:
            print(f"  {item}")
        print()

    # If structured extraction found nothing, fall back to last assistant message tail
    if not any(all_progress.values()):
        print("(No structured progress found. Last assistant message:)")
        last = asst_msgs[-1][-800:]
        print(last)


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

    # Smart summary instead of raw truncation
    if asst_msgs:
        print("=== SESSION SUMMARY ===")
        # Aggregate progress
        all_progress = {"completed": [], "in_progress": [], "next": [], "errors": [], "key_facts": []}
        for msg in asst_msgs:
            prog = extract_progress(msg)
            for key in all_progress:
                all_progress[key].extend(prog[key])

        found_structured = False
        for key in ["key_facts", "completed", "errors", "in_progress", "next"]:
            items = all_progress[key]
            if items:
                # Deduplicate
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
            # Fallback: last assistant message, last 600 chars
            print("  (No structured markers found. Last message tail:)")
            print(f"  {(asst_msgs[-1] or '')[-600:]}")
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
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
