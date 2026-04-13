# Carry Forward Roadmap

**Purpose:** Carry Forward is the decision engine that keeps Hermes autonomous loops productive. It answers one question: "should we spawn another session?" It's not an orchestrator, not a task manager -- it's a governor.

**Current state:** v5.8.0, 3840+ lines, 195 tests, 11 guard rail phases built. Powering the generic `chain` script across multiple projects.

## What Works

- Dead session thrash detection (Phase 1)
- Hallucination loop detection (Phase 2)
- Test count regression detection (Phase 3)
- Consecutive no-op counter (Phase 4)
- Outcome tracking with auto-calibration (Phase 5)
- Smarter context with learned lessons (Phase 6)
- Project-aware thresholds (Phase 7)
- Test command discovery (Phase 8)
- Session health dashboard (Phase 9)
- Technical pattern extraction (Phase 10)
- Next-task suggestion (Phase 11)
- Roadmap integration (scan project roadmaps, completion signals)
- Context extraction from session DB
- `check-can-continue` 5-stage decision pipeline
- `replay_harness.py` for backtesting decisions against history

## What's Missing

The engine is solid for "should we continue?" but weak on "what should we do next?" and "what did we learn?"

## Priority Order for Automated Development

- [x] Phase 5: Outcome tracking -- auto_record_outcomes runs after every chain cycle
- [x] Phase 6: Smarter context -- context command includes top 3 lessons
- [x] Phase 7: Project-aware thresholds -- different projects need different thresholds
- [x] Phase 8: Test command discovery -- detect project test commands
- [x] Phase 9: Session health dashboard -- daily snapshot

### Next Frontier

- [x] Phase 10: Technical pattern extraction -- lessons about *what* was worked on, not just whether sessions were productive. Track which files/directories appear in failed sessions vs successful ones. Surface "geometry_os/src/vm.rs appears in 8/10 failed sessions" as a lesson. New command `analyze-patterns` that scans session messages for file paths and correlates with outcomes.
- [x] Phase 11: Next-task suggestion -- when `chain` runs out of roadmap items (all checked), carry_forward should suggest the next logical task by analyzing: (a) what files were recently changed, (b) what the last session was working on when it stopped, (c) what uncommitted files exist. New command `suggest-next [project_dir]` that returns a ranked list of 3 candidate tasks with confidence scores. Also surfaces in `context` output and `get_context_data` JSON.
- [ ] Phase 12: Failure fingerprinting -- when a session is marked unproductive, scan its tool calls for common failure patterns: build errors, test failures, timeout kills, compilation errors. Store failure fingerprints and surface them in context: "last 3 failures on this project were all compilation errors in src/parser.rs." New command `analyze-failures [project_dir]`.

## Design Principles

- **Transport, not comprehension.** Carry forward packages what happened; agents interpret.
- **Binary decisions.** Continue or halt. No maybe, no "try harder."
- **Data-driven thresholds.** Every threshold should be calibrated from actual outcomes, not gut feelings.
- **Zero config for new projects.** Detect everything from the filesystem and session DB.

## Conventions

- Every new guard rail gets a test in tests/test_carry_forward.py
- Every new CLI command gets a test
- 195 tests must stay green
- Python 3.10+, no external dependencies for core (roadmap_builder is optional)
