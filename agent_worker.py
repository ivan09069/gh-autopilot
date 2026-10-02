#!/usr/bin/env python3
"""Always-on supervisor for a policy-gated, PR-only repository agent.

The untrusted agent edits only a local checkout. The trusted supervisor owns
GitHub credentials and can only push an agent/* proposal branch and open a
draft pull request. No merge, auto-merge, deploy, or release operation exists.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time
from typing import Any, Sequence

from agent_core import (
    CommandError, DiffStats, Issue, PolicyError, branch_for_issue, diff_stats,
    enforce_diff_policy, load_policy, redact, run, utc_now, validate_push_branch,
)


class State:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                issue_number INTEGER PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                status TEXT NOT NULL,
                branch TEXT,
                pr_url TEXT,
                detail TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.db.commit()

    def get(self, issue_number: int) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT fingerprint,status,branch,pr_url,detail,updated_at FROM jobs WHERE issue_number=?",
            (issue_number,),
        ).fetchone()
        if not row:
            return None
        return dict(zip(("fingerprint", "status", "branch", "pr_url", "detail", "updated_at"), row))

    def put(
        self,
        issue_number: int,
        fingerprint: str,
        status: str,
        *,
        branch: str | None = None,
        pr_url: str | None = None,
        detail: str = "",
    ) -> None:
        self.db.execute(
            """
            INSERT INTO jobs(issue_number,fingerprint,status,branch,pr_url,detail,updated_at)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(issue_number) DO UPDATE SET
                fingerprint=excluded.fingerprint,
                status=excluded.status,
                branch=excluded.branch,
                pr_url=excluded.pr_url,
                detail=excluded.detail,
                updated_at=excluded.updated_at
            """,
            (issue_number, fingerprint, status, branch, pr_url, redact(detail)[:12000], utc_now()),
        )
        self.db.commit()


class Supervisor:
    def __init__(self, policy_path: Path, dry_run: bool = False) -> None:
        self.policy_path = policy_path.resolve()
        self.policy = load_policy(self.policy_path)
        self.dry_run = dry_run
        self.repo_name = self.policy["repository"]
        self.default_branch = self.policy["default_branch"]
        root = Path(os.path.expanduser(self.policy.get("workspace_root", "~/.gh-autopilot-worker"))).resolve()
        self.root = root
        self.checkout = root / "repo"
        self.logs = root / "logs"
        self.sandbox_home = root / "agent-home"
        self.state = State(root / "state.sqlite3")
        self.root.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(parents=True, exist_ok=True)
        self.sandbox_home.mkdir(parents=True, exist_ok=True)

    def preflight(self) -> None:
        for exe in ("git", "gh"):
            if shutil.which(exe) is None:
                raise PolicyError(f"required executable not found: {exe}")
        agent_cmd = self.policy["agent"]["command"]
        if not agent_cmd or shutil.which(agent_cmd[0]) is None:
            raise PolicyError(f"agent executable not found: {agent_cmd[0] if agent_cmd else '<empty>'}")
        run(["gh", "auth", "status"], timeout=30)
        actual = run(["gh", "repo", "view", self.repo_name, "--json", "nameWithOwner,defaultBranchRef"]).stdout
        info = json.loads(actual)
        if info.get("nameWithOwner") != self.repo_name:
            raise PolicyError("authenticated GitHub repository identity mismatch")
        if (info.get("defaultBranchRef") or {}).get("name") != self.default_branch:
            raise PolicyError("policy default branch does not match GitHub")

    def list_ready_issues(self) -> list[Issue]:
        label = self.policy["trigger_label"]
        query = [
            "gh", "issue", "list", "--repo", self.repo_name,
            "--state", "open", "--label", label,
            "--limit", "25",
            "--json", "number,title,body,author,updatedAt,url",
        ]
        raw = json.loads(run(query).stdout or "[]")
        out: list[Issue] = []
        max_body = int(self.policy.get("limits", {}).get("max_issue_body_chars", 12000))
        allowed = set(self.policy.get("allowed_issue_authors", []))
        for item in raw:
            author = (item.get("author") or {}).get("login", "")
            if allowed and author not in allowed:
                continue
            body = (item.get("body") or "")[:max_body]
            out.append(
                Issue(
                    number=int(item["number"]),
                    title=item.get("title") or "",
                    body=body,
                    author=author,
                    updated_at=item.get("updatedAt") or "",
                    html_url=item.get("url") or "",
                )
            )
        return out

    def should_process(self, issue: Issue) -> bool:
        prior = self.state.get(issue.number)
        if not prior:
            return True
        if prior["status"] in {"pr_open", "running"}:
            return False
        return prior["fingerprint"] != issue.fingerprint

    def sync_checkout(self) -> None:
        clone_url = f"https://github.com/{self.repo_name}.git"
        if not (self.checkout / ".git").is_dir():
            if self.checkout.exists():
                shutil.rmtree(self.checkout)
            run(["git", "clone", "--origin", "origin", clone_url, str(self.checkout)], timeout=300)
        run(["git", "fetch", "origin", "--prune"], cwd=self.checkout, timeout=120)
        run(["git", "checkout", "-B", self.default_branch, f"origin/{self.default_branch}"], cwd=self.checkout)
        run(["git", "reset", "--hard", f"origin/{self.default_branch}"], cwd=self.checkout)
        run(["git", "clean", "-ffd"], cwd=self.checkout)

    def prepare_branch(self, issue: Issue) -> str:
        branch = branch_for_issue(self.policy, issue.number)
        existing = run(
            ["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
            cwd=self.checkout,
            check=False,
        ).stdout.strip()
        if existing:
            raise PolicyError(
                f"remote branch {branch} already exists; refusing to overwrite an existing proposal"
            )
        run(["git", "checkout", "-b", branch, f"origin/{self.default_branch}"], cwd=self.checkout)
        return branch

    def agent_env(self) -> dict[str, str]:
        env = os.environ.copy()
        for key in list(env):
            upper = key.upper()
            if upper in {"GH_TOKEN", "GITHUB_TOKEN", "GITHUB_PAT"} or any(
                word in upper for word in ("GITHUB_TOKEN", "GH_TOKEN")
            ):
                env.pop(key, None)
        env["GH_CONFIG_DIR"] = str(self.sandbox_home / "gh-empty")
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["AGENT_REPOSITORY"] = self.repo_name
        env["AGENT_DEFAULT_BRANCH"] = self.default_branch
        env["AGENT_PR_ONLY"] = "1"
        return env

    def prompt_for(self, issue: Issue) -> str:
        protected = "\n".join(f"- {p}" for p in self.policy.get("protected_paths", []))
        return f"""You are operating inside a policy-gated repository workspace.

