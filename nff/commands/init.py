"""nff init — interactive setup wizard (board config + optional platform onboarding)."""

import subprocess
import types

import click

from nff import config
from nff.tools import (
    auth as auth_tools,
    boards as boards_module,
    bootstrap,
    daemon,
    installer,
    netinfo,
    provisioning_client,
    toolchain,
)

# The bootstrap firmware runs Serial at 115200; keep the saved baud in sync so
# `nff monitor` (and onboarding's own serial watch) match the device.
_BOOTSTRAP_BAUD = 115200


def _register_mcp(host: str = "127.0.0.1", port: int = 3010) -> None:
    try:
        url = f"http://{host}:{port}/mcp"
        subprocess.run(
            ["claude", "mcp", "add", "--scope", "user", "--transport", "http", "nff", url],
            check=False,
        )
    except Exception:
        pass


def _ensure_logged_in() -> bool:
    """Make sure we hold a platform token; trigger browser login if not. Mirrors
    `nff auth login` (no-args browser flow)."""
    cfg = config.get_diagnosis_config()
    if cfg.get("access_token"):
        return True
    click.echo("\nYou're not signed in to the nff platform. Opening your browser…")
    try:
        sock, port = auth_tools.bind_callback_server()
    except Exception as exc:
        click.echo(f"  Could not start login: {exc}")
        return False
    callback_url = f"http://127.0.0.1:{port}/callback"
    # Use the frontend's /login page (same route the MCP `authenticate` flow uses) —
    # the SPA has no /auth/portal route.
    frontend_url = cfg.get("frontend_url", "https://nanoforgeflow.com")
    login_url = f"{frontend_url}/login?cb={auth_tools.percent_encode(callback_url)}"
    try:
        auth_tools.open_browser(login_url)
    except Exception:
        pass
    click.echo(f"  If your browser didn't open, visit: {login_url}")
    try:
        tokens = auth_tools.wait_for_callback(sock, 300)
    except TimeoutError:
        click.echo("  Login timed out.")
        return False
    config.set_diagnosis_tokens(tokens.access_token, tokens.refresh_token)
    click.echo("  ✓ Signed in")
    return True


def _resolve_login(cloud_flag: bool, offline_flag: bool) -> bool:
    """Decide how to handle sign-in without ever blocking init. LOCAL MODE IS THE
    DEFAULT: a plain `nff init` never opens a browser or asks for an account —
    compile/flash/monitor/debug work immediately. `--cloud` opts into the browser
    sign-in; `--offline` (or a truthy NFF_OFFLINE) additionally persists hard offline
    mode so cloud prompts stay silenced everywhere. An existing token from a previous
    `nff auth login` keeps cloud features on without any prompt.

    Returns whether cloud features are active for this run."""
    if offline_flag or config.is_offline():
        config.set_offline(True)
        click.echo("  Offline mode — local build/flash/monitor/debug work without an account.")
        click.echo("  Cloud features (repair, agent, device onboarding) stay disabled "
                   "until you run `nff auth login`.")
        return False
    if config.get_diagnosis_config().get("access_token"):
        click.echo("  ✓ Signed in to the nff platform — cloud features enabled.")
        return True
    if cloud_flag:
        if _ensure_logged_in():
            return True
        click.echo("  Continuing in local mode — run `nff auth login` later to enable "
                   "cloud features (repair, agent).")
        return False
    click.echo("  Local mode (default) — no account needed for build/flash/monitor/debug.")
    click.echo("  Cloud features (repair, agent, OTA, device onboarding) are opt-in: "
               "run `nff init --cloud` or `nff auth login`.")
    return False


def _resolve_wifi() -> tuple[str, str]:
    """Detect host WiFi (SSID + password), confirming/prompting as needed."""
    ssid, password = netinfo.detect_wifi()
    if ssid:
        click.echo(f"  Detected WiFi network: {ssid}")
        if not click.confirm("  Use this network for the device?", default=True):
            ssid, password = None, None
    if not ssid:
        ssid = click.prompt("  WiFi SSID")
        password = None
    if password is None:
        password = click.prompt(
            "  WiFi password", hide_input=True, default="", show_default=False
        )
    return ssid, password


