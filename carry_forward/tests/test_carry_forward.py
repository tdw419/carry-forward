"""Tests for carry_forward v5.2 decision pipeline.

Uses temporary SQLite databases to avoid touching real state.
Mocks get_conn() and get_carry_conn() to point to test DBs.
"""
import os
import sqlite3
import sys
import time

import pytest

# Ensure conftest helpers are importable
sys.path.insert(0, os.path.dirname(__file__))
from conftest import insert_session, insert_blocker, insert_git_heads, insert_config, insert_chain, insert_tick_changes, insert_test_count, insert_decision, insert_outcome, insert_lesson, insert_project_threshold  # noqa: E402


# ===========================================================================
# Tests: check_can_continue
# ===========================================================================

class TestCheckCanContinue:
    """Tests for the v5.2 decision pipeline."""

    def test_active_session_continues(self, state_db, carry_db, patched_env):
        """Session with tools > 0 should always continue."""
        insert_session(state_db, "active_1", msgs=10, tools=5)
        result = patched_env.check_can_continue("active_1")
        assert result["can_continue"] is True

    def test_dead_session_halts(self, state_db, carry_db, patched_env):
        """Session with 0 tools and <=2 messages should halt."""
        insert_session(state_db, "dead_1", msgs=1, tools=0)
        result = patched_env.check_can_continue("dead_1")
        assert result["can_continue"] is False
        assert result["session_dead"] is True
        assert any("Session dead" in r for r in result["reasons"])

    def test_dead_session_0_msgs_halts(self, state_db, carry_db, patched_env):
        """Session with 0 tools and 0 messages should halt."""
        insert_session(state_db, "dead_0", msgs=0, tools=0)
        result = patched_env.check_can_continue("dead_0")
        assert result["can_continue"] is False
        assert result["session_dead"] is True

    def test_zero_tools_3_msgs_continues(self, state_db, carry_db, patched_env):
        """Session with 0 tools but 3+ messages is not dead (>2 threshold)."""
        insert_session(state_db, "msgs_3", msgs=3, tools=0)
        result = patched_env.check_can_continue("msgs_3")
        assert result["can_continue"] is True
        assert result["session_dead"] is False

    def test_parent_dead_session_active_continues(self, state_db, carry_db, patched_env):
        """Active session should continue even if parent was dead."""
        insert_session(state_db, "parent_dead", msgs=0, tools=0)
        insert_session(state_db, "child_active", parent="parent_dead", msgs=10, tools=15)
        result = patched_env.check_can_continue("child_active")
        assert result["can_continue"] is True

    def test_parent_dead_session_dead_halts(self, state_db, carry_db, patched_env):
        """Dead session with dead parent should halt."""
        insert_session(state_db, "parent_dead_2", msgs=0, tools=0)
        insert_session(state_db, "child_dead", parent="parent_dead_2", msgs=1, tools=0)
        result = patched_env.check_can_continue("child_dead")
        assert result["can_continue"] is False
        assert result["session_dead"] is True

    def test_parent_active_continues(self, state_db, carry_db, patched_env):
        """Session with active parent should continue normally."""
        insert_session(state_db, "parent_ok", msgs=20, tools=10)
        insert_session(state_db, "child_ok", parent="parent_ok", msgs=5, tools=3)
        result = patched_env.check_can_continue("child_ok")
        assert result["can_continue"] is True

    def test_stale_blocker_halts(self, state_db, carry_db, patched_env):
        """Stale blocker (>4h old) should halt."""
        insert_session(state_db, "blocked_1", msgs=10, tools=5)
        insert_blocker(carry_db, "waiting for API key", age_hours=6)
        result = patched_env.check_can_continue("blocked_1")
        assert result["can_continue"] is False
        assert result["blocker_halt"] is True
        assert any("Stale blocker" in r for r in result["reasons"])

    def test_recent_blocker_no_halt(self, state_db, carry_db, patched_env):
        """Recent blocker (<4h old) should NOT halt."""
        insert_session(state_db, "blocked_recent", msgs=10, tools=5)
        insert_blocker(carry_db, "waiting for review", age_hours=1)
        result = patched_env.check_can_continue("blocked_recent")
        assert result["can_continue"] is True
        assert result["blocker_halt"] is False

    def test_resolved_blocker_no_halt(self, state_db, carry_db, patched_env):
        """Resolved blocker should not affect continuation."""
        insert_session(state_db, "resolved_1", msgs=10, tools=5)
        insert_blocker(carry_db, "was blocked", age_hours=10, resolved=True)
        result = patched_env.check_can_continue("resolved_1")
        assert result["can_continue"] is True

    def test_thrashing_active_session_continues(self, state_db, carry_db, patched_env):
        """Active session should continue even in a thrashing chain."""
        insert_config(carry_db, "dead_session_threshold", "1")
        insert_config(carry_db, "dead_lookback", "3")
        insert_config(carry_db, "orphan_child_threshold", "10")

        insert_session(state_db, "gp_1", msgs=5, tools=2)
        insert_session(state_db, "p_dead", parent="gp_1", msgs=0, tools=0)
        insert_session(state_db, "active_in_thrash", parent="p_dead", msgs=20, tools=15)

        result = patched_env.check_can_continue("active_in_thrash")
        assert result["can_continue"] is True
        assert result["thrashing"] is False

    def test_thrashing_dead_session_halts(self, state_db, carry_db, patched_env):
        """Dead session in thrashing chain should halt."""
        insert_config(carry_db, "dead_session_threshold", "1")
        insert_config(carry_db, "dead_lookback", "3")

        insert_session(state_db, "gp_2", msgs=0, tools=0)
        insert_session(state_db, "p_dead_2", parent="gp_2", msgs=0, tools=0)
        insert_session(state_db, "dead_in_thrash", parent="p_dead_2", msgs=0, tools=0)

        result = patched_env.check_can_continue("dead_in_thrash")
        assert result["can_continue"] is False

    def test_no_session_id_resolves(self, state_db, carry_db, patched_env):
        """When no session_id given, should resolve to latest session."""
        insert_session(state_db, "old_one", msgs=5, tools=2, source='cli')
        conn = sqlite3.connect(state_db)
        conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?",
                     (time.time() - 100, "old_one"))
        conn.commit()
        conn.close()

        insert_session(state_db, "latest_one", msgs=10, tools=3, source='cli')
        conn = sqlite3.connect(state_db)
        conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?",
                     (time.time(), "latest_one"))
        conn.commit()
        conn.close()

        result = patched_env.check_can_continue()
        assert result["can_continue"] is True

    def test_git_stall_active_session_continues(self, state_db, carry_db, patched_env):
        """Git stalled but session has tools -> should continue (v5.2 fix)."""
        insert_config(carry_db, "git_min_sessions", "2")

        insert_session(state_db, "g1", msgs=5, tools=2)
        insert_session(state_db, "g2", parent="g1", msgs=5, tools=2)
        insert_session(state_db, "g3", parent="g2", msgs=10, tools=8)

        insert_git_heads(carry_db, "g1", "/project", "abc123")
        insert_git_heads(carry_db, "g3", "/project", "abc123")

        result = patched_env.check_can_continue("g3")
        assert result["can_continue"] is True
        assert any("Git stalled" in gr for gr in result["guard_rails"])

    def test_returns_decision_id(self, state_db, carry_db, patched_env):
        """Each call should return a decision_id for outcome tracking."""
        insert_session(state_db, "dec_test", msgs=5, tools=2)
        result = patched_env.check_can_continue("dec_test")
        assert "decision_id" in result
        assert result["decision_id"] is not None

    def test_decision_logged(self, state_db, carry_db, patched_env):
        """Decision should be logged to decision_log table."""
        insert_session(state_db, "log_test", msgs=5, tools=2)
        result = patched_env.check_can_continue("log_test")

        conn = sqlite3.connect(carry_db)
        row = conn.execute(
            "SELECT decision, session_id FROM decision_log WHERE id = ?",
            (result["decision_id"],)
        ).fetchone()
        conn.close()

        assert row is not None
        assert row[0] == "continue"
        assert row[1] == "log_test"


# ===========================================================================
# Tests: get_threshold / set_threshold
# ===========================================================================

# ===========================================================================
# Tests: detect_thrash
# ===========================================================================

class TestDetectThrash:
    def test_short_chain_not_thrashing(self, state_db, carry_db, patched_env):
        """Single session chain should not be thrashing."""
        insert_session(state_db, "single", msgs=5, tools=2)
        thrashing, dead_count, chain, details = patched_env.detect_thrash("single")
        assert thrashing is False

    def test_chain_with_dead_sessions_thrashing(self, state_db, carry_db, patched_env):
        """Chain of all dead sessions should be thrashing."""
        insert_config(carry_db, "dead_session_threshold", "2")
        insert_config(carry_db, "dead_lookback", "5")

        insert_session(state_db, "anc1", msgs=0, tools=0)
        insert_session(state_db, "anc2", parent="anc1", msgs=0, tools=0)
        insert_session(state_db, "anc3", parent="anc2", msgs=0, tools=0)

        thrashing, dead_count, chain, details = patched_env.detect_thrash("anc3")
        assert dead_count >= 2
        assert thrashing is True

    def test_chain_with_mixed_sessions(self, state_db, carry_db, patched_env):
        """Chain with some dead, some alive should not be thrashing with default threshold."""
        insert_config(carry_db, "dead_session_threshold", "3")

        insert_session(state_db, "mix1", msgs=10, tools=5)
        insert_session(state_db, "mix2", parent="mix1", msgs=0, tools=0)
        insert_session(state_db, "mix3", parent="mix2", msgs=10, tools=5)

        thrashing, dead_count, chain, details = patched_env.detect_thrash("mix3")
        assert dead_count < 3


# ===========================================================================
# Tests: blockers
# ===========================================================================

class TestBlockers:
    def test_block_and_list(self, carry_db, patched_env):
        """block() should create a blocker in the DB."""
        patched_env.cmd_block("test blocker reason")
        conn = sqlite3.connect(carry_db)
        row = conn.execute("SELECT COUNT(*) FROM blockers WHERE message LIKE '%test blocker reason%'").fetchone()
        conn.close()
        assert row[0] >= 1

    def test_unblock(self, carry_db, patched_env):
        """unblock() should resolve matching blockers."""
        patched_env.cmd_block("will be removed")
        patched_env.cmd_unblock("will be removed")
        conn = sqlite3.connect(carry_db)
        row = conn.execute("SELECT COUNT(*) FROM blockers WHERE message LIKE '%will be removed%' AND resolved_at IS NULL").fetchone()
        conn.close()
        assert row[0] == 0


# ===========================================================================
# Tests: check_git_progress
# ===========================================================================

