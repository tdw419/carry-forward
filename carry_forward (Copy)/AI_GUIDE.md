# CARRY FORWARD -- AI Agent Guide

This document teaches AI agents how to continue work across Hermes sessions.

You may have been told to "load the carry-forward skill" or "read this document to continue previous work." This is that document. Read it top to bottom, then follow the instructions.

---

## What Is This

Hermes sessions are single conversations. When a session ends, everything in it is gone. Carry Forward reads the database of past conversations to figure out what was happening, then picks up where the last session left off.

The database is at `~/.hermes/state.db` (SQLite). Hermes writes every message to it automatically. You do not need to do anything to maintain it.

There is a helper script that reads this database for you. You do not need to write SQL.

---

## Step 1: Get Oriented

Run this command immediately:

```
python3 /home/jericho/zion/projects/carry_forward/carry_forward/carry_forward.py context
```

This prints four sections:

```
SESSION: 20260410_073701_503479        <-- the session you're continuing
TITLE: (untitled)                      <-- what Hermes knew about it

=== WHAT WAS REQUESTED ===             <-- first user message (the original task)

=== LAST USER MESSAGE ===              <-- the most recent user instruction

=== WHERE THINGS LEFT OFF ===          <-- the last thing the assistant said

=== PROJECT DIRECTORIES ===            <-- file paths detected from the conversation
  /home/jericho/zion/projects/geometry-os/src
```

From this output, determine:
- What project you're working on (from PROJECT DIRECTORIES)
- What the user asked for (from WHAT WAS REQUESTED)
- What was still in progress (from WHERE THINGS LEFT OFF)

---

## Step 2: Get More Detail If Needed

If the `context` output doesn't give you enough to continue confidently, get more history.

**List recent sessions:**
```
python3 /home/jericho/zion/projects/carry_forward/carry_forward/carry_forward.py last
```
Output: one line per session with ID, source, message count, and title.

**Read messages from a specific session:**
```
python3 /home/jericho/zion/projects/carry_forward/carry_forward/carry_forward.py messages SESSION_ID --last 10
```
Replace SESSION_ID with a real ID from the `last` output. Prints the last 10 user/assistant messages in chronological order.

**Get just the last session ID (for scripting):**
```
python3 /home/jericho/zion/projects/carry_forward/carry_forward/carry_forward.py last-id
```

---

## Step 3: Continue The Work

You now have enough context. Do the work. Rules:

1. **Do not repeat completed work.** If the last session committed code and the tests passed, move on to the next thing.
2. **Pick up at the exact stopping point.** The WHERE THINGS LEFT OFF section tells you where to resume.
3. **Use the same project directory.** The PROJECT DIRECTORIES section tells you where the code lives.
4. **Check git status first.** Run `git log --oneline -5` in the project directory to see what was committed. This confirms what's actually done vs. what was just discussed.

---

## Step 4: Carry Forward (Optional)

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
- You are blocked and need human input
- You are unsure what to do next
- This is continuation session 10+ in a chain (risk of thrashing)

---

## Step 5: Know When To Stop

Stop and do NOT schedule another session if any of these are true:

- **No clear next step.** You finished the task and there is nothing obviously next.
- **Stuck.** You tried something twice and it didn't work. A human needs to look at it.
- **Needs a decision.** The work requires an architectural or design choice that a human should make.
- **No progress.** Compare your session to the previous one. If the same thing failed again, stop.
- **Depth limit.** If the prompt mentioned "continuation N of M" and you are at M, stop.

When you stop, print a clear summary of what you accomplished and what still needs to happen. A human will read it.

---

## Command Reference

All commands use the helper script at:
```
/home/jericho/zion/projects/carry_forward/carry_forward/carry_forward.py
```

| Command | Flags | Output |
|---------|-------|--------|
| `context` | `--include-cron` | Full context from last session |
| `last` | `--depth N` (default 3) | List of recent sessions |
| `messages SESSION_ID` | `--last N` (default 20) | Messages from one session |
| `last-id` | (none) | Just the session ID |

---

## Database Details

If you ever need to query the database directly (the script doesn't cover your use case):

- Location: `/home/jericho/.hermes/state.db`
- Type: SQLite
- Main tables: `sessions`, `messages`
- Sessions have: `id`, `source` (cli/telegram/whatsapp/cron), `message_count`, `title`
- Messages have: `session_id`, `role` (user/assistant/tool/system), `content`, `timestamp`
- The helper script filters to `source IN ('cli', 'telegram', 'whatsapp')` and `message_count > 5` to skip trivial sessions. Use `--include-cron` to include cron sessions too.

---

## Troubleshooting

**"No sessions with real content found"**
The database has no sessions matching the filters. Either the database is empty, or all sessions had fewer than 5 messages. Try `--include-cron`.

**The context output shows the wrong session**
The script picks the most recent session with >5 messages from cli/telegram/whatsapp. If you want a different session, use `last` to find it, then `messages SESSION_ID` to read it.

**The work described doesn't match reality**
The conversation history might discuss plans that weren't executed. Always check `git log` in the project directory to see what was actually committed. Git history is more reliable than conversation summaries.

---

## File Locations

```
Helper script:  /home/jericho/zion/projects/carry_forward/carry_forward/carry_forward.py
This document:  /home/jericho/zion/projects/carry_forward/carry_forward/AI_GUIDE.md
Project README: /home/jericho/zion/projects/carry_forward/carry_forward/README.md
Session DB:     ~/.hermes/state.db
```
