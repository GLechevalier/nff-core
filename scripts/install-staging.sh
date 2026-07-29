#!/bin/sh
# nff staging installer — macOS / Linux
#
#   curl -fsSL https://nanoforgeflow.com/install-staging.sh | sh
#
# Installs the ROLLING STAGING PRERELEASE (built from the `staging` branch,
# refreshed on every push — not a stable release). Thin wrapper: selects the
# staging channel and delegates to the main installer.
#
# A copy of this file is served from the nanoforgeflow-landing repo's `public/`
# directory — keep the two in sync (canonical source: nff/scripts/install-staging.sh).

set -eu

NFF_VERSION=staging
export NFF_VERSION
curl -fsSL https://nanoforgeflow.com/install.sh | sh
