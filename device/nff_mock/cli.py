from __future__ import annotations

import asyncio
import json
import logging
import secrets
import sys
import time

import click

from nff_mock.config import NffMockSettings


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@click.group()
def cli() -> None:
    """nff-mock — a pure-software virtual ESP32."""


@cli.command()
def run() -> None:
    """Boot a virtual device and connect to the nff-fleet broker (reads NFF_MOCK_* env)."""
    from nff_mock.device import VirtualDevice

    cfg = NffMockSettings()
    _setup_logging(cfg.log_level)
    log = logging.getLogger("nff_mock")

    if not cfg.device_id:
        log.error("NFF_MOCK_DEVICE_ID is required")
        sys.exit(2)
    if not cfg.project_id:
        # The broker derives tenant scope from the cert's project and only allows
        # nff/{project_id}/devices/{id}/# — without it nothing can be published.
        log.error("NFF_MOCK_PROJECT_ID is required (must match the cert's project)")
        sys.exit(2)
    for path in (cfg.device_cert, cfg.device_key, cfg.ca_cert, cfg.cmd_verify_key):
        if not path.exists():
            log.error("missing credential file: %s "
                      "(run `nff-fleet provision new-device --id %s`)", path, cfg.device_id)
            sys.exit(2)

    device = VirtualDevice(cfg)
    log.info("starting virtual ESP32 '%s' (fw %s)", cfg.device_id, cfg.fw_version)
    try:
        asyncio.run(device.run())
    except KeyboardInterrupt:
        log.info("stopped")


