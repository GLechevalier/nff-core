"""Unit tests for firmware tools: extract_firmware, analyze_strings, scan_hardcoded_creds."""

from unittest.mock import AsyncMock, patch

from tests.conftest import BINWALK_JSON_LIST, make_cmd

from nff_pentester.tools.firmware.analyzer import (
    AnalyzeStringsInput,
    ScanHardcodedCredsInput,
    analyze_strings,
    scan_hardcoded_creds,
)
from nff_pentester.tools.firmware.extractor import ExtractFirmwareInput, extract_firmware

_PATCH_EXTRACT = "nff_pentester.tools.firmware.extractor.run_command"
_PATCH_ANALYZE = "nff_pentester.tools.firmware.analyzer.run_command"


# ── extract_firmware ───────────────────────────────────────────────────────────

async def test_extract_firmware_not_found(mock_ctx):
    with patch(_PATCH_EXTRACT, new=AsyncMock(return_value=make_cmd(returncode=1, stderr=""))):
        params = ExtractFirmwareInput(firmware_path="/tmp/fw.bin")
        result = await extract_firmware(params, mock_ctx)
    assert result.error == "binwalk not found"
    assert result.signature_count == 0


async def test_extract_firmware_stderr_message(mock_ctx):
    with patch(_PATCH_EXTRACT, new=AsyncMock(return_value=make_cmd(returncode=1, stderr="binwalk: command not found"))):
        params = ExtractFirmwareInput(firmware_path="/tmp/fw.bin")
        result = await extract_firmware(params, mock_ctx)
    assert "binwalk" in result.error


async def test_extract_firmware_timeout(mock_ctx):
    with patch(_PATCH_EXTRACT, new=AsyncMock(return_value=make_cmd(timed_out=True))):
        params = ExtractFirmwareInput(firmware_path="/tmp/fw.bin")
        result = await extract_firmware(params, mock_ctx)
    assert result.error == "Timed out"
    assert result.extracted is False


async def test_extract_firmware_parses_signatures(mock_ctx):
    with patch(_PATCH_EXTRACT, new=AsyncMock(return_value=make_cmd(stdout=BINWALK_JSON_LIST))):
        params = ExtractFirmwareInput(firmware_path="/tmp/fw.bin")
        result = await extract_firmware(params, mock_ctx)
    assert result.signature_count == 1
    assert result.signatures[0].offset == 0
    assert "gzip" in result.signatures[0].description


async def test_extract_firmware_signature_only_mode(mock_ctx):
    with patch(_PATCH_EXTRACT, new=AsyncMock(return_value=make_cmd(stdout=BINWALK_JSON_LIST))):
        params = ExtractFirmwareInput(firmware_path="/tmp/fw.bin", signature_scan_only=True)
        result = await extract_firmware(params, mock_ctx)
    assert result.extracted is False
    assert result.extract_dir is None


async def test_extract_firmware_full_extraction_sets_extracted(mock_ctx):
    with patch(_PATCH_EXTRACT, new=AsyncMock(return_value=make_cmd(stdout=BINWALK_JSON_LIST))):
        params = ExtractFirmwareInput(firmware_path="/tmp/fw.bin", signature_scan_only=False)
        result = await extract_firmware(params, mock_ctx)
    assert result.extracted is True
    assert result.extract_dir == "/tmp/fw.bin_extracted"


# ── analyze_strings ────────────────────────────────────────────────────────────

async def test_analyze_strings_not_found(mock_ctx):
    with patch(_PATCH_ANALYZE, new=AsyncMock(return_value=make_cmd(returncode=1, stderr=""))):
        params = AnalyzeStringsInput(path="/tmp/fw.bin")
        result = await analyze_strings(params, mock_ctx)
    assert result.error is not None
    assert result.string_count == 0


async def test_analyze_strings_timeout(mock_ctx):
    with patch(_PATCH_ANALYZE, new=AsyncMock(return_value=make_cmd(timed_out=True))):
        params = AnalyzeStringsInput(path="/tmp/fw.bin")
        result = await analyze_strings(params, mock_ctx)
    assert result.error == "Timed out"


async def test_analyze_strings_extracts_strings(mock_ctx):
    with patch(_PATCH_ANALYZE, new=AsyncMock(return_value=make_cmd(stdout="admin\npassword\nhello\n"))):
        params = AnalyzeStringsInput(path="/tmp/fw.bin")
        result = await analyze_strings(params, mock_ctx)
    assert result.string_count == 3
    assert "admin" in result.strings


async def test_analyze_strings_max_results_cap(mock_ctx):
    big_output = "\n".join(f"string{i}" for i in range(100))
    with patch(_PATCH_ANALYZE, new=AsyncMock(return_value=make_cmd(stdout=big_output))):
        params = AnalyzeStringsInput(path="/tmp/fw.bin", max_results=10)
        result = await analyze_strings(params, mock_ctx)
    assert result.string_count == 100
    assert len(result.strings) == 10


# ── scan_hardcoded_creds ───────────────────────────────────────────────────────

async def test_scan_hardcoded_creds_no_hits(mock_ctx):
    with patch(_PATCH_ANALYZE, new=AsyncMock(return_value=make_cmd(stdout="version 1.0\nbuild 2024-01-01\n"))):
        params = ScanHardcodedCredsInput(path="/tmp/fw.bin")
        result = await scan_hardcoded_creds(params, mock_ctx)
    assert result.hit_count == 0
    assert result.hits == []


async def test_scan_hardcoded_creds_password_pattern(mock_ctx):
    with patch(_PATCH_ANALYZE, new=AsyncMock(return_value=make_cmd(stdout="password=secret123\nharmonless line\n"))):
        params = ScanHardcodedCredsInput(path="/tmp/fw.bin")
        result = await scan_hardcoded_creds(params, mock_ctx)
    assert result.hit_count == 1
    assert result.hits[0].line == "password=secret123"


async def test_scan_hardcoded_creds_rsa_private_key(mock_ctx):
    with patch(_PATCH_ANALYZE, new=AsyncMock(return_value=make_cmd(stdout="-----BEGIN RSA PRIVATE KEY-----\nMIIEow==\n"))):
        params = ScanHardcodedCredsInput(path="/tmp/fw.bin")
        result = await scan_hardcoded_creds(params, mock_ctx)
    assert result.hit_count == 1


async def test_scan_hardcoded_creds_crypt_hash(mock_ctx):
    with patch(_PATCH_ANALYZE, new=AsyncMock(return_value=make_cmd(stdout="root:$6$salt$hashedpassword:18000:0:99999:7:::\n"))):
        params = ScanHardcodedCredsInput(path="/tmp/fw.bin")
        result = await scan_hardcoded_creds(params, mock_ctx)
    assert result.hit_count == 1


async def test_scan_hardcoded_creds_api_key_pattern(mock_ctx):
    with patch(_PATCH_ANALYZE, new=AsyncMock(return_value=make_cmd(stdout="api_key=AKIAIOSFODNN7EXAMPLE\n"))):
        params = ScanHardcodedCredsInput(path="/tmp/fw.bin")
        result = await scan_hardcoded_creds(params, mock_ctx)
    assert result.hit_count == 1


async def test_scan_hardcoded_creds_not_found(mock_ctx):
    with patch(_PATCH_ANALYZE, new=AsyncMock(return_value=make_cmd(returncode=1, stderr=""))):
        params = ScanHardcodedCredsInput(path="/tmp/fw.bin")
        result = await scan_hardcoded_creds(params, mock_ctx)
    assert result.error is not None
    assert result.hit_count == 0
