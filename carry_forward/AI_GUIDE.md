# CARRY FORWARD v2 -- AI Agent Guide

This document teaches AI agents how to continue work across Hermes sessions.

You may have been told to "load the carry-forward skill" or "read this document to continue previous work." This is that document. Read it top to bottom, then follow the instructions.

---

## What Is This

Hermes sessions are single conversations. When a session ends, everything in it is gone. Carry Forward reads the database of past conversations to figure out what was happening, then picks up where the last session left off.

**v2 adds:** git-aware project state, smart progress extraction, session chain tracking, and persistent blockers -- all in one `context` call.

The database is at `~/.hermes/state.db` (SQLite). Hermes writes every message to it automatically.
Carry Forward has its own metadata DB at `~/.hermes/carry_forward.db` for blockers and chain metadata.

---

## Step 1: Get Oriented

Run this command immediately:

```
python3 /home/jericho/zion/projects/carry_forward/carry_forward/carry_forward.py context
```

This prints everything you need:

```
SESSION: 20260410_085841_d10eaf
TITLE: Building a Self-Hosting Pixel Text Programming System
STARTED: 2026-04-10 08:58

=== WHAT WAS REQUESTED ===             <-- first user message (the original task)

=== SESSION SUMMARY ===                <-- smart extraction of completed/errors/next
  KEY FACTS:
    20/20 assembler tests pass including the 5 new ones
    11/11 tests pass, clean build, commit is real
  COMPLETED:
    [x] VM-resident micro-assembler
    [x] BEQ/BNE/BLT/BGE/BLTU/BGEU branch aliases
  NEXT / REMAINING:
    [ ] Fill in more opcodes
    [ ] Self-hosting assembler

=== PROJECT STATUS ===                 <-- git state for detected projects
  /home/jericho/zion/projects/geometry_os (branch: master)
    41d32bc feat: VM-resident micro-assembler
    e89ba71 feat: F9 inline code editor
    DIRTY: 1 uncommitted changes
      M src/vm.rs

=== CHAIN DEPTH: 2 continuation(s) === <-- how deep the carry-forward chain is

=== UNRESOLVED BLOCKERS ===            <-- any blockers recorded by previous sessions
  [2026-04-10 08:00] Need decision on opcode format
```

From this output, determine:
- What project you're working on (from PROJECT STATUS)
- What the user asked for (from WHAT WAS REQUESTED)
- What was completed (from SESSION SUMMARY)
- What's still in progress (from NEXT / REMAINING)
- What's blocking progress (from UNRESOLVED BLOCKERS)

---

## Step 2: Get More Detail If Needed

If the `context` output doesn't give you enough to continue confidently:

**Deep summary of a specific session:**
```
python3 .../carry_forward.py summary SESSION_ID
```

**Git status for a specific session's projects:**
```
python3 .../carry_forward.py status SESSION_ID
```

**Trace the full continuation chain:**
```
python3 .../carry_forward.py chain [SESSION_ID]
```
Shows all parent/child sessions in order. Warns if chain depth >= 10.

**Read raw messages from a session:**
```
python3 .../carry_forward.py messages SESSION_ID --last 10
```

**List recent sessions:**
```
python3 .../carry_forward.py last --depth 5
```

---

## Step 3: Continue The Work

You now have enough context. Do the work. Rules:

1. **Do not repeat completed work.** The SESSION SUMMARY shows what's done. The PROJECT STATUS shows what's committed. Trust git over conversation claims.
2. **Pick up at the exact stopping point.** The NEXT / REMAINING section tells you where to resume.
3. **Respect blockers.** If there are unresolved blockers, don't barrel past them. They were recorded for a reason.
4. **Use the same project directory.** The PROJECT STATUS section tells you where the code lives and what branch it's on.
5. **Check dirty state.** If PROJECT STATUS shows dirty files, the last session may have left uncommitted work. Investigate before overwriting.

---

## Step 4: Record Blockers (If Any)

If you hit something that needs human input, record it so the next session knows:

```
python3 .../carry_forward.py block "need Jericho to decide on the API contract"
```

Next session's `context` output will show this blocker automatically.

When resolved:
```
python3 .../carry_forward.py unblock "API contract"
```

---

## Step 5: Carry Forward (Optional)

If there is clearly more work to do and you want the NEXT session to continue after this one, schedule a one-shot cron job:

```
cronjob(
  action='create',
  name='carry-forward',
  schedule='1m',
  repeat=1,
  prompt='Read the file at /home/jericho/zion/projects/carry_forward/carry_forward/AI_GUIDE.md and follow its instructions. Then continue the work.',
  skills=['carry-forward']
)
```

