#!/usr/bin/env bash
# Run the vendored PlatformIO test suite through the CliRunner subprocess shim.
#
# Usage:
#   ./run_parity.sh baseline          # against the Python `pio` on PATH
#   ./run_parity.sh rust              # against target/{debug,release}/pio-rs
#   PIO_BIN=/custom/pio ./run_parity.sh raw [extra pytest args...]
#
# Scope (env PARITY_SCOPE, default "offline"):
#   offline  — curated subset that needs NO network/toolchain downloads. This is
#              the M0 baseline gate: it proves the subprocess shim faithfully
#              reproduces Click's in-process behaviour.
#   full     — everything except skip_ci + test_examples (matches upstream
#              `tox -e testcore`); requires network and is slow.
#
# Network/registry/email tests are excluded (`-k "not skip_ci"`), as is the
# heavyweight examples suite — matching upstream's `tox -e testcore`.

set -euo pipefail

# Curated offline subset (relative to platformio-core/tests). These passed green
# on the Python `pio` baseline with no network access; the rest pull packages
# from the registry and are gated to milestones M2+.
OFFLINE_TESTS=(
  project
  misc
  commands/test_settings.py
  commands/test_boards.py
  package/test_meta.py
  package/test_manifest.py
  package/test_pack.py
)

# Tests that CANNOT run through the subprocess shim: they monkeypatch in-process
# Python state and *then* invoke the CLI, so a fresh subprocess never sees the
# patch. These are reimplemented as Rust unit tests in the relevant milestone
# (the feature they cover is noted). Applied as --deselect in every scope.
SHIM_INCOMPATIBLE=(
  # patches maintenance.__version__ + upgrade.VERSION, then runs `pio platform list`
  # to assert the "new version available" banner -> upgrade-notification logic (M5/M3).
  "misc/test_maintenance.py::test_check_pio_upgrade"
  # hardcodes proc.exec_command(["pio","--help"]) -> ignores $PIO_BIN entirely, so it
  # can't target pio-rs. Reimplement as a Rust test: `pio-rs --help` prints usage (M0/M1).
  "misc/test_misc.py::test_platformio_cli"
)

# Shim-driven tests that require a command pio-rs has NOT ported yet. Deselected
# by exact nodeid (via --deselect, NOT -k) because `test_metadata_dump` collides
# by name with an unrelated internal-API test in package/test_meta.py. Applied in
# every scope so `PARITY_SCOPE=offline ./run_parity.sh rust` is achievable. Board
# metadata parity is covered by Rust unit tests (`get_brief_data`/`get_boards`/
# `humanize_file_size`); these two are the CLI cases pio-rs can't satisfy at M3.
# Nodeids are relative to the pytest rootdir ($HERE).
NETWORK_OR_DEFERRED=(
  # drives `pio platform search` (registry) then `pio boards` — `platform search`
  # is a deferred command (returns not-implemented in pio-rs). Reachable at M2+/M5.
  "platformio-core/tests/commands/test_boards.py::test_board_options"
  # drives `pio project metadata`, which needs build-system output (native platform
  # compile) -> that's the M4 build milestone, not M3.
  "platformio-core/tests/project/test_metadata.py::test_metadata_dump"
)

# Whole test files/dirs that exercise the build system (`pio ci`/`pio run`) — the
# M4 milestone, not M3. Not collected (--ignore) in any scope. Paths are relative
# to $HERE. (Kept out of the offline gate because they compile real sketches.)
DEFERRED_PATHS=(
  "platformio-core/tests/misc/ino2cpp"   # drives `pio ci` (compile) on example sketches
)

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TESTS="$HERE/platformio-core/tests"
NFF_RS_ROOT="$(cd "$HERE/../.." && pwd)"   # nff-rs/

# Prefer the dedicated harness venv (isolated platformio install) over whatever
# `python` is on PATH, so the heavy/conflicting PlatformIO deps never leak into
# the workspace's shared .venv. Create it once with:
#   python -m venv parity/.venv-harness
#   parity/.venv-harness/Scripts/pip install platformio==6.1.19 jsondiff pytest
PY="python"
for cand in "$HERE/.venv-harness/Scripts/python.exe" "$HERE/.venv-harness/bin/python"; do
  if [ -x "$cand" ]; then PY="$cand"; break; fi
done

mode="${1:-baseline}"
shift || true

case "$mode" in
  baseline)
    # Default to the harness venv's pio, else whatever's on PATH.
    if [ -z "${PIO_BIN:-}" ]; then
      for cand in "$HERE/.venv-harness/Scripts/pio.exe" "$HERE/.venv-harness/bin/pio"; do
        if [ -x "$cand" ]; then PIO_BIN="$cand"; break; fi
      done
    fi
    PIO_BIN="${PIO_BIN:-$(command -v pio || true)}"
    ;;
  rust)
    if [ -x "$NFF_RS_ROOT/target/release/pio-rs" ]; then
      PIO_BIN="$NFF_RS_ROOT/target/release/pio-rs"
    else
      PIO_BIN="$NFF_RS_ROOT/target/debug/pio-rs"
    fi
    ;;
  raw)
    : "${PIO_BIN:?set PIO_BIN for raw mode}"
    ;;
  *)
    echo "unknown mode: $mode (use baseline|rust|raw)" >&2
    exit 2
    ;;
esac

if [ -z "${PIO_BIN:-}" ]; then
  echo "ERROR: no PIO_BIN resolved (is 'pio' installed / has pio-rs been built?)" >&2
  exit 1
fi

scope="${PARITY_SCOPE:-offline}"
echo "== parity run: mode=$mode scope=$scope PIO_BIN=$PIO_BIN =="
export PIO_BIN

targets=()
if [ "$scope" = "offline" ]; then
  for t in "${OFFLINE_TESTS[@]}"; do targets+=("$TESTS/$t"); done
else
  targets=("$TESTS")
fi

# Build a -k filter that drops skip_ci plus every shim-incompatible test (matched
# by its function name, which is unique across the suite). Using -k rather than
# --deselect avoids absolute-path nodeid mismatches on Windows/git-bash.
kfilter="not skip_ci"
for nid in "${SHIM_INCOMPATIBLE[@]}"; do
  fn="${nid##*::}"
  kfilter="$kfilter and not $fn"
done

# The deferred/network tests are dropped by exact nodeid (avoids name collisions).
deselect_args=()
for nid in "${NETWORK_OR_DEFERRED[@]}"; do
  deselect_args+=(--deselect "$nid")
done

# Build-dependent files/dirs are not collected at all.
ignore_args=(--ignore "$TESTS/test_examples.py")
for rel in "${DEFERRED_PATHS[@]}"; do
  ignore_args+=(--ignore "$HERE/$rel")
done

exec "$PY" -m pytest \
  --rootdir "$HERE" \
  -c /dev/null \
  -p no:cacheprovider \
  -k "$kfilter" \
  "${deselect_args[@]}" \
  "${ignore_args[@]}" \
  "$@" \
  "${targets[@]}"