def _onboard_platform(device) -> None:
    """Log in, provision the device's project, bake in WiFi + cloud broker, flash, and
    wait for it to claim — so it shows up in the dashboard automatically."""
    if not _ensure_logged_in():
        click.echo("Skipping platform onboarding.")
        return

    click.echo("\nProvisioning your device on the nff platform…")
    try:
        data = provisioning_client.provision_batch()
    except provisioning_client.ProvisioningError as exc:
        click.echo(f"  Provisioning failed: {exc}")
        return
    config.set_platform_enrollment(data.get("project_id"), data.get("batch_id"))
    click.echo("  ✓ Reusing your existing enrollment batch" if data.get("reused")
               else "  ✓ Enrollment batch ready")

    ssid, password = _resolve_wifi()
    broker_host = config.get_platform_config().get("broker_host")

    try:
        sketch_dir = bootstrap.prepare_bootstrap_sketch(
            data["credentials_h"], ssid, password, broker_host
        )
    except bootstrap.BootstrapError as exc:
        click.echo(f"  Could not prepare firmware: {exc}")
        return

    fqbn = device.fqbn
    click.echo("\nSetting up the ESP32 toolchain (core, PubSubClient, nff library)…")
    ok, msg = installer.ensure_onboarding_toolchain(emit=lambda l: click.echo(f"  {l}"))
    if not ok:
        click.echo(f"  Toolchain setup failed: {msg}")
        click.echo("  Fix the above and re-run `nff init`, or install manually:")
        click.echo(
            "    arduino-cli core install esp32:esp32 --additional-urls "
            "https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json"
        )
        return

    click.echo("\nCompiling onboarding firmware…")
    compile_stream = toolchain.stream_compile(sketch_dir, fqbn)
    for line in compile_stream:
        click.echo(f"  {line}")
    if compile_stream.returncode != 0:
        click.echo(
            "  Compile failed. Onboarding firmware needs the ESP32 core and the nff "
            "Arduino library installed in arduino-cli."
        )
        return

    click.echo("\nFlashing your board…")
    upload_stream = toolchain.stream_upload(sketch_dir, fqbn, device.port)
    for line in upload_stream:
        click.echo(f"  {line}")
    if upload_stream.returncode != 0:
        click.echo("  Flashing failed — check the cable and that the port isn't in use.")
        return

    # Saved firmware runs at 115200; keep config in sync for `nff monitor`.
    config.set_default_device(device.port, device.board, fqbn, _BOOTSTRAP_BAUD)

    click.echo("\nWaiting for your device to connect to the platform…")
    claimed = False
    try:
        for line, result in bootstrap.watch_for_claim(device.port, _BOOTSTRAP_BAUD, timeout_s=150):
            click.echo(f"  {line}")
            if result:
                claimed = True
                break
    except bootstrap.BootstrapError as exc:
        click.echo(f"  (serial read ended: {exc})")

    frontend = config.get_diagnosis_config().get("frontend_url") or "https://nanoforgeflow.com"
    if claimed:
        click.echo("\n✓ Success! Your device is connected and claimed.")
        click.echo(f"  See it in your dashboard: {frontend}")
    else:
        click.echo("\nYour board was flashed and is announcing itself.")
        click.echo(f"  It should appear in your dashboard shortly: {frontend}")
        click.echo("  If it stays offline, re-check the WiFi password and the board's internet access.")


@click.command()
@click.option("--port", default=None)
@click.option("--baud", default=9600, type=int)
@click.option("--force", is_flag=True)
@click.option("--backend", type=click.Choice(["arduino", "platformio"]), default=None,
              help="Build backend to use (default: keep current / arduino)")
@click.option("--cloud", is_flag=True,
              help="Sign in to the nff platform (browser) and offer device onboarding. "
                   "Without it, init is local-only and never prompts for an account.")
