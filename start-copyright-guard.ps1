[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$Config = "config.yaml",
    [switch]$Once,
    [switch]$DryRun,
    [switch]$BrowserDebug,
    [switch]$Login,
    [switch]$ChannelOnly,
    [string[]]$VideoId = @(),
    [string[]]$ResetVideo = @(),
    [ValidateRange(1, 3600)]
    [int]$RestartDelaySeconds = 10,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ForwardedArguments = @()
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
$forwardedConfig = @($ForwardedArguments | Where-Object {
    $_ -eq "--config" -or $_ -like "--config=*"
}).Count -gt 0
$forwardedOnce = $ForwardedArguments -contains "--once"
$forwardedLogin = $ForwardedArguments -contains "--login"
$forwardedReset = @($ForwardedArguments | Where-Object {
    $_ -eq "--reset-video" -or $_ -like "--reset-video=*"
}).Count -gt 0

function Write-LauncherLog {
    param([Parameter(Mandatory = $true)][string]$Message)
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -LiteralPath $launcherLog -Value $line -Encoding UTF8
    Write-Host $line -ForegroundColor Cyan
}

while ($true) {
    Write-LauncherLog "Starting copyright_guard.py (restart number $restartCount)."
    $arguments = @("copyright_guard.py")
    if (-not $forwardedConfig) { $arguments += @("--config", $Config) }
    if ($Once) { $arguments += "--once" }
    if ($DryRun) { $arguments += "--dry-run" }
    if ($BrowserDebug) { $arguments += "--browser-debug" }
    if ($Login) { $arguments += "--login" }
    if ($ChannelOnly) { $arguments += "--channel-only" }
    foreach ($id in $VideoId) { $arguments += @("--video-id", $id) }
    foreach ($id in $ResetVideo) { $arguments += @("--reset-video", $id) }
    $arguments += $ForwardedArguments

    & $python @arguments
    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 130) {
        Write-LauncherLog "copyright_guard.py was interrupted by the user; not restarting."
        exit 130
    }
    if ($exitCode -eq 0) {
        Write-LauncherLog "copyright_guard.py exited successfully."
        exit 0
    }
    if ($Once -or $Login -or $ResetVideo.Count -gt 0 -or $forwardedOnce -or $forwardedLogin -or $forwardedReset) {
        Write-LauncherLog "copyright_guard.py exited with code $exitCode; one-shot mode will not restart."
        exit $exitCode
    }
    $restartCount++
    Write-LauncherLog "copyright_guard.py failed with code $exitCode. Restart $restartCount in $RestartDelaySeconds s."
    Start-Sleep -Seconds $RestartDelaySeconds
}
