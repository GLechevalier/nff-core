<#
.SYNOPSIS
    Sync the Arduino library from the nff-sdk-c repo, then build + flash a sketch.
.DESCRIPTION
    One-command sync-build-flash. Guarantees the device is built against the
    CURRENT repo sources (even uncommitted edits) by regenerating the Arduino
    library first, then compiling and uploading with arduino-cli.
.EXAMPLE
    .\flash.ps1                         # hello_nff -> COM10
.EXAMPLE
    .\flash.ps1 -Sketch blink_nff -Port COM7
#>
param(
    [string]$Sketch = 'hello_nff',
    [string]$Port   = 'COM10',
    [string]$Fqbn   = 'esp32:esp32:esp32'
)
$ErrorActionPreference = 'Stop'

# nff-sdk-c is a sibling of nff-fleet under nff_cli_mcp\.  sketches\ -> nff-fleet -> nff_cli_mcp
$sdk = Join-Path $PSScriptRoot '..\..\nff-sdk-c'
if (-not (Test-Path (Join-Path $sdk 'tools\sync_arduino_lib.py'))) {
    throw "Could not locate nff-sdk-c at $sdk"
}
$sketchDir = Join-Path $PSScriptRoot $Sketch
if (-not (Test-Path $sketchDir)) { throw "Sketch not found: $sketchDir" }

Write-Host "==> Syncing Arduino lib from repo ($sdk)"
python (Join-Path $sdk 'tools\sync_arduino_lib.py')
if ($LASTEXITCODE -ne 0) { throw "Arduino lib sync failed" }

Write-Host "==> Building + uploading '$Sketch' to $Port ($Fqbn)"
arduino-cli compile --fqbn $Fqbn -u -p $Port $sketchDir
if ($LASTEXITCODE -ne 0) { throw "compile/upload failed (is $Port free? close any Serial Monitor)" }

Write-Host "==> Done. '$Sketch' is flashed against the latest repo sources."
