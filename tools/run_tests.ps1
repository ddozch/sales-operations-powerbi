<#
.SYNOPSIS
    Golden-master test of the model: every figure in data\expected.json against the real Power BI engine.

.DESCRIPTION
    Steps:
      1. set_data_folder.ps1 writes the data\ path into the DataFolder parameter; in a Git clone
         the run fails when the Git index or the last commit holds a local path instead of the
         placeholder (no output when they do not);
      2. ModelTool validate checks the TMDL and the model rules offline;
      3. open_desktop.ps1 opens SalesOps.pbip and refreshes it (or -Port reuses a running Desktop);
      4. ModelTool test compares expected.json with DAX query results;
      5. the Desktop started by this script is stopped (unless -KeepOpen).
    Exit code 0 when every check passes, 1 on any failure.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\run_tests.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\run_tests.ps1 -Port 51234
#>
param(
    # Port of a running Power BI Desktop engine with SalesOps.pbip open and refreshed
    [int]$Port,
    # Leave the Desktop started by this script open (it prints its PORT and PID)
    [switch]$KeepOpen,
    # Stop a Power BI Desktop that is already running before opening a new one
    [switch]$StopRunning,
    # Print every FAIL line, not only the first ones of each section
    [switch]$AllFails,
    [int]$TimeoutSec = 300
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
. (Join-Path $PSScriptRoot 'shared.ps1')
$watch = [Diagnostics.Stopwatch]::StartNew()
$savedEncoding = Set-Utf8Console

$started = $null
$exitCode = 1
try {
    $tool = Get-ModelTool

    Write-Step 'Data folder'
    & (Join-Path $PSScriptRoot 'set_data_folder.ps1') | Out-Host
    $leaks = @(Get-DataFolderLeak $root)
    if ($leaks.Count) { throw ($leaks -join ' ') }

    Write-Step 'Validate the TMDL model'
    & $tool validate (Join-Path $root 'SalesOps.SemanticModel\definition') | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'Model validation failed' }

    if ($Port) {
        Write-Step "Reuse the running Power BI Desktop engine on port $Port"
    }
    else {
        Write-Step 'Open SalesOps.pbip in Power BI Desktop and refresh'
        $desktop = Open-OwnDesktop -TimeoutSec $TimeoutSec -StopRunning:$StopRunning
        $Port = $desktop.Port
        $started = $desktop.Id
    }

    Write-Step 'Golden-master test'
    $testArgs = @('test', $Port, (Join-Path $root 'data\expected.json'))
    if ($AllFails) { $testArgs += '--all-fails' }
    & $tool @testArgs | Out-Host
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

Write-Host ''
$elapsed = $watch.Elapsed.TotalSeconds.ToString('0', [Globalization.CultureInfo]::InvariantCulture)
if ($exitCode -eq 0) { Write-Host "TESTS PASSED in $elapsed s" } else { Write-Host "TESTS FAILED in $elapsed s" }
exit $exitCode