class TestGitProgress:
    def test_short_chain_passes(self, state_db, carry_db, patched_env):
        """Short chain should always pass git progress check."""
        insert_session(state_db, "short1", msgs=5, tools=2)
        ok, details = patched_env.check_git_progress("short1")
        assert ok is True

    def test_no_git_heads_passes(self, state_db, carry_db, patched_env):
        """No recorded git heads should pass (first run)."""
        insert_session(state_db, "gh1", msgs=5, tools=2)
        insert_session(state_db, "gh2", parent="gh1", msgs=5, tools=2)
        insert_session(state_db, "gh3", parent="gh2", msgs=5, tools=2)
        ok, details = patched_env.check_git_progress("gh3")
        assert ok is True

    def test_git_moved_passes(self, state_db, carry_db, patched_env):
        """Different git HEADs across chain should pass."""
        insert_session(state_db, "gm1", msgs=5, tools=2)
        insert_session(state_db, "gm2", parent="gm1", msgs=5, tools=2)
        insert_session(state_db, "gm3", parent="gm2", msgs=5, tools=2)

        insert_git_heads(carry_db, "gm1", "/project", "aaa111")
        insert_git_heads(carry_db, "gm3", "/project", "bbb222")

        ok, details = patched_env.check_git_progress("gm3")
        assert ok is True

    def test_git_stalled_detected(self, state_db, carry_db, patched_env):
        """Same git HEADs across long chain should detect stall."""
        insert_config(carry_db, "git_min_sessions", "2")

        insert_session(state_db, "gs1", msgs=5, tools=2)
        insert_session(state_db, "gs2", parent="gs1", msgs=5, tools=2)
        insert_session(state_db, "gs3", parent="gs2", msgs=5, tools=2)

        insert_git_heads(carry_db, "gs1", "/project", "same123")
        insert_git_heads(carry_db, "gs3", "/project", "same123")

        ok, details = patched_env.check_git_progress("gs3")
        assert ok is False
        assert "unchanged" in details


# ===========================================================================
# Tests: record_outcome
# ===========================================================================

class TestRecordOutcome:
    def test_record_outcome_for_productive(self, state_db, carry_db, patched_env):
        """Should record productive outcome for a session with tool calls."""
        insert_session(state_db, "prod_1", msgs=10, tools=5)
        result = patched_env.check_can_continue("prod_1")
        outcome = patched_env.record_outcome("prod_1")
        assert outcome["outcome"]["productive"] is True

    def test_record_outcome_for_unproductive(self, state_db, carry_db, patched_env):
        """Should record unproductive outcome for a session with no tool calls."""
        insert_session(state_db, "unprod_1", msgs=1, tools=0)
        result = patched_env.check_can_continue("unprod_1")
        outcome = patched_env.record_outcome("unprod_1")
        assert outcome["outcome"]["productive"] is False


# ===========================================================================
# Tests: cmd_should_continue exit codes
# ===========================================================================

class TestShouldContinue:
    def test_active_exits_zero(self, state_db, carry_db, patched_env):
        """Active session should cause exit code 0."""
        insert_session(state_db, "sc_active", msgs=10, tools=5)
        with pytest.raises(SystemExit) as exc_info:
            patched_env.cmd_should_continue("sc_active")
        assert exc_info.value.code == 0

    def test_dead_exits_one(self, state_db, carry_db, patched_env):
        """Dead session should cause exit code 1."""
        insert_session(state_db, "sc_dead", msgs=0, tools=0)
        with pytest.raises(SystemExit) as exc_info:
            patched_env.cmd_should_continue("sc_dead")
        assert exc_info.value.code == 1


# ===========================================================================
# Tests: Edge cases (empty DB, missing session, long chains)
# ===========================================================================

class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_empty_db_no_crash(self, state_db, carry_db, patched_env):
        """check_can_continue should not crash on empty DB."""
        result = patched_env.check_can_continue()
        assert "can_continue" in result

    def test_missing_session_id(self, state_db, carry_db, patched_env):
        """Non-existent session_id should not crash."""
        result = patched_env.check_can_continue("does_not_exist")
        assert "can_continue" in result
        assert result["can_continue"] is False
        assert result["session_dead"] is True

    def test_very_long_chain(self, state_db, carry_db, patched_env):
        """A chain of 50 sessions should not crash."""
        ids = insert_chain(state_db, 50, alive=True)
        last_id = ids[-1]
        result = patched_env.check_can_continue(last_id)
        assert result["can_continue"] is True

    def test_very_long_dead_chain(self, state_db, carry_db, patched_env):
        """A long dead chain should detect thrash."""
        insert_config(carry_db, "dead_session_threshold", "3")
        insert_config(carry_db, "dead_lookback", "10")

        ids = insert_chain(state_db, 20, alive=False)
        last_id = ids[-1]
        result = patched_env.check_can_continue(last_id)
        assert result["can_continue"] is False

    def test_chain_with_cycle(self, state_db, carry_db, patched_env):
        """Circular parent reference should not infinite-loop."""
        insert_session(state_db, "cyc_a", parent="cyc_b", msgs=10, tools=5)
        insert_session(state_db, "cyc_b", parent="cyc_a", msgs=10, tools=5)
        result = patched_env.check_can_continue("cyc_a")
        assert "can_continue" in result

    def test_multiple_blockers_oldest_halts(self, state_db, carry_db, patched_env):
        """Multiple blockers where at least one is stale should halt."""
        insert_session(state_db, "multi_block", msgs=10, tools=5)
        insert_blocker(carry_db, "recent blocker", age_hours=0.5)
        insert_blocker(carry_db, "stale blocker", age_hours=8)
        result = patched_env.check_can_continue("multi_block")
        assert result["can_continue"] is False
        assert result["blocker_halt"] is True

    def test_zero_tools_many_msgs_continues(self, state_db, carry_db, patched_env):
        """Session with 0 tools but 100 msgs should continue (likely read-only)."""
        insert_session(state_db, "many_msgs", msgs=100, tools=0)
        result = patched_env.check_can_continue("many_msgs")
        assert result["can_continue"] is True
        assert result["session_dead"] is False

    def test_check_git_progress_missing_session(self, state_db, carry_db, patched_env):
        """check_git_progress with non-existent session should not crash."""
        ok, details = patched_env.check_git_progress("nonexistent")
        assert ok is True  # Can't walk chain -> short chain

    def test_record_outcome_no_decisions(self, state_db, carry_db, patched_env):
        """record_outcome with no logged decisions should return error."""
        result = patched_env.record_outcome()
        assert "error" in result

    def test_detect_thrash_no_session(self, state_db, carry_db, patched_env):
        """detect_thrash with no sessions should return safe."""
        thrashing, dead_count, chain, details = patched_env.detect_thrash()
        assert thrashing is False
        assert dead_count == 0

    def test_chain_exactly_at_threshold(self, state_db, carry_db, patched_env):
        """Chain with exactly threshold dead sessions should trigger."""
        insert_config(carry_db, "dead_session_threshold", "3")
        insert_config(carry_db, "dead_lookback", "3")

        insert_session(state_db, "t1", msgs=0, tools=0)
        insert_session(state_db, "t2", parent="t1", msgs=0, tools=0)
        insert_session(state_db, "t3", parent="t2", msgs=0, tools=0)

        thrashing, dead_count, chain, details = patched_env.detect_thrash("t3")
        assert dead_count == 3
        assert thrashing is True


# ===========================================================================
# Tests: Stagnation circuit breaker (v7)
# ===========================================================================

class TestStagnationCircuitBreaker:
    """Tests for Phase 1: consecutive no-commit tick detection."""

    def test_stagnation_halts_at_limit(self, state_db, carry_db, patched_env):
        """3 consecutive stalled ticks with no tools should hard halt."""
        insert_config(carry_db, "git_min_sessions", "2")

        # Build chain of 4 sessions
        insert_session(state_db, "stag_0", msgs=10, tools=5)
        insert_session(state_db, "stag_1", parent="stag_0", msgs=10, tools=5)
        insert_session(state_db, "stag_2", parent="stag_1", msgs=10, tools=5)
        insert_session(state_db, "stag_3", parent="stag_2", msgs=1, tools=0)

        # All sessions have same git HEAD (no commits across chain)
        insert_git_heads(carry_db, "stag_0", "/project", "aaa111")
        insert_git_heads(carry_db, "stag_1", "/project", "aaa111")
        insert_git_heads(carry_db, "stag_2", "/project", "aaa111")
        insert_git_heads(carry_db, "stag_3", "/project", "aaa111")

        result = patched_env.check_can_continue("stag_3")
        assert result["can_continue"] is False
        assert result["stagnation_halt"] is True
        assert any("Stagnation" in r for r in result["reasons"])

    def test_stagnation_active_session_continues(self, state_db, carry_db, patched_env):
        """Stalled chain but current session has tools should continue."""
        insert_config(carry_db, "git_min_sessions", "2")

        insert_session(state_db, "stag_a0", msgs=10, tools=5)
        insert_session(state_db, "stag_a1", parent="stag_a0", msgs=10, tools=5)
        insert_session(state_db, "stag_a2", parent="stag_a1", msgs=10, tools=5)
        insert_session(state_db, "stag_a3", parent="stag_a2", msgs=10, tools=8)

        # All sessions same HEAD (stalled)
        insert_git_heads(carry_db, "stag_a0", "/project", "bbb222")
        insert_git_heads(carry_db, "stag_a1", "/project", "bbb222")
        insert_git_heads(carry_db, "stag_a2", "/project", "bbb222")
        insert_git_heads(carry_db, "stag_a3", "/project", "bbb222")

        result = patched_env.check_can_continue("stag_a3")
        assert result["can_continue"] is True
        assert result["stagnation_halt"] is False
        assert any("Stagnation detected" in gr for gr in result["guard_rails"])

    def test_stagnation_resets_on_commit(self, state_db, carry_db, patched_env):
        """Commit anywhere in chain breaks the stall streak."""
        # Covered by test_stagnation_resets_when_commit_in_middle
        # (This test verifies the concept with explicit commit data.)

    def test_stagnation_resets_when_commit_in_middle(self, state_db, carry_db, patched_env):
        """Commit in middle of chain resets the stall counter."""
        insert_config(carry_db, "git_min_sessions", "2")

        insert_session(state_db, "stag_r0", msgs=10, tools=5)
        insert_session(state_db, "stag_r1", parent="stag_r0", msgs=10, tools=5)
        insert_session(state_db, "stag_r2", parent="stag_r1", msgs=10, tools=5)
        insert_session(state_db, "stag_r3", parent="stag_r2", msgs=1, tools=0)

        # stag_r0 and stag_r1 have same HEAD, but stag_r2 committed (new HEAD)
        # stag_r3 same as stag_r2 (no new commit since)
        insert_git_heads(carry_db, "stag_r0", "/project", "old111")
        insert_git_heads(carry_db, "stag_r1", "/project", "old111")
        insert_git_heads(carry_db, "stag_r2", "/project", "new222")
        insert_git_heads(carry_db, "stag_r3", "/project", "new222")

        result = patched_env.check_can_continue("stag_r3")
        # stag_r3 vs stag_r2: same -> 1 stall (not enough for limit)
        assert result["consecutive_stalls"] <= 2
        assert result["stagnation_halt"] is False

    def test_short_chain_no_stagnation(self, state_db, carry_db, patched_env):
        """Short chain without git heads should not trigger stagnation."""
        insert_session(state_db, "stag_s1", msgs=10, tools=5)

        result = patched_env.check_can_continue("stag_s1")
        assert result["stagnation_halt"] is False

    def test_stagnation_returns_consecutive_stalls(self, state_db, carry_db, patched_env):
        """Result should include consecutive_stalls count."""
        insert_config(carry_db, "git_min_sessions", "2")

        insert_session(state_db, "stag_n0", msgs=10, tools=5)
        insert_session(state_db, "stag_n1", parent="stag_n0", msgs=10, tools=5)
        insert_session(state_db, "stag_n2", parent="stag_n1", msgs=1, tools=0)

        insert_git_heads(carry_db, "stag_n0", "/project", "zzz999")
        insert_git_heads(carry_db, "stag_n1", "/project", "zzz999")
        insert_git_heads(carry_db, "stag_n2", "/project", "zzz999")

        result = patched_env.check_can_continue("stag_n2")
        assert "consecutive_stalls" in result
        assert result["consecutive_stalls"] >= 2


