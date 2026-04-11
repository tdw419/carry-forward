# Carry Forward v7 ROADMAP

## Phase 1: Hard Stagnation Detection
The biggest token-saver. Promotes git_stalled from informational to hard halt.

- [x] Add `consecutive_stalls` column to chain_meta (default 0)
- [x] In check_can_continue: if git_stalled, increment counter; if commit landed, reset to 0
- [x] If consecutive_stalls >= 3, add hard halt reason ("3 ticks with no commits")
- [x] Active session override: if own_tools > 0, don't halt (session might be mid-commit)
- [x] Test: stalled chain halts at 3, productive chain resets counter
- [x] Test: mid-work session with tools doesn't get killed by stall counter

## Phase 2: Hallucination Loop Detection
Catches the BGT problem -- agent edits same files repeatedly without progress.

- [x] Add `tick_file_changes` table (session_id, tick_number, files_changed_json, committed bool)
- [x] After each tick, record `git diff --name-only HEAD~1..HEAD` (if commit) or `git diff --name-only` (if no commit)
- [x] New check: look at last 3 ticks. If same file(s) appear in all 3 with no commit, add halt reason
- [x] Guard rail (not hard halt): if same files edited 2 ticks in a row, warn but continue
- [x] Test: 3 ticks touching same file with no commit triggers halt
- [x] Test: different files each tick does not trigger

## Phase 3: Test Count Regression
Catches agents deleting tests to make suites pass.

- [x] Add `tick_test_counts` table (session_id, tick_number, test_count, source)
- [x] Record test count before and after each tick (grep `def test_` or `#[test]`)
- [x] New check: if test_count drops by more than 2 in a single tick, hard halt
- [x] Informational: if test_count unchanged for 5 ticks but ROADMAP has test items, warn
- [x] Test: count drops by 5 triggers halt
- [x] Test: count drops by 1 (refactor) does not trigger
- [x] Test: count increases is fine

## Phase 4: Consecutive No-Op Counter
Unified metric that subsumes Phase 1 and Phase 2.

- [ ] Add `consecutive_noops` column to chain_meta (default 0)
- [ ] No-op definition: tick with 0 commits AND 0 test count increase AND same files edited
- [ ] Increment on no-op, reset to 0 on any commit or test increase
- [ ] If consecutive_noops >= 3, hard halt
- [ ] Wire into check_can_continue as a single unified check
- [ ] Deprecate separate git_stalled halt (now subsumed)
- [ ] Test: 3 no-ops halts, 1 productive tick resets, then 2 more no-ops is fine

## Phase 5: Doctor Check
Environment validation before each tick starts work.

- [ ] Add `doctor_checks` config (project-specific, e.g. `cargo --version`, `node --version`)
- [ ] In chain_preflight.py: run doctor checks, halt if any fail
- [ ] Cache results for 30 minutes (don't re-check every 5 min)
- [ ] Log doctor failures to decision_log with reason
- [ ] Test: missing tool halts, present tool passes, cached result skips re-check

## Phase 6: Economic Circuit Breaker
Stop burning credits when the loop clearly isn't working.

- [ ] Track total no-op ticks in a 24-hour window
- [ ] If no-op rate > 80% over last 10 ticks, halt with message to human
- [ ] Do NOT auto-resume -- requires manual `unblock` to restart
- [ ] Log economic halt reason with stats (N no-ops / M total ticks)
- [ ] Test: 8 no-ops out of 10 triggers halt
- [ ] Test: 5 no-ops out of 10 (50%) does not trigger
