# Carry Forward Roadmap

**Purpose:** Carry Forward is the decision engine that keeps Hermes autonomous loops productive. It answers one question: "should we spawn another session?" It's not an orchestrator, not a task manager -- it's a governor.

**Current state:** v5.3.0, 2452 lines, 97 tests, 5 guard rail phases built. Powering the generic `chain` script across multiple projects.

## What Works

- Dead session thrash detection (Phase 1)
- Hallucination loop detection (Phase 2)
- Test count regression detection (Phase 3)
- Consecutive no-op counter (Phase 4)
- Outcome tracking with auto-calibration (Phase 5)
- Smarter context with learned lessons (Phase 6)
- Roadmap integration (scan project roadmaps, completion signals)
- Context extraction from session DB
- `check-can-continue` 5-stage decision pipeline
- `replay_harness.py` for backtesting decisions against history

## What's Missing

The engine is solid for "should we continue?" but weak on "what should we do next?" and "what did we learn?"

## Priority Order for Automated Development

- [x] Phase 5: Outcome tracking -- auto_record_outcomes runs after every chain cycle, records whether the next session was productive (committed code, tests passed). Feed this back into threshold calibration. Currently record_outcome exists but is never called automatically.
- [x] Phase 6: Smarter context -- context command includes top 3 lessons from outcome history (e.g. "this project fails on phases involving vm.rs changes -- use smaller steps"). Currently context is purely factual, no learned intelligence.
- [ ] Phase 7: Project-aware thresholds -- different projects need different stall/noop thresholds. A Rust project with cargo test takes longer per cycle than a Python project. Calibrate per-project from outcome data.
- [ ] Phase 8: Test command discovery -- detect project test commands (cargo test, npm test, pytest, make test) and surface them in context so the agent doesn't have to guess. The `chain` script already does this; carry_forward should too.
- [ ] Phase 9: Session health dashboard -- a simple CLI command that shows: sessions run today, tests pass rate, commits landed, time wasted on failed cycles. One command to answer "how's the loop doing?"

## Design Principles

- **Transport, not comprehension.** Carry forward packages what happened; agents interpret.
- **Binary decisions.** Continue or halt. No maybe, no "try harder."
- **Data-driven thresholds.** Every threshold should be calibrated from actual outcomes, not gut feelings.
- **Zero config for new projects.** Detect everything from the filesystem and session DB.

## Conventions

- Every new guard rail gets a test in tests/test_carry_forward.py
- Every new CLI command gets a test
- 79 tests must stay green
- Python 3.10+, no external dependencies for core (roadmap_builder is optional)
