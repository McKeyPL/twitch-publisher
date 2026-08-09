[CmdletBinding()]
param(
    [string]$Config = "config.yaml",
    [switch]$Once,
    [switch]$DryRun,
    [switch]$BrowserDebug,
    [switch]$Login,
    [string[]]$VideoId = @(),
    [ValidateRange(1, 3600)]
    [int]$RestartDelaySeconds = 10
)

Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
Set-Location -LiteralPath $PSScriptRoot

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    Write-Host "[ERROR] The .venv environment does not exist. Run start.ps1 or create it first." -ForegroundColor Red
    exit 2
}

$logsDirectory = Join-Path $PSScriptRoot "logs"
New-Item -ItemType Directory -Path $logsDirectory -Force | Out-Null
$launcherLog = Join-Path $logsDirectory "start_copyright_guard_ps1.log"
$restartCount = 0

function Write-LauncherLog {
    param([Parameter(Mandatory = $true)][string]$Message)
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -LiteralPath $launcherLog -Value $line -Encoding UTF8
    Write-Host $line -ForegroundColor Cyan
}

while ($true) {
    Write-LauncherLog "Starting copyright_guard.py (restart number $restartCount)."
    $arguments = @("copyright_guard.py", "--config", $Config)
    if ($Once) { $arguments += "--once" }
    if ($DryRun) { $arguments += "--dry-run" }
    if ($BrowserDebug) { $arguments += "--browser-debug" }
    if ($Login) { $arguments += "--login" }
    foreach ($id in $VideoId) { $arguments += @("--video-id", $id) }

    & $python @arguments
    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 0) {
        Write-LauncherLog "copyright_guard.py exited successfully."
        exit 0
    }
    if ($Once -or $Login) {
        Write-LauncherLog "copyright_guard.py exited with code $exitCode; one-shot mode will not restart."
        exit $exitCode
    }
    $restartCount++
    Write-LauncherLog "copyright_guard.py failed with code $exitCode. Restart $restartCount in $RestartDelaySeconds s."
    Start-Sleep -Seconds $RestartDelaySeconds
}