This fires a new Hermes session 1 minute after this one ends. The new session will read this same document, run the context script, and pick up.

**Do NOT carry forward if:**
- The work is done
- You are blocked and need human input (record a blocker instead)
- You are unsure what to do next
- Chain depth is >= 10 (check `context` output -- it warns you)

---

## Step 6: Know When To Stop

Stop and do NOT schedule another session if any of these are true:

- **No clear next step.** You finished the task and there is nothing obviously next.
- **Stuck.** You tried something twice and it didn't work. Record a blocker, stop.
- **Needs a decision.** The work requires an architectural or design choice that a human should make. Record it as a blocker.
- **No progress.** Compare the current state (git log) to what the previous session reported. If nothing new was committed, stop.
- **Chain too deep.** If the `context` output warns about chain depth, stop.

When you stop, print a clear summary of what you accomplished and what still needs to happen. A human will read it.

---

## Command Reference

All commands use the helper script at:
```
/home/jericho/zion/projects/carry_forward/carry_forward/carry_forward.py
```

| Command | Flags | Output |
|---------|-------|--------|
| `context` | `--include-cron` | Full context: summary + git state + chain + blockers |
| `status` | `[SESSION_ID]` | Git state for all projects detected in session |
| `summary` | `[SESSION_ID]` | Smart progress extraction (completed/errors/next) |
| `chain` | `[SESSION_ID]` | Parent/child continuation chain with depth warning |
| `last` | `--depth N`, `--include-cron` | List of recent sessions |
| `messages SESSION_ID` | `--last N` | Messages from one session |
| `last-id` | `--include-cron` | Just the session ID |
| `block MSG` | (none) | Record a new blocker |
| `blockers` | (none) | Show unresolved blockers |
| `unblock PATTERN` | (none) | Resolve blockers matching pattern |

If SESSION_ID is omitted, commands default to the most recent non-trivial session.

---

## Database Details

**Hermes state DB:** `/home/jericho/.hermes/state.db` (SQLite)
- Tables: `sessions`, `messages`
- Sessions: `id`, `source` (cli/telegram/whatsapp/cron), `parent_session_id`, `message_count`, `title`, timestamps
- Messages: `session_id`, `role` (user/assistant/tool/system), `content`, `tool_calls`, timestamps
- Full-text search index on messages via `messages_fts`

**Carry Forward metadata DB:** `~/.hermes/carry_forward.db` (SQLite, auto-created)
- Tables: `blockers` (persistent blockers), `chain_meta` (session chain tracking)
- Blockers: `session_id`, `message`, `created_at`, `resolved_at`
- Chain meta: `session_id`, `parent_session_id`, `continuation_count`, `outcome`, `project_dir`

---

## Troubleshooting

**"No sessions with real content found"**
The database has no sessions matching the filters. Either the database is empty, or all sessions had fewer than 5 messages. Try `--include-cron`.

**The context output shows the wrong session**
The script picks the most recent session with >5 messages from cli/telegram/whatsapp. If you want a different session, use `last` to find it, then `summary SESSION_ID` or `messages SESSION_ID` to read it.

**No git status showing**
The script detects file paths from the conversation and checks if they're in git repos. If no files were mentioned, or the paths aren't in git repos, git status won't appear. Use `status SESSION_ID` with a specific session that had file operations.

**The work described doesn't match reality**
The SESSION SUMMARY extracts from conversation text. Git history (shown in PROJECT STATUS) is more reliable. Always trust commits over conversation claims.

---

## File Locations

```
Helper script:   /home/jericho/zion/projects/carry_forward/carry_forward/carry_forward.py
This document:   /home/jericho/zion/projects/carry_forward/carry_forward/AI_GUIDE.md
Project README:  /home/jericho/zion/projects/carry_forward/carry_forward/README.md
Session DB:      ~/.hermes/state.db
Metadata DB:     ~/.hermes/carry_forward.db
```

---

## What Changed in v2

If you used carry_forward before, here's what's new:
- `context` now shows git state, smart summary, chain depth, and blockers (not just raw text)
- `status` command cross-references sessions with actual git history
- `summary` command extracts structured progress (completed/errors/next) instead of truncating
- `chain` command traces the full continuation chain with depth warnings
- `block/unblock/blockers` commands for persistent blockers that survive across sessions
- `carry_forward.db` for carry_forward's own metadata (separate from Hermes state.db)
