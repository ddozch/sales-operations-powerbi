<#
.SYNOPSIS
    Writes the absolute path of the data folder into the DataFolder parameter of the model.

.DESCRIPTION
    Power Query needs an absolute path, and the clone location differs on every PC.
    Run this once after cloning, before opening SalesOps.pbip. Without -Path it uses the
    data\ folder of this project. The path always ends with a backslash.

    The repository holds a placeholder instead of a real path. In a Git work tree the script
    also registers the clean filter "datafolder" of .gitattributes in the local .git\config:
    Git then stores the placeholder whenever it reads the file, so the path of this PC is never
    committed and git status stays clean. A clone does not copy .git\config, so run the script
    in every new clone before the first commit, also when DataFolder is set in Power BI Desktop.
    It warns when the Git index or the last commit already holds a local path. Without Git
    (a zip download) the Git steps are skipped.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\set_data_folder.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\set_data_folder.ps1 -Path D:\exports\2026-10-28
#>
param([string]$Path)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$file = Join-Path $root 'SalesOps.SemanticModel\definition\expressions.tmdl'
. (Join-Path $PSScriptRoot 'shared.ps1')   # $DataFolderPlaceholder, Get-DataFolderLeak
$placeholder = $DataFolderPlaceholder

# The clean filter replaces only the DataFolder literal with the placeholder (sed of Git for Windows;
# -b keeps CRLF line ends as they are). Git runs it whenever it reads the file (add, diff, status);
# the file on disk keeps the real path. The rule takes the whole M text literal ("" is an escaped
# quote) of a line that starts with 'expression DataFolder = ', whatever follows it, so a meta record
# written in another order by Desktop is cleaned too. \x22 is the double quote: PowerShell 5.1 does
# not pass a double quote inside an argument to git correctly. required: if sed cannot run, git add
# fails instead of storing the real path. Returns $true in a Git work tree with the filter registered.
function Register-GitFilter {
    $ErrorActionPreference = 'Continue'   # outside a work tree git writes to stderr
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { return $false }
    if ((git -C $root rev-parse --is-inside-work-tree 2>$null) -ne 'true') { return $false }
    $clean = "sed -b -E 's/^(expression DataFolder = \x22)([^\x22]|\x22\x22)*\x22/\1" +
        $placeholder.Replace('\', '\\') + "\x22/'"
    $settings = [ordered]@{ 'filter.datafolder.clean' = $clean; 'filter.datafolder.smudge' = 'cat'; 'filter.datafolder.required' = 'true' }
    $changed = $false
    foreach ($name in $settings.Keys) {
        if ((git -C $root config --local --get $name 2>$null) -ceq $settings[$name]) { continue }
        git -C $root config --local $name $settings[$name]
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Could not set $name in .git\config: do not commit expressions.tmdl, it holds the path of this PC"
            return $false
        }
        $changed = $true
    }
    if ($changed) { Write-Host "Git filter datafolder registered: the repository keeps DataFolder = $placeholder" }
    $true
}

# After a clone or checkout the index holds the size of the file with the placeholder. Git takes a
# file of another size as changed without running the filter, so git status would list the file
# although git diff shows nothing. When the filtered file equals the indexed one, refresh its entry.
function Update-GitIndex {
    $ErrorActionPreference = 'Continue'
    $dir = Split-Path $file -Parent
    $name = Split-Path $file -Leaf
    git -C $dir diff-files --quiet -- $name 2>$null
    if ($LASTEXITCODE -eq 0) { return }
    $indexed = git -C $dir rev-parse -q --verify ":./$name" 2>$null
    if (-not $indexed) { return }   # not tracked yet, or in a merge conflict
    $filtered = git -C $dir hash-object "--path=$name" -- $name 2>$null
    if ($filtered -ceq $indexed) { git -C $dir update-index -- $name 2>$null }
}

$inGit = Register-GitFilter
if (-not $Path) { $Path = Join-Path $root 'data' }
$folder = [IO.Path]::GetFullPath($Path).TrimEnd('\') + '\'
if (-not (Test-Path -LiteralPath $folder -PathType Container)) {
    Write-Warning "Folder $folder does not exist (yet)"
}

$text = [IO.File]::ReadAllText($file)
# M text literal: double the quotes; "#(" would start an escape sequence
$literal = $folder.Replace('"', '""').Replace('#(', '#(#)(')
$pattern = '(?m)^(expression DataFolder = )"(?:[^"]|"")*"'
if ($text -notmatch $pattern) { throw "DataFolder parameter not found in $file" }
$evaluator = { param($m) $m.Groups[1].Value + '"' + $literal + '"' }
$new = [regex]::Replace($text, $pattern, $evaluator)
if ($new -cne $text) {
    [IO.File]::WriteAllText($file, $new, (New-Object Text.UTF8Encoding $false))
}
if ($inGit) {
    Update-GitIndex
    foreach ($leak in Get-DataFolderLeak $root) { Write-Warning $leak }
}
"DataFolder = $folder"
