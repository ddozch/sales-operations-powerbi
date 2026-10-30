<#
.SYNOPSIS
    Mutation run: shows that the golden-master test catches deliberate mistakes in the model.

.DESCRIPTION
    Opens SalesOps.pbip (or reuses a running Desktop with -Port), then ModelTool applies each
    mistake of tools\mutants.json to the model deployed in Desktop (a wrong time-zone rule, a
    wrong CSV encoding, a missing filter in a measure, ...), refreshes, runs the whole test and
    expects failures. A mutant that passes every check "survives": the test has a gap there.
    The TMDL files are never changed; the original model is restored and must pass at the end.
    Do not save the Desktop afterwards. Exit code 0 when every mutant is killed.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\run_mutants.ps1
#>
param(
    [int]$Port,
    [switch]$KeepOpen,
    [switch]$StopRunning,
    [int]$TimeoutSec = 300
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
. (Join-Path $PSScriptRoot 'shared.ps1')
$savedEncoding = Set-Utf8Console

$started = $null
$exitCode = 1
try {
    $tool = Get-ModelTool
    if (-not $Port) {
        Write-Step 'Open SalesOps.pbip in Power BI Desktop and refresh'
        $desktop = Open-OwnDesktop -TimeoutSec $TimeoutSec -StopRunning:$StopRunning
        $Port = $desktop.Port
        $started = $desktop.Id
    }
    Write-Step 'Mutation run'
    & $tool mutants $Port (Join-Path $PSScriptRoot 'mutants.json') (Join-Path $root 'data\expected.json') | Out-Host
    $exitCode = if ($LASTEXITCODE -eq 0) { 0 } else { 1 }
}
catch {
    Write-Host "ERROR: $($_.Exception.Message)"
    $exitCode = 1
}
finally {
    if ($started) {
        if ($KeepOpen) { Write-Host "Power BI Desktop left open: PORT=$Port PID=$started" }
        else { Stop-OwnDesktop $started }
    }
    Restore-ConsoleEncoding $savedEncoding
}
exit $exitCode
