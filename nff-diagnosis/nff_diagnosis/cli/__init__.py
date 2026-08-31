import sys
from pathlib import Path

from nff_diagnosis.schemas import CrashRecord


def load_crash(source: str) -> CrashRecord:
    """Load a CrashRecord from a file path or '-' for stdin.

    Handles UTF-8, UTF-8-with-BOM, and UTF-16 LE/BE (PowerShell '>' redirect default).
    """
    if source == "-":
        raw_json = sys.stdin.read()
    else:
        raw_bytes = Path(source).read_bytes()
        if raw_bytes.startswith((b"\xff\xfe", b"\xfe\xff")):
            raw_json = raw_bytes.decode("utf-16")
        elif raw_bytes.startswith(b"\xef\xbb\xbf"):
            raw_json = raw_bytes.decode("utf-8-sig")
        else:
            raw_json = raw_bytes.decode("utf-8")
    return CrashRecord.model_validate_json(raw_json)
