<#
.SYNOPSIS
    GitHub Autopilot - Automated repo maintenance, PR review, and AI-powered risk scoring.
.DESCRIPTION
    Control plane for GitHub repository management. Ranks repos by activity, clones top repos,
    runs install/lint/test/build, updates stale GitHub Actions, reviews PRs with AI scoring,
    scouts public repos, and generates enriched reports.
.EXAMPLE
    .\github-autopilot.ps1 -TopRepos 3 -CreateMaintenancePRs
.EXAMPLE
    .\github-autopilot.ps1 -TopRepos 3 -ScoutMode -TopScoutRepos 5 -EnableAI
.EXAMPLE
    .\github-autopilot.ps1 -TopRepos 3 -ScoutMode -Loop -LoopHours 6 -EnableAI
#>
[CmdletBinding()]
param(
    [string]$Workspace = "$HOME\gh-autopilot",
    [int]$TopRepos = 5,
    [switch]$CreateMaintenancePRs,
    [switch]$ScoutMode,
    [int]$TopScoutRepos = 5,
    [switch]$Loop,
    [int]$LoopHours = 6,
    [int]$MaxPRLines = 500,
    # AI Reasoning Plane
    [switch]$EnableAI,
    [string]$AIPythonPath = "python",
    [string]$AIModulePath = "",
    [int]$AIAutoMergeThreshold = 25,
    [switch]$AIFallbackHeuristic
)

$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"

# ============================================================================
# INITIALIZATION
# ============================================================================
Write-Host "`n========================================" -ForegroundColor Cyan
Write-Host "  GitHub Autopilot" -ForegroundColor Cyan
Write-Host "  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor DarkGray
Write-Host "========================================`n" -ForegroundColor Cyan

# Validate gh CLI
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Write-Error "GitHub CLI (gh) not found. Install: winget install GitHub.cli"
    exit 1
}
try { gh auth status 2>&1 | Out-Null } catch {
    Write-Error "Not authenticated. Run: gh auth login"
    exit 1
}

# Create workspace
$repoDir = Join-Path $Workspace "repos"
$scoutDir = Join-Path $Workspace "scouted"
$reportDir = Join-Path $Workspace "reports"
$aiDir = Join-Path $Workspace "ai_results"
New-Item -ItemType Directory -Path $repoDir -Force | Out-Null
New-Item -ItemType Directory -Path $reportDir -Force | Out-Null

# AI Reasoning Plane Init
$AIEnabled = $false
$AIModule = $null
$AIRiskDir = $null

if ($EnableAI) {
    # Find ai_repo_intel.py
    $scriptDir = Split-Path -Parent $PSCommandPath
    $candidates = @(
        $AIModulePath,
        (Join-Path $scriptDir "ai_repo_intel.py"),
        (Join-Path $Workspace "ai_repo_intel.py"),
        (Join-Path $HOME "gh-autopilot\ai_repo_intel.py")
    )
    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c)) { $AIModule = $c; break }
    }
    if ($AIModule) {
        try {
            $pyVer = & $AIPythonPath --version 2>&1
            if ($env:ANTHROPIC_API_KEY) {
                $AIEnabled = $true
                $AIRiskDir = Join-Path $aiDir "pr_risks"
                New-Item -ItemType Directory -Path $AIRiskDir -Force | Out-Null
                Write-Host "[AI] Reasoning plane active ($pyVer)" -ForegroundColor Green
                Write-Host "[AI] Module: $AIModule" -ForegroundColor DarkGray
            } else {
                Write-Warning "[AI] ANTHROPIC_API_KEY not set. AI disabled."
            }
        } catch { Write-Warning "[AI] Python not found. AI disabled." }
    } else { Write-Warning "[AI] ai_repo_intel.py not found. AI disabled." }
}

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================
function Invoke-GH {
    param([string]$Args)
    $result = gh api $Args 2>&1
    if ($LASTEXITCODE -ne 0) { return $null }
    return $result | ConvertFrom-Json
}

