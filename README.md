# GitHub Autopilot + AI Reasoning Plane

Automated repo maintenance with AI-powered PR risk scoring and scout pattern extraction.

## Architecture

```
github-autopilot.ps1  (Control Plane)        ai_repo_intel.py  (Reasoning Plane)
├── Repo discovery & ranking                  ├── PR risk scoring (Anthropic API)
├── Clone / update                            ├── Scout pattern extraction
├── Install / lint / test / build             ├── Report enrichment
├── GitHub Actions updates                    └── Heuristic fallback (no API needed)
├── PR enumeration & merge execution
├── Scout mode
└── Report generation (JSON + Markdown)
```

## Quick Start

```powershell
# One-time setup
Set-ExecutionPolicy -Scope Process Bypass
$env:ANTHROPIC_API_KEY = "sk-ant-..."

# Basic run
.\github-autopilot.ps1 -TopRepos 3 -CreateMaintenancePRs

# With AI scoring
.\github-autopilot.ps1 -TopRepos 3 -EnableAI -CreateMaintenancePRs

# Scout + AI
.\github-autopilot.ps1 -TopRepos 3 -ScoutMode -TopScoutRepos 5 -EnableAI

# Loop mode (runs every 6 hours)
.\github-autopilot.ps1 -TopRepos 3 -ScoutMode -Loop -LoopHours 6 -EnableAI

# Stricter merge threshold
.\github-autopilot.ps1 -TopRepos 5 -EnableAI -AIAutoMergeThreshold 15 -CreateMaintenancePRs
```

## Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| -Workspace | $HOME\gh-autopilot | Working directory |
| -TopRepos | 5 | Number of owned repos to process |
| -CreateMaintenancePRs | off | Enable PR creation and auto-merge |
| -ScoutMode | off | Clone starred repos for pattern analysis |
| -TopScoutRepos | 5 | Number of repos to scout |
| -Loop | off | Continuous execution mode |
| -LoopHours | 6 | Hours between loop cycles |
| -MaxPRLines | 500 | Skip PRs larger than this |
| -EnableAI | off | Activate AI reasoning plane |
| -AIPythonPath | python | Path to Python executable |
| -AIModulePath | (auto) | Path to ai_repo_intel.py |
| -AIAutoMergeThreshold | 25 | Max AI score for auto-merge |

## Hard Guardrails

- Skips draft PRs
- Skips non-mergeable PRs
- Skips PRs over 500 changed lines (configurable)
- Requires validation pass before auto-merge
- AI score must be <= threshold for auto-merge
- Separates owned and scouted workspaces
- Hard rules override AI judgment:
  - Failing tests = floor score 50
  - Security/deploy paths = cannot auto-merge (unless deps-only + tests pass)
  - Workflow changes = escalated to review
  - No-test repo + core logic = cannot auto-merge

## AI Risk Scoring Bands

| Score | Band | Action |
|-------|------|--------|
| 0-25 | auto_merge | Safe to merge |
| 26-60 | review | Needs human review |
| 61-100 | skip | Do not merge |

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| ANTHROPIC_API_KEY | For AI | Claude API key |
| AI_REPO_INTEL_MODEL | No | Default: claude-sonnet-4-20250514 |
| AI_REPO_INTEL_DEBUG | No | Set "1" for verbose Python logs |

## Requirements

- PowerShell 5.1+ or 7+
- GitHub CLI (`gh`) authenticated
- Python 3.10+ (for AI features, stdlib only)
- Git
