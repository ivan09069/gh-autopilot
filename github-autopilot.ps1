<#
Compatibility entrypoint for the policy-gated worker.
The previous implementation included an automated PR merge path. That behavior is
intentionally removed. This shim can only start the PR-only supervisor.
#>
[CmdletBinding()]
param(
    [switch]$Loop,
    [switch]$DryRun,
    [string]$Policy = (Join-Path $PSScriptRoot "agent_policy.json")
)

$ErrorActionPreference = "Stop"
$worker = Join-Path $PSScriptRoot "agent_worker.py"
if (-not (Test-Path $worker)) { throw "agent_worker.py not found: $worker" }

$args = @($worker, "--policy", $Policy)
if ($Loop) { $args += "--loop" }
if ($DryRun) { $args += "--dry-run" }

& python @args
exit $LASTEXITCODE
