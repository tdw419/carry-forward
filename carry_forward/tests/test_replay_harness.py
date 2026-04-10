"""Tests for replay_harness.py functions.

Tests the classification, metrics, and fix-logic functions using
synthetic data rather than the live database.
"""
import json
import os
import sqlite3
import sys
import time

import pytest

# Make replay_harness importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import insert_session, insert_config, insert_chain  # noqa: E402

import replay_harness as rh  # noqa: E402


# ===========================================================================
# Tests: classify_outcome
# ===========================================================================

class TestClassifyOutcome:
    """Tests for the TP/FP/TN/FN classification function."""

    @staticmethod
    def _make_row(decision: str, productive: int):
        return (1, "test_session", decision, "[]", "{}",
                1 if decision == "continue" else 0,
                time.time(), productive, 5, 10, 0, 0)

    def test_true_positive(self):
        """Continue + productive = TP."""
        row = self._make_row("continue", productive=1)
        assert rh.classify_outcome(row) == "TP"

    def test_false_positive(self):
        """Continue + not productive = FP."""
        row = self._make_row("continue", productive=0)
        assert rh.classify_outcome(row) == "FP"

    def test_false_negative(self):
        """Halt + productive = FN."""
        row = self._make_row("halt", productive=1)
        assert rh.classify_outcome(row) == "FN"

    def test_true_negative(self):
        """Halt + not productive = TN."""
        row = self._make_row("halt", productive=0)
        assert rh.classify_outcome(row) == "TN"


# ===========================================================================
# Tests: compute_metrics
# ===========================================================================

class TestComputeMetrics:
    """Tests for precision/recall/F1 computation."""

    def test_perfect_scores(self):
        m = rh.compute_metrics(["TP"] * 10)
        assert m["precision"] == 1.0
        assert m["recall"] == 1.0
        assert m["f1"] == 1.0
        assert m["accuracy"] == 1.0

    def test_all_wrong(self):
        """All FP + FN = zero precision and recall."""
        m = rh.compute_metrics(["FP"] * 5 + ["FN"] * 5)
        assert m["precision"] == 0.0
        assert m["recall"] == 0.0
        assert m["f1"] == 0.0

    def test_mixed(self):
        m = rh.compute_metrics(["TP", "TP", "FP", "TN", "FN"])
        assert m["tp"] == 2
        assert m["fp"] == 1
        assert m["tn"] == 1
        assert m["fn"] == 1
        # precision = 2/(2+1) = 0.667, recall = 2/(2+1) = 0.667
        assert abs(m["precision"] - 2/3) < 0.01
        assert abs(m["recall"] - 2/3) < 0.01

    def test_empty(self):
        m = rh.compute_metrics([])
        assert m["f1"] == 0


# ===========================================================================
# Tests: replay_with_fix
# ===========================================================================

class TestReplayWithFix:
    """Tests for the proposed-fix logic."""

    @staticmethod
    def _make_original(decision="continue", reasons=None):
        return (
            1, "test_session", decision,
            json.dumps(reasons or []), "{}", 1, time.time(),
            1, 5, 10, 0, 0
        )

    def test_active_session_no_fix(self):
        """Session with tools should not be fixed to halt."""
        features = {"tool_call_count": 10, "message_count": 20,
                    "parent_tool_call_count": 5, "parent_message_count": 10}
        original = self._make_original()
        new_decision, reasons = rh.replay_with_fix(features, "continue", original)
        assert new_decision == "continue"

    def test_dead_session_fixed_to_halt(self):
        """Session with 0 tools and <=2 msgs should be fixed to halt."""
        features = {"tool_call_count": 0, "message_count": 1,
                    "parent_tool_call_count": 5, "parent_message_count": 10}
        original = self._make_original()
        new_decision, reasons = rh.replay_with_fix(features, "continue", original)
        assert new_decision == "halt"
        assert any("FIX" in r for r in reasons)

    def test_zero_tools_many_msgs_not_fixed(self):
        """Session with 0 tools but >2 msgs should NOT be fixed."""
        features = {"tool_call_count": 0, "message_count": 50,
                    "parent_tool_call_count": 5, "parent_message_count": 10}
        original = self._make_original()
        new_decision, reasons = rh.replay_with_fix(features, "continue", original)
        assert new_decision == "continue"

    def test_parent_dead_fixed(self):
        """Session with dead parent should be fixed."""
        features = {
            "tool_call_count": 5, "message_count": 10,
            "parent_tool_call_count": 0, "parent_message_count": 1,
        }
        original = self._make_original()
        new_decision, reasons = rh.replay_with_fix(features, "continue", original)
        assert new_decision == "halt"

    def test_no_features(self):
        """None features should not crash."""
        original = self._make_original()
        new_decision, reasons = rh.replay_with_fix(None, "continue", original)
        assert new_decision == "continue"

    def test_many_unproductive_children_fixed(self):
        """Session with many unproductive children should be fixed."""
        features = {
            "tool_call_count": 5, "message_count": 10,
            "child_count": 20, "productive_children": 1,
        }
        original = self._make_original()
        new_decision, reasons = rh.replay_with_fix(features, "continue", original)
        assert new_decision == "halt"
