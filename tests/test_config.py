"""Tests for nff.config — load, save, get/set device, exists, ConfigError."""

import json
import pytest
from unittest.mock import patch

from nff import config as cfg


# ---------------------------------------------------------------------------
# load()
# ---------------------------------------------------------------------------

def test_load_returns_default_when_no_file(isolated_config):
    result = cfg.load()
    assert result["version"] == "1"
    assert result["default_device"]["baud"] == 9600
    assert result["default_device"]["port"] is None


def test_load_returns_parsed_config(isolated_config):
    cfg_path = isolated_config / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({
        "version": "1",
        "default_device": {"port": "COM3", "board": "Arduino Uno",
                           "fqbn": "arduino:avr:uno", "baud": 9600},
    }))
    result = cfg.load()
    assert result["default_device"]["port"] == "COM3"
    assert result["default_device"]["board"] == "Arduino Uno"


def test_load_raises_config_error_on_bad_json(isolated_config):
    cfg_path = isolated_config / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("not json {{{")
    with pytest.raises(cfg.ConfigError, match="Could not read"):
        cfg.load()


# ---------------------------------------------------------------------------
# save()
# ---------------------------------------------------------------------------

def test_save_creates_directory_and_file(isolated_config):
    data = {"version": "1", "default_device": {"port": "COM5", "board": "X",
                                                "fqbn": "x:x:x", "baud": 115200}}
    cfg.save(data)
    assert cfg.CONFIG_PATH.exists()
    written = json.loads(cfg.CONFIG_PATH.read_text())
    assert written["default_device"]["port"] == "COM5"


def test_save_overwrites_existing_config(isolated_config):
    cfg.save({"version": "1", "default_device": {"port": "COM3", "board": "A",
                                                  "fqbn": "a:a:a", "baud": 9600}})
    cfg.save({"version": "1", "default_device": {"port": "COM7", "board": "B",
                                                  "fqbn": "b:b:b", "baud": 115200}})
    written = json.loads(cfg.CONFIG_PATH.read_text())
    assert written["default_device"]["port"] == "COM7"


def test_save_raises_config_error_on_write_failure(isolated_config):
    # mkdir must succeed first; patching Path.open makes the write fail
    isolated_config.mkdir(parents=True, exist_ok=True)
    with patch("pathlib.Path.open", side_effect=OSError("disk full")):
        with pytest.raises(cfg.ConfigError, match="Could not write"):
            cfg.save({"version": "1"})


# ---------------------------------------------------------------------------
# get_default_device() / set_default_device()
# ---------------------------------------------------------------------------

def test_get_default_device_returns_device_block(isolated_config):
    cfg_path = isolated_config / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({
        "version": "1",
        "default_device": {"port": "COM10", "board": "ESP32 (CP210x)",
                           "fqbn": "esp32:esp32:esp32", "baud": 115200},
    }))
    device = cfg.get_default_device()
    assert device["port"] == "COM10"
    assert device["fqbn"] == "esp32:esp32:esp32"


def test_get_default_device_returns_empty_dict_when_no_config(isolated_config):
    device = cfg.get_default_device()
    assert isinstance(device, dict)


def test_set_default_device_writes_and_persists(isolated_config):
    cfg.set_default_device(port="COM10", board="ESP32 (CP210x)",
                           fqbn="esp32:esp32:esp32", baud=115200)
    written = json.loads(cfg.CONFIG_PATH.read_text())
    assert written["default_device"]["port"] == "COM10"
    assert written["default_device"]["baud"] == 115200


def test_set_default_device_preserves_version(isolated_config):
    cfg.set_default_device(port="COM3", board="Arduino Uno",
                           fqbn="arduino:avr:uno", baud=9600)
    written = json.loads(cfg.CONFIG_PATH.read_text())
    assert written["version"] == "1"


# ---------------------------------------------------------------------------
# exists()
# ---------------------------------------------------------------------------

def test_exists_returns_false_when_no_config(isolated_config):
    assert cfg.exists() is False


def test_exists_returns_true_after_save(isolated_config):
    cfg.set_default_device(port="COM3", board="X", fqbn="x:x:x")
    assert cfg.exists() is True


# ---------------------------------------------------------------------------
# bump_nudge_count()
# ---------------------------------------------------------------------------

def test_bump_nudge_count_from_empty_increments_and_persists(isolated_config):
    assert cfg.bump_nudge_count() == 1
    assert cfg.bump_nudge_count() == 2
    assert cfg.bump_nudge_count() == 3
    assert json.loads(cfg.CONFIG_PATH.read_text())["nudge_count"] == 3


def test_bump_nudge_count_tolerates_legacy_config_without_key(isolated_config):
    cfg_path = isolated_config / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    # A config written before the nudge_count field existed.
    cfg_path.write_text(json.dumps({
        "version": "1",
        "default_device": {"port": None, "board": None, "fqbn": None, "baud": 9600},
    }))
    assert int(cfg.load().get("nudge_count") or 0) == 0
    assert cfg.bump_nudge_count() == 1


def test_bump_nudge_count_returns_zero_on_write_failure(isolated_config, monkeypatch):
    monkeypatch.setattr(cfg, "save", lambda data: (_ for _ in ()).throw(cfg.ConfigError("boom")))
    assert cfg.bump_nudge_count() == 0