TASK SOURCE
GitHub issue #{issue.number}: {issue.title}
Author: {issue.author}
URL: {issue.html_url}

ISSUE BODY
{issue.body}

HARD EXECUTION RULES
- Work only in the current repository workspace.
- Modify files needed to satisfy the issue, then run relevant local validation.
- Do not push, merge, deploy, publish, release, change repository settings, or contact external services.
- Do not use `gh`, `git push`, `git remote set-url`, or any credential/token discovery command.
- Never read or modify credentials, private keys, wallet/seed material, browser profiles, or files outside this workspace.
- Do not modify the policy/control-plane paths below:
{protected}
- Leave the working tree with the proposed implementation. The supervisor will inspect it and decide whether a draft PR is permitted.
"""

    def run_agent(self, issue: Issue) -> None:
        prompt = self.prompt_for(issue)
        template = self.policy["agent"]["command"]
        args = [str(x).replace("{prompt}", prompt) for x in template]
        timeout = int(self.policy["agent"].get("timeout_seconds", 3600))
        proc = run(args, cwd=self.checkout, env=self.agent_env(), timeout=timeout, check=False)
        log = self.logs / f"issue-{issue.number}-{int(time.time())}.log"
        log.write_text(redact(proc.stdout + "\n--- STDERR ---\n" + proc.stderr), encoding="utf-8")
        if proc.returncode != 0:
            raise CommandError(f"agent exited {proc.returncode}; sanitized log: {log}")

    def run_validation(self) -> list[tuple[str, bool, str]]:
        results: list[tuple[str, bool, str]] = []
        for command in self.policy.get("validation_commands", []):
            args = [str(x) for x in command]
            proc = run(args, cwd=self.checkout, timeout=1800, check=False)
            output = redact((proc.stdout + "\n" + proc.stderr)[-6000:])
            results.append((" ".join(args), proc.returncode == 0, output))
            if proc.returncode != 0:
                break
        return results

    def commit_and_open_pr(
        self,
        issue: Issue,
        branch: str,
        stats: DiffStats,
        validations: list[tuple[str, bool, str]],
    ) -> str:
        validate_push_branch(self.policy, branch)
        if not all(ok for _, ok, _ in validations):
            failed = next(cmd for cmd, ok, _ in validations if not ok)
            raise PolicyError(f"validation failed: {failed}")
        run(["git", "add", "--all"], cwd=self.checkout)
        staged = run(["git", "diff", "--cached", "--name-only"], cwd=self.checkout).stdout.splitlines()
        staged_stats = diff_stats(self.checkout, f"origin/{self.default_branch}")
        if set(staged) != set(staged_stats.files):
            raise PolicyError("staged-file set differs from inspected diff; refusing push")
        enforce_diff_policy(self.policy, staged_stats)
        run(["git", "commit", "-m", f"agent: issue #{issue.number} proposal"], cwd=self.checkout)

        current = run(["git", "branch", "--show-current"], cwd=self.checkout).stdout.strip()
        validate_push_branch(self.policy, current)
        if current != branch:
            raise PolicyError("current branch changed after validation")
        ahead = run(
            ["git", "rev-list", "--count", f"origin/{self.default_branch}..HEAD"], cwd=self.checkout
        ).stdout.strip()
        if ahead != "1":
            raise PolicyError(f"expected exactly one proposal commit; found {ahead}")
        if self.dry_run:
            return "DRY-RUN"

        run(["git", "push", "--set-upstream", "origin", f"HEAD:refs/heads/{branch}"], cwd=self.checkout, timeout=120)

        pr_cfg = self.policy.get("pr", {})
        if pr_cfg.get("draft") is not True:
            raise PolicyError("draft PR requirement unexpectedly disabled")
        title = f"{pr_cfg.get('title_prefix', 'agent: ')}{issue.title}"[:240]
        validation_lines = "\n".join(
            f"- {'PASS' if ok else 'FAIL'} `{cmd}`" for cmd, ok, _ in validations
        ) or "- no configured validation"
        body = (
            f"Automated proposal for #{issue.number}.\n\n"
            "### Policy boundary\n"
            "- Draft PR only; this worker has no merge/deploy codepath.\n"
            f"- Branch: `{branch}`\n"
            f"- Changed files: {len(stats.files)}\n"
            f"- Changed lines: {stats.changed_lines}\n\n"
            "### Validation\n"
            f"{validation_lines}\n\n"
            "Human review is required before any merge."
        )
        cmd = [
            "gh", "pr", "create", "--repo", self.repo_name,
            "--base", self.default_branch,
            "--head", branch,
            "--title", title,
            "--body", body,
            "--draft",
        ]
        pr_url = run(cmd, cwd=self.checkout, timeout=60).stdout.strip()
        if not pr_url.startswith("http"):
            raise CommandError(f"unexpected PR creation output: {redact(pr_url)}")
        return pr_url

    def process(self, issue: Issue) -> None:
        branch = branch_for_issue(self.policy, issue.number)
        self.state.put(issue.number, issue.fingerprint, "running", branch=branch)
        try:
            self.sync_checkout()
            branch = self.prepare_branch(issue)
            self.run_agent(issue)
            stats = diff_stats(self.checkout, f"origin/{self.default_branch}")
            enforce_diff_policy(self.policy, stats)
            validations = self.run_validation()
            pr_url = self.commit_and_open_pr(issue, branch, stats, validations)
            self.state.put(
                issue.number,
                issue.fingerprint,
                "pr_open" if not self.dry_run else "dry_run",
                branch=branch,
                pr_url=pr_url,
                detail=f"files={len(stats.files)} lines={stats.changed_lines}",
            )
            print(f"COMPLETED issue=#{issue.number} branch={branch} pr={pr_url}")
        except Exception as exc:
            self.state.put(
                issue.number,
                issue.fingerprint,
                "blocked",
                branch=branch,
                detail=str(exc),
            )
            print(f"BLOCKED issue=#{issue.number} reason={redact(str(exc))}", file=sys.stderr)
        finally:
            try:
                if self.checkout.exists() and (self.checkout / ".git").exists():
                    run(["git", "checkout", self.default_branch], cwd=self.checkout, check=False)
                    run(["git", "reset", "--hard", f"origin/{self.default_branch}"], cwd=self.checkout, check=False)
                    run(["git", "clean", "-ffd"], cwd=self.checkout, check=False)
            except Exception:
                pass

    def cycle(self) -> int:
        self.preflight()
        ready = self.list_ready_issues()
        candidates = [i for i in ready if self.should_process(i)]
        print(f"STATUS ready={len(ready)} actionable={len(candidates)} repository={self.repo_name}")
        for issue in candidates[:1]:
            self.process(issue)
        return len(candidates)

    def loop(self) -> None:
        seconds = max(60, int(self.policy.get("poll_seconds", 900)))
        while True:
            try:
                self.cycle()
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"CYCLE_BLOCKED {redact(str(exc))}", file=sys.stderr)
            time.sleep(seconds)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="PR-only repository agent supervisor")
    p.add_argument("--policy", type=Path, default=Path(__file__).with_name("agent_policy.json"))
    p.add_argument("--loop", action="store_true", help="poll continuously")
    p.add_argument("--dry-run", action="store_true", help="run agent/validation but do not push or create PR")
    p.add_argument("--check-policy", action="store_true", help="validate policy and exit")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        supervisor = Supervisor(args.policy, dry_run=args.dry_run)
        if args.check_policy:
            print("POLICY_OK")
            return 0
        if args.loop:
            supervisor.loop()
            return 0
        supervisor.cycle()
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"FATAL {redact(str(exc))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
