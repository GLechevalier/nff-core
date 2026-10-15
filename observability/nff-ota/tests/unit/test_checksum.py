import hashlib
from pathlib import Path

import pytest

from nff_ota.crypto import sha256_bytes, sha256_file


class TestSha256Bytes:
    def test_known_value(self):
        data = b"hello world"
        expected = hashlib.sha256(data).hexdigest()
        assert sha256_bytes(data) == expected

    def test_empty_bytes(self):
        expected = hashlib.sha256(b"").hexdigest()
        assert sha256_bytes(b"") == expected

    def test_tamper_detection(self):
        data = b"firmware-v1.0"
        tampered = b"firmware-v1.1"
        assert sha256_bytes(data) != sha256_bytes(tampered)

    def test_returns_hex_string(self):
        result = sha256_bytes(b"test")
        assert isinstance(result, str)
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_large_data(self):
        data = b"x" * (1024 * 1024)
        result = sha256_bytes(data)
        assert len(result) == 64


class TestSha256File:
    def test_matches_sha256_bytes(self, tmp_path: Path):
        data = b"firmware binary content"
        f = tmp_path / "fw.bin"
        f.write_bytes(data)
        assert sha256_file(f) == sha256_bytes(data)

    def test_empty_file(self, tmp_path: Path):
        f = tmp_path / "empty.bin"
        f.write_bytes(b"")
        assert sha256_file(f) == sha256_bytes(b"")

    def test_large_file_streams_correctly(self, tmp_path: Path):
        data = b"A" * (200 * 1024)  # 200 KB — crosses 64 KB chunk boundary
        f = tmp_path / "large.bin"
        f.write_bytes(data)
        assert sha256_file(f) == sha256_bytes(data)

    def test_tampered_file_differs(self, tmp_path: Path):
        original = tmp_path / "orig.bin"
        tampered = tmp_path / "tampered.bin"
        original.write_bytes(b"real firmware")
        tampered.write_bytes(b"evil firmware")
        assert sha256_file(original) != sha256_file(tampered)
