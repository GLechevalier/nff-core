# Parity-harness root conftest.
#
# pytest imports conftest.py files top-down (rootdir -> test dir), so THIS file
# is imported before the vendored upstream `platformio-core/tests/conftest.py`.
# By swapping `click.testing.CliRunner` for our subprocess shim *here*, the
# vendored conftest's `from click.testing import CliRunner` transparently picks
# up the shim — so its `clirunner` fixture yields a subprocess driver keyed on
# $PIO_BIN, and the vendored test files stay byte-for-byte pristine.
#
# Baseline:  PIO_BIN=$(which pio)             pytest platformio-core/tests ...
# Parity:    PIO_BIN=/path/to/pio-rs          pytest platformio-core/tests ...
#
# Note: the Python `platformio` package must be importable in BOTH modes — the
# vendored conftest imports it directly, and the shim walks its Click tree to map
# command objects to argv paths. That's intentional: parity is measured *against*
# the reference implementation.

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SHIM_DIR = os.path.join(_HERE, "shim")
if _SHIM_DIR not in sys.path:
    sys.path.insert(0, _SHIM_DIR)

# Swap CliRunner before the vendored conftest binds the name.
import click.testing as _click_testing  # noqa: E402
import clirunner_shim as _shim  # noqa: E402

_click_testing.CliRunner = _shim.CliRunner


def pytest_report_header(config):  # pylint: disable=unused-argument
    return f"parity: PIO_BIN={os.environ.get('PIO_BIN', 'pio')} (CliRunner -> subprocess shim)"
