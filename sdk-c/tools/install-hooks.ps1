<#
.SYNOPSIS
    Install nff git hooks that auto-regenerate the Arduino library on commit/merge.
.DESCRIPTION
    Git hooks live under .git/hooks/ which is NOT version-controlled, so this
    installer copies the tracked sources from tools/hooks/ into place. Run once
    per clone. After this, any `git commit` or `git merge`/`git pull` in this
    repo regenerates C:\Users\<you>\Documents\Arduino\libraries\nff from the repo.
#>
$ErrorActionPreference = 'Stop'

$repo     = Split-Path -Parent $PSScriptRoot      # tools\ -> repo root
$hooksSrc = Join-Path $PSScriptRoot 'hooks'
$hooksDst = Join-Path $repo '.git\hooks'

if (-not (Test-Path $hooksDst)) {
    throw "Not a git repo (missing $hooksDst). Run from inside the nff-sdk-c clone."
}

foreach ($hook in 'post-commit', 'post-merge') {
    $src = Join-Path $hooksSrc $hook
    $dst = Join-Path $hooksDst $hook
    Copy-Item $src $dst -Force
    Write-Host "installed $hook -> $dst"
}

Write-Host ""
Write-Host "Done. The Arduino library will auto-sync on every commit and merge/pull."
Write-Host "Tip: run 'python tools/sync_arduino_lib.py' anytime to sync manually."
