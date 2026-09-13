<#
.SYNOPSIS
  Repair Cursor Agent Windows better-sqlite3 ABI mismatch without clearing sign-in.

.DESCRIPTION
  Official Windows agent-cli packages (x64 and arm64) currently ship Node.js 24
  (NODE_MODULE_VERSION 137) alongside a better-sqlite3 native binary built for
  Node.js 22 (NODE_MODULE_VERSION 127). That mismatch crashes exec-daemon right
  after worker authentication.

  This script replaces only better_sqlite3.node with the matching official
  WiseLibs prebuild for ABI 137. It does NOT:
    - delete %LOCALAPPDATA%\cursor-agent
    - reinstall the CLI
    - run `agent login` / clear credentials
    - modify node_sqlite3.node (N-API; separate Smart App Control signing issue)

.PARAMETER AgentRoot
  Root of the cursor-agent install. Defaults to %LOCALAPPDATA%\cursor-agent.

.PARAMETER BetterSqlite3Version
  Optional pin. When omitted, reads the version from each install's package.json
  (falls back to 12.11.1, the version shipped in 2026.09.10-fd3934a).

.PARAMETER TargetAbi
  NODE_MODULE_VERSION to install. Default 137 (Node 24).

.PARAMETER DryRun
  Report actions without writing files.
#>
[CmdletBinding()]
param(
    [string]$AgentRoot = $(Join-Path $env:LOCALAPPDATA "cursor-agent"),
    [string]$BetterSqlite3Version,
    [int]$TargetAbi = 137,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-NativeArch {
    # Prefer WMI so WOW64 redirection cannot lie about ARM64 hosts.
    try {
        $systemType = (Get-CimInstance Win32_ComputerSystem).SystemType
        if ($systemType -like "*ARM64*") { return "arm64" }
    } catch {
        try {
            $systemType = (Get-WmiObject Win32_ComputerSystem).SystemType
            if ($systemType -like "*ARM64*") { return "arm64" }
        } catch { }
    }
    if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { return "arm64" }
    return "x64"
}

function Get-BetterSqlite3VersionFromInstall {
    param([string]$ReleaseNodePath, [string]$Fallback)
    $pkg = Join-Path (Split-Path (Split-Path (Split-Path $ReleaseNodePath -Parent) -Parent) -Parent) "package.json"
    if (Test-Path $pkg) {
        try {
            $json = Get-Content -Raw -Path $pkg | ConvertFrom-Json
            if ($json.version) { return [string]$json.version }
        } catch { }
    }
    return $Fallback
}

function Get-Sha256 {
    param([string]$Path)
    return (Get-FileHash -Algorithm SHA256 -Path $Path).Hash.ToLowerInvariant()
}

function Download-OfficialPrebuild {
    param(
        [string]$Version,
        [string]$Platform, # win32-x64 | win32-arm64
        [int]$Abi,
        [string]$DestDir
    )
    $name = "better-sqlite3-v$Version-node-v$Abi-$Platform.tar.gz"
    $url = "https://github.com/WiseLibs/better-sqlite3/releases/download/v$Version/$name"
    $archive = Join-Path $DestDir $name
    Write-Host "Downloading official prebuild:"
    Write-Host "  $url"
    Invoke-WebRequest -Uri $url -OutFile $archive

    $extractDir = Join-Path $DestDir ("extracted-" + [Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $extractDir | Out-Null

    # tar.exe ships with modern Windows 10/11.
    & tar -xzf $archive -C $extractDir
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to extract $archive (is tar.exe available?)"
    }

    $node = Get-ChildItem -Path $extractDir -Recurse -Filter "better_sqlite3.node" |
        Where-Object { $_.FullName -match '[\\/]build[\\/]Release[\\/]better_sqlite3\.node$' } |
        Select-Object -First 1
    if (-not $node) {
        throw "Extracted archive did not contain build/Release/better_sqlite3.node"
    }
    return $node.FullName
}

$arch = Get-NativeArch
$platform = if ($arch -eq "arm64") { "win32-arm64" } else { "win32-x64" }
$fallbackVersion = if ($BetterSqlite3Version) { $BetterSqlite3Version } else { "12.11.1" }

Write-Host "Cursor Agent better-sqlite3 ABI repair"
Write-Host "  AgentRoot : $AgentRoot"
Write-Host "  Arch      : $arch ($platform)"
Write-Host "  Target ABI: $TargetAbi"
Write-Host "  DryRun    : $DryRun"
Write-Host "  Sign-in   : preserved (credentials / versions tree are not deleted)"
Write-Host ""

if (-not (Test-Path $AgentRoot)) {
    throw "Agent root not found: $AgentRoot. Install Cursor CLI first, then re-run this repair (do not wipe the folder if you want to keep sign-in)."
}

$versionsRoot = Join-Path $AgentRoot "versions"
if (-not (Test-Path $versionsRoot)) {
    throw "No versions directory under $AgentRoot"
}

$targets = Get-ChildItem -Path $versionsRoot -Recurse -Filter "better_sqlite3.node" -ErrorAction SilentlyContinue |
    Where-Object {
        $_.FullName -match '[\\/]node_modules[\\/]better-sqlite3[\\/]build[\\/]Release[\\/]better_sqlite3\.node$'
    }

if (-not $targets -or $targets.Count -eq 0) {
    throw "No better_sqlite3.node installs found under $versionsRoot"
}

$work = Join-Path ([System.IO.Path]::GetTempPath()) ("cursor-sqlite-repair-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $work | Out-Null

try {
    $cache = @{} # version -> local prebuild path
    $repaired = 0

    foreach ($target in $targets) {
        $version = if ($BetterSqlite3Version) {
            $BetterSqlite3Version
        } else {
            Get-BetterSqlite3VersionFromInstall -ReleaseNodePath $target.FullName -Fallback $fallbackVersion
        }

        Write-Host "Found: $($target.FullName)"
        Write-Host "  package version: $version"
        Write-Host "  current sha256 : $(Get-Sha256 $target.FullName)"

        if (-not $cache.ContainsKey($version)) {
            $cache[$version] = Download-OfficialPrebuild -Version $version -Platform $platform -Abi $TargetAbi -DestDir $work
        }
        $replacement = $cache[$version]
        $replacementSha = Get-Sha256 $replacement
        Write-Host "  replacement    : $replacement"
        Write-Host "  replacement sha: $replacementSha"

        if ((Get-Sha256 $target.FullName) -eq $replacementSha) {
            Write-Host "  already ABI-matched; skipping"
            continue
        }

        $backup = "$($target.FullName).abi127.bak"
        if ($DryRun) {
            Write-Host "  DRY RUN: would back up to $backup and replace with ABI $TargetAbi binary"
        } else {
            if (-not (Test-Path $backup)) {
                Copy-Item -Path $target.FullName -Destination $backup -Force
                Write-Host "  backup         : $backup"
            } else {
                Write-Host "  backup exists  : $backup"
            }
            Copy-Item -Path $replacement -Destination $target.FullName -Force
            Write-Host "  wrote          : $($target.FullName)"
            Write-Host "  new sha256     : $(Get-Sha256 $target.FullName)"
        }
        $repaired++
    }

    Write-Host ""
    if ($DryRun) {
        Write-Host "Dry run complete. $repaired install(s) would be repaired."
    } else {
        Write-Host "Repaired $repaired install(s)."
        Write-Host "Existing agent login was left untouched."
        Write-Host "Restart workers with: agent worker start"
    }
}
finally {
    if (Test-Path $work) {
        Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
    }
}
