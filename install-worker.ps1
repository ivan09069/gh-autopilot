[CmdletBinding(SupportsShouldProcess=$true)]
param(
    [string]$InstallRoot = "$HOME\gh-autopilot-worker",
    [string]$Repository = "ivan09069/gh-autopilot",
    [string]$Branch = "master",
    [string]$TaskName = "GH-Autopilot-PolicyWorker"
)

$ErrorActionPreference = "Stop"

function Require-Command([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command not found: $Name"
    }
}

Require-Command git
Require-Command gh
Require-Command python
Require-Command codex

gh auth status | Out-Null

$repoDir = Join-Path $InstallRoot "control-plane"
New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null

if (Test-Path (Join-Path $repoDir ".git")) {
    git -C $repoDir fetch origin --prune
    git -C $repoDir checkout $Branch
    git -C $repoDir reset --hard "origin/$Branch"
} else {
    git clone "https://github.com/$Repository.git" $repoDir
    git -C $repoDir checkout $Branch
}

python (Join-Path $repoDir "agent_worker.py") --policy (Join-Path $repoDir "agent_policy.json") --check-policy
if ($LASTEXITCODE -ne 0) { throw "Policy self-check failed" }

$pythonPath = (Get-Command python).Source
$worker = Join-Path $repoDir "agent_worker.py"
$policy = Join-Path $repoDir "agent_policy.json"
$arg = '"{0}" --policy "{1}" --loop' -f $worker, $policy
$action = New-ScheduledTaskAction -Execute $pythonPath -Argument $arg -WorkingDirectory $repoDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "Policy-gated repository agent. PR-only writes; no merge/deploy path."

if ($PSCmdlet.ShouldProcess($TaskName, "Register and start policy worker")) {
    Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
    Start-ScheduledTask -TaskName $TaskName
}

Start-Sleep -Seconds 2
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host "STATUS=INSTALLED"
Write-Host "TASK=$TaskName"
Write-Host "STATE=$((Get-ScheduledTask -TaskName $TaskName).State)"
Write-Host "LAST_RESULT=$($info.LastTaskResult)"
Write-Host "CONTROL_PLANE=$repoDir"
