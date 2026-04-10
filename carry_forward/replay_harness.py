#!/usr/bin/env python3
"""
Replay Harness for carry_forward v5.

Re-runs check_can_continue() against historical sessions and measures
how well the decision logic predicts actual session outcomes.

Usage:
    python3 replay_harness.py                  # Full replay with metrics
    python3 replay_harness.py --quick          # Quick summary only
    python3 replay_harness.py --session ID     # Replay single session
    python3 replay_harness.py --fixes          # Show what fixes would change
    python3 replay_harness.py --compare        # Compare current vs fixed logic

Metrics:
    Precision: Of sessions we said "continue", how many were productive?
    Recall:    Of productive sessions, how many did we say "continue" for?
    F1:        Harmonic mean of precision and recall
    Accuracy:  Overall correct decisions / total decisions
"""

import sqlite3
import sys
import os
import time
import json
from datetime import datetime

STATE_DB = os.path.expanduser("~/.hermes/state.db")
CARRY_DB = os.path.expanduser("~/.hermes/carry_forward.db")


def get_state_conn():
    return sqlite3.connect(STATE_DB)


def get_carry_conn():
    return sqlite3.connect(CARRY_DB)


def get_sessions_with_decisions():
    """Get all sessions that have both a decision and an outcome."""
    conn = get_carry_conn()
    rows = conn.execute("""
        SELECT dl.id, dl.session_id, dl.decision, dl.reasons_json,
               dl.thresholds_json, dl.can_continue, dl.created_at,
               do.outcome_productive, do.outcome_tool_calls,
               do.outcome_message_count, do.outcome_git_moved,
               do.outcome_chain_continued
        FROM decision_log dl
        JOIN decision_outcomes do ON dl.id = do.decision_id
        ORDER BY dl.created_at
    """).fetchall()
    conn.close()
    return rows


def get_session_features(session_id):
    """Pull features from state.db for a session."""
    conn = get_state_conn()
    row = conn.execute("""
        SELECT source, message_count, tool_call_count, started_at,
               parent_session_id, model
        FROM sessions WHERE id = ?
    """, (session_id,)).fetchone()

    if not row:
        conn.close()
        return None

    features = {
        "source": row[0],
        "message_count": row[1],
        "tool_call_count": row[2],
        "started_at": row[3],
        "parent_session_id": row[4],
        "model": row[5],
    }

    # Get parent features if exists
    if row[4]:
        parent = conn.execute("""
            SELECT source, message_count, tool_call_count
            FROM sessions WHERE id = ?
        """, (row[4],)).fetchone()
        if parent:
            features["parent_source"] = parent[0]
            features["parent_message_count"] = parent[1]
            features["parent_tool_call_count"] = parent[2]

    # Count children
    children = conn.execute("""
        SELECT COUNT(*), SUM(CASE WHEN tool_call_count > 0 THEN 1 ELSE 0 END)
        FROM sessions WHERE parent_session_id = ?
    """, (session_id,)).fetchone()
    features["child_count"] = children[0]
    features["productive_children"] = children[1] or 0

    # Chain depth
    depth = 0
    current = row[4]
    visited = set()
    while current and current not in visited:
        visited.add(current)
        depth += 1
        current = conn.execute(
            "SELECT parent_session_id FROM sessions WHERE id = ?",
            (current,)
        ).fetchone()
        if not current:
            break
        current = current[0]
    features["chain_depth"] = depth

    conn.close()
    return features


def classify_outcome(row):
    """Classify a decision as TP, FP, TN, FN."""
    decision_id, session_id, decision, reasons_json, thresholds_json, \
        can_continue, created_at, productive, tool_calls, msg_count, \
        git_moved, chain_continued = row

    actual_positive = productive == 1  # Session was productive
    predicted_positive = decision == "continue"  # We said continue

    if predicted_positive and actual_positive:
        return "TP"  # Correctly continued
    elif predicted_positive and not actual_positive:
        return "FP"  # Continued but shouldn't have
    elif not predicted_positive and actual_positive:
        return "FN"  # Halted a productive session
    else:
        return "TN"  # Correctly halted


def compute_metrics(classifications):
    """Compute precision, recall, F1, accuracy."""
    tp = classifications.count("TP")
    fp = classifications.count("FP")
    tn = classifications.count("TN")
    fn = classifications.count("FN")

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0
    accuracy = (tp + tn) / (tp + fp + tn + fn) if (tp + fp + tn + fn) > 0 else 0

    return {
        "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        "precision": precision, "recall": recall, "f1": f1, "accuracy": accuracy,
        "total": tp + fp + tn + fn,
    }


