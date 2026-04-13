# Carry Forward -- AI Guide

## What This Is

Carry Forward is a decision engine for Hermes autonomous session loops. It reads `~/.hermes/state.db` to understand what the last session did, then decides whether to continue or halt.

It is NOT: a task manager, an orchestrator, a workflow engine, or a prompt builder.

## Architecture

```
carry_forward/
├── carry_forward.py         # Core: 3100+ lines, all logic
├── roadmap_integration.py   # Bridge: reads roadmap.yaml files for progress
├── replay_harness.py        # Testing: backtests decisions against history
├── tests/
│   ├── test_carry_forward.py    # 148 unit tests
│   └── test_replay_harness.py   # 15 replay tests
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
carry_forward.py health [--json]              # Session health dashboard for today
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

## Project-Aware Thresholds (Phase 7)

Different projects need different thresholds. A Rust project with `cargo test` takes longer per cycle than a Python project with `pytest`.

Resolution order for each threshold:
1. `project_thresholds` table -- explicit per-project override (from calibration or manual set)
2. `PROJECT_TYPE_DEFAULTS` -- auto-detected from project type (Cargo.toml -> rust, etc.)
3. `config` table -- global override
4. `THRESHOLD_DEFS` constants -- hardcoded defaults

Only `stagnation_stall_limit`, `noop_limit`, and `hallucination_loop_limit` are project-overridable. Other thresholds (dead_session_threshold, etc.) are always global.

Project type detection: scans for marker files (Cargo.toml, pyproject.toml, package.json, etc.).

Per-project calibration: `calibrate_project_thresholds()` uses the same logic as global calibration but scopes outcomes to sessions that involved a specific project (matched via `chain_git_heads.project_dir`). If fewer than 5 outcomes exist for a project, it seeds from `PROJECT_TYPE_DEFAULTS` instead.

CLI:
- `calibrate --project /path/to/project` -- calibrate thresholds for a specific project
- `show-config --project /path/to/project` -- show effective thresholds for a project

## How to Add a New Guard Rail

1. Write the detection function (e.g. `_detect_new_thing`)
2. Add it to `check_can_continue()` 
3. Add the result to the returned dict
4. Add tests in `tests/test_carry_forward.py`
5. Test with `replay_harness.py` to verify it doesn't break historical decisions

## Test Command Discovery (Phase 8)

`detect_test_command(project_dir)` uses the same marker-file approach as `detect_project_type()` but returns the test command string instead of the type. Surfaces in `cmd_context` output as `TEST: <command>` under each project's status.

Mapping (`TEST_COMMAND_MAP`):
- rust -> `cargo test`
- python -> `pytest`
- node -> `npm test`
- go -> `go test ./...`
- make -> `make test`
- java -> `mvn test`

CLI: `carry_forward.py detect-test-command <project_dir>`

## Session Health Dashboard (Phase 9)

`session_health_data()` queries both state.db and carry_forward.db to produce a daily snapshot:

- **Sessions today** -- total, active (tool_call_count > 0), dead (0 tools, non-cron)
- **Decisions** -- continue vs halt counts from decision_log
- **Commits landed** -- distinct new git HEADs in chain_git_heads today (excludes HEADs same as yesterday)
- **Test counts** -- latest test count per source from tick_test_counts
- **Time wasted** -- minutes spent on dead sessions (real duration when ended_at exists, 60s estimate otherwise)
- **Outcome accuracy** -- productive vs wasted from decision_outcomes

Verdict logic:
- NO DATA: 0 sessions
- HEALTHY: 60%+ active AND commits landing
- OK: 40%+ active
- SLOW: some activity but mostly dead
- STALLED: no active sessions

CLI: `carry_forward.py health [--json]`

## Dependencies

- Core: Python stdlib only (sqlite3, subprocess, re, json, time)
- Optional: `roadmap_builder` package for roadmap YAML parsing
- Dev: pytest, mypy

## Test Command

```bash
python3 -m pytest              # run all 185 tests
python3 -m pytest tests/test_carry_forward.py  # just core tests
python3 replay_harness.py      # backtest against history
```
