"""Tests for nff.commands.init — the onboarding wizard."""

import types
from unittest.mock import patch

from click.testing import CliRunner

from nff.commands import init as init_mod
from nff.commands.init import init
from nff.tools.boards import DetectedDevice


class _FakeStream:
    def __init__(self, lines, returncode=0):
        self._lines = lines
        self.returncode = returncode

    def __iter__(self):
        yield from self._lines


def _esp32(port="COM10"):
    return DetectedDevice(port=port, board="ESP32 (CP210x)",
                          fqbn="esp32:esp32:esp32", vendor_id="10c4", product_id="ea60")


# ---------------------------------------------------------------------------
# init command — real board path
# ---------------------------------------------------------------------------

def test_init_real_board_single_device_decline_onboarding(isolated_config):
    from nff import config as cfg
    with patch("nff.commands.init._resolve_login"), \
         patch("nff.commands.init.daemon.start_background", return_value=True), \
         patch("nff.commands.init.boards_module.list_devices", return_value=[_esp32()]), \
         patch("nff.commands.init.toolchain.find_arduino_cli", return_value="/bin/arduino-cli"), \
         patch("nff.commands.init._onboard_platform") as monboard, \
         patch("nff.commands.init._register_mcp"):
        # decline "Connect to platform now?"
        result = CliRunner().invoke(init, input="n\n")
    assert result.exit_code == 0, result.output
    dev = cfg.get_default_device()
    assert dev["port"] == "COM10"
    assert dev["fqbn"] == "esp32:esp32:esp32"
    monboard.assert_not_called()


def test_init_real_board_multi_device_select_and_onboard(isolated_config):
    devices = [_esp32("COM3"), _esp32("COM10")]
    with patch("nff.commands.init._resolve_login"), \
         patch("nff.commands.init.daemon.start_background", return_value=True), \
         patch("nff.commands.init.boards_module.list_devices", return_value=devices), \
         patch("nff.commands.init.toolchain.find_arduino_cli", return_value="/bin/arduino-cli"), \
         patch("nff.commands.init._onboard_platform") as monboard, \
         patch("nff.commands.init._register_mcp"):
        # select board #2, accept onboarding
        result = CliRunner().invoke(init, input="2\ny\n")
    assert result.exit_code == 0, result.output
    monboard.assert_called_once()
    selected = monboard.call_args[0][0]
    assert selected.port == "COM10"  # second device


# ---------------------------------------------------------------------------
# _register_mcp
# ---------------------------------------------------------------------------

def test_register_mcp_invokes_claude_cli():
    with patch("nff.commands.init.subprocess.run") as mrun:
        init_mod._register_mcp(port=3010)
    cmd = mrun.call_args[0][0]
    assert cmd[:3] == ["claude", "mcp", "add"]
    assert "http://127.0.0.1:3010/mcp" in cmd


def test_register_mcp_swallows_errors():
    with patch("nff.commands.init.subprocess.run", side_effect=FileNotFoundError("no claude")):
        init_mod._register_mcp()  # must not raise


# ---------------------------------------------------------------------------
# _resolve_login — sign-in is optional; init never aborts on login failure
# ---------------------------------------------------------------------------

def test_resolve_login_returns_when_logged_in(isolated_config):
    from nff import config as cfg
    with patch("nff.commands.init._ensure_logged_in", return_value=True):
        init_mod._resolve_login(False)  # must not raise
    assert cfg.is_offline() is False  # a successful login does not mark offline


def test_resolve_login_falls_back_to_local_mode_on_failure(isolated_config):
    # Login fails → init must NOT abort; it continues in local mode.
    with patch("nff.commands.init._ensure_logged_in", return_value=False):
        init_mod._resolve_login(False)  # must not raise (no SystemExit)


def test_resolve_login_offline_flag_skips_login_and_persists(isolated_config):
    from nff import config as cfg
    with patch("nff.commands.init._ensure_logged_in") as mlogin:
        init_mod._resolve_login(True)
    mlogin.assert_not_called()  # offline mode never touches the browser flow
    assert cfg.is_offline() is True  # choice is persisted


def test_init_starts_background_server(isolated_config):
    with patch("nff.commands.init._resolve_login"), \
         patch("nff.commands.init._register_mcp"), \
         patch("nff.commands.init.boards_module.list_devices", return_value=[_esp32()]), \
         patch("nff.commands.init.toolchain.find_arduino_cli", return_value="/bin/arduino-cli"), \
         patch("nff.commands.init._onboard_platform"), \
         patch("nff.commands.init.daemon.start_background", return_value=True) as mstart:
        result = CliRunner().invoke(init, input="n\n")  # real board, decline onboarding
    assert result.exit_code == 0, result.output
    mstart.assert_called_once()