def replay_with_fix(session_features, decision, original_row):
    """
    Apply proposed fixes and return what the new decision would be.
    Returns (new_decision, reasons).
    """
    decision_id, session_id, orig_decision, reasons_json, thresholds_json, \
        can_continue, created_at, productive, tool_calls, msg_count, \
        git_moved, chain_continued = original_row

    reasons = json.loads(reasons_json) if reasons_json else []

    # Fix 1: Don't continue empty sessions (tool_call_count == 0 in the session)
    # The decision is about whether to continue FROM this session.
    # If this session did nothing, there's nothing to continue.
    if session_features and session_features.get("tool_call_count", 0) == 0:
        if session_features.get("message_count", 0) <= 2:
            return "halt", reasons + ["FIX: session has no tool calls and <=2 messages"]

    # Fix 2: Don't continue if parent was a dead session
    if session_features and session_features.get("parent_tool_call_count", 0) == 0:
        if session_features.get("parent_message_count", 0) <= 2:
            return "halt", reasons + ["FIX: parent session was dead"]

    # Fix 3: Consider child productivity rate
    if session_features and session_features.get("child_count", 0) > 5:
        child_total = session_features["child_count"]
        child_productive = session_features.get("productive_children", 0)
        if child_total > 0 and child_productive / child_total < 0.1:
            return "halt", reasons + [f"FIX: {child_productive}/{child_total} children productive"]

    return decision, reasons


def run_replay():
    """Run the full replay and print metrics."""
    rows = get_sessions_with_decisions()
    if not rows:
        print("No decisions with outcomes to replay.")
        return

    print(f"=== CARRY FORWARD REPLAY HARNESS ===")
    print(f"Replaying {len(rows)} decisions\n")

    classifications = []
    fix_classifications = []
    misclassified = []
    fix_changed = []

    for row in rows:
        decision_id, session_id, decision, reasons_json, thresholds_json, \
            can_continue, created_at, productive, tool_calls, msg_count, \
            git_moved, chain_continued = row

        cls = classify_outcome(row)
        classifications.append(cls)

        # Get session features for fix evaluation
        features = get_session_features(session_id)

        # Apply proposed fixes
        new_decision, fix_reasons = replay_with_fix(features, decision, row)
        new_cls = classify_outcome((
            decision_id, session_id, new_decision, json.dumps(fix_reasons),
            thresholds_json, 1 if new_decision == "continue" else 0,
            created_at, productive, tool_calls, msg_count, git_moved, chain_continued
        ))
        fix_classifications.append(new_cls)

        if cls in ("FP", "FN"):
            misclassified.append({
                "decision_id": decision_id,
                "session_id": session_id,
                "classification": cls,
                "decision": decision,
                "productive": productive,
                "tool_calls": tool_calls,
                "msg_count": msg_count,
                "reasons": reasons_json,
                "features": features,
            })

        if new_decision != decision:
            fix_changed.append({
                "session_id": session_id,
                "old": decision,
                "new": new_decision,
                "productive": productive,
                "tool_calls": tool_calls,
                "msg_count": msg_count,
            })

    # Current metrics
    metrics = compute_metrics(classifications)
    fix_metrics = compute_metrics(fix_classifications)

    print("CURRENT LOGIC:")
    print(f"  TP={metrics['tp']} FP={metrics['fp']} TN={metrics['tn']} FN={metrics['fn']}")
    print(f"  Precision={metrics['precision']:.3f} Recall={metrics['recall']:.3f} F1={metrics['f1']:.3f} Accuracy={metrics['accuracy']:.3f}")

    print()
    print("WITH PROPOSED FIXES:")
    print(f"  TP={fix_metrics['tp']} FP={fix_metrics['fp']} TN={fix_metrics['tn']} FN={fix_metrics['fn']}")
    print(f"  Precision={fix_metrics['precision']:.3f} Recall={fix_metrics['recall']:.3f} F1={fix_metrics['f1']:.3f} Accuracy={fix_metrics['accuracy']:.3f}")

    print()
    print(f"DECISIONS CHANGED BY FIXES: {len(fix_changed)}/{len(rows)}")

    # Show what the fixes changed
    if fix_changed:
        improved = sum(1 for f in fix_changed
                       if (f["new"] == "halt" and not f["productive"]) or
                          (f["new"] == "continue" and f["productive"]))
        regressed = sum(1 for f in fix_changed
                        if (f["new"] == "halt" and f["productive"]) or
                           (f["new"] == "continue" and not f["productive"]))
        print(f"  IMPROVED (correct fix): {improved}")
        print(f"  REGRESSED (wrong fix):  {regressed}")

    # Breakdown by reason
    print("\n--- BREAKDOWN BY REASON ---")
    reason_stats = {}
    for row in rows:
        reasons = json.loads(row[3]) if row[3] else []
        key = reasons[0] if reasons else "(no reason)"
        if key not in reason_stats:
            reason_stats[key] = {"total": 0, "productive": 0, "decision": row[2]}
        reason_stats[key]["total"] += 1
        if row[7] == 1:
            reason_stats[key]["productive"] += 1

    for reason, stats in sorted(reason_stats.items(), key=lambda x: -x[1]["total"]):
        pct = 100.0 * stats["productive"] / stats["total"] if stats["total"] > 0 else 0
        print(f"  {reason:50s} | {stats['decision']:8s} | {stats['total']:3d} total | {pct:5.1f}% productive")

    return metrics, fix_metrics, misclassified, fix_changed


