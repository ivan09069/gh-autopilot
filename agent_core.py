#!/usr/bin/env python3
"""Pure policy and diff helpers for the PR-only repository agent."""
from __future__ import annotations

import fnmatch
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

REDACTIONS: tuple[re.Pattern[str], ...] = (
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?i)(authorization:\s*(?:bearer|token)\s+)[^\s]+"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password)\s*[=:]\s*)[^\s]+"),
)


class PolicyError(RuntimeError):
    """Fail-closed policy rejection."""


class CommandError(RuntimeError):
    """Trusted supervisor command failure."""


@dataclass(frozen=True)
class DiffStats:
    files: tuple[str, ...]
    changed_lines: int


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str
    author: str
    updated_at: str
    html_url: str

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "number": self.number,
                "title": self.title,
                "body": self.body,
                "author": self.author,
                "updated_at": self.updated_at,
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def redact(text: str) -> str:
    out = text
    for pattern in REDACTIONS:
        if pattern.pattern.startswith("(?i)(authorization") or "api[_-]?key" in pattern.pattern:
            out = pattern.sub(r"\1[REDACTED]", out)
        else:
            out = pattern.sub("[REDACTED]", out)
    return out


def run(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    timeout: int = 300,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        list(args),
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        input=input_text,
        stdin=None if input_text is not None else subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout,
        shell=False,
    )
    if check and proc.returncode != 0:
        cmd = " ".join(shlex.quote(x) for x in args)
        raise CommandError(
            f"command failed ({proc.returncode}): {cmd}\n"
            f"stdout: {redact(proc.stdout[-4000:])}\n"
            f"stderr: {redact(proc.stderr[-4000:])}"
        )
    return proc


def load_policy(path: Path) -> dict[str, Any]:
    policy = json.loads(path.read_text(encoding="utf-8"))
    required = ["repository", "default_branch", "trigger_label", "branch_prefix", "agent"]
    missing = [k for k in required if not policy.get(k)]
    if missing:
        raise PolicyError(f"policy missing required keys: {', '.join(missing)}")
    if policy["default_branch"].startswith(policy["branch_prefix"]):
        raise PolicyError("default branch must never match the agent branch prefix")
    if policy.get("pr", {}).get("draft") is not True:
        raise PolicyError("PR policy must require draft=true")
    return policy


def branch_for_issue(policy: dict[str, Any], issue_number: int) -> str:
    branch = f"{policy['branch_prefix']}issue-{issue_number}"
    validate_push_branch(policy, branch)
    return branch


def validate_push_branch(policy: dict[str, Any], branch: str) -> None:
    prefix = policy["branch_prefix"]
    default = policy["default_branch"]
    if branch == default or not branch.startswith(prefix):
        raise PolicyError(f"push denied for branch {branch!r}; only {prefix}* is writable")
    if branch in {"main", "master", "production", "prod", "release"}:
        raise PolicyError(f"push denied for protected branch name {branch!r}")


def path_is_protected(path: str, patterns: Iterable[str]) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    parts = normalized.split("/")
    for raw_pattern in patterns:
        pattern = raw_pattern.replace("\\", "/").lstrip("./")
        if fnmatch.fnmatch(normalized, pattern):
            return True

        # Python's fnmatch does not give ** recursive-directory semantics.
        # Treat a trailing /** as an explicit protected subtree.
        if pattern.endswith("/**"):
            prefix = pattern[:-3].rstrip("/")
            if normalized == prefix or normalized.startswith(prefix + "/"):
                return True

        # Also support recursive patterns such as **/secrets/** and
        # **/credentials/** by testing every path suffix.
        if pattern.startswith("**/"):
            tail = pattern[3:]
            for i in range(len(parts)):
                suffix = "/".join(parts[i:])
                if fnmatch.fnmatch(suffix, tail):
                    return True
                if tail.endswith("/**"):
                    prefix = tail[:-3].rstrip("/")
                    if suffix == prefix or suffix.startswith(prefix + "/"):
                        return True
    return False


def enforce_diff_policy(policy: dict[str, Any], stats: DiffStats) -> None:
    limits = policy.get("limits", {})
    max_files = int(limits.get("max_changed_files", 25))
    max_lines = int(limits.get("max_changed_lines", 1200))
    if len(stats.files) == 0:
        raise PolicyError("agent produced no file changes")
    if len(stats.files) > max_files:
        raise PolicyError(f"diff has {len(stats.files)} files; limit is {max_files}")
    if stats.changed_lines > max_lines:
        raise PolicyError(f"diff has {stats.changed_lines} changed lines; limit is {max_lines}")
    protected = tuple(policy.get("protected_paths", ()))
    blocked = [p for p in stats.files if path_is_protected(p, protected)]
    if blocked:
        raise PolicyError("protected paths changed: " + ", ".join(blocked))


def diff_stats(repo: Path, base_ref: str) -> DiffStats:
    names = run(["git", "diff", "--name-only", f"{base_ref}...HEAD"], cwd=repo).stdout.splitlines()
    names += run(["git", "ls-files", "--others", "--exclude-standard"], cwd=repo).stdout.splitlines()
    files = tuple(sorted({p.strip() for p in names if p.strip()}))

    numstat = run(["git", "diff", "--numstat", f"{base_ref}...HEAD"], cwd=repo).stdout.splitlines()
    changed_lines = 0
    for line in numstat:
        parts = line.split("\t")
        if len(parts) >= 2:
            for value in parts[:2]:
                if value.isdigit():
                    changed_lines += int(value)
    for path in run(["git", "ls-files", "--others", "--exclude-standard"], cwd=repo).stdout.splitlines():
        p = repo / path
        if p.is_file():
            try:
                changed_lines += len(p.read_text(encoding="utf-8", errors="replace").splitlines())
            except OSError:
                changed_lines += 1
    return DiffStats(files=files, changed_lines=changed_lines)
