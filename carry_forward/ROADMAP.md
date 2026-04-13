# Carry Forward v7

Simplification pass based on overengineering audit. 5/7 signals flagged, 3 severe. Target: reduce abstraction depth, eliminate parallel implementations, consolidate duplicated query patterns. 223 tests must stay green throughout.

**Progress:** 0/4 phases complete, 0 in progress

**Deliverables:** 0/9 complete

**Tasks:** 0/30 complete

## Scope Summary

| Phase | Status | Deliverables | LOC Target | Tests |
|-------|--------|-------------|-----------|-------|
| phase-1 Consolidate duplicated query patterns | PLANNED | 0/3 | 3,420 | 223 |
| phase-2 Eliminate parallel implementations | PLANNED | 0/2 | 3,270 | 223 |
| phase-3 Reduce abstraction depth | PLANNED | 0/3 | 3,100 | 223 |
| phase-4 Re-run overengineered audit | PLANNED | 0/1 | 3,950 | 223 |

## Dependencies

| From | To | Type | Reason |
|------|----|------|--------|
| phase-1 | phase-2 | soft | Independent but good to do consolidation first to reduce merge conflicts |
| phase-1 | phase-3 | soft | Phase 1 consolidation may reduce some depth naturally |
| phase-2 | phase-3 | soft | Phase 2 may flatten cmd_context depth naturally |
| phase-1 | phase-4 | hard | Must complete simplification before measuring |
| phase-2 | phase-4 | hard | Must complete simplification before measuring |
| phase-3 | phase-4 | hard | Must complete simplification before measuring |

## [ ] phase-1: Consolidate duplicated query patterns (PLANNED)

**Goal:** Merge the 4 get_top_* functions and 3 _update_*_counter functions into parameterized helpers

The audit found 3 families of duplicated query/update patterns:

1. get_top_lessons, get_top_technical_patterns, get_top_failure_fingerprints all do
   "SELECT from table, ORDER BY, LIMIT N". 80-100% call overlap. Should be one
   generic get_top_knowledge(type, n) function.

2. _update_stall_counter, _update_noop_counter, store_technical_patterns all do
   "upsert into table with session_id + metadata". 100% call overlap. Should be one
   generic _update_counter(table, session_id, data) function.

3. _get_chain_stalls and _count_consecutive_noops share 92% of called functions.
   Both scan session history counting consecutive events. Should be one
   _count_consecutive_events(session_id, condition_fn) function.

Estimated savings: ~80 lines, reduces duplication from 8 findings to ~2.


### Deliverables

- [ ] **Generic knowledge query function** -- Replace get_top_lessons, get_top_technical_patterns, get_top_failure_fingerprints with get_top_knowledge(table, n, project_dir=None)
  - [ ] `p1.d1.t1` Create get_top_knowledge(table, n, project_dir, order_by) helper
    > Generic function that queries any knowledge table with same SELECT/ORDER BY/LIMIT pattern. Returns list of dicts.
    _Files: carry_forward.py_
  - [ ] `p1.d1.t2` Refactor get_top_lessons to call get_top_knowledge (depends: p1.d1.t1)
    > Thin wrapper that calls get_top_knowledge('lessons', n)
    _Files: carry_forward.py_
  - [ ] `p1.d1.t3` Refactor get_top_technical_patterns to call get_top_knowledge (depends: p1.d1.t1)
    > Thin wrapper that calls get_top_knowledge('technical_patterns', n, project_dir)
    _Files: carry_forward.py_
  - [ ] `p1.d1.t4` Refactor get_top_failure_fingerprints to call get_top_knowledge (depends: p1.d1.t1)
    > Thin wrapper that calls get_top_knowledge('failure_fingerprints', n, project_dir)
    _Files: carry_forward.py_
  - [ ] `p1.d1.t5` Run full test suite and fix any regressions (depends: p1.d1.t2, p1.d1.t3, p1.d1.t4)
    > pytest tests/ -q -- all 223 must pass
    _Files: tests/test_carry_forward.py_
  - [ ] All 223 existing tests pass
    _Validation: pytest tests/ -q_
  - [ ] get_top_lessons, get_top_technical_patterns, get_top_failure_fingerprints still work identically
    _Validation: grep -c calls to new helper == 3_
  - [ ] No change to CLI output
    _Validation: diff before/after of cmd_learn, cmd_analyze_patterns, cmd_analyze_failures output_