def show_misclassified(limit=20):
    """Show details of misclassified sessions."""
    rows = get_sessions_with_decisions()
    print(f"\n=== MISCLASSIFIED SESSIONS (showing {limit}) ===\n")

    count = 0
    for row in rows:
        cls = classify_outcome(row)
        if cls in ("FP", "FN"):
            decision_id, session_id, decision, reasons_json, _, _, _, \
                productive, tool_calls, msg_count, git_moved, chain_continued = row

            features = get_session_features(session_id)
            src = features["source"] if features else "?"
            parent_tools = features.get("parent_tool_call_count", "?") if features else "?"

            print(f"  [{cls}] {session_id}")
            print(f"       decision={decision} productive={productive} tools={tool_calls} msgs={msg_count}")
            print(f"       source={src} parent_tools={parent_tools}")
            print(f"       reasons={reasons_json}")
            print()
            count += 1
            if count >= limit:
                break


def show_fixes():
    """Show what the proposed fixes would change."""
    rows = get_sessions_with_decisions()
    print(f"\n=== PROPOSED FIXES ===\n")

    fix_applied = {"FIX: session has no tool calls and <=2 messages": 0,
                   "FIX: parent session was dead": 0,
                   "FIX: children productive rate low": 0}

    for row in rows:
        features = get_session_features(row[1])
        new_decision, fix_reasons = replay_with_fix(features, row[2], row)
        if new_decision != row[2]:
            for reason in fix_reasons:
                for key in fix_applied:
                    if key in reason:
                        fix_applied[key] += 1
            productive = row[7]
            correct = (new_decision == "halt" and not productive) or \
                      (new_decision == "continue" and productive)
            symbol = "+" if correct else "-"
            print(f"  [{symbol}] {row[1]}: {row[2]} -> {new_decision} (was {'productive' if productive else 'unproductive'})")

    print(f"\nFix impact:")
    for fix, count in fix_applied.items():
        print(f"  {fix}: {count} decisions changed")


def replay_single(session_id):
    """Replay a single session in detail."""
    conn = get_carry_conn()
    rows = conn.execute("""
        SELECT dl.id, dl.session_id, dl.decision, dl.reasons_json,
               dl.thresholds_json, dl.can_continue, dl.created_at,
               do.outcome_productive, do.outcome_tool_calls,
               do.outcome_message_count, do.outcome_git_moved,
               do.outcome_chain_continued
        FROM decision_log dl
        JOIN decision_outcomes do ON dl.id = do.decision_id
        WHERE dl.session_id = ?
        ORDER BY dl.created_at
    """, (session_id,)).fetchall()
    conn.close()

    if not rows:
        print(f"No decision found for session {session_id}")
        return

    for row in rows:
        decision_id, sid, decision, reasons_json, thresholds_json, \
            can_continue, created_at, productive, tool_calls, msg_count, \
            git_moved, chain_continued = row

        cls = classify_outcome(row)
        features = get_session_features(sid)

        print(f"=== REPLAY: {sid} ===")
        print(f"  Decision: {decision} (can_continue={can_continue})")
        print(f"  Reasons: {reasons_json}")
        print(f"  Classification: {cls}")
        print(f"  Outcome: productive={productive} tools={tool_calls} msgs={msg_count}")
        if features:
            print(f"  Features: source={features['source']} parent_tools={features.get('parent_tool_call_count', '?')}")
            print(f"  Chain depth: {features.get('chain_depth', 0)}")
            print(f"  Children: {features.get('child_count', 0)} ({features.get('productive_children', 0)} productive)")

        new_decision, fix_reasons = replay_with_fix(features, decision, row)
        if new_decision != decision:
            print(f"  FIX would change to: {new_decision}")
            for r in fix_reasons:
                if r.startswith("FIX:"):
                    print(f"    Reason: {r}")
        print()


if __name__ == "__main__":
    args = sys.argv[1:]

    if "--session" in args:
        idx = args.index("--session")
        if idx + 1 < len(args):
            replay_single(args[idx + 1])
        else:
            print("Usage: replay_harness.py --session SESSION_ID")
    elif "--fixes" in args:
        show_fixes()
    elif "--misclassified" in args:
        show_misclassified()
    elif "--compare" in args:
        run_replay()
    else:
        metrics, fix_metrics, misclassified, fix_changed = run_replay()
        print(f"\n--- SUMMARY ---")
        print(f"  Current F1: {metrics['f1']:.3f} (P={metrics['precision']:.3f} R={metrics['recall']:.3f})")
        print(f"  Fixed F1:   {fix_metrics['f1']:.3f} (P={fix_metrics['precision']:.3f} R={fix_metrics['recall']:.3f})")
        delta = fix_metrics['f1'] - metrics['f1']
        print(f"  Delta:      {delta:+.3f} ({'IMPROVEMENT' if delta > 0 else 'REGRESSION' if delta < 0 else 'NO CHANGE'})")
