import pytest
from nff import config as cfg_module


@pytest.fixture(autouse=True)
def _pin_arduino_backend(request, monkeypatch):
    """Pin the arduino backend for the legacy/arduino-path suite.

    The production default is now ``platformio`` (``active_backend()``), which would
    silently reroute every arduino-path test through the pio dispatcher. The tests in
    ``test_platformio.py`` exercise backend selection themselves, so they opt out and
    drive ``NFF_BUILD_BACKEND`` directly.
    """
    if "test_platformio" in request.node.nodeid:
        return
    monkeypatch.setenv("NFF_BUILD_BACKEND", "arduino")


@pytest.fixture(autouse=True)
def _no_telemetry(monkeypatch):
    """Never let a test's simulated install/update send a real install-event ping."""
    monkeypatch.setenv("NFF_NO_TELEMETRY", "1")


@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    """Redirect config paths to tmp_path so tests never touch ~/.nff/config.json."""
    cfg_dir = tmp_path / ".nff"
    monkeypatch.setattr(cfg_module, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(cfg_module, "CONFIG_PATH", cfg_dir / "config.json")
    return cfg_dir