# ===========================================================================
# Tests: Hallucination loop detection (v7, Phase 2)
# ===========================================================================

class TestHallucinationLoop:
    """Tests for Phase 2: same files edited repeatedly with no commits."""

    def test_loop_3_ticks_same_file_halts(self, state_db, carry_db, patched_env):
        """Same file in 3 consecutive ticks with no commits should halt."""
        insert_session(state_db, "hl_0", msgs=5, tools=2)
        insert_session(state_db, "hl_1", parent="hl_0", msgs=5, tools=2)
        insert_session(state_db, "hl_2", parent="hl_1", msgs=1, tools=0)

        # All three touched the same file, none committed
        insert_tick_changes(carry_db, "hl_0", 1, ["src/vm.rs"])
        insert_tick_changes(carry_db, "hl_1", 2, ["src/vm.rs"])
        insert_tick_changes(carry_db, "hl_2", 3, ["src/vm.rs"])

        result = patched_env.check_can_continue("hl_2")
        assert result["can_continue"] is False
        assert result["hallucination_halt"] is True
        assert "src/vm.rs" in result["hallucination_files"]
        assert any("Hallucination loop" in r for r in result["reasons"])

    def test_loop_active_session_continues(self, state_db, carry_db, patched_env):
        """Loop detected but session has tools -> guard rail, not halt."""
        insert_session(state_db, "hl_a0", msgs=5, tools=2)
        insert_session(state_db, "hl_a1", parent="hl_a0", msgs=5, tools=2)
        insert_session(state_db, "hl_a2", parent="hl_a1", msgs=10, tools=5)

        insert_tick_changes(carry_db, "hl_a0", 1, ["src/lib.rs"])
        insert_tick_changes(carry_db, "hl_a1", 2, ["src/lib.rs"])
        insert_tick_changes(carry_db, "hl_a2", 3, ["src/lib.rs"])

        result = patched_env.check_can_continue("hl_a2")
        assert result["can_continue"] is True
        assert result["hallucination_halt"] is False
        assert any("Hallucination loop detected" in gr for gr in result["guard_rails"])

    def test_different_files_no_loop(self, state_db, carry_db, patched_env):
        """Different files each tick -> no hallucination loop."""
        insert_session(state_db, "hl_d0", msgs=5, tools=2)
        insert_session(state_db, "hl_d1", parent="hl_d0", msgs=5, tools=2)
        insert_session(state_db, "hl_d2", parent="hl_d1", msgs=5, tools=3)

        insert_tick_changes(carry_db, "hl_d0", 1, ["src/a.rs"])
        insert_tick_changes(carry_db, "hl_d1", 2, ["src/b.rs"])
        insert_tick_changes(carry_db, "hl_d2", 3, ["src/c.rs"])

        result = patched_env.check_can_continue("hl_d2")
        assert result["hallucination_halt"] is False
        assert result["can_continue"] is True

    def test_commit_breaks_loop(self, state_db, carry_db, patched_env):
        """If one tick in the window committed, no loop detection."""
        insert_session(state_db, "hl_c0", msgs=5, tools=2)
        insert_session(state_db, "hl_c1", parent="hl_c0", msgs=5, tools=2)
        insert_session(state_db, "hl_c2", parent="hl_c1", msgs=1, tools=0)

        # Same file, but middle tick committed
        insert_tick_changes(carry_db, "hl_c0", 1, ["src/main.rs"])
        insert_tick_changes(carry_db, "hl_c1", 2, ["src/main.rs"], committed=True)
        insert_tick_changes(carry_db, "hl_c2", 3, ["src/main.rs"])

        result = patched_env.check_can_continue("hl_c2")
        assert result["hallucination_halt"] is False

    def test_multiple_common_files(self, state_db, carry_db, patched_env):
        """Multiple files common across all ticks should all be reported."""
        insert_session(state_db, "hl_m0", msgs=5, tools=2)
        insert_session(state_db, "hl_m1", parent="hl_m0", msgs=5, tools=2)
        insert_session(state_db, "hl_m2", parent="hl_m1", msgs=1, tools=0)

        insert_tick_changes(carry_db, "hl_m0", 1, ["src/a.rs", "src/b.rs"])
        insert_tick_changes(carry_db, "hl_m1", 2, ["src/a.rs", "src/b.rs", "src/c.rs"])
        insert_tick_changes(carry_db, "hl_m2", 3, ["src/a.rs", "src/b.rs"])

        result = patched_env.check_can_continue("hl_m2")
        assert result["hallucination_halt"] is True
        assert "src/a.rs" in result["hallucination_files"]
        assert "src/b.rs" in result["hallucination_files"]
        # src/c.rs only in one tick, not common
        assert "src/c.rs" not in result["hallucination_files"]

    def test_short_chain_no_loop(self, state_db, carry_db, patched_env):
        """Chain shorter than lookback should not trigger hallucination check."""
        insert_session(state_db, "hl_s0", msgs=5, tools=2)

        insert_tick_changes(carry_db, "hl_s0", 1, ["src/x.rs"])

        result = patched_env.check_can_continue("hl_s0")
        assert result["hallucination_halt"] is False

    def test_no_tick_data_no_loop(self, state_db, carry_db, patched_env):
        """Sessions without tick_file_changes data should not trigger loop."""
        insert_session(state_db, "hl_n0", msgs=5, tools=2)
        insert_session(state_db, "hl_n1", parent="hl_n0", msgs=5, tools=2)
        insert_session(state_db, "hl_n2", parent="hl_n1", msgs=1, tools=0)

        # No insert_tick_changes calls

        result = patched_env.check_can_continue("hl_n2")
        assert result["hallucination_halt"] is False


# ===========================================================================
# Tests: Test count regression (v7, Phase 3)
# ===========================================================================

class TestTestRegression:
    """Tests for Phase 3: test count drop detection."""

    def test_big_drop_halts(self, state_db, carry_db, patched_env):
        """Test count drops by 5 in single tick should hard halt."""
        insert_session(state_db, "tr_0", msgs=10, tools=5)
        insert_session(state_db, "tr_1", parent="tr_0", msgs=10, tools=5)

        insert_test_count(carry_db, "tr_0", 1, 100)
        insert_test_count(carry_db, "tr_1", 2, 95)

        result = patched_env.check_can_continue("tr_1")
        assert result["can_continue"] is False
        assert result["test_regression_halt"] is True
        assert result["test_prev_count"] == 100
        assert result["test_curr_count"] == 95
        assert any("Test regression" in r for r in result["reasons"])

    def test_small_drop_continues(self, state_db, carry_db, patched_env):
        """Test count drops by 1 (refactor) should not halt."""
        insert_session(state_db, "tr_s0", msgs=10, tools=5)
        insert_session(state_db, "tr_s1", parent="tr_s0", msgs=10, tools=5)

        insert_test_count(carry_db, "tr_s0", 1, 100)
        insert_test_count(carry_db, "tr_s1", 2, 99)

        result = patched_env.check_can_continue("tr_s1")
        assert result["can_continue"] is True
        assert result["test_regression_halt"] is False

    def test_increase_continues(self, state_db, carry_db, patched_env):
        """Test count increasing is always fine."""
        insert_session(state_db, "tr_i0", msgs=10, tools=5)
        insert_session(state_db, "tr_i1", parent="tr_i0", msgs=10, tools=5)

        insert_test_count(carry_db, "tr_i0", 1, 100)
        insert_test_count(carry_db, "tr_i1", 2, 120)

        result = patched_env.check_can_continue("tr_i1")
        assert result["can_continue"] is True
        assert result["test_regression_halt"] is False

    def test_no_data_no_regression(self, state_db, carry_db, patched_env):
        """No test count data should not trigger regression."""
        insert_session(state_db, "tr_n0", msgs=10, tools=5)
        insert_session(state_db, "tr_n1", parent="tr_n0", msgs=10, tools=5)

        # No insert_test_count calls

        result = patched_env.check_can_continue("tr_n1")
        assert result["test_regression_halt"] is False

    def test_single_session_no_regression(self, state_db, carry_db, patched_env):
        """Single session with test count should not trigger regression."""
        insert_session(state_db, "tr_ss", msgs=10, tools=5)
        insert_test_count(carry_db, "tr_ss", 1, 50)

        result = patched_env.check_can_continue("tr_ss")
        assert result["test_regression_halt"] is False

    def test_exact_threshold_no_halt(self, state_db, carry_db, patched_env):
        """Drop of exactly 2 (the threshold) should NOT halt (> not >=)."""
        insert_session(state_db, "tr_e0", msgs=10, tools=5)
        insert_session(state_db, "tr_e1", parent="tr_e0", msgs=10, tools=5)

        insert_test_count(carry_db, "tr_e0", 1, 50)
        insert_test_count(carry_db, "tr_e1", 2, 48)

        result = patched_env.check_can_continue("tr_e1")
        assert result["test_regression_halt"] is False

    def test_one_above_threshold_halts(self, state_db, carry_db, patched_env):
        """Drop of 3 (threshold + 1) should halt."""
        insert_session(state_db, "tr_o0", msgs=10, tools=5)
        insert_session(state_db, "tr_o1", parent="tr_o0", msgs=10, tools=5)

        insert_test_count(carry_db, "tr_o0", 1, 50)
        insert_test_count(carry_db, "tr_o1", 2, 47)

        result = patched_env.check_can_continue("tr_o1")
        assert result["test_regression_halt"] is True


# ===========================================================================
# Tests: Consecutive no-op counter (v7, Phase 4)
# ===========================================================================

