# Carry Forward Roadmap

## NORTH STAR

Carry forward's job: help an AI agent pick up where the last session left off,
and stop the chain when it's clearly not making progress. Nothing more.

## Current State (v5.2)

- 58 tests, F1 0.868 on the decision pipeline
- Dead session check works (strongest signal)
- Git progress tracking works
- Blocker management works
- Continuation rate prediction is near-useless (F1 0.142 on real data)
- Context command dumps raw text instead of synthesizing
- Training data polluted with 895 dead cron sessions
- Most decision logic redundant when used inside a loop like chain_dev.py

## Phase 1: Clean the Data

**Goal:** Decisions calibrated on real sessions, not cron garbage.

- [ ] Purge dead cron sessions (source='cron', message_count=0, tool_call_count=0) from decision history
- [ ] Recalibrate thresholds on cleaned data
- [ ] Re-run replay harness, get baseline F1 on clean data
- [ ] Add source filter to record-outcome -- don't log outcomes for sessions with 0 tool calls and source='cron'

## Phase 2: Fix the Context Command

**Goal:** New session gets a useful handoff, not a text dump.

- [ ] Read ROADMAP.md or NORTH_STAR.md from detected project dirs
- [ ] Extract: what was attempted, what succeeded, what failed, what's next
- [ ] Summarize instead of dumping raw messages
- [ ] Include project file tree changes since last session (git diff --stat)
- [ ] Fallback to current behavior only if no project files found

## Phase 3: Strip the Over-Engineering

**Goal:** Delete code that doesn't earn its keep.

- [ ] Remove calibration/threshold sweeping (data doesn't support it)
- [ ] Remove pattern learning (learned_patterns table)
- [ ] Remove continuation_rate_min check (11.9% baseline means the signal is noise)
- [ ] Remove parent_size_warning and chain_depth_warning (warnings nobody acts on)
- [ ] Keep: dead session check, git stall check, blocker management
- [ ] Target: <500 lines, same or better real-world performance

## Phase 4: Make It Useful for Loops

**Goal:** A loop like chain_dev.py can use carry_forward in 3 lines.

- [x] Single entry point: `carry_forward.py run` that does context + should-continue + record-outcome
- [x] JSON output mode for programmatic consumption (`--json`)
- [x] Exit codes: 0=continue with context, 1=halt
- [ ] Document the integration pattern (how a cron loop wires it up)

## Phase 5: Prove It Works

**Goal:** Run it on a real project for a week and measure.

- [ ] Wire carry_forward into an active project loop
- [ ] Collect 100+ live decisions under the new logic
- [ ] Re-run replay harness on fresh data
- [ ] Publish results in the repo

## What We're Not Doing

- ML-based prediction (the data doesn't support it)
- Complex multi-factor decision models
- Anything that requires reading the agent's mind
- Replacing what chain_dev.py already does well
