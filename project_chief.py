#!/usr/bin/env python3
"""Deterministic project-chief kernel for gh-autopilot.

The module converts an open-ended stream of ideas/tasks into finite release packets.
It does not perform network operations, merges, deployments, financial actions,
secret handling, or destructive changes. Those remain policy-gated elsewhere.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

VALID_STATUSES = {
    "idea",
    "shaped",
    "buildable",
    "building",
    "review",
    "released",
    "monitoring",
    "blocked",
    "abandoned",
}

HUMAN_GATED_ACTIONS = {
    "delete_data",
    "external_transmission",
    "live_financial_action",
    "merge_default_branch",
    "production_deploy",
    "secrets_or_credentials",
    "spend_money",
    "visibility_change",
}

DEFAULT_GROUPS = (
    "control-plane",
    "evidence-forensics",
    "blockchain-tooling",
    "agent-platforms",
    "trading-research",
    "industrial-ai",
    "unclassified",
)


@dataclass(frozen=True)
class Task:
    task_id: str
    title: str
    group: str = "unclassified"
    status: str = "idea"
    impact: int = 3
    urgency: int = 3
    effort: int = 3
    blocked: bool = False
    requested_actions: tuple[str, ...] = ()
    repository: str | None = None

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "Task":
        missing = [key for key in ("task_id", "title") if not data.get(key)]
        if missing:
            raise ValueError(f"missing required field(s): {', '.join(missing)}")

        status = str(data.get("status", "idea"))
        if status not in VALID_STATUSES:
            raise ValueError(f"unknown status: {status}")

        def bounded(name: str, default: int) -> int:
            value = int(data.get(name, default))
            if not 1 <= value <= 5:
                raise ValueError(f"{name} must be between 1 and 5")
            return value

        actions = tuple(str(x) for x in data.get("requested_actions", ()))
        return cls(
            task_id=str(data["task_id"]),
            title=str(data["title"]),
            group=str(data.get("group", "unclassified")),
            status=status,
            impact=bounded("impact", 3),
            urgency=bounded("urgency", 3),
            effort=bounded("effort", 3),
            blocked=bool(data.get("blocked", False)),
            requested_actions=actions,
            repository=(str(data["repository"]) if data.get("repository") else None),
        )


def requires_human(task: Task) -> bool:
    """Return True if a task requests an action reserved for the principal."""
    return bool(HUMAN_GATED_ACTIONS.intersection(task.requested_actions))


def priority_score(task: Task) -> int:
    """Deterministic execution score; higher means earlier in the build queue.

    Impact and urgency dominate. Lower-effort work receives only a small bonus so
    the chief does not optimize for easy-but-low-value work.
    """
    if task.blocked or task.status in {"blocked", "released", "monitoring", "abandoned"}:
        return -1
    return task.impact * 20 + task.urgency * 15 + (6 - task.effort) * 4


def next_state(task: Task) -> str:
    """Recommend the next finite state without pretending an open system is 'done'."""
    if task.blocked or task.status == "blocked":
        return "blocked"
    if requires_human(task):
        return "human_approval"
    transitions = {
        "idea": "shaped",
        "shaped": "buildable",
        "buildable": "building",
        "building": "review",
        "review": "released",
        "released": "monitoring",
        "monitoring": "monitoring",
        "abandoned": "abandoned",
    }
    return transitions[task.status]


def build_plan(tasks: Iterable[Task], max_per_group: int = 3) -> dict[str, Any]:
    if max_per_group < 1:
        raise ValueError("max_per_group must be >= 1")

    grouped: dict[str, list[Task]] = {}
    approvals: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []

    for task in tasks:
        record = {
            "task_id": task.task_id,
            "title": task.title,
            "repository": task.repository,
            "group": task.group,
            "status": task.status,
        }
        if task.blocked or task.status == "blocked":
            blocked.append(record)
            continue
        if requires_human(task):
            record["gates"] = sorted(HUMAN_GATED_ACTIONS.intersection(task.requested_actions))
            approvals.append(record)
            continue
        if priority_score(task) < 0:
            continue
        grouped.setdefault(task.group, []).append(task)

    execution_groups: list[dict[str, Any]] = []
    for group in sorted(grouped):
        ranked = sorted(
            grouped[group],
            key=lambda t: (-priority_score(t), t.task_id),
        )[:max_per_group]
        execution_groups.append(
            {
                "group": group,
                "release_packets": [
                    {
                        "task_id": task.task_id,
                        "title": task.title,
                        "repository": task.repository,
                        "priority_score": priority_score(task),
                        "current_state": task.status,
                        "next_state": next_state(task),
                    }
                    for task in ranked
                ],
            }
        )

    approvals.sort(key=lambda x: x["task_id"])
    blocked.sort(key=lambda x: x["task_id"])
    return {
        "execution_groups": execution_groups,
        "human_approval_queue": approvals,
        "blocked_queue": blocked,
        "policy": {
            "writes": "proposal-branch-and-draft-pr-only",
            "principal_gates": sorted(HUMAN_GATED_ACTIONS),
            "operating_rule": (
                "Open-ended projects stay active; the chief closes finite release packets, "
                "then returns the system to monitoring or shapes the next packet."
            ),
        },
    }


def load_tasks(path: Path) -> list[Task]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("task file must contain a JSON array")
    return [Task.from_mapping(item) for item in raw]


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a deterministic project-chief execution plan.")
    parser.add_argument("tasks", type=Path, help="JSON array of task records")
    parser.add_argument("--max-per-group", type=int, default=3)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()

    plan = build_plan(load_tasks(args.tasks), max_per_group=args.max_per_group)
    print(json.dumps(plan, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
