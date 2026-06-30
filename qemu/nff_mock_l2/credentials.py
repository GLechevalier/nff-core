"""Build a per-device NVS credential blob and splice it into the emulated flash.

To keep ONE generic L2 image (and cheap spawns), a device's identity is injected at launch rather
than baked into a per-device firmware build: we generate an ESP-IDF NVS partition from the
fleet-provisioned certs and write it into the device's writable ``flash.bin`` at the NVS offset.
The firmware reads its creds from NVS (the SDK already has a ``Preferences``/``nff_port_nvs_*``
layer), exactly as a claimed field device would.

``build_nvs_csv`` (the ``nvs_partition_gen.py`` input) is a pure function and unit-tested. The
key NAMES below must match the SDK's NVS reader — that pairing is finalized with the firmware
variant (Stage 3); they are namespaced under ``nff`` and documented here so both sides agree.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nff_mock_l2.config import NffMockL2Settings

# NVS namespace + keys the firmware reads its identity/creds from. Keep in sync with the SDK
# reader (nff-mock/qemu/l2/components/nff_l2_net/nvs_creds.c). The cert/key/CA values are stored as
# DER — X.509 DER certs + PKCS#8 DER key — because nff_config_t takes DER and the SDK's set_tls wraps
# the key as a PKCS#8 "PRIVATE KEY" PEM (a SEC1 "EC PRIVATE KEY" would be rejected). materialize_der_creds
# converts the fleet-provisioned PEM at splice time; the firmware reads the DER blobs verbatim.
NVS_NAMESPACE = "nff"
KEY_DEVICE_ID = "device_id"
KEY_PROJECT_ID = "project_id"
KEY_BROKER_HOST = "broker_host"
KEY_BOOT_EPOCH = "boot_epoch"          # injected clock (QEMU has no RTC; SNTP is flaky)
KEY_DEVICE_CERT = "device_crt"         # X.509 DER (file)
KEY_DEVICE_KEY = "device_key"          # PKCS#8 DER (file)
KEY_CA_CERT = "ca_crt"                 # X.509 DER (file)
KEY_PROJECT_CA = "proj_ca_crt"         # X.509 DER (file), optional
# The fleet command-verify key. A real device carries only the PUBLIC half — the 65-byte
# uncompressed P-256 point (0x04 || X || Y) nff_config_t.cmd_verify_key expects — NOT the private
# cmd_sign.pem. Derive it with public_point_from_signing_key() and splice the raw blob in (Stage 5);
# the firmware reads it verbatim (no PEM/DER conversion). Optional: absent it, connect + heartbeat
# still work, but signed commands (ping/reboot/OTA) cannot be verified.
KEY_CMD_VERIFY_KEY = "cmd_vkey"        # 65-byte raw P-256 public point (binary)


def _csv_escape(value: str) -> str:
    """Quote a CSV value if it contains a comma/quote/newline (RFC-4180 minimal)."""
    if any(c in value for c in (",", '"', "\n")):
        return '"' + value.replace('"', '""') + '"'
    return value


def build_nvs_csv(strings: dict[str, str], files: dict[str, str]) -> str:
    """Render an ``nvs_partition_gen.py`` CSV for the ``nff`` namespace.

    ``strings`` are inline ``string`` entries (device_id, project_id, …); ``files`` are
    ``file``-encoded entries whose value is a path to the content (the cert PEMs). Returns the
    full CSV text (header + namespace row + entries).
    """
    rows = ["key,type,encoding,value", f"{NVS_NAMESPACE},namespace,,"]
    for key, val in strings.items():
        rows.append(f"{key},data,string,{_csv_escape(val)}")
    for key, path in files.items():
        rows.append(f"{key},file,binary,{_csv_escape(path)}")
    return "\n".join(rows) + "\n"


def public_point_from_signing_key(pem_path: Path) -> bytes:
    """Derive the 65-byte uncompressed P-256 public point from the fleet's private cmd_sign.pem.

    A field device never holds the signing key — only the public half it verifies commands with.
    Used by the Stage-5 splice to materialise the ``cmd_vkey`` NVS blob; the private key never
    reaches the device. (Lazy ``cryptography`` import so this module stays dependency-light for the
    pure CSV path.)
    """
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PublicFormat,
        load_pem_private_key,
    )

    key = load_pem_private_key(Path(pem_path).read_bytes(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise ValueError(f"{pem_path} is not an EC private key")
    return key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)


def materialize_der_creds(cfg: "NffMockL2Settings", dest: Path) -> dict[str, str]:
    """Convert the fleet-provisioned PEM creds to the DER forms the SDK/NVS contract requires.

    X.509 DER for the certs; **PKCS#8 DER** for the key (fleet provisions a SEC1 "EC PRIVATE KEY"
    PEM, which the SDK's PKCS#8 "PRIVATE KEY" wrapping would reject — so re-encode it). Writes the
    blobs into ``dest`` and returns ``{nvs_key: der_path}`` for ``nvs_csv_for_device``. Used by the
    Stage-5 splice. (Lazy ``cryptography`` import so the pure CSV path stays dependency-light.)
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {}

    cert_srcs = {KEY_DEVICE_CERT: cfg.device_cert, KEY_CA_CERT: cfg.ca_cert}
    pca = cfg.project_ca_cert
    if pca is not None:
        cert_srcs[KEY_PROJECT_CA] = pca
    for nvs_key, src in cert_srcs.items():
        cert = x509.load_pem_x509_certificate(Path(src).read_bytes())
        p = dest / f"{nvs_key}.der"
        p.write_bytes(cert.public_bytes(serialization.Encoding.DER))
        out[nvs_key] = str(p)

    key = serialization.load_pem_private_key(Path(cfg.device_key).read_bytes(), password=None)
    p = dest / f"{KEY_DEVICE_KEY}.der"
    p.write_bytes(
        key.private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    out[KEY_DEVICE_KEY] = str(p)
    return out


def nvs_csv_for_device(
    cfg: "NffMockL2Settings",
    *,
    boot_epoch: int,
    cmd_vkey_path: str | Path | None = None,
    der_dir: str | Path | None = None,
) -> str:
    """Assemble the NVS CSV for ``cfg``.

    ``der_dir`` (the real path) → certs/key are first converted to DER under it via
    ``materialize_der_creds`` and the CSV references those (what the splice does). ``None`` (the
    default, used by the pure unit tests) references the raw provisioned PEM paths for inspection.

    ``cmd_vkey_path`` (when given) points at a pre-materialised 65-byte public-point blob
    (see ``public_point_from_signing_key``); the splice derives it before generating the partition
    so the private signing key never lands on the device.
    """
    strings = {
        KEY_DEVICE_ID: cfg.device_id,
        KEY_PROJECT_ID: cfg.project_id or "",
        # The firmware connects to the fixed guest-side IP; the guestfwd forwarder reaches the
        # real broker host-side (see qemu.build_qemu_argv).
        KEY_BROKER_HOST: cfg.guest_broker_ip,
        KEY_BOOT_EPOCH: str(boot_epoch),
    }
    if der_dir is not None:
        files = materialize_der_creds(cfg, Path(der_dir))
    else:
        files = {
            KEY_DEVICE_CERT: str(cfg.device_cert),
            KEY_DEVICE_KEY: str(cfg.device_key),
            KEY_CA_CERT: str(cfg.ca_cert),
        }
        pca = cfg.project_ca_cert
        if pca is not None:
            files[KEY_PROJECT_CA] = str(pca)
    if cmd_vkey_path is not None:
        files[KEY_CMD_VERIFY_KEY] = str(cmd_vkey_path)
    return build_nvs_csv(strings, files)


# ── The shell-out steps (need esp-idf host tools; not exercised here) ─────────────────
# nvs_partition_gen.py generate <csv> <out.bin> <size>; then splice <out.bin> into flash.bin at
# the partition table's `nvs` offset. Kept thin + documented; finalized alongside the L2 image.

def write_nvs_csv(cfg: "NffMockL2Settings", *, boot_epoch: int, dest: Path) -> Path:
    """Write the assembled CSV to ``dest`` (the input to nvs_partition_gen.py)."""
    dest.write_text(nvs_csv_for_device(cfg, boot_epoch=boot_epoch), encoding="utf-8")
    return dest
