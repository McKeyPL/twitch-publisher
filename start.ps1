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
    Write-Host "[BLAD] Brak srodowiska .venv." -ForegroundColor Red
    Write-Host "Utworz je poleceniem: python -m venv .venv" -ForegroundColor Yellow
    exit 2
}

try {
    & $venvActivate
}
catch {
    Write-Host "[BLAD] Nie udalo sie aktywowac .venv." -ForegroundColor Red
    exit 3
}

if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot ".env") -PathType Leaf)) {
    Write-Host "[UWAGA] Brak .env; konfiguracja moze wymagac zmiennych srodowiskowych." -ForegroundColor Yellow
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
    Write-LauncherLog "Start main.py (restart nr $restartCount)."
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
        Write-LauncherLog "main.py zakonczyl dzialanie poprawnie."
        exit 0
    }

    if ($Once) {
        Write-LauncherLog "main.py zakonczyl sie kodem $pythonExitCode; tryb Once bez restartu."
        exit $pythonExitCode
    }

    $restartCount++
    Write-LauncherLog "Awaria main.py (kod $pythonExitCode). Restart nr $restartCount za $RestartDelaySeconds s."
    Write-Host "[UWAGA] Restart procesu..." -ForegroundColor Yellow
    Start-Sleep -Seconds $RestartDelaySeconds
}
