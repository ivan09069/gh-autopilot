# GitHub Autopilot — Policy-Gated Repository Agent

A self-hosted, always-on repository agent with **PR-only writes**.

The worker watches one repository for issues labeled `agent:ready`, lets Codex implement the task locally, validates the resulting diff, and—only if every policy gate passes—pushes an `agent/*` branch and opens a **draft pull request**. It never merges or deploys.

## Current canary scope

- Repository: `ivan09069/gh-autopilot`
- Default branch: `master`
- Trigger: open issue with label `agent:ready`
- Authorized issue author: `ivan09069`
- Maximum concurrency: 1
- External code writes: `agent/*` branch + draft PR only
- Merge/deploy/default-branch writes: **not implemented**

## Control architecture

```text
GitHub issue (agent:ready)
        |
        v
 trusted supervisor  ---- local SQLite memory / audit state
        |
        +--> sync master (trusted git/gh credentials)
        |
        +--> create local agent/issue-N branch
        |
        v
 Codex workspace sandbox
   - edits local files
   - no GH_TOKEN/GITHUB_TOKEN
   - empty GH_CONFIG_DIR
   - no merge/push/deploy authority
        |
        v
 trusted policy gate
   - protected-path check
   - file/line limits
   - validation commands
   - exact branch-prefix check
   - staged-diff consistency check
   - exactly one proposal commit
        |
        v
 push agent/issue-N --> OPEN DRAFT PR --> HUMAN REVIEW
```

## Files

- `agent_worker.py` — trusted supervisor and always-on poller; standard library only.
- `agent_core.py` — fail-closed policy, redaction, branch, path, and diff enforcement.
- `agent_policy.json` — repository scope, author allowlist, protected paths, limits, validation, and Codex command.
- `install-worker.ps1` — Windows self-host installer using Task Scheduler.
- `github-autopilot.ps1` — compatibility shim; the old auto-merge behavior is removed.
- `tests/test_policy.py` — policy regression tests.
- `SECURITY.md` — enforcement boundary and credential model.

## Local verification

```powershell
python -m unittest discover -s tests -v
python .\agent_worker.py --check-policy
python .\agent_worker.py --dry-run
```

`--dry-run` lets the agent edit and validate locally but prevents branch push and PR creation.

## Install on a Windows self-host

Run from an authenticated workstation that already has `git`, `gh`, `python`, and `codex`:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install-worker.ps1
```

The installer validates the policy, registers `GH-Autopilot-PolicyWorker` to start at logon, starts it immediately, and prints the task state/result.

## Triggering work

Create or edit a GitHub issue in this repository and add the label `agent:ready`. The issue title/body is the task specification. Only allowed authors are accepted.

The first accepted task becomes a local job. If Codex changes a protected control-plane path, exceeds policy limits, or fails validation, the job is recorded as blocked and **nothing is pushed**.

## Design note

OpenAI's current self-hosted-agent guidance recommends isolating the execution environment, keeping application/third-party credentials outside the agent environment, and enforcing approval/guardrail decisions at the side-effect boundary. This implementation follows that split: Codex proposes a local diff; the supervisor independently decides whether a draft PR write is allowed.
