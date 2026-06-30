from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any

import aiomqtt

from nff_mock_core import heartbeat, ota_state
from nff_mock_core.mqtt import build_ssl_context, topic_cmd, topic_heartbeat
from nff_mock_core.nvs import Nvs
from nff_mock_core.security import CommandVerifier

from nff_mock.config import NffMockSettings
from nff_mock.crash import CrashInjector
from nff_mock.dispatcher import Dispatcher
from nff_mock.firmware import FirmwareContext, load_firmware
from nff_mock.ota import OtaSimulator
from nff_mock.telemetry import Telemetry

logger = logging.getLogger(__name__)


def _version_gt(a: str, b: str) -> bool:
    """Strict 3-part semver `a > b`; False if either is unparseable (never falsely cure)."""
    try:
        pa = tuple(int(x) for x in a.split(".")[:3])
        pb = tuple(int(x) for x in b.split(".")[:3])
    except (ValueError, AttributeError):
        return False
    return pa > pb


class VirtualDevice:
    """A pure-software ESP32. One instance == one device identity.

    The lifecycle mirrors nff_core.c: (re)boot -> connect mTLS -> announce online
    heartbeat -> report any pending crash / ota_result -> serve commands. A
    "reboot" (from reboot/ota/crash) is simulated by cycling the MQTT session while
    the process — and its NVS — stays alive, exactly how a real reset carries NVS
    flags from one boot to the next.
    """

    def __init__(self, cfg: NffMockSettings) -> None:
        self.cfg = cfg
        self.fw_version = cfg.fw_version
        # Mutable like fw_version: an OTA can carry the build_id baked into the new image,
        # and the mock adopts it so its heartbeat/crash build_id matches what was deployed.
        self.build_id = cfg.build_id
        self.nvs = Nvs()
        self.telemetry = Telemetry(flash=cfg.flash)
        self.firmware = load_firmware(None, fallback_version=cfg.fw_version)
        self.fw_ctx = FirmwareContext(device_id=cfg.device_id, fw_version=self.fw_version)
        self.pending_fw_artifact: bytes | None = None

        self.verifier = CommandVerifier.from_key_file(
            cfg.cmd_verify_key, enforce=cfg.verify_commands
        )
        self.dispatcher = Dispatcher(self)
        self.ota = OtaSimulator(self)
        self.crash = CrashInjector(self)

        self._client: aiomqtt.Client | None = None
        self._publish_lock = asyncio.Lock()
        self._reboot = asyncio.Event()
        self._stop = False

        # Load an optional firmware sketch from disk at first boot.
        if cfg.firmware_spec:
            try:
                self.firmware = load_firmware(
                    cfg.firmware_spec.read_bytes(), fallback_version=cfg.fw_version
                )
            except OSError as exc:
                logger.warning("could not read firmware_spec: %s", exc)

    # ------------------------------------------------------------------ #
    # Publishing                                                          #
    # ------------------------------------------------------------------ #

    async def publish(
        self, topic: str, payload: dict[str, Any], *, qos: int = 1, retain: bool = False
    ) -> None:
        if self._client is None:
            return
        async with self._publish_lock:
            await self._client.publish(
                topic, payload=json.dumps(payload), qos=qos, retain=retain
            )

    def request_reboot(self) -> None:
        self._reboot.set()

    def stop(self) -> None:
        self._stop = True
        self._reboot.set()

    # ------------------------------------------------------------------ #
    # Boot helpers                                                        #
    # ------------------------------------------------------------------ #

    def _apply_pending_ota(self) -> None:
        """The bootloader-partition-switch analogue: before announcing online, decide
        whether the OTA-staged image is healthy and either commit it or roll back.

        This mirrors the SDK's automatic rollback (nff_ota.c trial/confirm/revert): a
        freshly-flashed image is on probation. The mock can't run a real .bin, so it
        keys health off the adopted *version* via the buggy_versions bridge — a known-bad
        release fails the health gate and the device reverts to the previous image and
        reports rolled_back, staying online (no crash loop). A healthy release commits:
        the new version takes effect, and a staged artifact (if any) becomes the new
        behaviour; a simulated download (no artifact) keeps the running behaviour."""
        if self.nvs.get_str(ota_state.NVS_OTA_PENDING) != "1":
            return
        new_version = self.nvs.get_str(ota_state.NVS_OTA_VERSION) or self.fw_version

        # Health gate (SDK: nff_ota_trial_tick). Known-bad version => trial fails.
        if new_version in self.cfg.buggy_version_map:
            prev = self.nvs.get_str(ota_state.NVS_OTA_PREV_VERSION) or self.fw_version
            prev_build = self.nvs.get_str(ota_state.NVS_OTA_PREV_BUILD_ID)
            scenario = self.cfg.buggy_version_map[new_version]
            reason = f"health gate failed on v{new_version} ({scenario})"
            logger.warning(
                "trial of buggy version %s failed health gate -> rolled back to %s",
                new_version, prev,
            )
            # Revert to the last-good image: stay on the previous version/behaviour.
            self.fw_version = prev
            self.fw_ctx.fw_version = prev
            self.firmware.version = prev
            if prev_build:
                self.build_id = prev_build
            self.pending_fw_artifact = None          # discard the staged bad artifact
            self.nvs.erase(ota_state.NVS_OTA_BUILD_ID)
            self.nvs.set_str(ota_state.NVS_OTA_COMMITTED, "0")   # check_pending_result -> rolled_back
            # Carry *why* we rolled back so the result message can report it (the SDK
            # logs an equivalent reason; here it surfaces all the way to the dashboard).
            self.nvs.set_str(ota_state.NVS_OTA_ROLLBACK_REASON, reason)
            return

        # Healthy => commit: adopt the new version (and artifact / build_id) for real.
        self.fw_version = new_version
        self.fw_ctx.fw_version = self.fw_version
        # Adopt the deployed build_id when the OTA carried one (nff-ota sends it as an
        # unsigned extra). Mirrors real firmware reporting its compiled-in build id.
        new_build = self.nvs.get_str(ota_state.NVS_OTA_BUILD_ID)
        if new_build:
            self.build_id = new_build
            self.nvs.erase(ota_state.NVS_OTA_BUILD_ID)
        if self.pending_fw_artifact is not None:
            self.firmware = load_firmware(
                self.pending_fw_artifact, fallback_version=self.fw_version
            )
            self.pending_fw_artifact = None
        else:
            # Version bump only; firmware behaviour carries over unchanged.
            self.firmware.version = self.fw_version

        # A higher version than a previously-injected (crash-injection) fault cures it —
        # the agent's merged fix shipping as a good OTA. Mirrors clear-on-recovery.
        if self.crash.fault_scenario and _version_gt(
            self.fw_version, self.nvs.get_str("fault_fixed_after") or "0.0.0"
        ):
            self.crash.clear_fault()
            logger.info("adopted fixed version %s -> fault cleared", self.fw_version)

    async def _heartbeat_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.cfg.heartbeat_interval_s)
                self.telemetry.sample()
                await self.publish(
                    topic_heartbeat(self.cfg.project_id, self.cfg.device_id),
                    heartbeat.build_online(self),
                    retain=True,
                )
        except asyncio.CancelledError:
            pass

    async def _random_crash_loop(self) -> None:
        """Optional chaos: Poisson-ish random crashes at `random_crash_rate` per hour."""
        rate = self.cfg.random_crash_rate
        try:
            while rate > 0:
                # Expected wait for the next event, in seconds.
                wait = random.expovariate(rate / 3600.0)
                await asyncio.sleep(wait)
                self.crash.inject(random.choice(["transient_load", "null_deref"]))
                return  # the reboot will restart this loop fresh
        except asyncio.CancelledError:
            pass

    async def _fault_recrash_loop(self) -> None:
        """Crash-loop driver: while a persistent fault is armed, re-crash after a short
        window so the device flaps until the fault is cleared (an OTA to a higher
        version). Re-armed every boot from _session, so one re-crash per loop is enough."""
        scenario = self.crash.fault_scenario
        if not scenario:
            return
        try:
            await asyncio.sleep(self.cfg.fault_recrash_delay_s)
            logger.info("fault active (%s) — re-crashing", scenario)
            self.crash.inject(scenario)
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------ #
    # Session                                                             #
    # ------------------------------------------------------------------ #

    async def _session(self) -> bool:
        """One connect -> serve -> disconnect cycle. Returns True if it ended due to
        a reboot request (reconnect immediately) rather than a connection error."""
        self._reboot.clear()
        self._apply_pending_ota()
        self.firmware.on_boot(self.fw_ctx)

        ctx = build_ssl_context(self.cfg)
        will = aiomqtt.Will(
            topic=topic_heartbeat(self.cfg.project_id, self.cfg.device_id),
            payload=json.dumps(heartbeat.build_offline(self)),
            qos=1,
            retain=True,
        )
        keepalive = max(15, self.cfg.heartbeat_interval_s + 5)

        async with aiomqtt.Client(
            hostname=self.cfg.broker_host,
            port=self.cfg.broker_port,
            identifier=self.cfg.device_id,  # MQTT client_id MUST equal device_id (ACL)
            tls_context=ctx,
            will=will,
            keepalive=keepalive,
        ) as client:
            self._client = client
            logger.info("connected as %s -> %s:%d", self.cfg.device_id,
                        self.cfg.broker_host, self.cfg.broker_port)

            # Boot announce, then drain any pending crash / OTA result.
            self.telemetry.sample()
            await self.publish(
                topic_heartbeat(self.cfg.project_id, self.cfg.device_id), heartbeat.build_online(self), retain=True
            )
            await self.crash.check_and_report()
            await self.ota.check_pending_result()

            await client.subscribe(topic_cmd(self.cfg.project_id, self.cfg.device_id), qos=1)
            tasks = [asyncio.create_task(self._heartbeat_loop())]
            if self.cfg.random_crash_rate > 0:
                tasks.append(asyncio.create_task(self._random_crash_loop()))
            if self.crash.fault_scenario:
                tasks.append(asyncio.create_task(self._fault_recrash_loop()))

            try:
                async for message in client.messages:
                    await self.dispatcher.handle(message.topic.value, message.payload)
                    if self._reboot.is_set():
                        break
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

            if self._reboot.is_set() and not self._stop:
                # Simulate the device blinking offline across a reboot.
                await self.publish(
                    topic_heartbeat(self.cfg.project_id, self.cfg.device_id),
                    heartbeat.build_offline(self),
                    retain=True,
                )
                self.telemetry.reset_uptime()
                self._client = None
                return True

        self._client = None
        return False

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop:
            try:
                immediate = await self._session()
            except aiomqtt.MqttError as exc:
                logger.warning("connection lost: %s", exc)
                immediate = False
            except (FileNotFoundError, OSError) as exc:
                logger.error("cannot start (cert/keys?): %s", exc)
                raise
            if self._stop:
                break
            if immediate:
                logger.info("rebooting %s", self.cfg.device_id)
                backoff = 1.0
                continue
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
