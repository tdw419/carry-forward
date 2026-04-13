"""Roadmap bridge for Carry Forward.

Scans detected project dirs for roadmap YAML files, parses them with
roadmap_builder, and provides structured progress data for:
  - cmd_context / get_context_data (shows current phase + next deliverable)
  - check_can_continue (roadmap completion as a positive/negative signal)
  - cmd_roadmap (new CLI subcommand)
"""

from __future__ import annotations

import os
import glob
from typing import Any, Dict, List, Optional

try:
    from roadmap_builder import parse_yaml, PhaseStatus, DeliverableStatus
    HAS_ROADMAP_BUILDER = True
except ImportError:
    HAS_ROADMAP_BUILDER = False

# Files to look for (in order of priority)
ROADMAP_FILE_PATTERNS = [
    "roadmap.yaml",
    "*_roadmap.yaml",
    "ROADMAP.yaml",
    "roadmap.yml",
]


def find_roadmap(project_dir: str) -> Optional[str]:
    """Find a roadmap YAML file in or near a project directory."""
    # Check the project dir itself
    for pattern in ROADMAP_FILE_PATTERNS:
        matches = glob.glob(os.path.join(project_dir, pattern))
        if matches:
            return matches[0]

    # Check parent dirs (up to 2 levels)
    check = project_dir
    for _ in range(2):
        parent = os.path.dirname(check)
        if parent == check or not parent:
            break
        for pattern in ROADMAP_FILE_PATTERNS:
            matches = glob.glob(os.path.join(parent, pattern))
            if matches:
                return matches[0]
        check = parent

    return None


def scan_project_roadmaps(project_dirs: List[str]) -> List[Dict[str, Any]]:
    """Scan a list of project dirs for roadmaps, return structured data.

    Returns list of dicts:
      {
        "project": "/path/to/project",
        "roadmap_file": "/path/to/roadmap.yaml",
        "title": "Geometry OS Roadmap",
        "phases_total": 14,
        "phases_complete": 8,
        "deliverables_total": 59,
        "deliverables_done": 33,
        "current_phase": {"id": "phase-8", "title": "Bare-Metal RV64", ...},
        "next_deliverables": [...],
        "progress_pct": 56.0,
      }
    """
    if not HAS_ROADMAP_BUILDER:
        return []

    results = []
    seen_files = set()
    for d in project_dirs:
        roadmap_file = find_roadmap(d)
        if not roadmap_file:
            continue

        # Deduplicate: same roadmap file found from multiple project dirs
        if roadmap_file in seen_files:
            continue
        seen_files.add(roadmap_file)

        try:
            roadmap = parse_yaml(roadmap_file)
        except Exception:
            continue

        phases_total = len(roadmap.phases)
        phases_complete = sum(1 for p in roadmap.phases if p.status == PhaseStatus.COMPLETE)
        all_deliverables = [dl for p in roadmap.phases for dl in p.deliverables]
        deliverables_total = len(all_deliverables)
        deliverables_done = sum(1 for d in all_deliverables if d.status == DeliverableStatus.DONE)

        # Find the current phase (first non-complete phase)
        current_phase = None
        next_deliverables = []
        for p in roadmap.phases:
            if p.status != PhaseStatus.COMPLETE:
                current_phase = {
                    "id": p.id,
                    "title": p.title,
                    "status": p.status.value,
                    "goal": p.goal,
                }
                next_deliverables = [
                    {
                        "name": d.name,
                        "description": d.description,
                        "status": d.status.value,
                    }
                    for d in p.deliverables
                    if d.status != DeliverableStatus.DONE
                ][:5]
                break

        progress_pct = (deliverables_done / deliverables_total * 100) if deliverables_total > 0 else 0

        results.append({
            "project": d,
            "roadmap_file": roadmap_file,
            "title": roadmap.title,
            "phases_total": phases_total,
            "phases_complete": phases_complete,
            "deliverables_total": deliverables_total,
            "deliverables_done": deliverables_done,
            "current_phase": current_phase,
            "next_deliverables": next_deliverables,
            "progress_pct": round(progress_pct, 1),
        })

    return results


def format_roadmap_context(roadmaps: List[Dict[str, Any]]) -> str:
    """Format roadmap data for inclusion in context output."""
    if not roadmaps:
        return ""

    lines = []
    for r in roadmaps:
        lines.append(f"  {r['title']} ({r['roadmap_file']})")
        lines.append(f"    Progress: {r['phases_complete']}/{r['phases_total']} phases, "
                     f"{r['deliverables_done']}/{r['deliverables_total']} deliverables "
                     f"({r['progress_pct']}%)")

        cp = r.get("current_phase")
        if cp:
            lines.append(f"    Current phase: {cp['id']} -- {cp['title']} ({cp['status']})")
            if cp.get("goal"):
                lines.append(f"    Goal: {cp['goal'][:120]}")

        nd = r.get("next_deliverables", [])
        if nd:
            lines.append("    Next deliverables:")
            for d in nd[:3]:
                lines.append(f"      - {d['name']}: {d['description'][:80]}")

        lines.append("")

    return "\n".join(lines)


def roadmap_completion_signal(roadmaps: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Evaluate roadmap completion as a signal for check_can_continue.

    Returns:
      {
        "all_complete": bool,      # every deliverable in every roadmap is done
        "any_in_progress": bool,   # at least one roadmap has incomplete work
        "details": str,            # human-readable summary
      }
    """
    if not roadmaps:
        return {"all_complete": False, "any_in_progress": False,
                "details": "no roadmaps found for tracked projects"}

    all_complete = True
    any_in_progress = False
    parts = []

    for r in roadmaps:
        if r["deliverables_done"] < r["deliverables_total"]:
            all_complete = False
            any_in_progress = True
            remaining = r["deliverables_total"] - r["deliverables_done"]
            parts.append(f"{r['title']}: {remaining} deliverables remaining")
        else:
            parts.append(f"{r['title']}: ALL DELIVERABLES COMPLETE")

    return {
        "all_complete": all_complete,
        "any_in_progress": any_in_progress,
        "details": "; ".join(parts),
    }
