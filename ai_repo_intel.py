#!/usr/bin/env python3
"""
ai_repo_intel.py - Reasoning plane for github-autopilot.ps1

Commands:
    pr-risk         Score PR merge risk via Anthropic Claude
    scout-patterns  Extract actionable patterns from scouted repos vs owned repos
    enrich-report   Merge AI results into markdown report

Env:
    ANTHROPIC_API_KEY   Optional. Without it, pr-risk keeps the local heuristic.
    AI_REPO_INTEL_MODEL Optional. Default: claude-sonnet-4-20250514
    AI_REPO_INTEL_DEBUG Optional. Set "1" for verbose stderr logging.
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# Config
API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-4-20250514"
MAX_TOKENS = 2048
RETRY_ATTEMPTS = 3
RETRY_BACKOFF = 2
TIMEOUT = 60

THRESHOLD_AUTO_MERGE = 25
THRESHOLD_REVIEW = 60

SECURITY_PATHS = [
    ".env", "secret", "credential", "auth", "token", "password",
    "key", "cert", "pem", "private", "ssh",
]
DEPLOY_PATHS = [
    "dockerfile", "docker-compose", "k8s", "kubernetes", "helm",
    "terraform", "pulumi", "cloudformation", ".deploy", "infra/",
    "deploy/", "railway.json", "vercel.json", "render.yaml",
    "fly.toml", "wrangler.toml",
]
CI_PATHS = [
    ".github/workflows", ".circleci", "jenkinsfile", ".gitlab-ci",
    ".travis", "azure-pipelines", "bitbucket-pipelines",
]
WORKFLOW_EXTENSIONS = [".yml", ".yaml"]
DEBUG = os.environ.get("AI_REPO_INTEL_DEBUG", "") == "1"

def log(msg):
    if DEBUG:
        print(f"[ai_repo_intel] {msg}", file=sys.stderr)

def call_claude(system_prompt, user_prompt):
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY not set")
    model = os.environ.get("AI_REPO_INTEL_MODEL", DEFAULT_MODEL)
    payload = json.dumps({
        "model": model, "max_tokens": MAX_TOKENS,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    last_err = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            req = urllib.request.Request(API_URL, data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                text_parts = [b["text"] for b in body.get("content", []) if b.get("type") == "text"]
                return "\n".join(text_parts)
        except urllib.error.HTTPError as e:
            last_err = e
            log(f"Attempt {attempt+1} HTTP {e.code}")
            if e.code == 429 or e.code >= 500:
                time.sleep(RETRY_BACKOFF * (2 ** attempt))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
            log(f"Attempt {attempt+1} network error: {e}")
            time.sleep(RETRY_BACKOFF * (2 ** attempt))
            continue
    raise RuntimeError(f"API failed after {RETRY_ATTEMPTS} attempts: {last_err}")

def parse_json_from_response(text):
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.split("\n")
        inner = []
        started = False
        for line in lines:
            if not started:
                if line.strip().startswith("```"):
                    started = True
                    continue
            else:
                if line.strip() == "```":
                    break
                inner.append(line)
        stripped = "\n".join(inner).strip()
    return json.loads(stripped)

def classify_paths(changed_files):
    hits = {"security": [], "deploy": [], "ci": [], "workflow_files": [],
            "test_files": [], "docs_only": True, "deps_only": True}
    doc_ext = {".md", ".txt", ".rst", ".adoc", ".doc", ".docx"}
    dep_files = {"package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
                 "requirements.txt", "poetry.lock", "pipfile.lock", "go.sum", "go.mod",
                 "cargo.lock", "cargo.toml", "gemfile.lock", "composer.lock"}
    for f in changed_files:
        fl = f.lower()
        fname = Path(f).name.lower()
        ext = Path(f).suffix.lower()
        if any(s in fl for s in SECURITY_PATHS): hits["security"].append(f)
        if any(d in fl for d in DEPLOY_PATHS): hits["deploy"].append(f)
        if any(c in fl for c in CI_PATHS): hits["ci"].append(f)
        if ".github/workflows" in fl and ext in WORKFLOW_EXTENSIONS: hits["workflow_files"].append(f)
        if "test" in fl or "spec" in fl or "__tests__" in fl: hits["test_files"].append(f)
        if ext not in doc_ext and fname not in {"readme.md", "changelog.md", "license", "license.md"}:
            hits["docs_only"] = False
        if fname not in dep_files: hits["deps_only"] = False
    return hits

def compute_heuristic_score(ctx, path_hits):
    score = 30
    reasons = []
    lines = ctx.get("changed_lines", 0)
    files = ctx.get("changed_files", [])
    validation = ctx.get("validation", {})
    if lines <= 10 and len(files) <= 2:
        score -= 15; reasons.append(f"Small change ({lines} lines, {len(files)} files)")
    elif lines > 400:
        score += 25; reasons.append(f"Very large change ({lines} lines)")
    elif lines > 200:
        score += 15; reasons.append(f"Large change ({lines} lines)")
    if path_hits["security"]:
        score += 40; reasons.append(f"Security-sensitive paths: {path_hits['security'][:3]}")
    if path_hits["deploy"]:
        score += 30; reasons.append(f"Deploy/infra paths: {path_hits['deploy'][:3]}")
    if path_hits["ci"]:
        score += 20; reasons.append(f"CI config changes: {path_hits['ci'][:3]}")
    if path_hits["workflow_files"]:
        score += 15; reasons.append(f"Workflow file changes: {path_hits['workflow_files'][:3]}")
    if path_hits["docs_only"]:
        score -= 25; reasons.append("Docs-only change")
    if path_hits["deps_only"]:
        score -= 15; reasons.append("Dependency-only change")
    if path_hits["test_files"] and not path_hits["security"]:
        score -= 10; reasons.append("Includes test changes")
    if validation.get("tests_passed") is True:
        score -= 15; reasons.append("Tests pass")
    elif validation.get("tests_passed") is False:
        score += 30; reasons.append("Tests FAIL")
    if validation.get("build_passed") is True:
        score -= 5; reasons.append("Build passes")
    elif validation.get("build_passed") is False:
        score += 20; reasons.append("Build FAILS")
    if validation.get("lint_passed") is False:
        score += 10; reasons.append("Lint fails")
    if not validation.get("has_tests", False) and not path_hits["docs_only"] and not path_hits["deps_only"]:
        score += 20; reasons.append("No test coverage for core logic change")
    return max(0, min(100, score)), reasons

def _file_type_summary(files):
    counts = {}
    for f in files:
        ext = Path(f).suffix.lower() or "(no ext)"
        counts[ext] = counts.get(ext, 0) + 1
    return dict(sorted(counts.items(), key=lambda x: -x[1]))

def _enforce_hard_rules(result, path_hits, ctx):
    score = result.get("risk_score", 50)
    band = result.get("decision_band", "review")
    flags = result.get("flags", [])
    validation = ctx.get("validation", {})
    if validation.get("tests_passed") is False and score < 50:
        score = 50; flags.append("HARD_RULE: failing tests floor 50")
    if path_hits["security"] or path_hits["deploy"]:
        if not (path_hits["deps_only"] and validation.get("tests_passed") is True):
            if band == "auto_merge":
                band = "review"; score = max(score, 30)
                flags.append("HARD_RULE: security/deploy paths escalated")
    if path_hits["workflow_files"] and band == "auto_merge":
        band = "review"; score = max(score, 30)
        flags.append("HARD_RULE: workflow changes escalated")
    if not validation.get("has_tests", False):
        if not path_hits["docs_only"] and not path_hits["deps_only"]:
            if band == "auto_merge":
                band = "review"; score = max(score, 35)
                flags.append("HARD_RULE: no tests + core logic escalated")
    if score <= THRESHOLD_AUTO_MERGE: band = "auto_merge"
    elif score <= THRESHOLD_REVIEW: band = "review"
    else: band = "skip"
    result["risk_score"] = score
    result["decision_band"] = band
    result["flags"] = flags
    return result

def pr_risk(input_path, output_path):
    with open(input_path, "r", encoding="utf-8-sig") as f:
        ctx = json.load(f)
    log(f"Scoring PR #{ctx.get('pr_number')} in {ctx.get('repo', 'unknown')}")
    changed_files = ctx.get("changed_files", [])
    path_hits = classify_paths(changed_files)
    heuristic_score, heuristic_reasons = compute_heuristic_score(ctx, path_hits)
    system = (
        "You are a senior software engineer reviewing a GitHub PR for automated merge risk scoring. "
        "Respond with ONLY a JSON object (no markdown):\n"
        '{"risk_score": <0-100>, "decision_band": "<auto_merge|review|skip>", '
        '"reasoning": "<2-3 sentences>", "flags": ["..."], "confidence": "<high|medium|low>"}\n\n'
        "Decision bands: auto_merge 0-25, review 26-60, skip 61-100.\n"
        "Hard rules: deploy/security paths cannot auto_merge unless deps-only+tests pass. "
        "No-test repo+core logic cannot auto_merge. Workflow changes +15 penalty. Failing tests=min 50."
    )
    pr_summary = {
        "repo": ctx.get("repo"), "pr_number": ctx.get("pr_number"),
        "title": ctx.get("title"), "author": ctx.get("author"),
        "changed_lines": ctx.get("changed_lines"), "file_count": len(changed_files),
        "changed_files": changed_files[:30],
        "file_type_mix": _file_type_summary(changed_files),
        "path_classification": {k: v for k, v in path_hits.items() if isinstance(v, list) and v},
        "docs_only": path_hits["docs_only"], "deps_only": path_hits["deps_only"],
        "validation": ctx.get("validation", {}),
        "heuristic_score": heuristic_score, "heuristic_reasons": heuristic_reasons,
    }
    try:
        response = call_claude(system, f"Analyze this PR:\n{json.dumps(pr_summary, indent=2)}")
        result = parse_json_from_response(response)
        for field in ["risk_score", "decision_band", "reasoning"]:
            if field not in result: raise ValueError(f"Missing: {field}")
        result = _enforce_hard_rules(result, path_hits, ctx)
    except Exception as e:
        log(f"AI failed, heuristic fallback: {e}")
        band = "auto_merge" if heuristic_score <= THRESHOLD_AUTO_MERGE else "review" if heuristic_score <= THRESHOLD_REVIEW else "skip"
        result = {"risk_score": heuristic_score, "decision_band": band,
                  "reasoning": f"Heuristic fallback ({e}). {'; '.join(heuristic_reasons)}",
                  "flags": heuristic_reasons, "confidence": "low", "ai_fallback": True}
    result["pr_number"] = ctx.get("pr_number")
    result["repo"] = ctx.get("repo")
    result["heuristic_score"] = heuristic_score
    result["heuristic_reasons"] = heuristic_reasons
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    log(f"Result: score={result['risk_score']} band={result['decision_band']}")
    print(json.dumps(result, indent=2))

def scout_patterns(owned_path, scouted_path, output_path):
    with open(owned_path, "r", encoding="utf-8-sig") as f: owned = json.load(f)
    with open(scouted_path, "r", encoding="utf-8-sig") as f: scouted = json.load(f)
    log(f"Comparing {len(owned.get('repos',[]))} owned vs {len(scouted.get('repos',[]))} scouted")
    system = (
        "You are a senior DevOps engineer comparing a developer's repos against scouted OSS repos. "
        "Respond with ONLY JSON:\n"
        '{"patterns": [{"category": "<ci|testing|deps|release|build|security|dx>", '
        '"observation": "...", "source_repo": "...", "gap": "...", '
        '"recommendation": "...", "effort": "<low|medium|high>", "impact": "<low|medium|high>"}], '
        '"tooling_deltas": {"test_framework": "...", "package_manager": "...", '
        '"ci_platform": "...", "release_strategy": "...", "linting": "..."}, '
        '"priority_actions": ["top 3 actions"]}'
    )
    try:
        response = call_claude(system,
            f"OWNED:\n{json.dumps(owned, indent=2)}\n\nSCOUTED:\n{json.dumps(scouted, indent=2)}")
        result = parse_json_from_response(response)
    except Exception as e:
        log(f"Scout analysis failed: {e}")
        result = {"patterns": [], "tooling_deltas": {}, "priority_actions": [], "error": str(e)}
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    print(json.dumps(result, indent=2))

def enrich_report(report_path, risk_dir, scout_path, output_path):
    report_md = ""
    if os.path.exists(report_path):
        with open(report_path, "r", encoding="utf-8-sig") as f: report_md = f.read()
    sections = []
    risk_results = []
    if risk_dir and os.path.isdir(risk_dir):
        for fname in sorted(os.listdir(risk_dir)):
            if fname.endswith(".json"):
                with open(os.path.join(risk_dir, fname), "r", encoding="utf-8-sig") as f:
                    risk_results.append(json.load(f))
    if risk_results:
        sections.append("\n## AI PR Risk Assessment\n")
        sections.append("| Repo | PR | Score | Band | Key Flags |")
        sections.append("|------|-----|-------|------|-----------|")
        for r in risk_results:
            flags = ", ".join(r.get("flags", [])[:3]) or "-"
            sections.append(f"| {r.get('repo','?')} | #{r.get('pr_number','?')} "
                          f"| {r.get('risk_score','?')} | **{r.get('decision_band','?')}** | {flags} |")
        sections.append("")
        for r in risk_results:
            sections.append(f"### PR #{r.get('pr_number')} - {r.get('repo','?')}")
            sections.append(f"- **Score:** {r.get('risk_score')} (heuristic: {r.get('heuristic_score')})")
            sections.append(f"- **Decision:** {r.get('decision_band')}")
            sections.append(f"- **Reasoning:** {r.get('reasoning','N/A')}")
            if r.get("ai_fallback"): sections.append("- WARNING: AI fallback - heuristic only")
            sections.append("")
    if scout_path and os.path.exists(scout_path):
        with open(scout_path, "r", encoding="utf-8-sig") as f: scout = json.load(f)
        if scout.get("patterns"):
            sections.append("\n## Scout Pattern Analysis\n")
            sections.append("### Priority Actions")
            for i, a in enumerate(scout.get("priority_actions", []), 1):
                sections.append(f"{i}. {a}")
            sections.append("")
            sections.append("| Category | Source | Recommendation | Effort | Impact |")
            sections.append("|----------|--------|----------------|--------|--------|")
            for p in scout["patterns"]:
                sections.append(f"| {p.get('category','')} | {p.get('source_repo','')} "
                              f"| {p.get('recommendation','')} | {p.get('effort','')} | {p.get('impact','')} |")
            sections.append("")
            if scout.get("tooling_deltas"):
                sections.append("### Tooling Comparison")
                for k, v in scout["tooling_deltas"].items():
                    sections.append(f"- **{k.replace('_',' ').title()}:** {v}")
                sections.append("")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(report_md + "\n".join(sections))
    print(f"Enriched report: {output_path}")

def main():
    parser = argparse.ArgumentParser(description="AI reasoning plane for github-autopilot")
    sub = parser.add_subparsers(dest="command", required=True)
    pr = sub.add_parser("pr-risk", help="Score PR merge risk")
    pr.add_argument("--input", required=True)
    pr.add_argument("--output", required=True)
    sp = sub.add_parser("scout-patterns", help="Extract patterns from scouted repos")
    sp.add_argument("--owned", required=True)
    sp.add_argument("--scouted", required=True)
    sp.add_argument("--output", required=True)
    er = sub.add_parser("enrich-report", help="Merge AI results into markdown report")
    er.add_argument("--report", required=True)
    er.add_argument("--risk-dir", default="")
    er.add_argument("--scout", default="")
    er.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.command == "pr-risk": pr_risk(args.input, args.output)
    elif args.command == "scout-patterns": scout_patterns(args.owned, args.scouted, args.output)
    elif args.command == "enrich-report": enrich_report(args.report, args.risk_dir, args.scout, args.output)

if __name__ == "__main__":
    main()
