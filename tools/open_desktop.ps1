<#
.SYNOPSIS
    Opens SalesOps.pbip in Power BI Desktop, refreshes the model and prints "PORT=<port> PID=<pid>".

.DESCRIPTION
    A PBIP has no data cache, so its tables are empty until a refresh. The script first writes
    the data folder into the DataFolder parameter (set_data_folder.ps1), so a fresh clone or a
    copy never loads the CSV files of another folder. Then it starts Power BI Desktop, waits for
    the Analysis Services engine of the NEW workspace (an older Desktop may still be running),
    runs a full refresh over XMLA with ModelTool, and closes first-run tip pop-ups through
    UI Automation (no mouse clicks or keystrokes).
    Desktop stays open: stop it with Stop-Process -Id <PID>.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\open_desktop.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\open_desktop.ps1 -DataFolder D:\exports\2026-10-28
#>
param(
    [string]$Pbip = (Join-Path (Split-Path $PSScriptRoot -Parent) 'SalesOps.pbip'),
    # Folder with the CSV export; default: the data\ folder of this project
    [string]$DataFolder,
    [switch]$NoRefresh,
    # Stops every running Power BI Desktop first (unsaved work in it is lost)
    [switch]$StopRunning,
    [int]$TimeoutSec = 300,
    # Text of the Close button on the first-run tips; add the word your Desktop uses in another language
    [string[]]$CloseNames = @('Close', 'Zamknij')
)
$ErrorActionPreference = 'Stop'
$Pbip = (Resolve-Path -LiteralPath $Pbip).Path
. (Join-Path $PSScriptRoot 'shared.ps1')

