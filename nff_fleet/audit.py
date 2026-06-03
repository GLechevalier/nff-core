import asyncio
import json
import time
from pathlib import Path

import aiofiles

from nff_fleet.config import settings
from nff_fleet import db

_lock = asyncio.Lock()


async def log_command(
    device_id: str,
    action: str,
    nonce: str,
    session_id: str,
) -> None:
    entry = {
        "timestamp": int(time.time()),
        "device_id": device_id,
        "action": action,
        "nonce": nonce,
        "session_id": session_id,
    }
    log_path: Path = settings.audit_log
    log_path.parent.mkdir(parents=True, exist_ok=True)
    async with _lock:
        async with aiofiles.open(log_path, "a") as f:
            await f.write(json.dumps(entry) + "\n")
    db.append_audit(device_id, action, nonce, session_id)
