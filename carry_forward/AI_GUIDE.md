# Carry Forward AI Guide

Everything an AI agent needs to understand, use, improve, and test carry_forward.
Zero prior context required.

## What Carry Forward Is

A decision engine for session continuation. When an autonomous loop wants to know
"should I spawn another session to keep working?", carry_forward answers yes or no
based on evidence: chain health, git progress, blocker state, and session activity.

It is NOT a task manager, context summarizer, or workflow orchestrator. It makes
one decision (continue or halt) and logs everything so that decision can be audited
and improved.

## The Feedback Loop (How It Improves Itself)

```
1. check_can_continue() runs
   -> logs decision to decision_log (continue/halt, reasons, thresholds used)

2. The session plays out (or doesn't)

3. record_outcome checks what actually happened
   -> logs to decision_outcomes (was it productive? did git move? tool call count?)

4. calibrate sweeps threshold values against decision+outcome pairs
   -> writes optimal thresholds to config table

5. replay_harness.py measures overall accuracy (precision, recall, F1)
   -> tells you if a change helped or hurt
```

This is the legitimate recursive improvement loop: not self-modifying code, but
self-tuning behavior from evidence. Every decision is logged, every outcome is
tracked, every threshold can be calibrated from data.

## The Decision Pipeline

When `check_can_continue(session_id)` is called, these checks run in order:

### Check 1: Dead Session Thrash
Walks the parent chain backwards. Counts how many recent sessions had 0 messages
and 0 tool calls. If dead_count >= dead_session_threshold (default 3) out of the
last dead_lookback (default 5) sessions, the chain is thrashing. Halt.

### Check 2: Git Progress
Compares git HEAD snapshots from the start and end of the chain. If nothing moved
across git_min_sessions (default 3) sessions, the chain is busy but unproductive.
Halt. (This is nested inside thrash detection -- git stall sets thrashing=True.)

### Check 3: Learned Patterns
Checks the learned_patterns table for:
- Source continuation rates (e.g., "cli sessions: 12% productive"). Below
  continuation_rate_min (default 15%) triggers a warning.
- Parent session size warnings (massive parents tend to produce dead continuations).

These are warnings/guard_rails, not hard halts. They appear in the decision output
but don't block continuation on their own.

### Check 4: Blocker Age
Persistent blockers with a timestamp older than blocker_halt_hours (default 4h)
trigger a hard halt. The idea: if something is blocked and nobody has fixed it in
hours, continuing will just waste cycles.

### Check 5: Session Activity (v5.1)
Checks whether the session being evaluated has actually done anything. If it has
0 tool calls AND <=2 messages, it's dead. Don't spawn continuations from a dead
session. This was the #1 fix from replay analysis -- 92% of "continue" decisions
were for empty sessions.

### Decision
```
can_continue = NOT thrashing AND NOT blocker_halt AND NOT session_dead
```

## Commands

### Reading State

```bash
# Full context: what happened, what's next, can we continue?
python3 ~/zion/projects/carry_forward/carry_forward/carry_forward.py context

# JSON output of the full decision for programmatic use
python3 ~/zion/projects/carry_forward/carry_forward/carry_forward.py check-can-continue [SESSION_ID]

# Exit code interface (0=safe, 1=halt) for cron/loop scripts
python3 ~/zion/projects/carry_forward/carry_forward/carry_forward.py should-continue
```

### Recording

```bash
# Snapshot git HEADs at the start of a session (for progress tracking)
python3 ~/zion/projects/carry_forward/carry_forward/carry_forward.py record-git-heads SESSION_ID

# Record what happened after a decision (auto-called from context)
python3 ~/zion/projects/carry_forward/carry_forward/carry_forward.py record-outcome [SESSION_ID]
```

### Tuning

```bash
# Auto-tune thresholds from decision history (needs 10+ outcomes)
python3 ~/zion/projects/carry_forward/carry_forward/carry_forward.py calibrate

# Show current threshold values and where they came from
python3 ~/zion/projects/carry_forward/carry_forward/carry_forward.py show-config

# Mine session history for patterns (source rates, time-of-day, size effects)
python3 ~/zion/projects/carry_forward/carry_forward/carry_forward.py learn
```

### Blockers

```bash
# List active blockers
python3 ~/zion/projects/carry_forward/carry_forward/carry_forward.py blockers

# Add a blocker (will halt future continuations until resolved)
python3 ~/zion/projects/carry_forward/carry_forward/carry_forward.py block "waiting on API key from ops"

# Remove a blocker
python3 ~/zion/projects/carry_forward/carry_forward/carry_forward.py unblock "API key"
```

### Replay Testing

```bash
# Full replay: compare current and proposed logic against historical outcomes
python3 ~/zion/projects/carry_forward/carry_forward/replay_harness.py

# Show which sessions would change and whether the change is correct
python3 ~/zion/projects/carry_forward/carry_forward/replay_harness.py --fixes

# Show misclassified sessions (false positives and false negatives)
python3 ~/zion/projects/carry_forward/carry_forward/replay_harness.py --misclassified

# Replay a single session in detail
python3 ~/zion/projects/carry_forward/carry_forward/replay_harness.py --session SESSION_ID
```

