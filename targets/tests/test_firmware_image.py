"""The firmware blob must trip the pentester's firmware-analysis tools."""

from __future__ import annotations

import gzip
import io
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from nff_mock_targets.common.banners import LINUX
from nff_mock_targets.common.firmware_image import build_firmware_blob

# The exact regexes scan_hardcoded_creds uses (nff-pentester analyzer.py).
_CRED_PATTERNS = [
    re.compile(r"(?i)password\s*[:=]\s*\S+"),
    re.compile(r"BEGIN (RSA|EC|DSA|OPENSSH) PRIVATE KEY"),
    re.compile(r"root:[x*]:\d+:\d+:"),
]


def test_blob_contains_planted_secrets():
    text = build_firmware_blob(LINUX).decode("latin-1")
    for rx in _CRED_PATTERNS:
        assert rx.search(text), f"firmware blob missing a hit for {rx.pattern!r}"


def test_blob_contains_insecure_urls():
    text = build_firmware_blob(LINUX).decode("latin-1")
    assert "http://" in text
    assert "mqtt://" in text


def test_embedded_rootfs_has_backdoor_account():
    """Decompress the appended gzip-tar directly and confirm the uid-0 backdoor."""
    blob = build_firmware_blob(LINUX)
    gz_start = blob.index(b"\x1f\x8b")  # gzip magic
    raw = gzip.decompress(blob[gz_start:])
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        passwd = next(m for m in tar.getmembers() if m.name.endswith("etc/passwd"))
        content = tar.extractfile(passwd).read().decode()
    # A uid-0 account whose name is not 'root' -> parse_unix_users.extra_uid0_accounts.
    uid0 = [ln.split(":")[0] for ln in content.splitlines() if ln.split(":")[2:4] == ["0", "0"]]
    assert "root" in uid0
    assert any(name != "root" for name in uid0), "no backdoor uid-0 account planted"


@pytest.mark.skipif(shutil.which("binwalk") is None, reason="binwalk not installed")
def test_binwalk_extracts_passwd(tmp_path: Path):
    """End-to-end: binwalk unpacks the blob into a tree containing etc/passwd."""
    fw = tmp_path / "fw.bin"
    fw.write_bytes(build_firmware_blob(LINUX))
    out = tmp_path / "extracted"
    subprocess.run(
        ["binwalk", "--extract", f"--directory={out}", "--quiet", str(fw)],
        check=False, timeout=120,
    )
    passwd_files = list(out.rglob("etc/passwd"))
    assert passwd_files, "binwalk did not extract an etc/passwd from the blob"
    assert "nffsvc" in passwd_files[0].read_text()
