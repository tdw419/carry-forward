#!/usr/bin/env python3
"""
Carry Forward - reads Hermes session history from the SQLite DB.
Usage:
    python3 carry_forward.py last [--depth N]              # Last N non-trivial sessions summary
    python3 carry_forward.py messages SESSION_ID [--last N] # Messages from a session
    python3 carry_forward.py context                        # Auto-extract context from last session
"""
import sqlite3
import re
import sys
import json

DB_PATH = "/home/jericho/.hermes/state.db"


def get_conn():
    return sqlite3.connect(DB_PATH)


def cmd_last(depth=3):
    """Show last N non-trivial sessions."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, source, model, message_count, tool_call_count, title, started_at
        FROM sessions 
        WHERE message_count > 5
        ORDER BY started_at DESC LIMIT ?
    """, (depth,))
    rows = cur.fetchall()
    for r in rows:
        title = (r[5] or "---")[:60]
        print(f"{r[0]} | {r[1]} | msgs={r[3]} tools={r[4]} | {title}")
    conn.close()


def cmd_messages(session_id, last_n=20):
    """Show messages from a specific session."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f"""
        SELECT role, content FROM messages 
        WHERE session_id = ? AND role IN ('user', 'assistant')
        ORDER BY timestamp DESC LIMIT ?
    """, (session_id, last_n))
    rows = cur.fetchall()
    for r in reversed(rows):
        role = r[0]
        content = (r[1] or "(empty)")[:400]
        print(f"[{role}] {content}")
        print()
    conn.close()


def cmd_context(include_cron=False):
    """Auto-extract: what was requested, what's done, what's next."""
    conn = get_conn()
    cur = conn.cursor()

    # Find last non-trivial session
    sources = "('cli', 'telegram', 'whatsapp')" if not include_cron else "('cli', 'telegram', 'whatsapp', 'cron')"
    cur.execute(f"""
        SELECT id, message_count, title 
        FROM sessions 
        WHERE message_count > 5 AND source IN {sources}
        ORDER BY started_at DESC LIMIT 1
    """)
    row = cur.fetchone()
    if not row:
        print("No sessions with real content found.")
        return

    session_id, msg_count, title = row
    print(f"SESSION: {session_id}")
    print(f"TITLE: {title or '(untitled)'}")
    print()

    # Get all user/assistant messages
    cur.execute(f"""
        SELECT role, content FROM messages 
        WHERE session_id = ? AND role IN ('user', 'assistant')
        ORDER BY timestamp ASC
    """, (session_id,))
    rows = cur.fetchall()

    user_msgs = [r[1] for r in rows if r[0] == "user"]
    asst_msgs = [r[1] for r in rows if r[0] == "assistant"]

    if user_msgs:
        print("=== WHAT WAS REQUESTED ===")
        print((user_msgs[0] or "")[:500])
        print()

    if user_msgs and len(user_msgs) > 1:
        print("=== LAST USER MESSAGE ===")
        print((user_msgs[-1] or "")[:500])
        print()

    if asst_msgs:
        print("=== WHERE THINGS LEFT OFF ===")
        print((asst_msgs[-1] or "")[:800])
        print()

    # Detect project paths
    all_text = " ".join(r[1] or "" for r in rows)
    paths = set(re.findall(r"/home/jericho/[a-zA-Z0-9_/.-]+\.[a-z]{1,4}", all_text))
    # Deduplicate to directory roots
    dirs = sorted(set(p.rsplit("/", 1)[0] for p in paths))[:10]
    if dirs:
        print("=== PROJECT DIRECTORIES ===")
        for d in dirs:
            print(f"  {d}")

    conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "last":
        depth = int(sys.argv[3]) if "--depth" in sys.argv and sys.argv.index("--depth") + 1 < len(sys.argv) else 3
        cmd_last(depth)
    elif cmd == "messages":
        if len(sys.argv) < 3:
            print("Usage: replay_reader.py messages SESSION_ID [--last N]")
            sys.exit(1)
        sid = sys.argv[2]
        last_n = 20
        if "--last" in sys.argv and sys.argv.index("--last") + 1 < len(sys.argv):
            last_n = int(sys.argv[sys.argv.index("--last") + 1])
        cmd_messages(sid, last_n)
    elif cmd == "context":
        include_cron = "--include-cron" in sys.argv
        cmd_context(include_cron)
    elif cmd == "last-id":
        # Just print the last session ID (useful for scripting)
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT id FROM sessions 
            WHERE message_count > 5 AND source IN ('cli', 'telegram', 'whatsapp')
            ORDER BY started_at DESC LIMIT 1
        """)
        row = cur.fetchone()
        if row:
            print(row[0])
        conn.close()
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