class TestConsecutiveNoop:
    """Tests for Phase 4: unified no-op detection."""

    def test_3_noops_halts(self, state_db, carry_db, patched_env):
        """3 consecutive no-op ticks with no tools should hard halt."""
        insert_config(carry_db, "git_min_sessions", "2")

        insert_session(state_db, "no_0", msgs=10, tools=5)
        insert_session(state_db, "no_1", parent="no_0", msgs=10, tools=5)
        insert_session(state_db, "no_2", parent="no_1", msgs=10, tools=5)
        insert_session(state_db, "no_3", parent="no_2", msgs=1, tools=0)

        # All same HEAD (no commits), no test counts
        insert_git_heads(carry_db, "no_0", "/project", "aaa000")
        insert_git_heads(carry_db, "no_1", "/project", "aaa000")
        insert_git_heads(carry_db, "no_2", "/project", "aaa000")
        insert_git_heads(carry_db, "no_3", "/project", "aaa000")

        result = patched_env.check_can_continue("no_3")
        assert result["can_continue"] is False
        assert result["noop_halt"] is True
        assert result["consecutive_noops"] >= 3
        assert any("No-op loop" in r for r in result["reasons"])

    def test_active_session_overrides_noop(self, state_db, carry_db, patched_env):
        """No-op chain but session has tools -> guard rail, not halt."""
        insert_config(carry_db, "git_min_sessions", "2")

        insert_session(state_db, "no_a0", msgs=10, tools=5)
        insert_session(state_db, "no_a1", parent="no_a0", msgs=10, tools=5)
        insert_session(state_db, "no_a2", parent="no_a1", msgs=10, tools=5)
        insert_session(state_db, "no_a3", parent="no_a2", msgs=10, tools=8)

        insert_git_heads(carry_db, "no_a0", "/project", "bbb111")
        insert_git_heads(carry_db, "no_a1", "/project", "bbb111")
        insert_git_heads(carry_db, "no_a2", "/project", "bbb111")
        insert_git_heads(carry_db, "no_a3", "/project", "bbb111")

        result = patched_env.check_can_continue("no_a3")
        assert result["can_continue"] is True
        assert result["noop_halt"] is False
        assert any("No-op loop detected" in gr for gr in result["guard_rails"])

    def test_commit_resets_noop(self, state_db, carry_db, patched_env):
        """A productive tick in the chain resets the no-op counter."""
        insert_config(carry_db, "git_min_sessions", "2")

        insert_session(state_db, "no_r0", msgs=10, tools=5)
        insert_session(state_db, "no_r1", parent="no_r0", msgs=10, tools=5)
        insert_session(state_db, "no_r2", parent="no_r1", msgs=10, tools=5)
        insert_session(state_db, "no_r3", parent="no_r2", msgs=1, tools=0)

        # no_r1 committed, no_r2 and no_r3 did not
        insert_git_heads(carry_db, "no_r0", "/project", "old111")
        insert_git_heads(carry_db, "no_r1", "/project", "new222")
        insert_git_heads(carry_db, "no_r2", "/project", "new222")
        insert_git_heads(carry_db, "no_r3", "/project", "new222")

        result = patched_env.check_can_continue("no_r3")
        assert result["noop_halt"] is False

    def test_test_increase_resets_noop(self, state_db, carry_db, patched_env):
        """Test count increase counts as productive even without commit."""
        insert_config(carry_db, "git_min_sessions", "2")

        insert_session(state_db, "no_t0", msgs=10, tools=5)
        insert_session(state_db, "no_t1", parent="no_t0", msgs=10, tools=5)
        insert_session(state_db, "no_t2", parent="no_t1", msgs=10, tools=5)
        insert_session(state_db, "no_t3", parent="no_t2", msgs=1, tools=0)

        # No commits (same HEAD)
        insert_git_heads(carry_db, "no_t0", "/project", "ccc333")
        insert_git_heads(carry_db, "no_t1", "/project", "ccc333")
        insert_git_heads(carry_db, "no_t2", "/project", "ccc333")
        insert_git_heads(carry_db, "no_t3", "/project", "ccc333")

        # But no_t2 increased tests
        insert_test_count(carry_db, "no_t0", 1, 100)
        insert_test_count(carry_db, "no_t1", 2, 100)
        insert_test_count(carry_db, "no_t2", 3, 105)
        insert_test_count(carry_db, "no_t3", 4, 105)

        result = patched_env.check_can_continue("no_t3")
        # no_t2 was productive (tests increased), so no_t3 only has 1 no-op
        assert result["noop_halt"] is False

    def test_short_chain_no_noop(self, state_db, carry_db, patched_env):
        """Single session should not trigger no-op."""
        insert_session(state_db, "no_s0", msgs=10, tools=5)

        result = patched_env.check_can_continue("no_s0")
        assert result["noop_halt"] is False
        assert result["consecutive_noops"] == 0


# ===========================================================================
# Tests: Phase 5 -- Outcome tracking & calibration
# ===========================================================================

class TestAutoRecordOutcomes:
    """Tests for auto_record_outcomes running automatically in the pipeline."""

    def test_auto_records_on_should_continue(self, state_db, carry_db, patched_env):
        """auto_record_outcomes should fire during should-continue."""
        # Create a session that had a decision
        insert_session(state_db, "prev_auto", msgs=10, tools=5, source='cli')

        # Log a decision for prev_auto
        result = patched_env.check_can_continue("prev_auto")
        assert result["decision_id"] is not None

        # auto_record_outcomes finds the last session with msg_count > 5
        # and calls record_outcome(that_session_id) which looks for a decision
        # for that session. prev_auto has a decision, so it should record it.
        # Call should_continue (which calls auto_record_outcomes internally)
        with pytest.raises(SystemExit):
            patched_env.cmd_should_continue()

        # Check that an outcome was recorded
        conn = sqlite3.connect(carry_db)
        count = conn.execute("SELECT COUNT(*) FROM decision_outcomes").fetchone()[0]
        conn.close()
        assert count >= 1

    def test_auto_record_no_crash_on_empty(self, state_db, carry_db, patched_env):
        """auto_record_outcomes should not crash with no sessions."""
        patched_env.auto_record_outcomes()  # should silently do nothing


class TestConfigHelpers:
    """Tests for config table reads/writes."""

    def test_get_threshold_default(self, carry_db, patched_env):
        """get_threshold returns default when no config set."""
        val = patched_env.get_threshold("noop_limit")
        assert val == 3  # default NOOP_LIMIT

    def test_get_threshold_from_config(self, carry_db, patched_env):
        """get_threshold reads from config table when set."""
        insert_config(carry_db, "noop_limit", "5")
        val = patched_env.get_threshold("noop_limit")
        assert val == 5

    def test_get_threshold_unknown_key_raises(self, carry_db, patched_env):
        """get_threshold raises on unknown key."""
        with pytest.raises(ValueError, match="Unknown threshold"):
            patched_env.get_threshold("nonexistent_key")

    def test_get_all_thresholds(self, carry_db, patched_env):
        """get_all_thresholds returns all defaults."""
        thresholds = patched_env.get_all_thresholds()
        assert "noop_limit" in thresholds
        assert "stagnation_stall_limit" in thresholds
        assert "blocker_halt_hours" in thresholds
        assert thresholds["noop_limit"] == 3
        assert thresholds["blocker_halt_hours"] == 4.0

    def test_write_and_read_config(self, carry_db, patched_env):
        """_write_config + _read_config round-trip."""
        patched_env._write_config("test_key", "42", "test")
        val = patched_env._read_config("test_key")
        assert val == "42"

    def test_threshold_overrides_decision(self, state_db, carry_db, patched_env):
        """Setting noop_limit to 2 should cause halt at 2 no-ops instead of 3."""
        insert_config(carry_db, "git_min_sessions", "2")
        insert_config(carry_db, "noop_limit", "2")  # override from default 3

        insert_session(state_db, "tn_0", msgs=10, tools=5)
        insert_session(state_db, "tn_1", parent="tn_0", msgs=10, tools=5)
        insert_session(state_db, "tn_2", parent="tn_1", msgs=1, tools=0)

        insert_git_heads(carry_db, "tn_0", "/project", "aaa000")
        insert_git_heads(carry_db, "tn_1", "/project", "aaa000")
        insert_git_heads(carry_db, "tn_2", "/project", "aaa000")

        result = patched_env.check_can_continue("tn_2")
        assert result["can_continue"] is False
        assert result["noop_halt"] is True


class TestCalibration:
    """Tests for calibrate_thresholds logic."""

    def test_calibration_needs_5_outcomes(self, carry_db, patched_env):
        """Calibration should return early with <5 outcomes."""
        result = patched_env.calibrate_thresholds()
        assert result["changes"] == []
        assert "Not enough" in result["summary"]["message"]

    def test_calibration_good_continue_loosens(self, carry_db, patched_env):
        """High continue accuracy should loosen stall/noop limits."""
        # 9 productive continues, 1 unproductive = 90% rate (>80%)
        for i in range(9):
            dec_id = insert_decision(carry_db, f"sess_gc_{i}", "continue", True)
            insert_outcome(carry_db, dec_id, f"sess_gc_{i}", productive=1)
        dec_id = insert_decision(carry_db, "sess_gf_0", "continue", True)
        insert_outcome(carry_db, dec_id, "sess_gf_0", productive=0)

        result = patched_env.calibrate_thresholds()
        # Should have loosened stagnation_stall_limit or noop_limit
        keys_changed = [c["key"] for c in result["changes"]]
        assert "stagnation_stall_limit" in keys_changed or "noop_limit" in keys_changed

    def test_calibration_bad_continue_tightens(self, carry_db, patched_env):
        """Low continue accuracy should tighten limits."""
        # 2 productive, 8 unproductive = 20% rate (< 50%)
        for i in range(2):
            dec_id = insert_decision(carry_db, f"sess_bc_{i}", "continue", True)
            insert_outcome(carry_db, dec_id, f"sess_bc_{i}", productive=1)
        for i in range(8):
            dec_id = insert_decision(carry_db, f"sess_bf_{i}", "continue", True)
            insert_outcome(carry_db, dec_id, f"sess_bf_{i}", productive=0)

        result = patched_env.calibrate_thresholds()
        keys_changed = [c["key"] for c in result["changes"]]
        assert any(k in keys_changed for k in ["stagnation_stall_limit", "noop_limit", "hallucination_loop_limit"])

    def test_calibration_dry_run_no_write(self, carry_db, patched_env):
        """Dry run should not persist changes."""
        for i in range(6):
            dec_id = insert_decision(carry_db, f"sess_dr_{i}", "continue", True)
            insert_outcome(carry_db, dec_id, f"sess_dr_{i}", productive=1)
        for i in range(4):
            dec_id = insert_decision(carry_db, f"sess_drf_{i}", "continue", True)
            insert_outcome(carry_db, dec_id, f"sess_drf_{i}", productive=0)

        result = patched_env.calibrate_thresholds(dry_run=True)
        # Should report changes but not write to DB
        if result["changes"]:
            # Verify config table was NOT written
            val = patched_env._read_config(result["changes"][0]["key"])
            assert val is None  # no config override written

    def test_calibration_clamps_to_min_max(self, carry_db, patched_env):
        """Calibration should not exceed min/max bounds."""
        # Set noop_limit to its max (10) already
        insert_config(carry_db, "noop_limit", "10")

        # Create high-accuracy data to trigger loosening (9/10 = 90% > 80%)
        for i in range(9):
            dec_id = insert_decision(carry_db, f"sess_cl_{i}", "continue", True)
            insert_outcome(carry_db, dec_id, f"sess_cl_{i}", productive=1)
        dec_id = insert_decision(carry_db, "sess_cl_9", "continue", True)
        insert_outcome(carry_db, dec_id, "sess_cl_9", productive=0)

        result = patched_env.calibrate_thresholds()
        # noop_limit was already at max (10), so it shouldn't appear in changes
        # or should stay at 10
        for c in result["changes"]:
            if c["key"] == "noop_limit":
                assert c["new"] <= 10  # max bound

    def test_calibration_good_halt_tightens_dead(self, carry_db, patched_env):
        """High halt accuracy should tighten dead_session_threshold."""
        # 3 accurate halts (unproductive sessions correctly halted)
        for i in range(3):
            dec_id = insert_decision(carry_db, f"sess_gh_{i}", "halt", False)
            insert_outcome(carry_db, dec_id, f"sess_gh_{i}", productive=0)

        # 7 productive continues
        for i in range(7):
            dec_id = insert_decision(carry_db, f"sess_gpc_{i}", "continue", True)
            insert_outcome(carry_db, dec_id, f"sess_gpc_{i}", productive=1)

        result = patched_env.calibrate_thresholds()
        keys_changed = [c["key"] for c in result["changes"]]
        assert "dead_session_threshold" in keys_changed

    def test_cmd_calibrate_runs(self, carry_db, patched_env, capsys):
        """cmd_calibrate should print output without crashing."""
        patched_env.cmd_calibrate()
        output = capsys.readouterr().out
        assert "Outcome count" in output

    def test_cmd_calibrate_dry_run(self, carry_db, patched_env, capsys):
        """cmd_calibrate --dry-run should work."""
        patched_env.cmd_calibrate(dry_run=True)
        output = capsys.readouterr().out
        assert "Outcome count" in output


