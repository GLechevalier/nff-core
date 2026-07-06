"""Controller configuration (env prefix ``NFF_MOCKCTL_``).

The controller is the only public-facing-adjacent piece of the mock stack, so it holds
*no* secrets it doesn't strictly need: cert minting is delegated to fleet (the CA signing
key never leaves fleet), and Docker is reached only through a scoped socket-proxy.
"""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class NffMockControllerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NFF_MOCKCTL_", env_file=(".env", ".env.dev"), extra="ignore"
    )

    # ── Docker driver (mirrors nff-github/sandbox.py) ─────────────────────────
    # docker_host = "" → local daemon (dev fallback); prod points at the controller's
    # OWN scoped socket-proxy, e.g. tcp://mock-docker-socket-proxy:2375.
    docker_path: str = "docker"
    docker_host: str = ""
    docker_timeout: float = 60.0

    # ── What we spawn ─────────────────────────────────────────────────────────
    mock_image: str = "nff-mock:dev"
    # The docker network the spawned mock joins to reach the broker. Must be the
    # *resolved* network name (give the compose network an explicit `name:`), since
    # this literal is passed straight to `docker run --network`.
    mock_network: str = "nff"
    # Named volume holding provisioned certs + the cmd-sign key, mounted :ro into each
    # mock ("shared" cert mode — dev/internal). Public hardening uses per-device staging.
    fleet_data_volume: str = "nff-dev_fleet-data-dev"
    broker_host: str = "nff-fleet"
    broker_port: int = 8883
    # Where the per-project command-signing keys live *as seen inside a spawned mock*
    # (the fleet data volume mounts at /data). The mock verifies commands with its
    # project's key at {mock_project_keys_dir}/{project_id}/cmd_sign.pem.
    mock_project_keys_dir: str = "/data/keys/projects"

    # Per-container hard limits (mirror sandbox.py).
    mock_mem: str = "128m"
    mock_cpus: str = "0.25"
    mock_pids: int = 128

    # Demo bridge: when set, stamped into every spawned mock as NFF_MOCK_BUGGY_VERSIONS
    # (a "version=scenario" map, e.g. "3.0.18=null_deref"). Lets the self-healing-firmware
    # demo make mocks crash-loop on a buggy release and recover on the fix. Empty = off.
    mock_buggy_versions: str = ""

    # ── Complexity levels ─────────────────────────────────────────────────────
    # A mock's fidelity tier (see nff-mock/core/README.md):
    #   0 = none          → spawn nothing
    #   1 = lightweight   → the Python protocol sim (mock_image); cheap, spawns often
    #   2 = virtual ESP32 → the real firmware booted in QEMU (l2_image); heavy, spawns rarely
    #   3 = real hardware → not implemented; POST /mocks {level:3} returns 501
    # default_level is what a request without an explicit `level` gets (1 = unchanged
    # behaviour). Set max_level=1 in any environment that hasn't built the L2 image.
    default_level: int = 1
    max_level: int = 2

    # Level 2 (real QEMU ESP32) — heavy, so its own image + larger caps + longer TTL,
    # and a small fleet cap. read_only is off because QEMU needs a writable flash image
    # (mounted on a dedicated tmpfs, l2_tmpfs_size).
    l2_image: str = "nff-mock-l2:dev"
    l2_mem: str = "1g"
    l2_cpus: str = "1.0"
    l2_pids: int = 512
    l2_ttl_s: int = 7200
    l2_read_only: bool = False
    l2_tmpfs_size: str = "256m"
    max_l2_per_project: int = 2
    max_l2_total: int = 4
    # device_type stamped into an L2 mock so its `devices` row is distinguishable from an
    # L1 mock — nff-ota's source build branches on it (real compile vs the L1 regex path).
    l2_device_type: str = "nff-qemu-esp32"
    # The baseline/stock L2 firmware build a fresh emulated device boots. nff-ota's realcompile
    # path stages each build's merged.bin on the fleet volume at /data/firmware/{build_id}/
    # firmware.bin (store_bin); the controller stamps that path into NFF_MOCK_FIRMWARE_IMAGE so
    # the supervisor reads it off the shared /data:ro mount. Empty → the supervisor falls back to
    # a pre-staged flash_path (or errors). Auto-resolving the latest build is a follow-up.
    l2_firmware_build_id: str = ""
    # How many times the L2 supervisor relaunches a crash-looping device before declaring it bricked
    # (maps to NffMockL2Settings.max_trial_boots via NFF_MOCK_MAX_TRIAL_BOOTS). The default 3 models a
    # real device giving up, but the self-heal e2e needs the device to keep rebooting/reconnecting for
    # minutes so a later online window receives the agent's fix OTA — raise it (e.g. 1000) for that.
    l2_max_trial_boots: int = 3
    # Guest DNS for the QEMU L2 mock. The firmware downloads OTA images from a public Supabase
    # signed URL, so the emulated guest must resolve a hostname. QEMU user-net (SLIRP) forwards guest
    # DNS to the container's resolv.conf, which under Docker is the loopback 127.0.0.11 embedded
    # resolver that libslirp can't use → resolution fails and the OTA download times out. Giving the
    # mock container a real nameserver (docker run --dns) makes SLIRP forward there. Empty → no --dns.
    l2_guest_dns: str = "8.8.8.8"
    # Where the supervisor tees the QEMU UART inside the container (writable tmpfs). Lets us read the
    # full serial (openeth/mTLS/OTA-download/panic) via `docker exec cat` while the mock runs — the
    # supervisor otherwise only logs boot-banner + panic markers to stdout. Empty → don't capture.
    l2_serial_log_path: str = "/run/qemu/uart.log"

    # Level 3 (real hardware) seam — a URL to a hardware-rack bridge. Unset → 501.
    l3_bridge_url: str = ""

    # ── Fleet provisioning plane ──────────────────────────────────────────────
    # We POST to fleet's /internal/{provision,deprovision} to mint/remove certs.
    fleet_url: str = "http://nff-fleet:3001"
    fleet_secret: str = ""        # must equal nff-fleet's NFF_FLEET_MCP_SECRET
    fleet_timeout: float = 30.0

    # ── Controller API (this service) ─────────────────────────────────────────
    # control_secret gates POST/GET/DELETE /mocks via the X-Control-Secret header.
    control_secret: str = ""
    http_host: str = "0.0.0.0"
    http_port: int = 3004

    # ── Lifecycle limits ──────────────────────────────────────────────────────
    mock_ttl_s: int = 1800        # default lifetime; reaper tears down past this
    reaper_interval_s: int = 60
    max_per_project: int = 100
    max_total: int = 100

    # ── Browser keep-alive (presence) ─────────────────────────────────────────
    # A connected dashboard pings POST /mocks/presence for the project it is viewing every
    # ~60 s. While a project's last ping is within presence_ttl_s, its LEVEL-1 mocks are never
    # reaped for TTL — they stay up as long as the user is online, and drain once the pings
    # stop (tab closed / navigated away). Keep this comfortably above the browser ping interval
    # so a single missed beat doesn't reap. In-memory + soft: a controller restart just reverts
    # to label-TTL reaping until the next ping.
    presence_ttl_s: int = 150
    # Level-2 (heavy QEMU) mocks are a scarce shared resource, so presence does NOT keep them
    # alive. Each instead carries an idle lease: employing one (a compile / OTA / self-heal cycle
    # against it) POSTs /mocks/renew to reset this timer; unused for l2_idle_ttl_s it is released,
    # even while the user is online. Absent any renew, an L2 falls back to its spawn TTL label.
    l2_idle_ttl_s: int = 600

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


settings = NffMockControllerSettings()
