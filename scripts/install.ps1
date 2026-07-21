# nff installer — Windows
#
#   irm https://nanoforgeflow.com/install.ps1 | iex
#   irm https://raw.githubusercontent.com/GLechevalier/nff/main/scripts/install.ps1 | iex   # mirror
#
# Downloads the prebuilt standalone nff.exe from the latest GitHub Release
# (or a pinned version) and installs it to %LOCALAPPDATA%\Programs\nff.
#
#   $env:NFF_VERSION = "0.2.40"; irm .../install.ps1 | iex    # pin a version
#
# No param() block on purpose: Invoke-Expression of piped script text cannot
# carry parameters, so configuration is via NFF_VERSION / NFF_INSTALL_DIR.
#
# A copy of this file is served from the nanoforgeflow-landing repo's `public/`
# directory — keep the two in sync (canonical source: nff/scripts/install.ps1).

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Repo = "GLechevalier/nff"
$Version = if ($env:NFF_VERSION) { $env:NFF_VERSION.TrimStart("v") } else { "latest" }
$InstallDir = if ($env:NFF_INSTALL_DIR) { $env:NFF_INSTALL_DIR } else { Join-Path $env:LOCALAPPDATA "Programs\nff" }

# ── Detect platform → release asset name ────────────────────────────────────
$Arch = if ([Environment]::Is64BitOperatingSystem) { $env:PROCESSOR_ARCHITECTURE } else { "x86" }
if ($Arch -notin @("AMD64", "ARM64")) {
    throw "Unsupported architecture: $Arch. Try: pip install nff"
}
# No native Windows-arm64 build yet; the x64 binary runs on ARM64 via emulation.
$Asset = "nff-windows-x64.exe"

$BaseUrl = if ($Version -eq "latest") {
    "https://github.com/$Repo/releases/latest/download"
} else {
    "https://github.com/$Repo/releases/download/v$Version"
}

# ── Download ────────────────────────────────────────────────────────────────
$TmpDir = Join-Path ([IO.Path]::GetTempPath()) "nff-install-$([IO.Path]::GetRandomFileName())"
New-Item -ItemType Directory -Path $TmpDir -Force | Out-Null
$TmpExe = Join-Path $TmpDir "nff.exe"

try {
    Write-Host "Downloading $Asset ($Version) ..."
    Invoke-WebRequest -UseBasicParsing -Uri "$BaseUrl/$Asset" -OutFile $TmpExe

    # ── Verify checksum (best-effort: older releases have no SHA256SUMS) ────
    $SumsFile = Join-Path $TmpDir "SHA256SUMS"
    $Expected = $null
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "$BaseUrl/SHA256SUMS" -OutFile $SumsFile
        $Line = Get-Content $SumsFile | Where-Object { $_ -match [regex]::Escape($Asset) } | Select-Object -First 1
        if ($Line) { $Expected = ($Line -split "\s+")[0].ToLower() }
    } catch {
        Write-Host "WARNING: no SHA256SUMS published for this release - skipping verification."
    }
    if ($Expected) {
        $Actual = (Get-FileHash -Algorithm SHA256 $TmpExe).Hash.ToLower()
        if ($Actual -ne $Expected) {
            throw "Checksum mismatch for $Asset (expected $Expected, got $Actual)"
        }
        Write-Host "Checksum OK."
    }

    # ── Install ─────────────────────────────────────────────────────────────
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    $Target = Join-Path $InstallDir "nff.exe"
    Move-Item -Force $TmpExe $Target
    Write-Host "Installed to $Target"

    # ── Ensure InstallDir is on the user PATH (mirrors nff-rs installer.rs) ─
    $UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if (-not $UserPath) { $UserPath = "" }
    $OnPath = ($UserPath -split ";" | Where-Object { $_.TrimEnd("\") -ieq $InstallDir.TrimEnd("\") }).Count -gt 0
    if (-not $OnPath) {
        $NewPath = if ($UserPath) { "$UserPath;$InstallDir" } else { $InstallDir }
        [Environment]::SetEnvironmentVariable("Path", $NewPath, "User")
        Write-Host "Added $InstallDir to your user PATH."
        Write-Host "Open a NEW terminal for the change to take effect everywhere."
    }
    # Make `nff` work in THIS session too.
    if (($env:Path -split ";") -notcontains $InstallDir) {
        $env:Path = "$env:Path;$InstallDir"
    }

    # ── Verify ──────────────────────────────────────────────────────────────
    Write-Host ""
    Write-Host "nff installed successfully:"
    & $Target --version
    Write-Host ""
    Write-Host "Next: run 'nff init' to detect your board and register the MCP server."
}
finally {
    Remove-Item -Recurse -Force $TmpDir -ErrorAction SilentlyContinue
}