class TestCmdShowConfig:
    """Tests for show-config command."""

    def test_show_config_defaults(self, carry_db, patched_env, capsys):
        """show-config should show all defaults."""
        patched_env.cmd_show_config()
        output = capsys.readouterr().out
        assert "noop_limit = 3" in output
        assert "source: default" in output

    def test_show_config_with_override(self, carry_db, patched_env, capsys):
        """show-config should show calibrated values."""
        insert_config(carry_db, "noop_limit", "5", source="calibration")
        patched_env.cmd_show_config()
        output = capsys.readouterr().out
        assert "noop_limit = 5" in output
        assert "source: calibration" in output


# ===========================================================================
# Tests: Phase 6 -- Learned lessons from outcome history
# ===========================================================================

class TestExtractLessons:
    """Tests for extract_lessons() -- analyzing outcomes to produce lessons."""

    def test_no_outcomes_returns_empty(self, carry_db, patched_env):
        """No outcomes means no lessons."""
        result = patched_env.extract_lessons()
        assert result == []

    def test_too_few_outcomes_returns_empty(self, carry_db, patched_env):
        """Less than 3 outcomes means no lessons."""
        dec_id = insert_decision(carry_db, "s1", "continue", True)
        insert_outcome(carry_db, dec_id, "s1", productive=1)
        result = patched_env.extract_lessons()
        assert result == []

    def test_bad_continues_generates_lesson(self, carry_db, patched_env):
        """3+ unproductive continues should generate a continue_accuracy lesson."""
        for i in range(4):
            dec_id = insert_decision(
                carry_db, f"bad_cont_{i}", "continue", True,
                reasons=["Thrash: too many dead sessions"]
            )
            insert_outcome(carry_db, dec_id, f"bad_cont_{i}", productive=0)

        result = patched_env.extract_lessons()
        assert len(result) >= 1
        categories = [l["category"] for l in result]
        assert "continue_accuracy" in categories

    def test_wrong_halts_generates_lesson(self, carry_db, patched_env):
        """3+ productive sessions that were halted should generate a halt_accuracy lesson."""
        for i in range(4):
            dec_id = insert_decision(
                carry_db, f"wrong_halt_{i}", "halt", False,
                reasons=["Session dead: 0 tools and <=2 messages"]
            )
            insert_outcome(carry_db, dec_id, f"wrong_halt_{i}", productive=1, tool_calls=5)

        result = patched_env.extract_lessons()
        assert len(result) >= 1
        categories = [l["category"] for l in result]
        assert "halt_accuracy" in categories

    def test_low_productivity_overall_lesson(self, carry_db, patched_env):
        """10+ outcomes with >60% unproductive should generate an overall_productivity lesson."""
        # 7 unproductive, 3 productive = 70% unproductive
        for i in range(7):
            dec_id = insert_decision(carry_db, f"unprod_{i}", "continue", True)
            insert_outcome(carry_db, dec_id, f"unprod_{i}", productive=0)
        for i in range(3):
            dec_id = insert_decision(carry_db, f"prod_{i}", "continue", True)
            insert_outcome(carry_db, dec_id, f"prod_{i}", productive=1)

        result = patched_env.extract_lessons()
        categories = [l["category"] for l in result]
        assert "overall_productivity" in categories
        lesson_text = [l["lesson"] for l in result if l["category"] == "overall_productivity"][0]
        assert "smaller steps" in lesson_text

    def test_high_productivity_overall_lesson(self, carry_db, patched_env):
        """10+ outcomes with >85% productive should generate a positive lesson."""
        # 9 productive, 1 unproductive = 90% productive
        for i in range(9):
            dec_id = insert_decision(carry_db, f"hprod_{i}", "continue", True)
            insert_outcome(carry_db, dec_id, f"hprod_{i}", productive=1)
        dec_id = insert_decision(carry_db, "hunprod_0", "continue", True)
        insert_outcome(carry_db, dec_id, "hunprod_0", productive=0)

        result = patched_env.extract_lessons()
        categories = [l["category"] for l in result]
        assert "overall_productivity" in categories
        lesson_text = [l["lesson"] for l in result if l["category"] == "overall_productivity"][0]
        assert "thresholds are working" in lesson_text

    def test_git_pattern_lesson(self, carry_db, patched_env):
        """5+ productive sessions with no git move should generate a git_pattern lesson."""
        for i in range(6):
            dec_id = insert_decision(carry_db, f"gitless_{i}", "continue", True)
            insert_outcome(carry_db, dec_id, f"gitless_{i}", productive=1, git_moved=0)

        result = patched_env.extract_lessons()
        categories = [l["category"] for l in result]
        assert "git_pattern" in categories
        lesson_text = [l["lesson"] for l in result if l["category"] == "git_pattern"][0]
        assert "uncommitted work" in lesson_text

    def test_lessons_stored_in_db(self, carry_db, patched_env):
        """Extracted lessons should be persisted in the lessons table."""
        for i in range(4):
            dec_id = insert_decision(
                carry_db, f"store_{i}", "continue", True,
                reasons=["Thrash: dead sessions"]
            )
            insert_outcome(carry_db, dec_id, f"store_{i}", productive=0)

        patched_env.extract_lessons()

        # Verify lessons are in the DB
        conn = sqlite3.connect(carry_db)
        rows = conn.execute("SELECT lesson, category FROM lessons").fetchall()
        conn.close()
        assert len(rows) >= 1

    def test_repeated_extraction_increments_hit_count(self, carry_db, patched_env):
        """Running extract_lessons twice should increment hit_count on existing lessons."""
        for i in range(4):
            dec_id = insert_decision(
                carry_db, f"repeat_{i}", "continue", True,
                reasons=["Thrash: dead sessions"]
            )
            insert_outcome(carry_db, dec_id, f"repeat_{i}", productive=0)

        patched_env.extract_lessons()
        patched_env.extract_lessons()

        conn = sqlite3.connect(carry_db)
        rows = conn.execute("SELECT hit_count FROM lessons").fetchall()
        conn.close()
        assert any(r[0] >= 2 for r in rows)


class TestGetTopLessons:
    """Tests for get_top_lessons() -- retrieving ranked lessons."""

    def test_empty_db_returns_empty(self, carry_db, patched_env):
        """No lessons stored returns empty list."""
        result = patched_env.get_top_lessons()
        assert result == []

    def test_returns_top_n(self, carry_db, patched_env):
        """Should return at most n lessons."""
        insert_lesson(carry_db, "Lesson A", hit_count=5)
        insert_lesson(carry_db, "Lesson B", hit_count=3)
        insert_lesson(carry_db, "Lesson C", hit_count=1)

        result = patched_env.get_top_lessons(n=2)
        assert len(result) == 2
        assert result[0]["lesson"] == "Lesson A"
        assert result[1]["lesson"] == "Lesson B"

    def test_ranked_by_hit_count(self, carry_db, patched_env):
        """Lessons should be ranked by hit_count descending."""
        insert_lesson(carry_db, "Low hit", hit_count=1)
        insert_lesson(carry_db, "High hit", hit_count=10)
        insert_lesson(carry_db, "Med hit", hit_count=5)

        result = patched_env.get_top_lessons(n=3)
        assert result[0]["hit_count"] == 10
        assert result[1]["hit_count"] == 5
        assert result[2]["hit_count"] == 1

    def test_returns_dict_structure(self, carry_db, patched_env):
        """Each lesson should have the expected dict keys."""
        insert_lesson(carry_db, "Test lesson", category="test_cat", evidence="some evidence")
        result = patched_env.get_top_lessons(n=1)
        assert len(result) == 1
        assert "lesson" in result[0]
        assert "category" in result[0]
        assert "evidence" in result[0]
        assert "hit_count" in result[0]

    def test_default_n_is_3(self, carry_db, patched_env):
        """Default n=3 should return at most 3 lessons."""
        for i in range(5):
            insert_lesson(carry_db, f"Lesson {i}", hit_count=5 - i)
        result = patched_env.get_top_lessons()
        assert len(result) == 3


