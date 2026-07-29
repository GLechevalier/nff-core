# nff staging installer — Windows
#
#   irm https://nanoforgeflow.com/install-staging.ps1 | iex
#
# Installs the ROLLING STAGING PRERELEASE (built from the `staging` branch,
# refreshed on every push — not a stable release). Thin wrapper: selects the
# staging channel and delegates to the main installer.
#
# A copy of this file is served from the nanoforgeflow-landing repo's `public/`
# directory — keep the two in sync (canonical source: nff/scripts/install-staging.ps1).

$env:NFF_VERSION = "staging"
irm https://nanoforgeflow.com/install.ps1 | iex
