from __future__ import annotations

import logging
import time
from collections import deque
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

logger = logging.getLogger(__name__)

# Mirror the SDK constants (nff.h): NFF_NONCE_RING_SIZE / NFF_TIMESTAMP_WINDOW_S.
NONCE_RING_SIZE = 16
TIMESTAMP_WINDOW_S = 300


def _build_tbs(payload: dict) -> str:
    """To-be-signed canonical string — byte-identical to nff_security.c build_tbs()
    and nff-fleet/nff_fleet/signing.py _build_tbs().

      ping/reboot/diag/custom: "{action}|{nonce}|{timestamp}"
      ota:                     "ota|{version}|{sha256}|{nonce}|{timestamp}"
    """
    action = payload["action"]
    nonce = payload["nonce"]
    ts = payload["timestamp"]
    if action == "ota":
        return f"ota|{payload['version']}|{payload['sha256']}|{nonce}|{ts}"
    return f"{action}|{nonce}|{ts}"


class CommandVerifier:
    """Verifies signed commands exactly like the device firmware does.

    Reproduces nff_security_verify_cmd(): timestamp window, nonce replay ring, then
    ECDSA-P256 signature over the TBS string. A failure means the command is dropped
    with no response — which is how a real device reacts to a forged command, and is
    what makes fleet's send_command time out (504) on a bad signature.
    """

    def __init__(self, *, public_key: ec.EllipticCurvePublicKey, enforce: bool = True) -> None:
        self._pub = public_key
        self._enforce = enforce
        self._nonces: deque[str] = deque(maxlen=NONCE_RING_SIZE)

    @classmethod
    def from_key_file(cls, path: Path, *, enforce: bool = True) -> "CommandVerifier":
        """Load the command-verify key. The provisioned file is the fleet's *private*
        signing key (cmd_sign.pem); a real device only carries the public half, so we
        derive it. A PEM public key or a raw 65-byte point is also accepted.
        """
        data = path.read_bytes()
        try:
            priv = serialization.load_pem_private_key(data, password=None)
            pub = priv.public_key()
        except (ValueError, TypeError):
            try:
                pub = serialization.load_pem_public_key(data)
            except (ValueError, TypeError):
                pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), data)
        return cls(public_key=pub, enforce=enforce)

    def verify(self, payload: dict) -> bool:
        if not self._enforce:
            return True

        nonce = payload.get("nonce")
        ts = payload.get("timestamp")
        sig_hex = payload.get("cmd_sig")
        if not nonce or ts is None or not sig_hex:
            return False

        # 1. Timestamp window — the mock has a real wall clock, so always enforce.
        now = int(time.time())
        if abs(now - int(ts)) > TIMESTAMP_WINDOW_S:
            logger.warning("cmd rejected: stale timestamp (Δ=%ss)", abs(now - int(ts)))
            return False

        # 2. Nonce replay ring — reject before the expensive ECDSA check.
        if nonce in self._nonces:
            logger.warning("cmd rejected: replayed nonce %s", nonce)
            return False

        # 3. ECDSA-P256 over the TBS string.
        try:
            self._pub.verify(
                bytes.fromhex(sig_hex),
                _build_tbs(payload).encode(),
                ec.ECDSA(hashes.SHA256()),
            )
        except (InvalidSignature, ValueError, KeyError):
            logger.warning("cmd rejected: bad signature")
            return False

        # 4. Record the nonce only after a successful verify (matches the SDK).
        self._nonces.append(nonce)
        return True