- [ ] **Generic counter update function** -- Replace _update_stall_counter, _update_noop_counter with _update_event_counter(table, session_id, active, metadata)
  - [ ] `p1.d2.t1` Create _update_event_counter helper
    > Generic upsert for event counters. Takes table name, session_id, active flag.
    _Files: carry_forward.py_
  - [ ] `p1.d2.t2` Refactor _update_stall_counter and _update_noop_counter (depends: p1.d2.t1)
    > Replace bodies with calls to _update_event_counter
    _Files: carry_forward.py_
  - [ ] `p1.d2.t3` Run full test suite (depends: p1.d2.t2)
    > All 223 tests must pass
  - [ ] All tests pass
    _Validation: pytest tests/ -q_
- [ ] **Merge consecutive event counters** -- Replace _get_chain_stalls and _count_consecutive_noops with _count_consecutive(session_id, check_fn)
  - [ ] `p1.d3.t1` Create _count_consecutive helper
    > Generic consecutive-event counter. Takes session_id and a callable that returns True for events to count.
    _Files: carry_forward.py_
  - [ ] `p1.d3.t2` Refactor _get_chain_stalls and _count_consecutive_noops (depends: p1.d3.t1)
    > Replace with calls to _count_consecutive with appropriate check functions
    _Files: carry_forward.py_
  - [ ] `p1.d3.t3` Run full test suite (depends: p1.d3.t2)
    > All 223 tests must pass
  - [ ] All tests pass
    _Validation: pytest tests/ -q_

### Technical Notes

These are purely structural refactorings. No behavior changes. Each function
family has identical call patterns -- just different table names and column names.
The parameterized versions should accept the varying parts as arguments.


### Risks

- Tests may depend on specific function names in mocks/patches -- check conftest.py
- store_technical_patterns has different shape (takes patterns list, not just session_id) -- may not fit generic counter

## [ ] phase-2: Eliminate parallel implementations (PLANNED)

**Goal:** Merge cmd_context/get_context_data (129 waste lines) and cmd_last/get_last_assistant_messages

Two pairs of functions doing the same work:

1. cmd_context (239 lines) and get_context_data (129 lines) -- 63% call overlap.
   cmd_context is the CLI version (prints to stdout), get_context_data is the JSON
   API version (returns dict). cmd_context should call get_context_data and format
   the result, not re-implement the logic.

2. cmd_last (17 lines) and get_last_assistant_messages (27 lines) -- same pattern.
   cmd_last should call get_last_assistant_messages and format output.

Estimated savings: ~146 lines.


### Deliverables

- [ ] **cmd_context delegates to get_context_data** -- Rewrite cmd_context to call get_context_data() and format the returned dict as terminal output. Remove all duplicated logic.
  - [ ] `p2.d1.t1` Audit what cmd_context does that get_context_data doesn't
    > Compare both functions line by line. Document any logic only in cmd_context.
    _Files: carry_forward.py_
  - [ ] `p2.d1.t2` Ensure get_context_data covers all cmd_context logic (depends: p2.d1.t1)
    > If cmd_context has extra logic (formatting, display), move the data-gathering into get_context_data and keep only formatting in cmd_context.
    _Files: carry_forward.py_
  - [ ] `p2.d1.t3` Rewrite cmd_context as thin formatter over get_context_data (depends: p2.d1.t2)
    > cmd_context calls get_context_data, then formats the dict for terminal output. No duplicated queries or DB access.
    _Files: carry_forward.py_
  - [ ] `p2.d1.t4` Run full test suite (depends: p2.d1.t3)
    > All 223 tests must pass
  - [ ] cmd_context output is identical to before
    _Validation: Run both versions, diff output_
  - [ ] get_context_data JSON is identical to before
    _Validation: Run both versions, diff JSON_
  - [ ] All 223 tests pass
    _Validation: pytest tests/ -q_