@click.option("--offline", is_flag=True,
              help="Persist hard offline mode (local is already the default; this also "
                   "silences cloud hints in doctor/status until `nff auth login`).")
def init(port, baud, force, backend, cloud, offline):
    """Interactive setup — detect board and configure nff."""
    if backend:
        config.set_build_backend(backend)
    active_backend = backend or toolchain.active_backend()
    is_pio = active_backend == "platformio"

    click.echo("Welcome to nff init!\n")

    # No account required: local build/flash/monitor/debug and the MCP tools work without
    # one, so a plain init never opens a browser. `--cloud` (or a prior `nff auth login`)
    # enables the cloud features — and even then, never block init on login.
    cloud_enabled = _resolve_login(cloud, offline)
    offline_mode = offline or config.is_offline()

    if is_pio:
        click.echo("Build backend: PlatformIO (board-universal)\n")

    click.pause("\nPlug your board into a USB port, then press any key…")
    devices = boards_module.list_devices()
    if devices:
        click.echo("\nDetected boards:")
        for i, d in enumerate(devices, 1):
            click.echo(f"  {i}) {d.board} on {d.port}")
        if len(devices) == 1:
            selected = devices[0]
        else:
            idx = click.prompt("Select board", type=int, default=1) - 1
            selected = devices[max(0, min(idx, len(devices) - 1))]
        resolved_port = port or selected.port
        board_name, fqbn = selected.board, selected.fqbn
        if is_pio:
            pio_board = selected.pio_board or click.prompt(
                "PlatformIO board id", default="esp32dev")
            config.set_build_board(pio_board)
        config.set_default_device(resolved_port, board_name, fqbn, baud)
    else:
        if not port:
            port = click.prompt("No boards detected. Enter port manually")
        resolved_port = port
        board_name = click.prompt("Board name")
        if is_pio:
            config.set_build_board(click.prompt("PlatformIO board id (e.g. esp32dev)"))
            fqbn = ""
        else:
            fqbn = click.prompt("Board FQBN (e.g. esp32:esp32:esp32)")
        config.set_default_device(resolved_port, board_name, fqbn, baud)

    if is_pio:
        from nff.tools.backends import platformio as pio
        if not pio.find_platformio():
            click.echo("\nPlatformIO not found — installing…")
            ok, msg = pio.ensure_toolchain(emit=lambda l: click.echo(f"  {l}"))
            if not ok:
                click.echo(f"Warning: could not install PlatformIO: {msg}")
    elif not toolchain.find_arduino_cli():
        click.echo("\narduino-cli not found — installing…")
        try:
            installer.install()
        except Exception as exc:
            click.echo(f"Warning: could not install arduino-cli: {exc}")

    if offline_mode:
        click.echo("\nOffline mode — skipping cloud platform onboarding. "
                   "Your board is configured for local build/flash/monitor.")
    elif is_pio:
        click.echo("\nCloud platform onboarding currently runs on the arduino "
                   "backend; skipping. Your board is configured for PlatformIO builds.")
    elif cloud_enabled:
        device = types.SimpleNamespace(port=resolved_port, board=board_name, fqbn=fqbn)
        if fqbn.startswith("esp32") and click.confirm(
            "\nConnect this device to the nff platform now?", default=True
        ):
            _onboard_platform(device)
    else:
        click.echo("\nTip: connect this board to the nff platform later with "
                   "`nff init --cloud` or `nff auth login` (OTA, fleet, repair).")

    _register_mcp()

    click.echo("\nStarting the nff MCP server in the background…")
    if daemon.start_background():
        click.echo("  ✓ Server running on http://127.0.0.1:3010/mcp")
    else:
        click.echo("  Couldn't start the server automatically — start it with `nff mcp`.")
        click.echo(f"  (logs: {daemon.log_path()})")

    click.echo("\n✓ nff configured! Restart Claude Code to pick up the nff MCP server.")
    click.echo("  Verify anytime with `nff doctor`.")
