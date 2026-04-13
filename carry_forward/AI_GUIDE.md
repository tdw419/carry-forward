# Carry Forward -- AI Guide

## What This Is

Carry Forward is a decision engine for Hermes autonomous session loops. It reads `~/.hermes/state.db` to understand what the last session did, then decides whether to continue or halt.

It is NOT: a task manager, an orchestrator, a workflow engine, or a prompt builder.

## Architecture

```
carry_forward/
├── carry_forward.py         # Core: 2452 lines, all logic
├── roadmap_integration.py   # Bridge: reads roadmap.yaml files for progress
├── replay_harness.py        # Testing: backtests decisions against history
├── tests/
│   ├── test_carry_forward.py    # 83 unit tests
│   └── test_replay_harness.py   # 14 replay tests
├── ROADMAP.md                # Phases and priorities
├── AI_GUIDE.md               # This file
└── pyproject.toml            # Python 3.10+, pytest, mypy
```

## Key Files

- `~/.hermes/state.db` -- Hermes session database (read-only for carry_forward)
- `~/.hermes/carry_forward.db` -- Carry forward's own DB (decisions, outcomes, patterns, thresholds)

## Commands

```
carry_forward.py context [--include-cron]     # Full context from last session
carry_forward.py status                       # Git-aware project state
carry_forward.py summary [SESSION_ID]         # Smart session summary
carry_forward.py last [--depth N]             # Recent non-trivial sessions
carry_forward.py messages SESSION_ID [--last N]  # Session messages
carry_forward.py chain [SESSION_ID]           # Trace continuation chain
carry_forward.py blockers                     # Show blockers
carry_forward.py should-continue              # Exit 0=go, 1=stop
carry_forward.py check-can-continue [SESSION] # JSON decision
carry_forward.py record-git-heads SESSION_ID  # Snapshot git HEADs
carry_forward.py learn                        # Analyze history, record patterns
carry_forward.py record-outcome [SESSION_ID]  # Record decision outcome
carry_forward.py calibrate                    # Auto-tune thresholds
carry_forward.py roadmap                      # Show project roadmap progress
```

## Decision Pipeline (check_can_continue)

5 stages, all must pass to continue:

1. **Dead session thrash** -- 3/5 recent sessions with 0 messages = halt
2. **Git progress** -- no commits across N sessions = stall
3. **Pattern recognition** -- low-success sources get warned
4. **Hallucination loop** -- same files edited repeatedly with no commits = halt
5. **Test regression** -- test count dropped = halt
6. **No-op counter** -- consecutive sessions with 0 tool calls = halt
7. **Roadmap completion** -- all deliverables done = informational (not a hard halt)

All thresholds are tunable via the config table and auto-calibrated from outcome data.

## Outcome Tracking (Phase 5)

After every `should-continue` call, `auto_record_outcomes` checks the previous session's decision against what actually happened:
- Was the session productive (had tool calls)?
- Did git HEAD move?
- Did the chain continue?

Every 10 outcomes, thresholds are auto-calibrated:
- High continue accuracy (>80% productive) -> loosen limits (allow more cycles)
- Low continue accuracy (<50% productive) -> tighten limits (catch stalls sooner)
- High halt accuracy (>80% correctly caught) -> tighten dead session detection
- Low halt accuracy (<50% correct) -> loosen limits (avoid false stops)

## How to Add a New Guard Rail

1. Write the detection function (e.g. `_detect_new_thing`)
2. Add it to `check_can_continue()` 
3. Add the result to the returned dict
4. Add tests in `tests/test_carry_forward.py`
5. Test with `replay_harness.py` to verify it doesn't break historical decisions

## Dependencies

- Core: Python stdlib only (sqlite3, subprocess, re, json, time)
- Optional: `roadmap_builder` package for roadmap YAML parsing
- Dev: pytest, mypy

## Test Command

```bash
python3 -m pytest              # run all 97 tests
python3 -m pytest tests/test_carry_forward.py  # just core tests
python3 replay_harness.py      # backtest against history
```