function Get-RepoActivity {
    param([object]$Repo)
    $score = 0
    $pushed = [DateTime]::Parse($Repo.pushed_at)
    $daysSincePush = ((Get-Date) - $pushed).TotalDays
    if ($daysSincePush -lt 7) { $score += 50 }
    elseif ($daysSincePush -lt 30) { $score += 30 }
    elseif ($daysSincePush -lt 90) { $score += 10 }
    $score += [math]::Min($Repo.open_issues_count * 5, 30)
    $score += [math]::Min($Repo.stargazers_count, 20)
    if (-not $Repo.fork) { $score += 10 }
    return $score
}

function Sync-Repo {
    param([string]$CloneUrl, [string]$DestPath, [string]$DefaultBranch)
    if (Test-Path (Join-Path $DestPath ".git")) {
        Write-Host "  Updating $DestPath..." -ForegroundColor DarkGray
        Push-Location $DestPath
        git fetch origin 2>&1 | Out-Null
        git checkout $DefaultBranch 2>&1 | Out-Null
        git pull origin $DefaultBranch 2>&1 | Out-Null
        Pop-Location
    } else {
        Write-Host "  Cloning to $DestPath..." -ForegroundColor DarkGray
        git clone --depth 50 $CloneUrl $DestPath 2>&1 | Out-Null
    }
}

function Get-ProjectType {
    param([string]$Path)
    $types = @()
    if (Test-Path (Join-Path $Path "package.json")) { $types += "node" }
    if (Test-Path (Join-Path $Path "requirements.txt")) { $types += "python" }
    if (Test-Path (Join-Path $Path "Cargo.toml")) { $types += "rust" }
    if (Test-Path (Join-Path $Path "go.mod")) { $types += "go" }
    return $types
}

function Invoke-RepoValidation {
    param([string]$Path)
    $result = @{ install = $null; lint = $null; test = $null; build = $null; has_tests = $false }
    $types = Get-ProjectType -Path $Path
    Push-Location $Path
    if ($types -contains "node" -and (Test-Path "package.json")) {
        $pkg = Get-Content "package.json" -Raw | ConvertFrom-Json
        # Install
        if (Test-Path "pnpm-lock.yaml") { $null = pnpm install --frozen-lockfile 2>&1; $result.install = ($LASTEXITCODE -eq 0) }
        elseif (Test-Path "yarn.lock") { $null = yarn install --frozen-lockfile 2>&1; $result.install = ($LASTEXITCODE -eq 0) }
        else { $null = npm ci 2>&1; $result.install = ($LASTEXITCODE -eq 0) }
        # Lint
        if ($pkg.scripts -and $pkg.scripts.lint) {
            $null = npm run lint 2>&1; $result.lint = ($LASTEXITCODE -eq 0)
        }
        # Test
        if ($pkg.scripts -and $pkg.scripts.test) {
            $testScript = $pkg.scripts.test
            if ($testScript -ne 'echo "Error: no test specified" && exit 1') {
                $result.has_tests = $true
                $null = npm test 2>&1; $result.test = ($LASTEXITCODE -eq 0)
            }
        }
        # Build
        if ($pkg.scripts -and $pkg.scripts.build) {
            $null = npm run build 2>&1; $result.build = ($LASTEXITCODE -eq 0)
        }
    }
    Pop-Location
    return $result
}

function Update-GitHubActions {
    param([string]$Path)
    $workflowDir = Join-Path $Path ".github\workflows"
    if (-not (Test-Path $workflowDir)) { return @() }
    $updates = @()
    $knownUpdates = @{
        "actions/checkout@v3" = "actions/checkout@v4"
        "actions/setup-node@v3" = "actions/setup-node@v4"
        "actions/setup-python@v4" = "actions/setup-python@v5"
        "actions/cache@v3" = "actions/cache@v4"
        "actions/upload-artifact@v3" = "actions/upload-artifact@v4"
        "actions/download-artifact@v3" = "actions/download-artifact@v4"
    }
    Get-ChildItem $workflowDir -Filter "*.yml" -Recurse | ForEach-Object {
        $content = Get-Content $_.FullName -Raw
        $changed = $false
        foreach ($old in $knownUpdates.Keys) {
            if ($content -match [regex]::Escape($old)) {
                $content = $content -replace [regex]::Escape($old), $knownUpdates[$old]
                $updates += @{ file = $_.Name; from = $old; to = $knownUpdates[$old] }
                $changed = $true
            }
        }
        if ($changed) { Set-Content -Path $_.FullName -Value $content -NoNewline }
    }
    return $updates
}