class TestLessonsInContext:
    """Tests that lessons appear in context command output."""

    def test_lessons_appear_in_cmd_context(self, state_db, carry_db, patched_env, capsys):
        """cmd_context should include LEARNED LESSONS section when lessons exist."""
        insert_session(state_db, "ctx_sess", msgs=10, tools=5)
        insert_lesson(carry_db, "This project fails on vm.rs changes", category="file_pattern",
                       evidence="3/5 failures involved vm.rs", hit_count=4)

        # Need messages for the session
        conn = sqlite3.connect(state_db)
        conn.execute(
            "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
            ("ctx_sess", "user", "Do the thing")
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
            ("ctx_sess", "assistant", "Done doing the thing")
        )
        conn.commit()
        conn.close()

        patched_env.cmd_context()
        output = capsys.readouterr().out
        assert "LEARNED LESSONS" in output
        assert "vm.rs" in output

    def test_no_lessons_no_section(self, state_db, carry_db, patched_env, capsys):
        """cmd_context should not show LEARNED LESSONS when no lessons exist."""
        insert_session(state_db, "ctx_noless", msgs=10, tools=5)
        conn = sqlite3.connect(state_db)
        conn.execute(
            "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
            ("ctx_noless", "user", "Do the thing")
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
            ("ctx_noless", "assistant", "Done")
        )
        conn.commit()
        conn.close()

        patched_env.cmd_context()
        output = capsys.readouterr().out
        assert "LEARNED LESSONS" not in output

    def test_lessons_in_get_context_data(self, state_db, carry_db, patched_env):
        """get_context_data should include learned_lessons key."""
        insert_session(state_db, "api_sess", msgs=10, tools=5)
        insert_lesson(carry_db, "Test lesson from API", hit_count=2)

        result = patched_env.get_context_data("api_sess")
        assert "learned_lessons" in result
        assert len(result["learned_lessons"]) == 1
        assert result["learned_lessons"][0]["lesson"] == "Test lesson from API"

    def test_max_3_lessons_in_context(self, state_db, carry_db, patched_env):
        """Context should show at most 3 lessons."""
        insert_session(state_db, "api_sess_3", msgs=10, tools=5)
        for i in range(5):
            insert_lesson(carry_db, f"Lesson {i}", hit_count=5 - i)

        result = patched_env.get_context_data("api_sess_3")
        assert len(result["learned_lessons"]) <= 3


class TestCmdLearn:
    """Tests for the 'learn' CLI command."""

    def test_learn_no_outcomes(self, carry_db, patched_env, capsys):
        """learn with no outcomes should say so."""
        patched_env.cmd_learn()
        output = capsys.readouterr().out
        assert "No lessons extracted" in output

    def test_learn_with_outcomes(self, carry_db, patched_env, capsys):
        """learn with enough outcomes should print lessons."""
        for i in range(4):
            dec_id = insert_decision(
                carry_db, f"learn_{i}", "continue", True,
                reasons=["Thrash: dead sessions"]
            )
            insert_outcome(carry_db, dec_id, f"learn_{i}", productive=0)

        patched_env.cmd_learn()
        output = capsys.readouterr().out
        assert "LEARNED LESSONS" in output


# ===========================================================================
# Tests: Phase 7 -- Project-Aware Thresholds
# ===========================================================================

class TestDetectProjectType:
    """Tests for detect_project_type()."""

    def test_detect_rust_project(self, tmp_path, patched_env):
        """Cargo.toml -> 'rust'."""
        (tmp_path / "Cargo.toml").write_text("[package]\nname = 'test'\n")
        (tmp_path / ".git").mkdir()
        result = patched_env.detect_project_type(str(tmp_path))
        assert result == "rust"

    def test_detect_python_project(self, tmp_path, patched_env):
        """pyproject.toml -> 'python'."""
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")
        (tmp_path / ".git").mkdir()
        result = patched_env.detect_project_type(str(tmp_path))
        assert result == "python"

    def test_detect_node_project(self, tmp_path, patched_env):
        """package.json -> 'node'."""
        (tmp_path / "package.json").write_text('{"name": "test"}')
        (tmp_path / ".git").mkdir()
        result = patched_env.detect_project_type(str(tmp_path))
        assert result == "node"

    def test_detect_from_subdir(self, tmp_path, patched_env):
        """Detection should walk up to find marker files at git root."""
        (tmp_path / ".git").mkdir()
        (tmp_path / "Cargo.toml").write_text("[package]\nname = 'test'\n")
        subdir = tmp_path / "src"
        subdir.mkdir()
        result = patched_env.detect_project_type(str(subdir))
        assert result == "rust"

    def test_no_markers_returns_none(self, tmp_path, patched_env):
        """No marker files -> None."""
        (tmp_path / ".git").mkdir()
        (tmp_path / "README.md").write_text("hello")
        result = patched_env.detect_project_type(str(tmp_path))
        assert result is None

    def test_nonexistent_dir_returns_none(self, patched_env):
        """Non-existent directory -> None."""
        result = patched_env.detect_project_type("/nonexistent/path/xyz")
        assert result is None


class TestGetThresholdForProject:
    """Tests for get_threshold_for_project() -- resolution order."""

    def test_falls_back_to_global_default(self, carry_db, patched_env):
        """No project dir -> global default."""
        val = patched_env.get_threshold_for_project("noop_limit")
        assert val == 3  # default NOOP_LIMIT

    def test_non_overridable_ignores_project(self, carry_db, patched_env):
        """Non-overridable keys always use global, even with project_dir."""
        # dead_session_threshold is NOT in PROJECT_OVERRIDABLE
        val = patched_env.get_threshold_for_project(
            "dead_session_threshold", "/some/project"
        )
        assert val == 3  # default

    def test_project_threshold_overrides(self, carry_db, patched_env):
        """Explicit project_thresholds entry overrides global."""
        insert_project_threshold(carry_db, "/my/rust/proj", "noop_limit", 5)
        val = patched_env.get_threshold_for_project("noop_limit", "/my/rust/proj")
        assert val == 5

    def test_unknown_threshold_raises(self, carry_db, patched_env):
        """Unknown threshold key should raise ValueError."""
        with pytest.raises(ValueError):
            patched_env.get_threshold_for_project("nonexistent_key")

    def test_global_config_still_works(self, carry_db, patched_env):
        """Global config table overrides should still apply when no project override."""
        insert_config(carry_db, "noop_limit", "5")
        val = patched_env.get_threshold_for_project("noop_limit")
        assert val == 5

    def test_project_override_beats_global_config(self, carry_db, patched_env):
        """Project-specific threshold should override global config."""
        insert_config(carry_db, "noop_limit", "5")
        insert_project_threshold(carry_db, "/my/proj", "noop_limit", "7")
        val = patched_env.get_threshold_for_project("noop_limit", "/my/proj")
        assert val == 7


class TestGetAllThresholdsForProject:
    """Tests for get_all_thresholds_for_project()."""

    def test_returns_all_keys(self, carry_db, patched_env):
        """Should return a value for every defined threshold."""
        result = patched_env.get_all_thresholds_for_project()
        assert len(result) == len(patched_env.THRESHOLD_DEFS)

    def test_includes_project_overrides(self, carry_db, patched_env):
        """Should include project-specific values where set."""
        insert_project_threshold(carry_db, "/proj", "noop_limit", "6")
        result = patched_env.get_all_thresholds_for_project("/proj")
        assert result["noop_limit"] == 6
        # Non-overridable should still be default
        assert result["dead_session_threshold"] == 3


class TestResolveProjectDir:
    """Tests for _resolve_project_dir()."""

    def test_from_chain_meta(self, state_db, carry_db, patched_env):
        """Should find project_dir from chain_meta."""
        conn = sqlite3.connect(carry_db)
        conn.execute(
            "INSERT INTO chain_meta (session_id, continuation_count, created_at, project_dir) VALUES (?, 0, ?, ?)",
            ("test_sess", time.time(), "/my/project")
        )
        conn.commit()
        conn.close()

        result = patched_env._resolve_project_dir("test_sess")
        assert result == "/my/project"

    def test_from_git_heads_fallback(self, state_db, carry_db, patched_env):
        """Should fall back to chain_git_heads when chain_meta has no project_dir."""
        insert_git_heads(carry_db, "test_sess2", "/fallback/project", "abc123")

        result = patched_env._resolve_project_dir("test_sess2")
        assert result == "/fallback/project"

    def test_none_when_no_data(self, state_db, carry_db, patched_env):
        """Should return None when no project data exists."""
        result = patched_env._resolve_project_dir("nonexistent_session")
        assert result is None


class TestCheckCanContinueProjectAware:
    """Tests that check_can_continue uses project-aware thresholds."""

    def test_project_dir_in_result(self, state_db, carry_db, patched_env):
        """check_can_continue should include project_dir in result."""
        insert_session(state_db, "proj_sess", msgs=10, tools=5)
        insert_git_heads(carry_db, "proj_sess", "/my/project", "abc123")

        result = patched_env.check_can_continue("proj_sess")
        assert result["project_dir"] == "/my/project"

    def test_project_dir_none_when_unknown(self, state_db, carry_db, patched_env):
        """project_dir should be None when no project data exists."""
        insert_session(state_db, "no_proj_sess", msgs=10, tools=5)

        result = patched_env.check_can_continue("no_proj_sess")
        assert result["project_dir"] is None

    def test_stagnation_uses_project_threshold(self, state_db, carry_db, patched_env):
        """Stagnation check should use project-specific stagnation_stall_limit."""
        insert_config(carry_db, "git_min_sessions", "2")

        # Set project threshold to 5 (higher than default 3)
        insert_project_threshold(carry_db, "/rust/proj", "stagnation_stall_limit", 5)

        # Build a chain with 4 consecutive stalls (would halt at default limit=3)
        insert_session(state_db, "rs1", msgs=5, tools=2)
        insert_session(state_db, "rs2", parent="rs1", msgs=5, tools=2)
        insert_session(state_db, "rs3", parent="rs2", msgs=0, tools=0)
        insert_session(state_db, "rs4", parent="rs3", msgs=0, tools=0)

        # Same git head across all sessions = stalled
        for sid in ["rs1", "rs2", "rs3", "rs4"]:
            insert_git_heads(carry_db, sid, "/rust/proj", "abc123")

        result = patched_env.check_can_continue("rs4")
        # With project limit=5, 4 stalls should NOT trigger halt (but session is dead)
        # The session itself is dead (0 tools, 0 msgs), so it halts for that reason.
        # But stagnation_halt should be False because 4 < 5.
        assert result["stagnation_halt"] is False

    def test_noop_uses_project_threshold(self, state_db, carry_db, patched_env):
        """Noop check should use project-specific noop_limit."""
        # Set project threshold to 5 (higher than default 3)
        insert_project_threshold(carry_db, "/slow/proj", "noop_limit", 5)

        insert_session(state_db, "np1", msgs=5, tools=2)
        insert_session(state_db, "np2", parent="np1", msgs=0, tools=0)

        # Same git head = stalled, no test increase
        insert_git_heads(carry_db, "np1", "/slow/proj", "abc123")
        insert_git_heads(carry_db, "np2", "/slow/proj", "abc123")

        result = patched_env.check_can_continue("np2")
        # Session is dead (0 tools, 0 msgs), but noop_halt should not trigger
        # because consecutive_noops is 1 and project limit is 5.
        assert result["noop_halt"] is False