- [ ] **cmd_last delegates to get_last_assistant_messages** -- Rewrite cmd_last to call get_last_assistant_messages() and format output.
  - [ ] `p2.d2.t1` Rewrite cmd_last to call get_last_assistant_messages
    > cmd_last becomes: messages = get_last_assistant_messages(depth); for m in messages: print(m)
    _Files: carry_forward.py_
  - [ ] `p2.d2.t2` Run full test suite (depends: p2.d2.t1)
    > All 223 tests must pass
  - [ ] cmd_last output unchanged
    _Validation: Run both versions, diff output_
  - [ ] All tests pass
    _Validation: pytest tests/ -q_

### Technical Notes

cmd_context at line 3276 and get_context_data at line 3862. The 63% overlap means
37% is different -- likely formatting vs data gathering. The key is making
get_context_data the single source of truth for ALL data, and cmd_context just
a presentation layer.


### Risks

- cmd_context has 239 lines of formatting logic -- some of it may be intertwined with data gathering
- Tests may assert on specific print output format -- check before refactoring

## [ ] phase-3: Reduce abstraction depth (PLANNED)

**Goal:** Flatten the call chains in cmd_run (8 layers), cmd_should_continue (7), cmd_context (7)

The dominant signal: 15 abstraction_depth findings, 3 severe. The core decision
pipeline goes through too many layers:

cmd_run (8 layers)
  -> cmd_should_continue
    -> check_can_continue
      -> detect_thrash
        -> _get_chain_stalls
          -> get_conn()
            -> SQL query

Each layer should have a one-sentence justification. Most don't. The fix is to
flatten: combine thin wrappers, inline trivial delegation, and group related
checks into fewer functions with more explicit flow.

Target: no function with more than 4 layers of call depth.


### Deliverables

- [ ] **Flatten check_can_continue pipeline** -- Reduce check_can_continue from 6 layers to 3-4 by merging thin wrappers
  - [ ] `p3.d1.t1` Map the full call chain for check_can_continue
    > Trace every function call from check_can_continue down to leaf functions. Identify which layers are trivial wrappers (< 5 lines of actual logic).
    _Files: carry_forward.py_
  - [ ] `p3.d1.t2` Inline or merge thin wrapper layers (depends: p3.d1.t1)
    > For each trivial wrapper in the chain: either inline it at the call site or merge it with its caller. Keep functions that do real work.
    _Files: carry_forward.py_
  - [ ] `p3.d1.t3` Verify call depth reduced (depends: p3.d1.t2)
    > Re-run overengineered.py or manually verify no function in the check_can_continue chain has >5 depth.
  - [ ] `p3.d1.t4` Run full test suite (depends: p3.d1.t3)
    > All 223 tests must pass
  - [ ] No function has >5 layers of call depth
    _Validation: Re-run overengineered.py and verify 0 severe abstraction_depth findings_
  - [ ] All 223 tests pass
    _Validation: pytest tests/ -q_
  - [ ] check_can_continue returns same structure
    _Validation: Compare JSON output before/after_
- [ ] **Flatten cmd_run and cmd_should_continue** -- Reduce cmd_run from 8 layers and cmd_should_continue from 7
  - [ ] `p3.d2.t1` Map call chains for cmd_run and cmd_should_continue (depends: p3.d1.t4)
    > Trace full call chains. These likely go through check_can_continue which was already flattened.
    _Files: carry_forward.py_
  - [ ] `p3.d2.t2` Flatten remaining deep chains (depends: p3.d2.t1)
    > Apply same thin-wrapper elimination. cmd_run likely just calls cmd_should_continue + some extra -- verify the extra layers are necessary.
    _Files: carry_forward.py_
  - [ ] `p3.d2.t3` Run full test suite (depends: p3.d2.t2)
    > All 223 tests must pass
  - [ ] cmd_run has <=5 call depth
    _Validation: Re-run overengineered.py_
  - [ ] cmd_should_continue has <=5 call depth
    _Validation: Re-run overengineered.py_
  - [ ] All tests pass
    _Validation: pytest tests/ -q_