function Invoke-AIRepoIntel {
    param([string]$Command, [string[]]$Arguments)
    if (-not $AIEnabled) { return @{ Success = $false } }
    try {
        $allArgs = @($AIModule, $Command) + $Arguments
        $output = & $AIPythonPath @allArgs 2>&1
        $stdout = ($output | Where-Object { $_ -is [string] -or $_.GetType().Name -ne 'ErrorRecord' }) -join "`n"
        $stderr = ($output | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] }) -join "`n"
        if ($LASTEXITCODE -ne 0) { return @{ Success = $false; Error = $stderr } }
        return @{ Success = $true; Output = $stdout }
    } catch { return @{ Success = $false; Error = $_.ToString() } }
}

function Invoke-PRRiskScore {
    param([string]$Repo, [int]$PRNum, [string]$Title, [string]$Author,
          [string[]]$Files, [int]$Lines, [hashtable]$Validation)
    $ctx = @{
        repo = $Repo; pr_number = $PRNum; title = $Title; author = $Author
        changed_files = $Files; changed_lines = $Lines; validation = $Validation
    }
    $inputFile = Join-Path $AIRiskDir "pr_ctx_${PRNum}.json"
    $outputFile = Join-Path $AIRiskDir "pr_risk_${PRNum}.json"
    $ctx | ConvertTo-Json -Depth 10 | Set-Content -Path $inputFile -Encoding UTF8
    $r = Invoke-AIRepoIntel -Command "pr-risk" -Arguments @("--input", $inputFile, "--output", $outputFile)
    if ($r.Success -and (Test-Path $outputFile)) {
        $risk = Get-Content $outputFile -Raw | ConvertFrom-Json
        $color = switch ($risk.decision_band) { "auto_merge" {"Green"} "review" {"Yellow"} default {"Red"} }
        Write-Host "    [AI] Score: $($risk.risk_score) - $($risk.decision_band)" -ForegroundColor $color
        return $risk
    }
    Write-Warning "    [AI] Scoring failed"
    return $null
}

function Build-RepoSummary {
    param([string]$Path, [string]$Name)
    $s = @{ name=$Name; package_manager="unknown"; test_framework="unknown"
            ci_platform=@(); has_tests=$false; linting=@(); release_tool="none"; coverage=$false }
    if (Test-Path (Join-Path $Path "pnpm-lock.yaml")) { $s.package_manager = "pnpm" }
    elseif (Test-Path (Join-Path $Path "yarn.lock")) { $s.package_manager = "yarn" }
    elseif (Test-Path (Join-Path $Path "package-lock.json")) { $s.package_manager = "npm" }
    elseif (Test-Path (Join-Path $Path "poetry.lock")) { $s.package_manager = "poetry" }
    elseif (Test-Path (Join-Path $Path "Cargo.lock")) { $s.package_manager = "cargo" }
    $pkgFile = Join-Path $Path "package.json"
    if (Test-Path $pkgFile) {
        try {
            $pkg = Get-Content $pkgFile -Raw | ConvertFrom-Json
            $deps = @()
            if ($pkg.devDependencies) { $deps += ($pkg.devDependencies | Get-Member -MemberType NoteProperty).Name }
            if ($pkg.dependencies) { $deps += ($pkg.dependencies | Get-Member -MemberType NoteProperty).Name }
            if ($deps -contains "vitest") { $s.test_framework = "vitest" }
            elseif ($deps -contains "jest") { $s.test_framework = "jest" }
            elseif ($deps -contains "mocha") { $s.test_framework = "mocha" }
            if ($deps -contains "eslint") { $s.linting += "eslint" }
            if ($deps -contains "prettier") { $s.linting += "prettier" }
            if ($deps -contains "biome") { $s.linting += "biome" }
            if ($deps -contains "changesets") { $s.release_tool = "changesets" }
            elseif ($deps -contains "semantic-release") { $s.release_tool = "semantic-release" }
            if ($pkg.scripts -and $pkg.scripts.test -and $pkg.scripts.test -ne 'echo "Error: no test specified" && exit 1') {
                $s.has_tests = $true
            }
        } catch {}
    }
    if (Test-Path (Join-Path $Path ".github\workflows")) { $s.ci_platform += "github-actions" }
    foreach ($cf in @("jest.config.*","vitest.config.*",".nycrc",".c8rc","codecov.yml")) {
        if (Get-ChildItem -Path $Path -Filter $cf -ErrorAction SilentlyContinue) { $s.coverage = $true; break }
    }
    return $s
}

