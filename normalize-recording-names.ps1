[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$RootPath = $env:RECORDINGS_ROOT,

    [string]$VideoPath,

    [string]$DatabasePath = (Join-Path $PSScriptRoot "data\upload_state.sqlite3"),

    [string]$PythonPath,

    [string]$UploadedDirectoryName = "_uploaded",

    [ValidateRange(60, 180)]
    [int]$MaxBaseLength = 140,

    [switch]$Apply,

    [switch]$AllowMissingDatabase
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Info {
    param([string]$Message)
    Write-Host $Message -ForegroundColor Cyan
}

function Write-WarningMessage {
    param([string]$Message)
    Write-Host $Message -ForegroundColor Yellow
}

function Repair-Mojibake {
    param([string]$Value)

    if (
        [string]::IsNullOrEmpty($Value) -or
        $Value -notmatch "[\u00C3\u00C4\u00C5\u00F0\u00E2]"
    ) {
        return $Value
    }

    try {
        $windows1252 = [System.Text.Encoding]::GetEncoding(
            1252,
            [System.Text.EncoderFallback]::ExceptionFallback,
            [System.Text.DecoderFallback]::ExceptionFallback
        )
        $strictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
        $candidate = $strictUtf8.GetString($windows1252.GetBytes($Value))
        if ($candidate -and $candidate -notmatch [char]0xFFFD) {
            return $candidate
        }
    }
    catch {
        # The value was not a reversible UTF-8/Windows-1252 mojibake sequence.
    }
    return $Value
}

function Convert-ToAscii {
    param([string]$Value)

    $Value = $Value.Replace([string][char]0x0141, "L")
    $Value = $Value.Replace([string][char]0x0142, "l")
    $Value = $Value.Replace([string][char]0x00D8, "O")
    $Value = $Value.Replace([string][char]0x00F8, "o")
    $Value = $Value.Replace([string][char]0x00DF, "ss")
    $normalized = $Value.Normalize([System.Text.NormalizationForm]::FormD)
    $builder = New-Object System.Text.StringBuilder

    foreach ($character in $normalized.ToCharArray()) {
        $category = [System.Globalization.CharUnicodeInfo]::GetUnicodeCategory($character)
        if ($category -eq [System.Globalization.UnicodeCategory]::NonSpacingMark) {
            continue
        }
        if ([int]$character -le 127) {
            [void]$builder.Append($character)
        }
    }

    return $builder.ToString()
}

function Convert-ToSafeTitle {
    param([string]$Value)

    $Value = Repair-Mojibake $Value
    $Value = [regex]::Replace($Value, "(?<!\S)![\p{L}\p{N}_-]+", " ")
    $Value = $Value.Replace("_", " ")
    $Value = Convert-ToAscii $Value
    $Value = [regex]::Replace($Value, "[^A-Za-z0-9 -]+", " ")
    $Value = [regex]::Replace($Value, "\s+", " ")
    return $Value.Trim([char[]]" .-_")
}

function Test-IsUnderUploadedDirectory {
    param(
        [string]$FullPath,
        [string]$UploadedName
    )

    # Inspect path segments instead of comparing textual root prefixes. Windows
    # can expose the same directory once as an 8.3 short path and once as its
    # long path, which would make a safe child look as if it were outside Root.
    foreach ($segment in ($FullPath -split "[\\/]")) {
        if ($segment.Equals(
            $UploadedName,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            return $true
        }
    }
    return $false
}

function Get-CleanBaseName {
    param(
        [System.IO.FileInfo]$Video,
        [int]$MaximumLength
    )

    $oldBase = [System.IO.Path]::GetFileNameWithoutExtension($Video.Name)
    $channel = $Video.Directory.Name
    $pattern = "^(?<stamp>\d{8}_\d{6})_" +
        [regex]::Escape($channel) +
        "(?:_(?<title>.*))?$"
    $match = [regex]::Match(
        $oldBase,
        $pattern,
        [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
    )

    if ($match.Success) {
        $prefix = $match.Groups["stamp"].Value + "_" + $channel
        $title = Convert-ToSafeTitle $match.Groups["title"].Value
        if ([string]::IsNullOrWhiteSpace($title)) {
            $title = "stream"
        }
        $available = $MaximumLength - $prefix.Length - 1
        if ($available -lt 1) {
            throw "MaxBaseLength is too small for recording prefix: $oldBase"
        }
        if ($title.Length -gt $available) {
            $title = $title.Substring(0, $available).Trim([char[]]" .-_")
        }
        return $prefix + "_" + $title
    }

    $fallback = Convert-ToSafeTitle $oldBase
    if ([string]::IsNullOrWhiteSpace($fallback)) {
        $fallback = "recording"
    }
    if ($fallback.Length -gt $MaximumLength) {
        $fallback = $fallback.Substring(0, $MaximumLength).Trim([char[]]" .-_")
    }
    return $fallback
}

function Resolve-PythonExecutable {
    param([string]$RequestedPath)

    if (-not [string]::IsNullOrWhiteSpace($RequestedPath)) {
        return (Resolve-Path -LiteralPath $RequestedPath).Path
    }

    $venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        return (Resolve-Path -LiteralPath $venvPython).Path
    }

    $command = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        throw "Python was not found. Pass -PythonPath or create .venv first."
    }
    return $command.Source
}

function Get-ReversedItems {
    param([object[]]$Items)

    for ($index = $Items.Count - 1; $index -ge 0; $index -= 1) {
        Write-Output $Items[$index]
    }
}

function Invoke-DatabasePathMigration {
    param(
        [object[]]$Mappings,
        [string]$Database,
        [string]$RequestedPython,
        [bool]$MaySkipMissingDatabase
    )

    if (-not (Test-Path -LiteralPath $Database -PathType Leaf)) {
        if ($MaySkipMissingDatabase) {
            Write-WarningMessage "State database is missing; path migration was skipped: $Database"
            return
        }
        throw (
            "State database is missing: $Database. Renaming without migrating " +
            "upload_status could cause duplicate uploads. Use -AllowMissingDatabase " +
            "only when this installation has no existing state."
        )
    }

    $python = Resolve-PythonExecutable $RequestedPython
    $mappingFile = [System.IO.Path]::GetTempFileName()
    $migrationFile = Join-Path (
        [System.IO.Path]::GetTempPath()
    ) (
        "twitch-publisher-migrate-" + [guid]::NewGuid().ToString("N") + ".py"
    )
    $migrationCode = @'
import json
import os
import sqlite3
import sys

database_path, mapping_path = sys.argv[1:3]

with open(mapping_path, "r", encoding="utf-8-sig") as handle:
    mappings = json.load(handle)

def normalized(value):
    return os.path.normcase(os.path.abspath(value))

connection = sqlite3.connect(database_path, timeout=30.0)
try:
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("BEGIN IMMEDIATE")
    for item in mappings:
        old_path = normalized(item["old"])
        new_path = normalized(item["new"])
        conflict = connection.execute(
            "SELECT platform FROM upload_status WHERE video_path = ? LIMIT 1",
            (new_path,),
        ).fetchone()
        if conflict is not None:
            raise RuntimeError(
                "upload_status already contains the target path "
                f"{new_path!r} for platform {conflict[0]!r}"
            )
        connection.execute(
            "UPDATE upload_status SET video_path = ? WHERE video_path = ?",
            (new_path, old_path),
        )
    connection.commit()
except Exception:
    connection.rollback()
    raise
finally:
    connection.close()
'@

    try {
        $mappingPayload = @(
            $Mappings |
                Select-Object @{Name = "old"; Expression = { $_.OldVideo }},
                    @{Name = "new"; Expression = { $_.NewVideo }}
        )
        ConvertTo-Json -InputObject $mappingPayload -Depth 3 |
            Set-Content -LiteralPath $mappingFile -Encoding UTF8
        Set-Content -LiteralPath $migrationFile -Value $migrationCode -Encoding UTF8
        & $python $migrationFile $Database $mappingFile
        if ($LASTEXITCODE -ne 0) {
            throw "SQLite path migration failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        Remove-Item -LiteralPath $mappingFile -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $migrationFile -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-RenameGroup {
    param([object[]]$Operations)

    $staged = New-Object System.Collections.ArrayList
    try {
        $index = 0
        foreach ($operation in $Operations) {
            $temporaryName = ".twitch-publisher-rename-" +
                [guid]::NewGuid().ToString("N") +
                "-" +
                $index +
                ".tmp"
            $temporaryPath = Join-Path (
                [System.IO.Path]::GetDirectoryName($operation.Source)
            ) $temporaryName
            Rename-Item -LiteralPath $operation.Source -NewName $temporaryName
            [void]$staged.Add([pscustomobject]@{
                Source = $operation.Source
                Target = $operation.Target
                Temporary = $temporaryPath
                Finalized = $false
            })
            $index += 1
        }

        foreach ($entry in $staged) {
            Rename-Item -LiteralPath $entry.Temporary -NewName (
                [System.IO.Path]::GetFileName($entry.Target)
            )
            $entry.Finalized = $true
        }
    }
    catch {
        foreach ($entry in (Get-ReversedItems -Items @($staged))) {
            $currentPath = $entry.Temporary
            if ($entry.Finalized) {
                $currentPath = $entry.Target
            }
            if (Test-Path -LiteralPath $currentPath -PathType Leaf) {
                Rename-Item -LiteralPath $currentPath -NewName (
                    [System.IO.Path]::GetFileName($entry.Source)
                ) -ErrorAction SilentlyContinue
            }
        }
        throw
    }
}

function Undo-RenameGroup {
    param([object[]]$Operations)

    foreach ($operation in (Get-ReversedItems -Items @($Operations))) {
        if (Test-Path -LiteralPath $operation.Target -PathType Leaf) {
            Rename-Item -LiteralPath $operation.Target -NewName (
                [System.IO.Path]::GetFileName($operation.Source)
            )
        }
    }
}

if ([string]::IsNullOrWhiteSpace($RootPath)) {
    $RootPath = "E:\TwitchRecordings"
}
$resolvedRoot = (Resolve-Path -LiteralPath $RootPath).Path
$resolvedDatabase = [System.IO.Path]::GetFullPath($DatabasePath)

if (-not [string]::IsNullOrWhiteSpace($VideoPath)) {
    $singleVideo = Get-Item -LiteralPath $VideoPath
    if ($singleVideo.Extension -ine ".mkv") {
        throw "-VideoPath must point to an MKV file: $VideoPath"
    }
    $videos = @($singleVideo)
}
else {
    $videos = @(
        Get-ChildItem -LiteralPath $resolvedRoot -Filter "*.mkv" -File -Recurse |
            Where-Object {
                -not (Test-IsUnderUploadedDirectory `
                    -FullPath $_.FullName `
                    -UploadedName $UploadedDirectoryName)
            }
    )
}

$plans = New-Object System.Collections.ArrayList
$claimedCompanions = @{}
$targetPaths = @{}

foreach ($video in ($videos | Sort-Object FullName)) {
    if (Test-IsUnderUploadedDirectory `
        -FullPath $video.FullName `
        -UploadedName $UploadedDirectoryName) {
        Write-WarningMessage "Skipping a video inside $UploadedDirectoryName`: $($video.FullName)"
        continue
    }

    $oldBase = [System.IO.Path]::GetFileNameWithoutExtension($video.Name)
    $newBase = Get-CleanBaseName -Video $video -MaximumLength $MaxBaseLength
    foreach ($suffix in @("_chat.srt", "_meta.txt")) {
        $existingCompanion = Join-Path $video.DirectoryName ($oldBase + $suffix)
        if (Test-Path -LiteralPath $existingCompanion -PathType Leaf) {
            $claimedCompanions[$existingCompanion.ToLowerInvariant()] = $true
        }
    }
    if ($oldBase -ceq $newBase) {
        continue
    }

    $sourceFiles = New-Object System.Collections.ArrayList
    [void]$sourceFiles.Add([pscustomobject]@{
        Source = $video.FullName
        Suffix = ".mkv"
    })
    foreach ($suffix in @("_chat.srt", "_meta.txt")) {
        $companionPath = Join-Path $video.DirectoryName ($oldBase + $suffix)
        if (Test-Path -LiteralPath $companionPath -PathType Leaf) {
            [void]$sourceFiles.Add([pscustomobject]@{
                Source = (Resolve-Path -LiteralPath $companionPath).Path
                Suffix = $suffix
            })
            $claimedCompanions[$companionPath.ToLowerInvariant()] = $true
        }
        elseif ($suffix -eq "_meta.txt") {
            Write-WarningMessage "Metadata companion is missing: $companionPath"
        }
    }

    $operations = New-Object System.Collections.ArrayList
    foreach ($sourceFile in $sourceFiles) {
        $target = Join-Path $video.DirectoryName ($newBase + $sourceFile.Suffix)
        if (
            (Test-Path -LiteralPath $target) -and
            -not $sourceFile.Source.Equals(
                $target,
                [System.StringComparison]::OrdinalIgnoreCase
            )
        ) {
            throw "Refusing to overwrite an existing target: $target"
        }
        $targetKey = $target.ToLowerInvariant()
        if ($targetPaths.ContainsKey($targetKey)) {
            throw "Two recordings would produce the same target: $target"
        }
        $targetPaths[$targetKey] = $true
        [void]$operations.Add([pscustomobject]@{
            Source = $sourceFile.Source
            Target = $target
        })
    }

    [void]$plans.Add([pscustomobject]@{
        OldVideo = $video.FullName
        NewVideo = (Join-Path $video.DirectoryName ($newBase + ".mkv"))
        Operations = @($operations)
    })
}

$orphanCompanions = @(
    Get-ChildItem -LiteralPath $resolvedRoot -File -Recurse |
        Where-Object {
            ($_.Name -like "*_chat.srt" -or $_.Name -like "*_meta.txt") -and
            -not (Test-IsUnderUploadedDirectory `
                -FullPath $_.FullName `
                -UploadedName $UploadedDirectoryName) -and
            -not $claimedCompanions.ContainsKey($_.FullName.ToLowerInvariant())
        }
)

foreach ($orphan in $orphanCompanions) {
    Write-WarningMessage "Unmatched companion was not renamed: $($orphan.FullName)"
}

if ($plans.Count -eq 0) {
    Write-Host "No recording names require changes." -ForegroundColor Green
    exit 0
}

foreach ($plan in $plans) {
    Write-Info ("VIDEO: " + $plan.OldVideo)
    foreach ($operation in $plan.Operations) {
        Write-Host ("  -> " + $operation.Target)
    }
}

if (-not $Apply) {
    Write-WarningMessage (
        "Dry run only: $($plans.Count) recording set(s) would be renamed. " +
        "Stop Twitch Publisher, review the list, then rerun with -Apply."
    )
    exit 0
}

$databaseExists = Test-Path -LiteralPath $resolvedDatabase -PathType Leaf
if (-not $AllowMissingDatabase -and -not $databaseExists) {
    throw (
        "State database is missing: $resolvedDatabase. Renaming without migrating " +
        "upload_status could cause duplicate uploads. Use -AllowMissingDatabase " +
        "only when this installation has no existing state."
    )
}

Write-WarningMessage "Applying changes. Twitch Publisher must remain stopped."
$completed = New-Object System.Collections.ArrayList
try {
    foreach ($plan in $plans) {
        Invoke-RenameGroup -Operations $plan.Operations
        [void]$completed.Add($plan)
    }

    Invoke-DatabasePathMigration `
        -Mappings @($completed) `
        -Database $resolvedDatabase `
        -RequestedPython $PythonPath `
        -MaySkipMissingDatabase ([bool]$AllowMissingDatabase)
}
catch {
    Write-WarningMessage "Renaming failed; rolling back completed file groups."
    foreach ($plan in (Get-ReversedItems -Items @($completed))) {
        Undo-RenameGroup -Operations $plan.Operations
    }
    throw
}

if ($databaseExists) {
    Write-Host (
        "Renamed $($completed.Count) recording set(s) and migrated SQLite paths."
    ) -ForegroundColor Green
}
else {
    Write-Host (
        "Renamed $($completed.Count) recording set(s); SQLite migration was skipped."
    ) -ForegroundColor Green
}
