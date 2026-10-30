# Helpers shared by the tool scripts; dot-source it: . (Join-Path $PSScriptRoot 'shared.ps1')

# What the repository holds in the DataFolder parameter instead of the data folder of a PC
$DataFolderPlaceholder = 'C:\path\to\sales-operations-powerbi\data\'

# In a Git work tree, checks the DataFolder line of expressions.tmdl in the Git index and in the last
# commit: both must hold the placeholder. They hold a local path when the file was added before
# set_data_folder.ps1 registered the clean filter, for example in a new clone where DataFolder was set
# in Power BI Desktop. Returns one message for each place with another value, without the value itself.
# Returns nothing outside Git, before the file is added and before the first commit.
function Get-DataFolderLeak([string]$Root) {
    $ErrorActionPreference = 'Continue'   # outside a work tree git writes to stderr
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { return }
    if ((git -C $Root rev-parse --is-inside-work-tree 2>$null) -ne 'true') { return }
    $path = 'SalesOps.SemanticModel/definition/expressions.tmdl'
    $expected = 'expression DataFolder = "' + $DataFolderPlaceholder + '"'
    $places = [ordered]@{
        ":$path"     = "The Git index holds a local DataFolder path in expressions.tmdl: run git add $path (the filter is registered now and stores the placeholder)"
        "HEAD:$path" = "The last commit holds a local DataFolder path in expressions.tmdl: after git add, run git commit --amend before you push (older commits may hold it too)"
    }
    foreach ($spec in $places.Keys) {
        $blob = git -C $Root rev-parse -q --verify $spec 2>$null
        if (-not $blob) { continue }
        $lines = @(git -C $Root cat-file blob $blob 2>$null | Where-Object { $_ -cmatch '^expression DataFolder\b' })
        $ok = $lines.Count -gt 0
        foreach ($line in $lines) {
            $line = $line.TrimEnd("`r")
            # the placeholder literal, not the start of a longer literal ("" is an escaped quote)
            if (-not $line.StartsWith($expected, [StringComparison]::Ordinal) -or
                ($line.Length -gt $expected.Length -and $line[$expected.Length] -eq '"')) { $ok = $false }
        }
        if (-not $ok) { $places[$spec] }
    }
}

# Builds tools\ModelTool when a source file is newer than the exe, and returns the path of the exe
function Get-ModelTool {
    if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) {
        throw 'dotnet not found: install the .NET SDK 10 or later (https://dotnet.microsoft.com/download)'
    }
    $project = Join-Path $PSScriptRoot 'ModelTool\ModelTool.csproj'
    $framework = ([xml](Get-Content -Raw $project)).Project.PropertyGroup.TargetFramework | Where-Object { $_ } | Select-Object -First 1
    $exe = Join-Path $PSScriptRoot "ModelTool\bin\Release\$framework\ModelTool.exe"
    $newest = Get-ChildItem (Join-Path $PSScriptRoot 'ModelTool') -Include *.cs, *.csproj -Recurse |
        Where-Object { $_.FullName -notmatch '\\(bin|obj)\\' } |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not (Test-Path $exe) -or (Get-Item $exe).LastWriteTime -lt $newest.LastWriteTime) {
        & dotnet build $project -c Release -nologo -v q | Out-Host
        if ($LASTEXITCODE -ne 0) { throw 'ModelTool build failed' }
    }
    $exe
}

function Write-Step([string]$Text) { Write-Host ''; Write-Host "== $Text" }

# Runs open_desktop.ps1 and returns @{ Port; Id }. On failure the Desktop it started is stopped,
# because the calling script owns it.
function Open-OwnDesktop([int]$TimeoutSec, [switch]$StopRunning) {
    $before = @(Get-Process PBIDesktop -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
    try {
        $open = Join-Path $PSScriptRoot 'open_desktop.ps1'
        $result = if ($StopRunning) { & $open -TimeoutSec $TimeoutSec -StopRunning } else { & $open -TimeoutSec $TimeoutSec }
    }
    catch {
        Get-Process PBIDesktop -ErrorAction SilentlyContinue | Where-Object { $before -notcontains $_.Id } |
            ForEach-Object { Write-Host "Stopping Power BI Desktop PID $($_.Id)"; Stop-Process -Id $_.Id -Force }
        throw
    }
    $line = @($result) | Where-Object { $_ -match '^PORT=(\d+) PID=(\d+)$' } | Select-Object -Last 1
    if (-not $line) { throw "open_desktop.ps1 did not print PORT and PID: $result" }
    @{ Port = [int]$Matches[1]; Id = [int]$Matches[2] }
}

function Stop-OwnDesktop([int]$Id) {
    Stop-Process -Id $Id -Force -ErrorAction SilentlyContinue
    Wait-Process -Id $Id -Timeout 30 -ErrorAction SilentlyContinue
    Write-Host "Stopped Power BI Desktop PID $Id"
}

# ModelTool writes UTF-8 (names with Polish letters); the console must decode it as UTF-8 too.
# Returns the previous encoding, for Restore-ConsoleEncoding.
function Set-Utf8Console {
    $saved = $null
    try { $saved = [Console]::OutputEncoding; [Console]::OutputEncoding = New-Object Text.UTF8Encoding $false } catch { }
    $saved
}

function Restore-ConsoleEncoding($Saved) {
    if ($Saved) { try { [Console]::OutputEncoding = $Saved } catch { } }
}
