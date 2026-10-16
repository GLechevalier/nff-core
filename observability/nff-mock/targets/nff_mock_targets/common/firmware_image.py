"""Builds a fake firmware blob with planted secrets, for the firmware chains.

The blob is designed to light three pentester findings without any real device:

1. ``scan_hardcoded_creds`` (default ``strings`` mode) — the blob's plaintext
   header carries ``*_password=...``, an ``/etc/passwd`` root line, and a
   ``BEGIN OPENSSH PRIVATE KEY`` marker, all matching the tool's regexes.
2. ``extract_firmware`` (binwalk) + ``parse_unix_users`` — a real gzip-wrapped tar
   of a minirootfs is appended. binwalk recognises gzip + tar and unpacks it; the
   extracted ``etc/passwd`` + ``etc/shadow`` then drive ``parse_unix_users``,
   including a uid-0 *backdoor* account (name != root) → ``extra_uid0_accounts``.
3. ``extract_firmware_urls`` — insecure ``http://`` / ``mqtt://`` update + telemetry
   URLs are embedded as strings → ``insecure_count > 0``.

Everything is built with the stdlib (``tarfile`` + ``gzip``); no external tooling.
"""

from __future__ import annotations

import gzip
import io
import tarfile
from pathlib import Path

from nff_mock_targets.common.banners import Flavor

# A syntactically-plausible but fake key body — enough to trip the OPENSSH regex
# without being a usable credential.
_FAKE_OPENSSH_KEY = """-----BEGIN OPENSSH PRIVATE KEY-----
b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW
QyNTUxOQAAACBuZmZtb2NrZmFrZWtleW5vdHJlYWxub3RyZWFsbm90cmVhbGsAAAAJAAAA
THIS-IS-A-FAKE-NFF-MOCK-KEY-DO-NOT-USE-IT-IS-NOT-A-REAL-CREDENTIAL00000
-----END OPENSSH PRIVATE KEY-----
"""

# Insecure trust relationships an auditor should flag.
_FIRMWARE_URLS = [
    "http://updates.nff-mock.local/firmware/latest.bin",
    "http://telemetry.nff-mock.local/v1/report",
    "mqtt://broker.nff-mock.local:1883",
    "https://api.nff-mock.local/device",  # one secure one, for contrast
]


def _passwd_text() -> str:
    # 7 colon-separated fields. `nffsvc` is uid 0 but not named root -> backdoor.
    return (
        "root:x:0:0:root:/root:/bin/sh\n"
        "daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin\n"
        "nffsvc:x:0:0:nff service:/root:/bin/sh\n"
        "admin:x:1000:1000:admin:/home/admin:/bin/sh\n"
    )


def _shadow_text() -> str:
    # $6$ = sha512crypt -> parse_unix_users reports has_hash / hash_algo.
    return (
        "root:$6$nffmocksalt$1A2b3C4d5E6f7G8h9I0jKlMnOpQrStUvWxYz0123456789abcdef:19000:0:99999:7:::\n"
        "nffsvc:$6$bdsalt$0fedcba9876543210ZYXwvuTSRqponMLKjihGFEdcBA9876543210zyxw:19000:0:99999:7:::\n"
        "daemon:*:19000:0:99999:7:::\n"
        "admin:$6$adminsalt$abcdef0123456789ZYXWVUTSRQPONMLKJIHGFEDCBA9876543210zyxwv:19000:0:99999:7:::\n"
    )


def _config_text(flavor: Flavor) -> str:
    urls = "\n".join(f"url = {u}" for u in _FIRMWARE_URLS)
    return (
        f"# {flavor.firmware_product} config\n"
        f"device_id = {flavor.device_id}\n"
        f"fw_version = {flavor.fw_version}\n"
        "admin_password=admin123\n"
        "mqtt_password=changeme\n"
        "api_secret=s3cr3t-nff-mock\n"
        f"{urls}\n"
    )


def _build_rootfs_targz(flavor: Flavor) -> bytes:
    """A gzip-compressed tar of a minimal vulnerable rootfs."""
    files = {
        "etc/passwd": _passwd_text(),
        "etc/shadow": _shadow_text(),
        "etc/config/system.conf": _config_text(flavor),
        "root/.ssh/id_rsa": _FAKE_OPENSSH_KEY,
    }
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name=f"squashfs-root/{name}")
            info.size = len(data)
            info.mode = 0o600 if name.endswith("id_rsa") else 0o644
            tar.addfile(info, io.BytesIO(data))
    # mtime fixed for reproducible blobs (no Date.now equivalent needed).
    return gzip.compress(raw.getvalue(), mtime=0)


def _header(flavor: Flavor) -> bytes:
    """Plaintext header so `strings`-mode scans find secrets without extraction."""
    lines = [
        "NFFMOCK-FW",  # fake magic
        f"product={flavor.firmware_product}",
        f"version={flavor.fw_version}",
        f"build=nff-mock-{flavor.name}",
        "admin_password=admin123",
        "mqtt_password=changeme",
        "root:x:0:0:root:/root:/bin/sh",
        *(f"update_url={u}" for u in _FIRMWARE_URLS),
        _FAKE_OPENSSH_KEY,
        "",
    ]
    return "\n".join(lines).encode()


def build_firmware_blob(flavor: Flavor) -> bytes:
    """Return the full firmware image bytes (plaintext header + gzip-tar rootfs)."""
    return _header(flavor) + _build_rootfs_targz(flavor)


def write_firmware_blob(flavor: Flavor, path: Path) -> Path:
    """Build the blob and write it to ``path`` (parents created). Returns the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_firmware_blob(flavor))
    return path