# Closes the first-run tip pop-ups (buttons named as in -CloseNames, inside the report area).
# Buttons in the title bar are skipped, so Desktop itself is never closed. In another interface
# language the tips just stay open; the script carries on after 30 s.
function Close-Tips([int]$ProcessId) {
    Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
    $handle = (Get-Process -Id $ProcessId).MainWindowHandle
    if ($handle -eq [IntPtr]::Zero) { return 0 }
    $window = [Windows.Automation.AutomationElement]::FromHandle($handle)
    $top = $window.Current.BoundingRectangle.Top
    $isButton = New-Object Windows.Automation.PropertyCondition(
        [Windows.Automation.AutomationElement]::ControlTypeProperty, [Windows.Automation.ControlType]::Button)
    $names = $CloseNames
    $closed = 0
    foreach ($button in $window.FindAll([Windows.Automation.TreeScope]::Descendants, $isButton)) {
        $info = $button.Current
        if ($names -notcontains $info.Name) { continue }
        if ($info.BoundingRectangle.Top - $top -lt 60) { continue }
        $pattern = $null
        if ($button.TryGetCurrentPattern([Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) {
            $pattern.Invoke()
            $closed++
        }
    }
    $closed
}

# Waits until the main window shows the file name. A titled window that is not a Power BI
# Desktop window means an error dialog (for example an unsupported model): its text is the error.
function Wait-Document([int]$ProcessId, [string]$Name, [datetime]$Deadline) {
    Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
    $auto = [Windows.Automation.AutomationElement]
    $ofProcess = New-Object Windows.Automation.PropertyCondition($auto::ProcessIdProperty, $ProcessId)
    $isText = New-Object Windows.Automation.PropertyCondition($auto::ControlTypeProperty, [Windows.Automation.ControlType]::Text)
    while ((Get-Date) -lt $Deadline) {
        if (-not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) { throw 'Power BI Desktop exited while opening the file' }
        $windows = @($auto::RootElement.FindAll([Windows.Automation.TreeScope]::Children, $ofProcess))
        if ($windows | Where-Object { $_.Current.Name -like "$Name*" }) { return }
        $dialog = $windows | Where-Object { $_.Current.Name -and $_.Current.Name -notlike '*Power BI Desktop*' } | Select-Object -First 1
        if ($dialog) {
            Start-Sleep -Seconds 1
            # The main window has a passing title while the report loads; it is not an error dialog
            # when it shows the file name a moment later
            if ($dialog.Current.Name -like "$Name*") { return }
            $texts = @($dialog.FindAll([Windows.Automation.TreeScope]::Descendants, $isText) | ForEach-Object { $_.Current.Name } | Where-Object { $_ })
            throw ("Power BI Desktop could not open the file (PID $ProcessId is still open): " + (@($dialog.Current.Name) + $texts -join ' | '))
        }
        Start-Sleep -Seconds 2
    }
    throw "Power BI Desktop did not open $Name within the time limit"
}

$running = @(Get-Process PBIDesktop -ErrorAction SilentlyContinue)
if ($running.Count -gt 0) {
    $ids = ($running | ForEach-Object { $_.Id }) -join ', '
    if (-not $StopRunning) {
        throw "Power BI Desktop is already running (PID $ids). Close it, pass -StopRunning, or reuse it with run_tests.ps1 -Port <port>."
    }
    $running | Stop-Process -Force
    Start-Sleep -Seconds 3
}

$tool = Get-ModelTool
$setFolder = Join-Path $PSScriptRoot 'set_data_folder.ps1'
if ($DataFolder) { & $setFolder -Path $DataFolder | Out-Host } else { & $setFolder | Out-Host }
$roots = @(
    (Join-Path $env:USERPROFILE 'Microsoft\Power BI Desktop Store App\AnalysisServicesWorkspaces'),
    (Join-Path $env:LOCALAPPDATA 'Microsoft\Power BI Desktop\AnalysisServicesWorkspaces'))
function Get-Workspaces { $roots | Where-Object { Test-Path $_ } | ForEach-Object { Get-ChildItem $_ -Directory } }
$before = @(Get-Workspaces | ForEach-Object { $_.FullName })

$store = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\PBIDesktopStore.exe'
$installed = Join-Path $env:ProgramFiles 'Microsoft Power BI Desktop\bin\PBIDesktop.exe'
$exe = if (Test-Path $store) { $store } elseif (Test-Path $installed) { $installed } else { throw 'Power BI Desktop is not installed' }
Write-Host "Opening $Pbip"
Start-Process $exe -ArgumentList ('"' + $Pbip + '"')

# Each Desktop session creates a new workspace folder; its engine writes the port file there
$deadline = (Get-Date).AddSeconds($TimeoutSec)
$port = $null
while (-not $port) {
    if ((Get-Date) -gt $deadline) { throw "Power BI Desktop did not start its engine within $TimeoutSec s" }
    Start-Sleep -Seconds 2
    foreach ($ws in (Get-Workspaces | Where-Object { $before -notcontains $_.FullName })) {
        $file = Join-Path $ws.FullName 'Data\msmdsrv.port.txt'
        if (Test-Path $file) {
            $text = [IO.File]::ReadAllText($file, [Text.Encoding]::Unicode) -replace '\D', ''
            if ($text) { $port = [int]$text }
        }
    }
}
$desktop = Get-Process PBIDesktop -ErrorAction SilentlyContinue | Sort-Object StartTime -Descending | Select-Object -First 1
Write-Host "Engine on port $port, Power BI Desktop PID $($desktop.Id)"
Wait-Document $desktop.Id ([IO.Path]::GetFileNameWithoutExtension($Pbip)) $deadline
Write-Host 'File opened'

if (-not $NoRefresh) {
    & $tool refresh $port --wait $TimeoutSec | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Refresh failed (Desktop PID $($desktop.Id) is still open)" }
}

# The tips appear when the report canvas is ready, which can be a little after the refresh
$tipDeadline = (Get-Date).AddSeconds(30)
do {
    Start-Sleep -Seconds 3
    $closed = Close-Tips $desktop.Id
    if ($closed -gt 0) { Write-Host "Closed $closed tip pop-up(s)" }
} while ($closed -eq 0 -and (Get-Date) -lt $tipDeadline)

"PORT=$port PID=$($desktop.Id)"