- [ ] **Flatten remaining deep functions** -- Fix calibrate_thresholds (5), auto_record_outcomes (6), calibrate_project_thresholds (6), get_context_data (6)
  - [ ] `p3.d3.t1` Flatten calibration chain (depends: p3.d2.t3)
    > calibrate_thresholds -> calibrate_project_thresholds -> _adjust_project -> _adjust has 4 function layers for what is essentially 'compute adjustment, apply it'. Flatten.
    _Files: carry_forward.py_
  - [ ] `p3.d3.t2` Flatten auto_record_outcomes chain (depends: p3.d2.t3)
    > Trace and flatten the 6-layer chain in auto_record_outcomes
    _Files: carry_forward.py_
  - [ ] `p3.d3.t3` Run full test suite (depends: p3.d3.t1, p3.d3.t2)
    > All 223 tests must pass
  - [ ] 0 functions with >5 call depth
    _Validation: Re-run overengineered.py -- 0 severe abstraction_depth findings_
  - [ ] All tests pass
    _Validation: pytest tests/ -q_

### Technical Notes

The abstraction depth problem is the core signal. 15 findings. But fixing it is
the highest-risk phase because it touches the decision pipeline. Go slow,
test after every change.

Key insight: many of these layers exist because phases were added incrementally.
Phase 1 (stalls) + Phase 4 (noops) + Phase 3 (test regression) each added a check
function, and check_can_continue calls them all. The result is deep but narrow
call trees. Consolidating checks into fewer, wider functions would reduce depth
without losing clarity.


### Risks

- Touching check_can_continue is high-risk -- it's the core decision engine
- Tests may depend on internal function names in mocks
- Flattening too aggressively makes individual checks harder to test in isolation

## [ ] phase-4: Re-run overengineered audit (PLANNED)

**Goal:** Verify all 7 signals. Target: <=3/7 signals flagged, 0 severe, no parallel implementations.

After phases 1-3, re-run the overengineered tool against the codebase. 
Measure: how many signals fire now? What's the verdict?

Success criteria:
- 0 severe findings
- 0 parallel_implementations findings
- <=2 abstraction_depth findings (mild only)
- Verdict drops from "Severely overengineered" to "Slightly over" or better
- 223 tests still green
- Total LOC reduced by 200+ from current 4142


### Deliverables

- [ ] **Audit results** -- Re-run overengineered.py and document the improvement
  - [ ] `p4.d1.t1` Run overengineered audit and record results
    > Run full audit, save JSON results, compare to baseline (5/7 signals, 3 severe, 12 significant, 21 mild)
  - [ ] `p4.d1.t2` Fix any remaining low-hanging fruit (depends: p4.d1.t1)
    > If there are still easy wins (mild findings that take <5 minutes each), knock them out.
    _Files: carry_forward.py_
  - [ ] `p4.d1.t3` Update ROADMAP.md and version to v7.0.0 (depends: p4.d1.t2)
    > Update the roadmap markdown, bump version, document what changed.
    _Files: ROADMAP.md_
  - [ ] 0 severe findings
    _Validation: python3 overengineered.py --json | check severe_count == 0_
  - [ ] 0 parallel_implementations
    _Validation: python3 overengineered.py --json | check parallel_implementations count == 0_
  - [ ] 223 tests pass
    _Validation: pytest tests/ -q_
  - [ ] Source lines < 3950
    _Validation: wc -l carry_forward.py_

## Global Risks

- 4142-line single file makes refactoring risky -- consider splitting into modules after v7
- 223 tests are the safety net -- any test breakage must be fixed before proceeding
- carry_forward.py is chmod u+w needed on some systems
- The chain script (~/zion/scripts/chain) depends on carry_forward CLI interface -- don't change argument signatures

## Conventions

- Every change must keep all 223 tests green
- No new external dependencies
- Python 3.10+
- CLI argument signatures must not change (chain script depends on them)
- Test before every commit in each phase