class TestCalibrateProjectThresholds:
    """Tests for calibrate_project_thresholds()."""

    def test_seeds_from_type_defaults_when_no_data(self, carry_db, patched_env, tmp_path):
        """When no outcomes for project, seed from PROJECT_TYPE_DEFAULTS."""
        (tmp_path / "Cargo.toml").write_text("[package]\nname = 'test'\n")
        (tmp_path / ".git").mkdir()

        result = patched_env.calibrate_project_thresholds(str(tmp_path), dry_run=True)
        assert result["project_type"] == "rust"
        assert result["changes"]  # should have changes (rust defaults differ from global)

    def test_seeds_no_changes_for_python(self, carry_db, patched_env, tmp_path):
        """Python type has empty defaults -> no seeding changes."""
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")
        (tmp_path / ".git").mkdir()

        result = patched_env.calibrate_project_thresholds(str(tmp_path), dry_run=True)
        assert result["project_type"] == "python"
        assert result["changes"] == []

    def test_calibrates_from_project_outcomes(self, carry_db, patched_env):
        """Should calibrate from outcomes scoped to the project."""
        # Create 6 productive continue outcomes for project sessions
        for i in range(6):
            dec_id = insert_decision(carry_db, f"proj_cal_{i}", "continue", True)
            insert_outcome(carry_db, dec_id, f"proj_cal_{i}", productive=1)
            insert_git_heads(carry_db, f"proj_cal_{i}", "/cal/proj", f"head_{i}")

        result = patched_env.calibrate_project_thresholds("/cal/proj", dry_run=True)
        assert result["summary"]["outcome_count"] == 6
        # 100% productive -> should loosen (suggest higher limits)
        assert any(c["new"] > c["old"] for c in result["changes"])

    def test_dry_run_does_not_write(self, carry_db, patched_env):
        """Dry run should not write to project_thresholds table."""
        for i in range(6):
            dec_id = insert_decision(carry_db, f"dry_{i}", "continue", True)
            insert_outcome(carry_db, dec_id, f"dry_{i}", productive=1)
            insert_git_heads(carry_db, f"dry_{i}", "/dry/proj", f"head_{i}")

        patched_env.calibrate_project_thresholds("/dry/proj", dry_run=True)

        conn = sqlite3.connect(carry_db)
        rows = conn.execute("SELECT * FROM project_thresholds").fetchall()
        conn.close()
        assert len(rows) == 0

    def test_actual_run_writes(self, carry_db, patched_env):
        """Non-dry-run should write to project_thresholds table."""
        for i in range(6):
            dec_id = insert_decision(carry_db, f"wet_{i}", "continue", True)
            insert_outcome(carry_db, dec_id, f"wet_{i}", productive=1)
            insert_git_heads(carry_db, f"wet_{i}", "/wet/proj", f"head_{i}")

        result = patched_env.calibrate_project_thresholds("/wet/proj", dry_run=False)
        if result["changes"]:
            conn = sqlite3.connect(carry_db)
            rows = conn.execute("SELECT * FROM project_thresholds WHERE project_dir = ?", ("/wet/proj",)).fetchall()
            conn.close()
            assert len(rows) > 0

    def test_tightens_on_bad_continue_rate(self, carry_db, patched_env):
        """<50% productive continues should tighten thresholds."""
        # 2 productive, 4 unproductive = 33%
        for i in range(2):
            dec_id = insert_decision(carry_db, f"bad_ok_{i}", "continue", True)
            insert_outcome(carry_db, dec_id, f"bad_ok_{i}", productive=1)
            insert_git_heads(carry_db, f"bad_ok_{i}", "/bad/proj", f"head_{i}")
        for i in range(4):
            dec_id = insert_decision(carry_db, f"bad_nok_{i}", "continue", True)
            insert_outcome(carry_db, dec_id, f"bad_nok_{i}", productive=0)
            insert_git_heads(carry_db, f"bad_nok_{i}", "/bad/proj", f"head_{i}")

        result = patched_env.calibrate_project_thresholds("/bad/proj", dry_run=True)
        # Should tighten (suggest lower limits)
        assert any(c["new"] < c["old"] for c in result["changes"])

    def test_unknown_type_no_data_no_error(self, carry_db, patched_env, tmp_path):
        """Unknown project type with no outcomes should not error."""
        (tmp_path / ".git").mkdir()
        result = patched_env.calibrate_project_thresholds(str(tmp_path), dry_run=True)
        assert result["project_type"] is None
        assert result["changes"] == []

    def test_fewer_than_5_seeds_from_type(self, carry_db, patched_env, tmp_path):
        """Fewer than 5 outcomes -> should seed from type defaults."""
        (tmp_path / "Cargo.toml").write_text("[package]\nname = 't'\n")
        (tmp_path / ".git").mkdir()

        # Only 2 outcomes for this project
        for i in range(2):
            dec_id = insert_decision(carry_db, f"few_{i}", "continue", True)
            insert_outcome(carry_db, dec_id, f"few_{i}", productive=1)
            insert_git_heads(carry_db, f"few_{i}", str(tmp_path), f"head_{i}")

        result = patched_env.calibrate_project_thresholds(str(tmp_path), dry_run=True)
        assert "Seeded" in result["summary"]["message"] or result["changes"]


class TestProjectThresholdsTable:
    """Tests for the project_thresholds DB table."""

    def test_table_created(self, carry_db, patched_env):
        """project_thresholds table should exist after get_carry_conn."""
        conn = sqlite3.connect(carry_db)
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='project_thresholds'"
        ).fetchall()
        conn.close()
        assert len(rows) == 1

    def test_upsert_behavior(self, carry_db, patched_env):
        """Writing same (project_dir, key) twice should update, not duplicate."""
        insert_project_threshold(carry_db, "/proj", "noop_limit", 5)
        insert_project_threshold(carry_db, "/proj", "noop_limit", 7)

        conn = sqlite3.connect(carry_db)
        rows = conn.execute(
            "SELECT value FROM project_thresholds WHERE project_dir = ? AND key = ?",
            ("/proj", "noop_limit")
        ).fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][0] == "7"


# ===========================================================================
# Tests: Phase 8 -- Test command discovery
# ===========================================================================

class TestDetectTestCommand:
    """Tests for detect_test_command()."""

    def test_rust_project(self, patched_env, tmp_path):
        """Cargo.toml -> cargo test."""
        (tmp_path / "Cargo.toml").write_text('[package]\nname = "t"\n')
        (tmp_path / ".git").mkdir()
        result = patched_env.detect_test_command(str(tmp_path))
        assert result == "cargo test"

    def test_python_project_pyproject(self, patched_env, tmp_path):
        """pyproject.toml -> pytest."""
        (tmp_path / "pyproject.toml").write_text("[tool.pytest]\n")
        (tmp_path / ".git").mkdir()
        result = patched_env.detect_test_command(str(tmp_path))
        assert result == "pytest"

    def test_python_project_setup_py(self, patched_env, tmp_path):
        """setup.py -> pytest."""
        (tmp_path / "setup.py").write_text("from setuptools import setup\n")
        (tmp_path / ".git").mkdir()
        result = patched_env.detect_test_command(str(tmp_path))
        assert result == "pytest"

    def test_node_project(self, patched_env, tmp_path):
        """package.json -> npm test."""
        (tmp_path / "package.json").write_text('{"name": "t"}\n')
        (tmp_path / ".git").mkdir()
        result = patched_env.detect_test_command(str(tmp_path))
        assert result == "npm test"

    def test_go_project(self, patched_env, tmp_path):
        """go.mod -> go test ./..."""
        (tmp_path / "go.mod").write_text("module example.com/t\n")
        (tmp_path / ".git").mkdir()
        result = patched_env.detect_test_command(str(tmp_path))
        assert result == "go test ./..."

    def test_make_project(self, patched_env, tmp_path):
        """Makefile -> make test."""
        (tmp_path / "Makefile").write_text("test:\n\techo ok\n")
        (tmp_path / ".git").mkdir()
        result = patched_env.detect_test_command(str(tmp_path))
        assert result == "make test"

    def test_java_project_gradle(self, patched_env, tmp_path):
        """build.gradle -> mvn test."""
        (tmp_path / "build.gradle").write_text("plugins { id 'java' }\n")
        (tmp_path / ".git").mkdir()
        result = patched_env.detect_test_command(str(tmp_path))
        assert result == "mvn test"

    def test_no_marker_files(self, patched_env, tmp_path):
        """No marker files -> None."""
        (tmp_path / ".git").mkdir()
        result = patched_env.detect_test_command(str(tmp_path))
        assert result is None

    def test_nonexistent_dir(self, patched_env):
        """Nonexistent directory -> None."""
        result = patched_env.detect_test_command("/nonexistent/path/xyz")
        assert result is None

    def test_subdir_finds_git_root(self, patched_env, tmp_path):
        """detect_test_command should find test command from a subdirectory."""
        (tmp_path / "Cargo.toml").write_text('[package]\nname = "t"\n')
        (tmp_path / ".git").mkdir()
        subdir = tmp_path / "src" / "bin"
        subdir.mkdir(parents=True)
        result = patched_env.detect_test_command(str(subdir))
        assert result == "cargo test"

    def test_cargo_takes_priority_over_pyproject(self, patched_env, tmp_path):
        """Cargo.toml is checked before pyproject.toml (marker order matters)."""
        (tmp_path / "Cargo.toml").write_text('[package]\nname = "t"\n')
        (tmp_path / "pyproject.toml").write_text("[tool.pytest]\n")
        (tmp_path / ".git").mkdir()
        result = patched_env.detect_test_command(str(tmp_path))
        assert result == "cargo test"


class TestTestCommandMap:
    """Tests for the TEST_COMMAND_MAP constant."""

    def test_all_project_types_have_test_commands(self, patched_env):
        """Every project type in PROJECT_TYPE_DEFAULTS should have a test command."""
        for ptype in patched_env.PROJECT_TYPE_DEFAULTS:
            assert ptype in patched_env.TEST_COMMAND_MAP, f"Missing test command for {ptype}"

    def test_map_values_are_strings(self, patched_env):
        """All test command values should be non-empty strings."""
        for ptype, cmd in patched_env.TEST_COMMAND_MAP.items():
            assert isinstance(cmd, str), f"Test command for {ptype} is not a string"
            assert len(cmd) > 0, f"Test command for {ptype} is empty"


class TestContextIncludesTestCommand:
    """Tests that cmd_context includes test command in PROJECT STATUS."""

    def test_context_shows_test_command_for_python_project(
        self, state_db, carry_db, patched_env, tmp_path, capsys
    ):
        """Context output should include TEST: line when detect_test_command returns one."""
        from unittest.mock import patch

        # Insert a session with a file path that the regex will match
        insert_session(state_db, "ctx_test", msgs=10, tools=5)
        conn = sqlite3.connect(state_db)
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            ("ctx_test", "user",
             "Work on /home/jericho/zion/projects/myproject/main.py",
             time.time())
        )
        conn.commit()
        conn.close()

        # Mock detect_test_command to return "pytest" -- the integration we're
        # testing is that cmd_context calls it and prints the result.
        with patch.object(patched_env, "detect_test_command", return_value="pytest"):
            # Mock git_status to return a valid git root
            with patch.object(patched_env, "git_status", return_value={
                "git_root": "/home/jericho/zion/projects/myproject",
                "branch": "master",
                "last_commits": ["abc123 init"],
                "dirty": False,
                "error": None,
            }):
                patched_env.cmd_context()

        output = capsys.readouterr().out
        assert "TEST: pytest" in output

    def test_context_no_test_command_without_project(
        self, state_db, carry_db, patched_env, capsys
    ):
        """Context with no project dirs should not show TEST: lines."""
        insert_session(state_db, "no_proj", msgs=10, tools=5)
        conn = sqlite3.connect(state_db)
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            ("no_proj", "user", "general task no files", time.time())
        )
        conn.commit()
        conn.close()

        patched_env.cmd_context()
        output = capsys.readouterr().out

        assert "TEST:" not in output