@cli.command()
def selftest() -> None:
    """Exercise the command pipeline offline (no broker) and report pass/fail.

    Generates an ephemeral signing key, drives signed ping/diag/ota/crash commands
    plus a forged one through the real verify+dispatch path, and checks the
    responses — the same logic that runs against the live fleet, minus the network.
    """
    _setup_logging("WARNING")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    from nff_mock import heartbeat
    from nff_mock.device import VirtualDevice
    from nff_mock.mqtt import topic_cmd
    from nff_mock.security import _build_tbs

    import tempfile
    from pathlib import Path

    key = ec.generate_private_key(ec.SECP256R1())
    tmp = Path(tempfile.mkdtemp()) / "cmd_sign.pem"
    tmp.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )

    cfg = NffMockSettings(device_id="selftest-01", project_id="selftest", cmd_verify_key=tmp, verify_commands=True)
    device = VirtualDevice(cfg)

    captured: list[tuple[str, dict]] = []

    async def fake_publish(topic, payload, *, qos=1, retain=False):
        captured.append((topic, payload))

    device.publish = fake_publish  # type: ignore[method-assign]

    def sign(payload: dict) -> bytes:
        sig = key.sign(_build_tbs(payload).encode(), ec.ECDSA(hashes.SHA256()))
        return json.dumps({**payload, "cmd_sig": sig.hex()}).encode()

    def cmd(action: str, **extra) -> dict:
        return {"action": action, "nonce": secrets.token_hex(4), "timestamp": int(time.time()), **extra}

    results: list[tuple[str, bool]] = []

    async def drive() -> None:
        t = topic_cmd(cfg.project_id, cfg.device_id)

        # ping -> pong
        captured.clear()
        await device.dispatcher.handle(t, sign(cmd("ping")))
        results.append(("ping->pong", any(p.get("type") == "pong" for _, p in captured)))

        # diag -> diag with heap
        captured.clear()
        await device.dispatcher.handle(t, sign(cmd("diag")))
        results.append(("diag", any(p.get("type") == "diag" and "free_heap" in p for _, p in captured)))

        # forged signature -> dropped (no response)
        captured.clear()
        forged = cmd("ping")
        bad = json.dumps({**forged, "cmd_sig": "00" * 70}).encode()
        await device.dispatcher.handle(t, bad)
        results.append(("forged->dropped", len(captured) == 0))

        # unknown action -> error
        captured.clear()
        await device.dispatcher.handle(t, sign(cmd("frobnicate")))
        results.append(("unknown->error", any(p.get("type") == "error" for _, p in captured)))

        # OTA upgrade -> ota_started, then committed after sim reboot, fw bumped
        captured.clear()
        await device.dispatcher.handle(t, sign(cmd("ota", version="1.2.0", url="http://127.0.0.1:1/x.bin", sha256="0" * 64)))
        started = any(p.get("type") == "ota_started" for _, p in captured)
        device._apply_pending_ota()
        await device.ota.check_pending_result()
        committed = any(p.get("type") == "ota_result" and p.get("status") == "committed" for _, p in captured)
        results.append(("ota started+committed", started and committed))
        results.append(("ota fw bumped", device.fw_version == "1.2.0"))

        # OTA that DELIVERS a sketch artifact -> device runs new behaviour after reboot.
        # Stub the download so no network is needed (returns real sketch bytes).
        sketch = {
            "version": "1.3.0",
            "heartbeat_extra": {"led": "off"},
            "telemetry_extra": {"sensor_temp": {"random_walk": [18, 26]}},
            "commands": {"set_led": {"sets_state": "led", "response": {"type": "led_ack"}}},
        }
        sketch_bytes = json.dumps(sketch).encode()

        async def deliver_sketch(url, expected_sha):  # matches _download(url, expected_sha)
            return sketch_bytes, True, None

        device.ota._download = deliver_sketch  # type: ignore[method-assign]
        captured.clear()
        await device.dispatcher.handle(t, sign(cmd("ota", version="1.3.0", url="http://artifact/x.bin", sha256="0" * 64)))
        device._apply_pending_ota()
        device.firmware.on_boot(device.fw_ctx)  # boot order: apply OTA, then init firmware
        await device.ota.check_pending_result()
        hb = heartbeat.build_online(device)
        results.append(("ota applies sketch: heartbeat field", hb.get("led") == "off"))
        results.append(("ota applies sketch: fw bumped", device.fw_version == "1.3.0"))
        captured.clear()
        await device.dispatcher.handle(t, sign(cmd("diag")))
        results.append(("ota applies sketch: diag telemetry", any("sensor_temp" in p for _, p in captured)))
        captured.clear()
        await device.dispatcher.handle(t, sign(cmd("set_led", led="on")))
        results.append(("ota applies sketch: new command", any(p.get("type") == "led_ack" for _, p in captured)))

        # OTA whose artifact is unreachable -> version bumps but behaviour is preserved
        # (the just-applied sketch must NOT revert to stock firmware).
        async def fail_download(url, expected_sha):
            return None, True, None

        device.ota._download = fail_download  # type: ignore[method-assign]
        captured.clear()
        await device.dispatcher.handle(t, sign(cmd("ota", version="1.4.0", url="http://x/y", sha256="0" * 64)))
        device._apply_pending_ota()
        await device.ota.check_pending_result()
        hb2 = heartbeat.build_online(device)
        results.append(("ota missing artifact: version bumped", device.fw_version == "1.4.0"))
        results.append(("ota missing artifact: behaviour preserved", hb2.get("led") == "off"))

        # downgrade -> rejected
        captured.clear()
        await device.dispatcher.handle(t, sign(cmd("ota", version="1.0.0", url="http://x/y", sha256="0" * 64)))
        results.append(("downgrade->rejected", any(p.get("msg", "").startswith("ota: downgrade") for _, p in captured)))

    async def drive_crash() -> None:
        # Crash scenarios + the "self-healing firmware" version bridge, on a fresh device
        # configured with a buggy version map. (3.0.18 is bad; a higher version cures it.)
        cfg2 = NffMockSettings(
            device_id="selftest-02", project_id="selftest", cmd_verify_key=tmp,
            verify_commands=True, fw_version="3.0.17", build_id="abc123def456abcd",
            buggy_versions="3.0.18=null_deref",
        )
        dev = VirtualDevice(cfg2)
        cap: list[tuple[str, dict]] = []

        async def pub(topic, payload, *, qos=1, retain=False):
            cap.append((topic, payload))

        dev.publish = pub  # type: ignore[method-assign]

        async def sim_download(url, expected_sha):  # no network; version-only adopt
            return None, True, None

        dev.ota._download = sim_download  # type: ignore[method-assign]
        t2 = topic_cmd(cfg2.project_id, cfg2.device_id)

        # `crash` command with a scenario -> scheduled, then an ingest-aligned report.
        cap.clear()
        await dev.dispatcher.handle(t2, sign(cmd("crash", scenario="null_deref")))
        results.append(("crash scheduled", any(p.get("type") == "crash_scheduled" for _, p in cap)))
        await dev.crash.check_and_report()
        rep = next((p for _, p in cap if p.get("type") == "crash"), None)
        aligned = bool(rep) and rep.get("build_id") == cfg2.build_id and rep.get("exception_cause") == 29 \
            and bool(rep.get("backtrace")) and bool(rep.get("rtc_log")) \
            and "Guru Meditation" in (rep.get("raw") or "")
        results.append(("crash report ingest-aligned", aligned))
        results.append(("ota scenario arms fault", dev.crash.fault_scenario == "null_deref"))

        # Adopting the buggy version -> simulated rollback + fault (re)armed.
        cap.clear()
        await dev.dispatcher.handle(t2, sign(cmd("ota", version="3.0.18", url="http://x/y", sha256="0" * 64)))
        dev._apply_pending_ota()
        await dev.ota.check_pending_result()
        results.append(("buggy version -> rolled_back",
                        any(p.get("type") == "ota_result" and p.get("status") == "rolled_back" for _, p in cap)))
        results.append(("buggy version adopted + fault armed",
                        dev.fw_version == "3.0.18" and dev.crash.fault_scenario == "null_deref"))

        # Adopting a higher version (the merged fix) -> committed + fault cleared.
        cap.clear()
        await dev.dispatcher.handle(t2, sign(cmd("ota", version="3.0.19", url="http://x/y", sha256="0" * 64)))
        dev._apply_pending_ota()
        await dev.ota.check_pending_result()
        results.append(("fix version -> committed",
                        any(p.get("type") == "ota_result" and p.get("status") == "committed" for _, p in cap)))
        results.append(("fix version -> fault cleared",
                        dev.fw_version == "3.0.19" and dev.crash.fault_scenario is None))

        # An OTA carrying a build_id -> the device adopts it (so nff-github post-deploy
        # verification, which checks heartbeat build_id, passes for a mock).
        cap.clear()
        await dev.dispatcher.handle(t2, sign(cmd(
            "ota", version="3.0.20", url="http://x/y", sha256="0" * 64, build_id="deadbeefbuild002")))
        dev._apply_pending_ota()
        await dev.ota.check_pending_result()
        hb = heartbeat.build_online(dev)
        results.append(("ota build_id adopted",
                        dev.build_id == "deadbeefbuild002" and hb.get("build") == "deadbeefbuild002"))

    asyncio.run(drive())
    asyncio.run(drive_crash())

    ok = True
    for name, passed in results:
        click.echo(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed
    if not ok:
        sys.exit(1)
    click.echo("selftest: all checks passed")