# ---------------------------------------------------------------------------
# _ensure_logged_in
# ---------------------------------------------------------------------------

def test_ensure_logged_in_returns_true_when_token_present(isolated_config):
    from nff import config as cfg
    cfg.set_diagnosis_tokens("acc", "ref")
    with patch("nff.tools.auth.open_browser") as mbrowser:
        assert init_mod._ensure_logged_in() is True
    mbrowser.assert_not_called()


def test_ensure_logged_in_runs_browser_flow_and_saves(isolated_config):
    from nff import config as cfg
    from nff.tools.auth import TokenResponse
    import socket
    sock = socket.socket()
    with patch("nff.tools.auth.bind_callback_server", return_value=(sock, 9999)), \
         patch("nff.tools.auth.open_browser"), \
         patch("nff.tools.auth.wait_for_callback",
               return_value=TokenResponse("tok", "rtok")):
        assert init_mod._ensure_logged_in() is True
    assert cfg.get_diagnosis_config()["access_token"] == "tok"


def test_ensure_logged_in_returns_false_on_timeout(isolated_config):
    import socket
    sock = socket.socket()
    with patch("nff.tools.auth.bind_callback_server", return_value=(sock, 9999)), \
         patch("nff.tools.auth.open_browser"), \
         patch("nff.tools.auth.wait_for_callback", side_effect=TimeoutError("nope")):
        assert init_mod._ensure_logged_in() is False


# ---------------------------------------------------------------------------
# _resolve_wifi
# ---------------------------------------------------------------------------

def test_resolve_wifi_uses_detected_and_confirmed():
    with patch("nff.commands.init.netinfo.detect_wifi", return_value=("Net", "pw")), \
         patch("nff.commands.init.click.confirm", return_value=True):
        ssid, pw = init_mod._resolve_wifi()
    assert (ssid, pw) == ("Net", "pw")


def test_resolve_wifi_prompts_when_nothing_detected():
    with patch("nff.commands.init.netinfo.detect_wifi", return_value=(None, None)), \
         patch("nff.commands.init.click.prompt", side_effect=["ManualNet", "manualpw"]):
        ssid, pw = init_mod._resolve_wifi()
    assert (ssid, pw) == ("ManualNet", "manualpw")


# ---------------------------------------------------------------------------
# _onboard_platform — the full provision → toolchain → compile → flash → claim
# ---------------------------------------------------------------------------

def _onboard_patches(toolchain_ok=True, claimed=True):
    """Common patch set for _onboard_platform; returns a contextlib.ExitStack."""
    import contextlib
    prov = {"project_id": "p", "batch_id": "b", "reused": False, "credentials_h": "// c"}
    claim_lines = [("booting", None), ("CLAIMED mode", True)] if claimed else [("booting", None)]

    stack = contextlib.ExitStack()
    stack.enter_context(patch("nff.commands.init._ensure_logged_in", return_value=True))
    stack.enter_context(patch("nff.commands.init.provisioning_client.provision_batch",
                              return_value=prov))
    stack.enter_context(patch("nff.commands.init._resolve_wifi", return_value=("Net", "pw")))
    stack.enter_context(patch("nff.commands.init.bootstrap.prepare_bootstrap_sketch",
                              return_value="/tmp/sketch"))
    stack.enter_context(patch("nff.commands.init.installer.ensure_onboarding_toolchain",
                              return_value=(toolchain_ok, "msg")))
    stack.enter_context(patch("nff.commands.init.toolchain.stream_compile",
                              return_value=_FakeStream(["compiling"], 0)))
    stack.enter_context(patch("nff.commands.init.toolchain.stream_upload",
                              return_value=_FakeStream(["uploading"], 0)))
    stack.enter_context(patch("nff.commands.init.bootstrap.watch_for_claim",
                              return_value=iter(claim_lines)))
    return stack


def test_onboard_platform_happy_path(isolated_config, capsys):
    device = types.SimpleNamespace(port="COM10", board="ESP32 (CP210x)", fqbn="esp32:esp32:esp32")
    with _onboard_patches(toolchain_ok=True, claimed=True):
        init_mod._onboard_platform(device)
    assert "Success" in capsys.readouterr().out


def test_onboard_platform_aborts_when_toolchain_fails(isolated_config, capsys):
    device = types.SimpleNamespace(port="COM10", board="ESP32 (CP210x)", fqbn="esp32:esp32:esp32")
    with _onboard_patches(toolchain_ok=False) as stack:
        compile_mock = stack.enter_context(
            patch("nff.commands.init.toolchain.stream_compile"))
        init_mod._onboard_platform(device)
    assert "Toolchain setup failed" in capsys.readouterr().out
    compile_mock.assert_not_called()  # never reached the compile step