## The Database

carry_forward reads session history from `~/.hermes/state.db` (Hermes core) and
stores its own metadata in `~/.hermes/carry_forward.db`.

### Tables in carry_forward.db

| Table | Purpose |
|-------|---------|
| decision_log | Every check_can_continue() call: decision, reasons, thresholds |
| decision_outcomes | What actually happened after each decision |
| config | 8 tunable thresholds (defaults + calibration overrides) |
| chain_meta | Session chain metadata |
| chain_git_heads | Git HEAD snapshots per session per project |
| blockers | Persistent blockers with timestamps |
| learned_patterns | Source rates, size effects, time-of-day patterns |

### Key Relationships

```
decision_log.id  -->  decision_outcomes.decision_id  (1:1)
sessions.id      -->  decision_log.session_id         (many:1, from state.db)
sessions.parent_session_id  -->  sessions.id          (chain)
```

## Tunable Thresholds

| Key | Default | What it controls |
|-----|---------|-----------------|
| dead_session_threshold | 3 | dead sessions >= this in lookback = thrashing |
| dead_lookback | 5 | how many recent sessions to check for dead count |
| orphan_child_threshold | 10 | dead children >= this = runaway loop |
| continuation_rate_min | 15 | source rate < this% = warning |
| blocker_halt_hours | 4 | blocker age > this = halt |
| git_min_sessions | 3 | chain length needed for git progress check |
| parent_size_warning | 200 | parent msg count > this = warning |
| chain_depth_warning | 8 | chain depth >= this = warning |

All are read via `get_threshold(key)`, written via `set_threshold(key, value, source)`.
Calibration writes with source="calibration". Manual changes use source="manual".

## How to Make carry_forward Better

### The Safe Process

1. **Run the replay harness** to get current metrics
2. **Analyze misclassified sessions** to find patterns
3. **Propose a fix** and add it to replay_with_fix() in replay_harness.py
4. **Re-run the harness** to see if F1 improves
5. **If F1 went up**: apply the fix to check_can_continue(), commit
6. **If F1 went down or regressed**: discard the fix, keep the old logic

### What Counts as "Productive"

A session is marked productive (outcome_productive=1) if it had any tool calls.
This is a blunt instrument -- a session that made 1 API call and 100 tool calls
are both "productive" -- but it correlates well with actual work getting done.

### Common Pitfalls

- **Don't over-fit to historical data.** The replay harness measures against past
  decisions. If you tune thresholds to perfectly predict those 499 sessions, you
  may make the system worse for new situations. Prefer simple rules that catch
  clear patterns (like the dead-session check) over complex multi-factor rules.

- **Batch data skews metrics.** The 188 decisions logged at a single timestamp
  were a bulk backfill, not live decisions. They're mostly "halt" on productive
  sessions, which tanks your FN count. The harness flags these. When computing
  live metrics, exclude them (timestamp 1775844222).

- **"Alive" has a low bar.** A session with 1 message and 0 tool calls is
  considered "alive" in thrash detection. This is intentional -- some sessions
  are short by design (user asked a quick question, got an answer). The
  session_dead check (0 tools AND <=2 msgs) catches the ones that are truly dead.

- **Outcome recording is lazy.** Outcomes are recorded when `context` or
  `record-outcome` runs, not automatically at session end. If nobody calls these,
  the outcome never gets recorded and the calibration data is incomplete.

## Architecture Diagram

```
state.db (Hermes core)
  sessions ──────────┐
  messages           │
                     │
                     ▼
              detect_thrash() ──► thrashing? dead_count
              check_git_progress() ──► git_ok? git_details
              learned_patterns ──► source_rate, size_warning
              blockers ──► stale? age_hours
              session self ──► tool_calls, message_count
                     │
                     ▼
              check_can_continue()
                     │
              ┌──────┴──────┐
              │             │
         continue        halt
              │             │
              ▼             ▼
         decision_log (with reasons, thresholds)
              │
              ▼
         session plays out
              │
              ▼
         record_outcome() ──► decision_outcomes
              │
              ▼
         calibrate() ──► config (threshold updates)
              │
              ▼
         replay_harness.py ──► precision, recall, F1
```

## File Locations

```
~/zion/projects/carry_forward/carry_forward/
  carry_forward.py    -- the main script (~1890 lines, zero deps)
  replay_harness.py   -- accuracy testing against historical decisions
  AI_GUIDE.md         -- this file

~/.hermes/
  state.db            -- Hermes session/message data (READ ONLY)
  carry_forward.db    -- carry_forward metadata (READ/WRITE)
```

## Quick Reference for Agents

"I need to decide whether to continue a session":
  -> `should-continue` (exit code 0/1)

"I need the full context for handoff":
  -> `context` (prints everything)

"I want to tune the system":
  -> `calibrate` then `replay_harness.py`

"Something is blocking progress":
  -> `block "description"`

"I changed the decision logic and want to verify":
  -> `replay_harness.py` before and after. F1 should go up.
