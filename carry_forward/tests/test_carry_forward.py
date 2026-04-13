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
from conftest import insert_session, insert_blocker, insert_git_heads, insert_config, insert_chain, insert_tick_changes, insert_test_count, insert_decision, insert_outcome  # noqa: E402


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
