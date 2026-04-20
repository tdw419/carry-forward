"""Tests for model-aware dead session classification (v5.5).

Verifies that:
- Dead sessions using unreliable models are classified as model failures, not task thrash
- The chain doesn't halt when dead sessions are caused by model failures
- Reliable models' dead sessions still count as genuine task issues
- model-health and retry-model CLI commands work
- Backward compatibility: existing behavior preserved for sessions without model info
"""
import json
import os
import sqlite3
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import (
    insert_session, insert_config, insert_chain,
    insert_model_sessions, insert_model_health_cache,
)


# ===========================================================================
# Tests: _get_model_health
# ===========================================================================

class TestGetModelHealth:
    """Tests for the model health tracking function."""

    def test_reliable_model(self, state_db, carry_db, patched_env):
        """Model with high productivity rate should be reliable."""
        insert_model_sessions(state_db, "glm-5.1", total=20, productive=18)
        health = patched_env._get_model_health("glm-5.1")
        assert health["is_reliable"] is True
        assert health["total_sessions"] == 20
        assert health["productive_sessions"] == 18
        assert health["productivity_rate"] == 0.9

    def test_unreliable_model(self, state_db, carry_db, patched_env):
        """Model with low productivity rate should be unreliable."""
        insert_model_sessions(state_db, "glm-5-turbo", total=20, productive=4)
        health = patched_env._get_model_health("glm-5-turbo")
        assert health["is_reliable"] is False
        assert health["productivity_rate"] == 0.2

    def test_unknown_model_is_reliable(self, state_db, carry_db, patched_env):
        """Unknown model (no sessions) should default to reliable."""
        health = patched_env._get_model_health("unknown-model-xyz")
        assert health["is_reliable"] is True
        assert health["total_sessions"] == 0

    def test_empty_model_name_is_reliable(self, state_db, carry_db, patched_env):
        """Empty model name should default to reliable."""
        health = patched_env._get_model_health("")
        assert health["is_reliable"] is True

    def test_none_model_name_is_reliable(self, state_db, carry_db, patched_env):
        """None model name should default to reliable."""
        health = patched_env._get_model_health(None)
        assert health["is_reliable"] is True

    def test_few_sessions_defaults_reliable(self, state_db, carry_db, patched_env):
        """Model with fewer than 5 sessions should default to reliable."""
        insert_model_sessions(state_db, "rare-model", total=3, productive=0)
        health = patched_env._get_model_health("rare-model")
        # Not enough data to declare unreliable
        assert health["is_reliable"] is True

    def test_exactly_at_threshold(self, state_db, carry_db, patched_env):
        """Model exactly at 70% should be reliable (>= threshold)."""
        # 7 productive out of 10 = 70%
        insert_model_sessions(state_db, "borderline-model", total=10, productive=7)
        health = patched_env._get_model_health("borderline-model")
        assert health["is_reliable"] is True
        assert health["productivity_rate"] == 0.7

    def test_just_below_threshold(self, state_db, carry_db, patched_env):
        """Model just below 70% should be unreliable."""
        # 6 productive out of 10 = 60%
        insert_model_sessions(state_db, "weak-model", total=10, productive=6)
        health = patched_env._get_model_health("weak-model")
        assert health["is_reliable"] is False

    def test_caching(self, state_db, carry_db, patched_env):
        """Results should be cached in model_health_cache table."""
        insert_model_sessions(state_db, "cached-model", total=10, productive=8)
        health1 = patched_env._get_model_health("cached-model")
        
        # Verify cache entry exists
        conn = sqlite3.connect(carry_db)
        row = conn.execute(
            "SELECT total_sessions, productivity_rate FROM model_health_cache WHERE model = ?",
            ("cached-model",)
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == 10
        assert abs(row[1] - 0.8) < 0.01


# ===========================================================================
# Tests: _is_model_failure
# ===========================================================================

class TestIsModelFailure:
    """Tests for the model failure detection function."""

    def test_unreliable_model_dead_session(self, state_db, carry_db, patched_env):
        """Dead session with unreliable model should be a model failure."""
        insert_model_sessions(state_db, "glm-5-turbo", total=20, productive=4)
        insert_session(state_db, "dead_bad_model", msgs=0, tools=0, model="glm-5-turbo")
        
        is_failure, model_name = patched_env._is_model_failure("dead_bad_model")
        assert is_failure is True
        assert model_name == "glm-5-turbo"

    def test_reliable_model_dead_session(self, state_db, carry_db, patched_env):
        """Dead session with reliable model should NOT be a model failure."""
        insert_model_sessions(state_db, "glm-5.1", total=20, productive=18)
        insert_session(state_db, "dead_good_model", msgs=0, tools=0, model="glm-5.1")
        
        is_failure, model_name = patched_env._is_model_failure("dead_good_model")
        assert is_failure is False
        assert model_name == "glm-5.1"

    def test_active_session_not_failure(self, state_db, carry_db, patched_env):
        """Active session (tools > 0) should never be a model failure."""
        insert_model_sessions(state_db, "glm-5-turbo", total=20, productive=4)
        insert_session(state_db, "active_bad_model", msgs=10, tools=5, model="glm-5-turbo")
        
        is_failure, model_name = patched_env._is_model_failure("active_bad_model")
        assert is_failure is False

    def test_no_model_not_failure(self, state_db, carry_db, patched_env):
        """Session without model info should not be a model failure."""
        insert_session(state_db, "no_model", msgs=0, tools=0)
        
        is_failure, model_name = patched_env._is_model_failure("no_model")
        assert is_failure is False

    def test_missing_session_not_failure(self, state_db, carry_db, patched_env):
        """Non-existent session should not be a model failure."""
        is_failure, model_name = patched_env._is_model_failure("does_not_exist")
        assert is_failure is False

    def test_empty_session_id_not_failure(self, state_db, carry_db, patched_env):
        """Empty session_id should not be a model failure."""
        is_failure, model_name = patched_env._is_model_failure("")
        assert is_failure is False


# ===========================================================================
# Tests: Model-aware check_can_continue integration
# ===========================================================================

class TestModelAwareCheckCanContinue:
    """Tests that model-aware logic integrates correctly with the decision pipeline."""

    def test_dead_session_bad_model_does_not_halt(self, state_db, carry_db, patched_env):
        """Dead session using unreliable model should NOT halt (model failure, not task issue)."""
        # Set up unreliable model with lots of dead sessions
        insert_model_sessions(state_db, "glm-5-turbo", total=20, productive=4)
        insert_session(state_db, "dead_mf_1", msgs=0, tools=0, model="glm-5-turbo")
        
        result = patched_env.check_can_continue("dead_mf_1")
        # Should not halt -- model failure, not task death
        assert result["can_continue"] is True
        assert result["session_dead"] is False
        assert result["session_model_failure"] is True
        assert result["session_model_name"] == "glm-5-turbo"
        assert any("model failure" in gr.lower() for gr in result["guard_rails"])

    def test_dead_session_good_model_still_halts(self, state_db, carry_db, patched_env):
        """Dead session using reliable model should still halt (genuine task issue)."""
        insert_model_sessions(state_db, "glm-5.1", total=20, productive=18)
        insert_session(state_db, "dead_good_1", msgs=0, tools=0, model="glm-5.1")
        
        result = patched_env.check_can_continue("dead_good_1")
        assert result["can_continue"] is False
        assert result["session_dead"] is True
        assert result["session_model_failure"] is False

    def test_thrash_all_model_failures_does_not_halt(self, state_db, carry_db, patched_env):
        """Chain of all dead sessions with unreliable model should NOT halt."""
        insert_config(carry_db, "dead_session_threshold", "2")
        insert_config(carry_db, "dead_lookback", "5")
        
        insert_model_sessions(state_db, "glm-5-turbo", total=20, productive=4)
        
        # Chain of 3 dead sessions, all using unreliable model
        insert_session(state_db, "mf_a", msgs=0, tools=0, model="glm-5-turbo")
        insert_session(state_db, "mf_b", parent="mf_a", msgs=0, tools=0, model="glm-5-turbo")
        insert_session(state_db, "mf_c", parent="mf_b", msgs=0, tools=0, model="glm-5-turbo")
        
        result = patched_env.check_can_continue("mf_c")
        # All deaths are model failures -> adjusted dead count = 0 -> not thrashing
        assert result["can_continue"] is True
        assert len(result["model_failure_sessions"]) == 3

    def test_thrash_mixed_model_failure_and_real_halts(self, state_db, carry_db, patched_env):
        """Chain with some model failures and some real dead sessions.
        
        When model failures are excluded and real dead sessions are still below
        the threshold, the chain continues. But parent_dead and noop checks
        still apply to genuine dead sessions.
        """
        insert_config(carry_db, "dead_session_threshold", "3")
        insert_config(carry_db, "dead_lookback", "5")
        
        insert_model_sessions(state_db, "glm-5-turbo", total=20, productive=4)
        insert_model_sessions(state_db, "good-model", total=20, productive=18)
        
        # 1 model failure + 1 real dead + 1 model failure + 1 alive = threshold 3
        # After excluding 2 model failures -> 1 real dead -> below threshold 3
        # Chain: mf -> real_dead -> mf -> alive
        # The alive session at the end should continue regardless
        insert_session(state_db, "mix_mf1", msgs=0, tools=0, model="glm-5-turbo")
        insert_session(state_db, "mix_real", parent="mix_mf1", msgs=0, tools=0, model="good-model")
        insert_session(state_db, "mix_mf2", parent="mix_real", msgs=0, tools=0, model="glm-5-turbo")
        insert_session(state_db, "mix_alive", parent="mix_mf2", msgs=10, tools=5, model="good-model")
        
        result = patched_env.check_can_continue("mix_alive")
        # Active session should always continue
        assert result["can_continue"] is True
        # Active session overrides thrash detection, so model_failure_sessions
        # won't be populated (check 1 doesn't run model analysis for active sessions)

    def test_thrash_real_dead_exceeds_threshold_halts(self, state_db, carry_db, patched_env):
        """Even with some model failures, if real dead exceeds threshold, halt."""
        insert_config(carry_db, "dead_session_threshold", "2")
        insert_config(carry_db, "dead_lookback", "6")
        
        insert_model_sessions(state_db, "glm-5-turbo", total=20, productive=4)
        insert_model_sessions(state_db, "good-model", total=20, productive=18)
        
        # 1 model failure + 2 real dead = 2 model failures excluded
        # Adjusted: 2 dead (at threshold) -> halt
        insert_session(state_db, "real_halt1", msgs=0, tools=0, model="good-model")
        insert_session(state_db, "real_halt2", parent="real_halt1", msgs=0, tools=0, model="glm-5-turbo")
        insert_session(state_db, "real_halt3", parent="real_halt2", msgs=0, tools=0, model="good-model")
        insert_session(state_db, "real_halt4", parent="real_halt3", msgs=0, tools=0, model="good-model")
        
        result = patched_env.check_can_continue("real_halt4")
        # 3 total dead, 1 model failure excluded, adjusted = 2 -> at threshold -> halt
        assert result["can_continue"] is False

    def test_backward_compat_no_model_info(self, state_db, carry_db, patched_env):
        """Sessions without model info should behave exactly as before."""
        # Dead session without model -> should halt (same as v5.2)
        insert_session(state_db, "legacy_dead", msgs=0, tools=0)
        result = patched_env.check_can_continue("legacy_dead")
        assert result["can_continue"] is False
        assert result["session_dead"] is True
        assert result["session_model_failure"] is False

    def test_backward_compat_active_session(self, state_db, carry_db, patched_env):
        """Active session should still continue regardless of model."""
        insert_model_sessions(state_db, "glm-5-turbo", total=20, productive=4)
        insert_session(state_db, "active_any_model", msgs=10, tools=5, model="glm-5-turbo")
        
        result = patched_env.check_can_continue("active_any_model")
        assert result["can_continue"] is True


# ===========================================================================
# Tests: _get_suggested_replacement_model
# ===========================================================================

class TestSuggestedReplacement:
    """Tests for the replacement model suggestion function."""

    def test_suggests_reliable_model(self, state_db, carry_db, patched_env):
        """Should suggest a reliable model as replacement."""
        insert_model_sessions(state_db, "glm-5-turbo", total=20, productive=4)
        insert_model_sessions(state_db, "glm-5.1", total=20, productive=18)
        
        replacement = patched_env._get_suggested_replacement_model("glm-5-turbo")
        assert replacement == "glm-5.1"

    def test_no_reliable_alternative(self, state_db, carry_db, patched_env):
        """Should return None if no reliable alternative exists."""
        insert_model_sessions(state_db, "bad-model-1", total=10, productive=2)
        insert_model_sessions(state_db, "bad-model-2", total=10, productive=3)
        
        replacement = patched_env._get_suggested_replacement_model("bad-model-1")
        assert replacement is None

    def test_empty_failed_model(self, state_db, carry_db, patched_env):
        """Should return None for empty/None failed model."""
        assert patched_env._get_suggested_replacement_model("") is None
        assert patched_env._get_suggested_replacement_model(None) is None


# ===========================================================================
# Tests: cmd_retry_model
# ===========================================================================

class TestRetryModelCLI:
    """Tests for the retry-model CLI command."""

    def test_model_failure_exits_zero(self, state_db, carry_db, patched_env, capsys):
        """Should exit 0 when model failure is detected."""
        insert_model_sessions(state_db, "glm-5-turbo", total=20, productive=4)
        insert_session(state_db, "retry_test_1", msgs=0, tools=0, model="glm-5-turbo")
        
        with pytest.raises(SystemExit) as exc_info:
            patched_env.cmd_retry_model("retry_test_1")
        assert exc_info.value.code == 0
        
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["model_failure"] is True
        assert data["failed_model"] == "glm-5-turbo"

    def test_not_model_failure_exits_one(self, state_db, carry_db, patched_env, capsys):
        """Should exit 1 when no model failure."""
        insert_model_sessions(state_db, "glm-5.1", total=20, productive=18)
        insert_session(state_db, "retry_test_2", msgs=0, tools=0, model="glm-5.1")
        
        with pytest.raises(SystemExit) as exc_info:
            patched_env.cmd_retry_model("retry_test_2")
        assert exc_info.value.code == 1
        
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["model_failure"] is False


# ===========================================================================
# Tests: cmd_model_health
# ===========================================================================

class TestModelHealthCLI:
    """Tests for the model-health CLI command."""

    def test_model_health_json(self, state_db, carry_db, patched_env, capsys):
        """Should output JSON with model health data."""
        insert_model_sessions(state_db, "glm-5.1", total=20, productive=18)
        insert_model_sessions(state_db, "glm-5-turbo", total=20, productive=4)
        
        patched_env.cmd_model_health(json_output=True)
        output = capsys.readouterr().out
        data = json.loads(output)
        
        assert len(data) == 2
        models = {e["model"]: e for e in data}
        assert "glm-5.1" in models
        assert "glm-5-turbo" in models
        assert models["glm-5.1"]["is_reliable"] is True
        assert models["glm-5-turbo"]["is_reliable"] is False

    def test_model_health_ascii(self, state_db, carry_db, patched_env, capsys):
        """Should output readable ASCII dashboard."""
        insert_model_sessions(state_db, "glm-5.1", total=20, productive=18)
        
        patched_env.cmd_model_health(json_output=False)
        output = capsys.readouterr().out
        assert "MODEL HEALTH DASHBOARD" in output
        assert "glm-5.1" in output

    def test_model_health_empty(self, state_db, carry_db, patched_env, capsys):
        """Should handle empty session history."""
        patched_env.cmd_model_health(json_output=False)
        output = capsys.readouterr().out
        assert "No models found" in output
