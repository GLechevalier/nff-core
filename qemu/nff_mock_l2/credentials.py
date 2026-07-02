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

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

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


def write_nvs_csv(
    cfg: "NffMockL2Settings",
    *,
    boot_epoch: int,
    dest: Path,
    der_dir: str | Path | None = None,
    cmd_vkey_path: str | Path | None = None,
) -> Path:
    """Write the assembled CSV to ``dest`` (the input to nvs_partition_gen.py).

    ``der_dir`` / ``cmd_vkey_path`` are threaded to ``nvs_csv_for_device``: the splice passes them
    so the CSV references the materialised DER creds + the pre-derived command-verify blob (rather
    than the raw provisioned PEMs, which the pure unit tests use)."""
    dest.write_text(
        nvs_csv_for_device(
            cfg, boot_epoch=boot_epoch, der_dir=der_dir, cmd_vkey_path=cmd_vkey_path
        ),
        encoding="utf-8",
    )
    return dest


# ── The splice: NVS partition generation + write into the flash image ─────────────────
# The identity is injected at launch (one generic image, cheap spawns): generate an ESP-IDF NVS
# partition from the provisioned certs and write it into the writable flash at the `nvs` offset.
# Needs esp-idf's nvs_partition_gen.py (present in the IDF base image); the splice itself is pure
# Python (the RUNBOOK's `dd ... seek=0x9000 conv=notrunc`).

def _nvs_partition_gen_cmd(cfg: "NffMockL2Settings") -> list[str]:
    """Resolve the ``nvs_partition_gen`` invocation: an explicit config override, else the script
    under ``$IDF_PATH`` (the IDF base image), else a ``nvs_partition_gen`` console entry on PATH."""
    override = getattr(cfg, "nvs_partition_gen_path", None)
    if override:
        return [sys.executable, str(override)]
    idf = os.environ.get("IDF_PATH")
    if idf:
        script = (
            Path(idf)
            / "components" / "nvs_flash" / "nvs_partition_generator" / "nvs_partition_gen.py"
        )
        if script.exists():
            return [sys.executable, str(script)]
    return ["nvs_partition_gen"]


def generate_nvs_bin(
    cfg: "NffMockL2Settings",
    *,
    boot_epoch: int,
    work_dir: str | Path,
    size: int | None = None,
) -> Path:
    """Generate this device's NVS partition image under ``work_dir`` and return the ``.bin`` path.

    Materialises the DER creds + derives the 65-byte command-verify point (when the fleet signing
    key is present — connect+heartbeat still work without it, only signed-command verification is
    skipped), writes the CSV, and shells out to ``nvs_partition_gen.py generate <csv> <bin> <size>``.
    """
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    size = cfg.nvs_size if size is None else size

    cmd_vkey_path: Path | None = None
    signing_key = Path(cfg.cmd_verify_key)
    if signing_key.exists():
        cmd_vkey_path = work_dir / "cmd_vkey.bin"
        cmd_vkey_path.write_bytes(public_point_from_signing_key(signing_key))
    else:
        logger.warning(
            "no command-verify key at %s — signed commands cannot be verified", signing_key
        )

    csv_path = write_nvs_csv(
        cfg,
        boot_epoch=boot_epoch,
        dest=work_dir / "nff_nvs.csv",
        der_dir=work_dir / "der",
        cmd_vkey_path=cmd_vkey_path,
    )
    out_bin = work_dir / "nff_nvs.bin"
    cmd = _nvs_partition_gen_cmd(cfg) + ["generate", str(csv_path), str(out_bin), hex(size)]
    logger.info("generating NVS partition: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)
    return out_bin


def splice_nvs_into_flash(
    flash_path: str | Path, nvs_bin: str | Path, *, offset: int, size_limit: int | None = None
) -> None:
    """Write ``nvs_bin`` into ``flash_path`` at ``offset`` in place (the ``dd conv=notrunc`` step).

    ``size_limit`` (the nvs partition size) guards against a blob that would overrun into the next
    partition."""
    data = Path(nvs_bin).read_bytes()
    if size_limit is not None and len(data) > size_limit:
        raise ValueError(
            f"NVS blob is {len(data)} bytes, exceeds the nvs partition ({size_limit} bytes)"
        )
    with open(flash_path, "r+b") as f:
        f.seek(offset)
        f.write(data)