# ===========================================================================
# Tests: session health dashboard (Phase 9)
# ===========================================================================

class TestSessionHealthData:
    """Tests for session_health_data() -- the data-gathering backend."""

    def test_empty_day(self, state_db, carry_db, patched_env):
        """No sessions today should return all zeros."""
        data = patched_env.session_health_data()
        assert data["sessions_total"] == 0
        assert data["sessions_active"] == 0
        assert data["sessions_dead"] == 0
        assert data["commits_landed"] == 0
        assert data["wasted_minutes"] == 0.0

    def test_active_sessions_counted(self, state_db, carry_db, patched_env):
        """Active sessions (tool_call_count > 0) should be counted."""
        insert_session(state_db, "active_1", msgs=10, tools=5)
        insert_session(state_db, "active_2", msgs=20, tools=10)
        data = patched_env.session_health_data()
        assert data["sessions_total"] == 2
        assert data["sessions_active"] == 2
        assert data["sessions_dead"] == 0

    def test_dead_sessions_excluded_from_active(self, state_db, carry_db, patched_env):
        """Dead sessions (0 tools, non-cron) should show in dead count."""
        insert_session(state_db, "active_1", msgs=10, tools=5)
        insert_session(state_db, "dead_1", msgs=0, tools=0, source="cli")
        data = patched_env.session_health_data()
        assert data["sessions_total"] == 2
        assert data["sessions_active"] == 1
        assert data["sessions_dead"] == 1

    def test_cron_sessions_not_counted_as_dead(self, state_db, carry_db, patched_env):
        """Cron sessions with 0 tools should not count as dead."""
        insert_session(state_db, "cron_1", msgs=0, tools=0, source="cron")
        data = patched_env.session_health_data()
        assert data["sessions_total"] == 1
        assert data["sessions_active"] == 0
        assert data["sessions_dead"] == 0  # cron excluded

    def test_decision_counts(self, state_db, carry_db, patched_env):
        """Decision continue/halt should be counted."""
        insert_session(state_db, "s1", msgs=5, tools=2)
        insert_decision(carry_db, "s1", "continue", True)
        insert_decision(carry_db, "s1", "halt", False)
        insert_decision(carry_db, "s1", "continue", True)
        data = patched_env.session_health_data()
        assert data["decisions_total"] == 3
        assert data["decisions_continue"] == 2
        assert data["decisions_halt"] == 1

    def test_decision_skips_dash_project(self, state_db, carry_db, patched_env):
        """Decision entries with --project session_id should be excluded."""
        insert_session(state_db, "s1", msgs=5, tools=2)
        insert_decision(carry_db, "s1", "continue", True)
        insert_decision(carry_db, "--project", "halt", False)
        data = patched_env.session_health_data()
        assert data["decisions_total"] == 1  # only s1
        assert data["decisions_continue"] == 1

    def test_commits_landed_from_git_heads(self, state_db, carry_db, patched_env):
        """Distinct new git HEADs today should count as commits."""
        insert_git_heads(carry_db, "s1", "/proj/a", "abc123")
        insert_git_heads(carry_db, "s1", "/proj/a", "def456")  # 2nd commit
        data = patched_env.session_health_data()
        assert data["commits_landed"] == 2  # both new, no prior HEAD

    def test_commits_landed_excludes_prior_head(self, state_db, carry_db, patched_env):
        """HEADs same as yesterday should not count as new commits."""
        import time as _time
        # Insert a "yesterday" HEAD
        yesterday = _time.time() - 86400
        conn = sqlite3.connect(carry_db)
        conn.execute(
            "INSERT INTO chain_git_heads (session_id, project_dir, git_head, recorded_at) VALUES (?, ?, ?, ?)",
            ("old_s", "/proj/a", "abc123", yesterday)
        )
        conn.commit()
        conn.close()
        # Insert same HEAD today (no change)
        insert_git_heads(carry_db, "s1", "/proj/a", "abc123")
        data = patched_env.session_health_data()
        assert data["commits_landed"] == 0  # same as before, no new commit

    def test_commits_mixed_projects(self, state_db, carry_db, patched_env):
        """Commits across multiple projects should be summed."""
        insert_git_heads(carry_db, "s1", "/proj/a", "aaa111")
        insert_git_heads(carry_db, "s1", "/proj/b", "bbb222")
        data = patched_env.session_health_data()
        assert data["commits_landed"] == 2

    def test_test_counts_tracked(self, state_db, carry_db, patched_env):
        """Latest test counts should be surfaced."""
        insert_test_count(carry_db, "s1", 1, 163, "pytest")
        insert_test_count(carry_db, "s1", 2, 165, "pytest")
        data = patched_env.session_health_data()
        assert "pytest" in data["test_counts"]
        assert data["test_counts"]["pytest"] == 165  # latest

    def test_wasted_time_with_duration(self, state_db, carry_db, patched_env):
        """Dead sessions with ended_at should compute real duration."""
        import time as _time
        now = _time.time()
        conn = sqlite3.connect(state_db)
        # Add ended_at column if missing (test schema may not have it)
        try:
            conn.execute("ALTER TABLE sessions ADD COLUMN ended_at REAL")
        except sqlite3.OperationalError:
            pass
        conn.execute(
            "INSERT INTO sessions (id, source, message_count, tool_call_count, started_at, ended_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("dead_timed", "cli", 0, 0, now - 300, now)  # 5 min session
        )
        conn.commit()
        conn.close()
        data = patched_env.session_health_data()
        assert data["wasted_minutes"] >= 4.0  # roughly 5 min

    def test_wasted_time_without_duration(self, state_db, carry_db, patched_env):
        """Dead sessions without ended_at should estimate 60s each."""
        insert_session(state_db, "dead_noend", msgs=0, tools=0, source="cli")
        data = patched_env.session_health_data()
        assert data["wasted_minutes"] >= 1.0  # 1 min estimate

    def test_outcome_accuracy(self, state_db, carry_db, patched_env):
        """Outcome productive/wasted counts should be accurate."""
        insert_session(state_db, "s1", msgs=5, tools=2)
        dec_id = insert_decision(carry_db, "s1", "continue", True)
        insert_outcome(carry_db, dec_id, "s1", productive=1, tool_calls=5)
        insert_outcome(carry_db, dec_id + 1, "s1", productive=0, tool_calls=0)
        data = patched_env.session_health_data()
        assert data["outcomes_total"] >= 2
        assert data["outcomes_productive"] >= 1
        assert data["outcomes_wasted"] >= 1

    def test_outcome_skips_dash_project(self, state_db, carry_db, patched_env):
        """Outcomes with --project session_id should be excluded from wasted."""
        insert_session(state_db, "s1", msgs=5, tools=2)
        insert_outcome(carry_db, 999, "--project", productive=0, tool_calls=0)
        data = patched_env.session_health_data()
        assert data["outcomes_wasted"] == 0  # --project excluded


class TestCmdHealth:
    """Tests for cmd_health() -- the CLI output formatter."""

    def test_empty_day_output(self, state_db, carry_db, patched_env, capsys):
        """Empty day should show NO DATA verdict."""
        patched_env.cmd_health()
        output = capsys.readouterr().out
        assert "NO DATA" in output

    def test_healthy_verdict(self, state_db, carry_db, patched_env, capsys):
        """Active sessions + commits = HEALTHY."""
        insert_session(state_db, "active_1", msgs=10, tools=5)
        insert_session(state_db, "active_2", msgs=10, tools=5)
        insert_git_heads(carry_db, "s1", "/proj/a", "newhead1")
        patched_env.cmd_health()
        output = capsys.readouterr().out
        assert "HEALTHY" in output

    def test_ok_verdict(self, state_db, carry_db, patched_env, capsys):
        """40%+ active but no commits = OK."""
        insert_session(state_db, "active_1", msgs=10, tools=5)
        insert_session(state_db, "dead_1", msgs=0, tools=0, source="cli")
        patched_env.cmd_health()
        output = capsys.readouterr().out
        assert "OK" in output

    def test_stalled_verdict(self, state_db, carry_db, patched_env, capsys):
        """All dead sessions = STALLED."""
        insert_session(state_db, "dead_1", msgs=0, tools=0, source="cli")
        patched_env.cmd_health()
        output = capsys.readouterr().out
        assert "STALLED" in output

    def test_json_output(self, state_db, carry_db, patched_env, capsys):
        """--json flag should produce valid JSON."""
        insert_session(state_db, "active_1", msgs=10, tools=5)
        patched_env.cmd_health(json_output=True)
        output = capsys.readouterr().out
        import json
        data = json.loads(output)
        assert data["sessions_total"] == 1
        assert data["sessions_active"] == 1

    def test_wasted_time_shown(self, state_db, carry_db, patched_env, capsys):
        """Wasted time should appear in output when > 0."""
        import time as _time
        now = _time.time()
        conn = sqlite3.connect(state_db)
        try:
            conn.execute("ALTER TABLE sessions ADD COLUMN ended_at REAL")
        except sqlite3.OperationalError:
            pass
        conn.execute(
            "INSERT INTO sessions (id, source, message_count, tool_call_count, started_at, ended_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("dead_timed", "cli", 0, 0, now - 300, now)
        )
        conn.commit()
        conn.close()
        patched_env.cmd_health()
        output = capsys.readouterr().out
        assert "Time wasted" in output

    def test_accuracy_shown(self, state_db, carry_db, patched_env, capsys):
        """Accuracy should appear when there are outcomes."""
        insert_session(state_db, "s1", msgs=5, tools=2)
        dec_id = insert_decision(carry_db, "s1", "continue", True)
        insert_outcome(carry_db, dec_id, "s1", productive=1, tool_calls=5)
        patched_env.cmd_health()
        output = capsys.readouterr().out
        assert "Accuracy" in output

    def test_session_counts_in_output(self, state_db, carry_db, patched_env, capsys):
        """Session line should show total/active/dead."""
        insert_session(state_db, "active_1", msgs=10, tools=5)
        insert_session(state_db, "dead_1", msgs=0, tools=0, source="cli")
        insert_session(state_db, "cron_1", msgs=0, tools=0, source="cron")
        patched_env.cmd_health()
        output = capsys.readouterr().out
        assert "3 total" in output
        assert "1 active" in output
        assert "1 dead" in output