# ============================================================================
# MAIN EXECUTION
# ============================================================================
function Invoke-AutopilotCycle {
    $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $cycleReport = @{ timestamp = $timestamp; repos = @(); prs = @(); scout = @(); ai = @() }

    # --- STEP 1: Repo Discovery & Ranking ---
    Write-Host "`n[1/6] Discovering repos..." -ForegroundColor Cyan
    $allRepos = @()
    $page = 1
    do {
        $batch = gh api "user/repos?per_page=100&page=$page&type=owner&sort=pushed" 2>&1 | ConvertFrom-Json
        if ($batch -and $batch.Count -gt 0) { $allRepos += $batch; $page++ }
        else { break }
    } while ($batch.Count -eq 100)
    Write-Host "  Found $($allRepos.Count) owned repos" -ForegroundColor Green

    # Rank by activity
    $ranked = $allRepos | ForEach-Object {
        [PSCustomObject]@{ Repo = $_; Score = (Get-RepoActivity $_); Name = $_.full_name }
    } | Sort-Object Score -Descending
    $topRepos = $ranked | Select-Object -First $TopRepos
    Write-Host "  Top $TopRepos by activity:"
    $topRepos | ForEach-Object { Write-Host "    $($_.Score) - $($_.Name)" -ForegroundColor Yellow }

    # --- STEP 2: Clone/Update & Validate ---
    Write-Host "`n[2/6] Cloning and validating..." -ForegroundColor Cyan
    foreach ($entry in $topRepos) {
        $repo = $entry.Repo
        $repoPath = Join-Path $repoDir $repo.name
        Write-Host "`n  >> $($repo.full_name)" -ForegroundColor White
        Sync-Repo -CloneUrl $repo.clone_url -DestPath $repoPath -DefaultBranch $repo.default_branch

        # Validate
        Write-Host "  Running validation..." -ForegroundColor DarkGray
        $val = Invoke-RepoValidation -Path $repoPath
        $valSummary = @()
        if ($val.install -ne $null) { $valSummary += "install:$(if($val.install){'OK'}else{'FAIL'})" }
        if ($val.lint -ne $null) { $valSummary += "lint:$(if($val.lint){'OK'}else{'FAIL'})" }
        if ($val.test -ne $null) { $valSummary += "test:$(if($val.test){'OK'}else{'FAIL'})" }
        if ($val.build -ne $null) { $valSummary += "build:$(if($val.build){'OK'}else{'FAIL'})" }
        if ($valSummary.Count -gt 0) { Write-Host "  [$($valSummary -join ' | ')]" -ForegroundColor DarkGray }
        else { Write-Host "  [No detectable build system]" -ForegroundColor DarkGray }

        # Update GitHub Actions
        $actionsUpdates = Update-GitHubActions -Path $repoPath
        if ($actionsUpdates.Count -gt 0) {
            Write-Host "  Updated $($actionsUpdates.Count) GitHub Actions versions" -ForegroundColor Yellow
        }

        # Commit actions updates as maintenance PR
        if ($CreateMaintenancePRs -and $actionsUpdates.Count -gt 0) {
            Push-Location $repoPath
            $branch = "autopilot/update-actions-$timestamp"
            git checkout -b $branch 2>&1 | Out-Null
            git add -A 2>&1 | Out-Null
            git commit -m "chore: update GitHub Actions to latest versions" 2>&1 | Out-Null
            git push origin $branch 2>&1 | Out-Null
            gh pr create --title "chore: update GitHub Actions versions" `
                --body "Automated update by github-autopilot.`n`nUpdates:`n$(($actionsUpdates | ForEach-Object { "- $($_.from) -> $($_.to)" }) -join "`n")" `
                --base $repo.default_branch 2>&1 | Out-Null
            Write-Host "  Created maintenance PR on branch $branch" -ForegroundColor Green
            Pop-Location
        }

        $cycleReport.repos += @{
            name = $repo.full_name; score = $entry.Score
            validation = $val; actions_updates = $actionsUpdates.Count
        }
    }

    # --- STEP 3: PR Review ---
    Write-Host "`n[3/6] Reviewing open PRs..." -ForegroundColor Cyan
    foreach ($entry in $topRepos) {
        $repo = $entry.Repo
        $prs = gh api "repos/$($repo.full_name)/pulls?state=open&per_page=30" 2>&1 | ConvertFrom-Json
        if (-not $prs -or $prs.Count -eq 0) { continue }
        Write-Host "`n  $($repo.full_name): $($prs.Count) open PRs" -ForegroundColor White

        foreach ($pr in $prs) {
            $prNum = $pr.number
            $prTitle = $pr.title
            Write-Host "  PR #$prNum - $prTitle" -ForegroundColor DarkGray

            # Guardrails
            if ($pr.draft) { Write-Host "    SKIP: draft" -ForegroundColor DarkGray; continue }
            if ($pr.mergeable_state -eq "dirty" -or $pr.mergeable -eq $false) {
                Write-Host "    SKIP: not mergeable" -ForegroundColor DarkGray; continue
            }

            # Get PR details
            $prFiles = gh api "repos/$($repo.full_name)/pulls/$prNum/files?per_page=100" 2>&1 | ConvertFrom-Json
            $changedFiles = @($prFiles | ForEach-Object { $_.filename })
            $changedLines = ($prFiles | Measure-Object -Property changes -Sum).Sum

            if ($changedLines -gt $MaxPRLines) {
                Write-Host "    SKIP: $changedLines lines (max $MaxPRLines)" -ForegroundColor DarkGray
                continue
            }

            # Validation context for AI
            $repoPath = Join-Path $repoDir $repo.name
            $val = Invoke-RepoValidation -Path $repoPath
            $canMerge = $true
            $aiRisk = $null

            # AI Risk Scoring
            if ($AIEnabled) {
                $aiRisk = Invoke-PRRiskScore `
                    -Repo $repo.full_name -PRNum $prNum -Title $prTitle `
                    -Author $pr.user.login -Files $changedFiles -Lines $changedLines `
                    -Validation @{
                        tests_passed = $val.test; build_passed = $val.build
                        lint_passed = $val.lint; has_tests = $val.has_tests
                    }
                if ($aiRisk -and $aiRisk.risk_score -gt $AIAutoMergeThreshold) {
                    $canMerge = $false
                }
            }

            # Merge decision
            if ($CreateMaintenancePRs -and $canMerge) {
                if ($val.test -eq $false) {
                    Write-Host "    SKIP: tests failing" -ForegroundColor Red
                    $canMerge = $false
                }
            }

            if ($CreateMaintenancePRs -and $canMerge) {
                Write-Host "    MERGING PR #$prNum" -ForegroundColor Green
                gh api -X PUT "repos/$($repo.full_name)/pulls/$prNum/merge" `
                    -f merge_method=squash 2>&1 | Out-Null
            }

            $cycleReport.prs += @{
                repo = $repo.full_name; number = $prNum; title = $prTitle
                author = $pr.user.login; lines = $changedLines; files = $changedFiles.Count
                merged = ($CreateMaintenancePRs -and $canMerge)
                ai_score = if ($aiRisk) { $aiRisk.risk_score } else { $null }
                ai_band = if ($aiRisk) { $aiRisk.decision_band } else { $null }
            }
        }
    }

    # --- STEP 4: Scout Mode ---
    $scoutPatternsFile = $null
    if ($ScoutMode) {
        Write-Host "`n[4/6] Scouting public repos..." -ForegroundColor Cyan
        New-Item -ItemType Directory -Path $scoutDir -Force | Out-Null

        # Find starred/trending repos
        $starred = gh api "user/starred?per_page=$TopScoutRepos&sort=updated" 2>&1 | ConvertFrom-Json
        if ($starred) {
            foreach ($sr in $starred | Select-Object -First $TopScoutRepos) {
                $sPath = Join-Path $scoutDir $sr.name
                Write-Host "  Scouting: $($sr.full_name)" -ForegroundColor Yellow
                Sync-Repo -CloneUrl $sr.clone_url -DestPath $sPath -DefaultBranch $sr.default_branch
                $cycleReport.scout += @{ name = $sr.full_name; stars = $sr.stargazers_count }
            }
        }

        # AI Pattern Analysis
        if ($AIEnabled -and $cycleReport.scout.Count -gt 0) {
            Write-Host "`n  [AI] Analyzing patterns..." -ForegroundColor Cyan
            $ownedSummaries = @()
            foreach ($entry in $topRepos) {
                $rp = Join-Path $repoDir $entry.Repo.name
                if (Test-Path $rp) { $ownedSummaries += Build-RepoSummary -Path $rp -Name $entry.Name }
            }
            $scoutSummaries = @()
            foreach ($sr in $cycleReport.scout) {
                $sp = Join-Path $scoutDir ($sr.name -split '/')[-1]
                if (Test-Path $sp) { $scoutSummaries += Build-RepoSummary -Path $sp -Name $sr.name }
            }
            $ownedFile = Join-Path $Workspace "owned_summary.json"
            $scoutFile = Join-Path $Workspace "scouted_summary.json"
            $scoutPatternsFile = Join-Path $Workspace "scout_patterns.json"
            @{ repos = $ownedSummaries } | ConvertTo-Json -Depth 10 | Set-Content $ownedFile -Encoding UTF8
            @{ repos = $scoutSummaries } | ConvertTo-Json -Depth 10 | Set-Content $scoutFile -Encoding UTF8
            $r = Invoke-AIRepoIntel -Command "scout-patterns" `
                -Arguments @("--owned", $ownedFile, "--scouted", $scoutFile, "--output", $scoutPatternsFile)
            if ($r.Success) {
                $patterns = Get-Content $scoutPatternsFile -Raw | ConvertFrom-Json
                $pCount = ($patterns.patterns | Measure-Object).Count
                Write-Host "  [AI] Extracted $pCount patterns" -ForegroundColor Green
                if ($patterns.priority_actions) {
                    foreach ($a in $patterns.priority_actions) { Write-Host "    -> $a" -ForegroundColor Yellow }
                }
            }
        }
    } else { Write-Host "`n[4/6] Scout mode: off" -ForegroundColor DarkGray }

    # --- STEP 5: Generate Reports ---
    Write-Host "`n[5/6] Generating reports..." -ForegroundColor Cyan
    $jsonReport = Join-Path $reportDir "autopilot_${timestamp}.json"
    $mdReport = Join-Path $reportDir "autopilot_${timestamp}.md"
    $cycleReport | ConvertTo-Json -Depth 10 | Set-Content $jsonReport -Encoding UTF8

    # Markdown report
    $md = @()
    $md += "# GitHub Autopilot Report"
    $md += "**Generated:** $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
    $md += ""
    $md += "## Repos Analyzed ($($cycleReport.repos.Count))"
    $md += "| Repo | Score | Install | Lint | Test | Build | Actions |"
    $md += "|------|-------|---------|------|------|-------|---------|"
    foreach ($r in $cycleReport.repos) {
        $i = if($r.validation.install -eq $true){"OK"}elseif($r.validation.install -eq $false){"FAIL"}else{"-"}
        $l = if($r.validation.lint -eq $true){"OK"}elseif($r.validation.lint -eq $false){"FAIL"}else{"-"}
        $t = if($r.validation.test -eq $true){"OK"}elseif($r.validation.test -eq $false){"FAIL"}else{"-"}
        $b = if($r.validation.build -eq $true){"OK"}elseif($r.validation.build -eq $false){"FAIL"}else{"-"}
        $md += "| $($r.name) | $($r.score) | $i | $l | $t | $b | $($r.actions_updates) |"
    }
    $md += ""
    if ($cycleReport.prs.Count -gt 0) {
        $md += "## PRs Reviewed ($($cycleReport.prs.Count))"
        $md += "| Repo | PR | Lines | AI Score | Band | Merged |"
        $md += "|------|----|-------|----------|------|--------|"
        foreach ($p in $cycleReport.prs) {
            $aiS = if ($p.ai_score -ne $null) { $p.ai_score } else { "-" }
            $aiB = if ($p.ai_band) { $p.ai_band } else { "-" }
            $merged = if ($p.merged) { "YES" } else { "no" }
            $md += "| $($p.repo) | #$($p.number) | $($p.lines) | $aiS | $aiB | $merged |"
        }
        $md += ""
    }

    if ($cycleReport.scout.Count -gt 0) {
        $md += "## Scouted Repos ($($cycleReport.scout.Count))"
        foreach ($s in $cycleReport.scout) {
            $md += "- **$($s.name)** ($($s.stars) stars)"
        }
        $md += ""
    }

    $md -join "`n" | Set-Content $mdReport -Encoding UTF8

    # AI Report Enrichment
    if ($AIEnabled) {
        Write-Host "  [AI] Enriching report..." -ForegroundColor Cyan
        $enrichArgs = @("--report", $mdReport, "--output", $mdReport)
        if ($AIRiskDir -and (Test-Path $AIRiskDir)) { $enrichArgs += @("--risk-dir", $AIRiskDir) }
        if ($scoutPatternsFile -and (Test-Path $scoutPatternsFile)) { $enrichArgs += @("--scout", $scoutPatternsFile) }
        Invoke-AIRepoIntel -Command "enrich-report" -Arguments $enrichArgs | Out-Null
    }

    Write-Host "  JSON: $jsonReport" -ForegroundColor DarkGray
    Write-Host "  Markdown: $mdReport" -ForegroundColor DarkGray

    # --- STEP 6: Summary ---
    Write-Host "`n[6/6] Summary" -ForegroundColor Cyan
    Write-Host "  Repos analyzed: $($cycleReport.repos.Count)" -ForegroundColor White
    Write-Host "  PRs reviewed:   $($cycleReport.prs.Count)" -ForegroundColor White
    $merged = ($cycleReport.prs | Where-Object { $_.merged }).Count
    Write-Host "  PRs merged:     $merged" -ForegroundColor $(if($merged -gt 0){"Green"}else{"White"})
    Write-Host "  Repos scouted:  $($cycleReport.scout.Count)" -ForegroundColor White
    if ($AIEnabled) { Write-Host "  AI scoring:     active" -ForegroundColor Green }
    Write-Host "`n========================================`n" -ForegroundColor Cyan
}

# ============================================================================
# ENTRY POINT
# ============================================================================
if ($Loop) {
    Write-Host "Loop mode: running every $LoopHours hours. Ctrl+C to stop." -ForegroundColor Yellow
    while ($true) {
        try { Invoke-AutopilotCycle }
        catch { Write-Warning "Cycle failed: $_" }
        Write-Host "Next cycle in $LoopHours hours..." -ForegroundColor DarkGray
        Start-Sleep -Seconds ($LoopHours * 3600)
    }
} else {
    Invoke-AutopilotCycle
}
