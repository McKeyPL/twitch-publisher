[CmdletBinding()]
param(
    [string]$Config = "config.yaml",
    [switch]$Once,
    [switch]$BrowserDebug,
    [ValidateRange(1, 3600)]
    [int]$RestartDelaySeconds = 10
)

Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
Set-Location -LiteralPath $PSScriptRoot

$venvActivate = Join-Path $PSScriptRoot ".venv\Scripts\Activate.ps1"
if (-not (Test-Path -LiteralPath $venvActivate -PathType Leaf)) {
    Write-Host "[ERROR] The .venv environment does not exist." -ForegroundColor Red
    Write-Host "Create it with: python -m venv .venv" -ForegroundColor Yellow
    exit 2
}

try {
    & $venvActivate
}
catch {
    Write-Host "[ERROR] Could not activate .venv." -ForegroundColor Red
    exit 3
}

if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot ".env") -PathType Leaf)) {
    Write-Host "[WARNING] .env is missing; configuration may require environment variables." -ForegroundColor Yellow
}

$logsDirectory = Join-Path $PSScriptRoot "logs"
New-Item -ItemType Directory -Path $logsDirectory -Force | Out-Null
$launcherLog = Join-Path $logsDirectory "start_ps1.log"
$restartCount = 0

function Write-LauncherLog {
    param([Parameter(Mandatory = $true)][string]$Message)
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -LiteralPath $launcherLog -Value $line -Encoding UTF8
    Write-Host $line -ForegroundColor Cyan
}

while ($true) {
    Write-LauncherLog "Starting main.py (restart number $restartCount)."
    $pythonArguments = @("main.py", "--config", $Config)
    if ($Once) {
        $pythonArguments += "--once"
    }
    if ($BrowserDebug) {
        $pythonArguments += "--browser-debug"
    }

    & python @pythonArguments
    $pythonExitCode = $LASTEXITCODE

    if ($pythonExitCode -eq 0) {
        Write-LauncherLog "main.py exited successfully."
        exit 0
    }

    if ($Once) {
        Write-LauncherLog "main.py exited with code $pythonExitCode; Once mode will not restart."
        exit $pythonExitCode
    }

    $restartCount++
    Write-LauncherLog "main.py failed with code $pythonExitCode. Restart $restartCount in $RestartDelaySeconds s."
    Write-Host "[WARNING] Restarting process..." -ForegroundColor Yellow
    Start-Sleep -Seconds $RestartDelaySeconds
}
