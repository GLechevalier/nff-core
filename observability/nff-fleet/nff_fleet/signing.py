"""Per-project ECDSA-P256 command signing.

Each project owns its own command-signing keypair (DEVICE_OWNERSHIP_DESIGN.md §6): a device
pins **its project's** verify key in credentials.h, and a command is signed with the **device's
project** key. A command signed for project A therefore won't verify on a project B device,
regardless of who reached /internal/command. Keys live at
``{project_keys_dir}/{project_id}/cmd_sign.pem`` and are lazy-loaded + cached.
"""

import base64
import hashlib
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from nff_fleet.config import settings

# Cache of (private, public) keypairs keyed by project_id.
_keys: dict[str, tuple[ec.EllipticCurvePrivateKey, ec.EllipticCurvePublicKey]] = {}

# Fleet-level rollover key (NOT per-project): used to sign the `rollover_cert` command that hands a
# bootstrap device its first operational cert. At that moment the device has no project key yet, so
# it pins the fleet rollover-verify key (baked into the bootstrap credentials.h) to authenticate the
# rollover. After rollover, the device uses its project's command-verify key like any claimed device.
_rollover_key: tuple[ec.EllipticCurvePrivateKey, ec.EllipticCurvePublicKey] | None = None


def _key_path(project_id: str) -> Path:
    return settings.project_keys_dir / project_id / "cmd_sign.pem"


def _rollover_key_path() -> Path:
    return settings.keys_dir / "rollover_sign.pem"


def ensure_rollover_key() -> Path:
    """Create the fleet rollover-signing key if it does not exist yet. Idempotent."""
    path = _rollover_key_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        key = ec.generate_private_key(ec.SECP256R1())
        path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
    return path


def _load_rollover() -> tuple[ec.EllipticCurvePrivateKey, ec.EllipticCurvePublicKey]:
    global _rollover_key
    if _rollover_key is not None:
        return _rollover_key
    ensure_rollover_key()
    priv = serialization.load_pem_private_key(_rollover_key_path().read_bytes(), password=None)
    _rollover_key = (priv, priv.public_key())
    return _rollover_key


def sign_rollover(payload: dict) -> dict:
    """Sign a `rollover_cert` command with the fleet rollover key (see _rollover_key)."""
    priv, _ = _load_rollover()
    der_sig = priv.sign(_build_tbs(payload).encode(), ec.ECDSA(hashes.SHA256()))
    return {**payload, "cmd_sig": der_sig.hex()}


def rollover_verify_key_uncompressed() -> bytes:
    """65-byte uncompressed P-256 point of the fleet rollover-verify key, for the bootstrap header."""
    _, pub = _load_rollover()
    return pub.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )


def ensure_project_key(project_id: str) -> Path:
    """Create the project's signing key if it does not exist yet. Idempotent."""
    path = _key_path(project_id)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        key = ec.generate_private_key(ec.SECP256R1())
        path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
    return path


def _load(project_id: str) -> tuple[ec.EllipticCurvePrivateKey, ec.EllipticCurvePublicKey]:
    cached = _keys.get(project_id)
    if cached is not None:
        return cached
    path = _key_path(project_id)
    if not path.exists():
        raise RuntimeError(f"No command-signing key for project {project_id!r}")
    priv = serialization.load_pem_private_key(path.read_bytes(), password=None)
    pair = (priv, priv.public_key())
    _keys[project_id] = pair
    return pair


def _build_tbs(payload: dict) -> str:
    """Build the to-be-signed string matching the device's nff_security.c build_tbs().

    Format (from nff.h):
      ping/reboot/diag/custom: "{action}|{nonce}|{timestamp}"
      ota:                     "ota|{version}|{sha256}|{nonce}|{timestamp}"
      rollover_cert:           "rollover_cert|{project_id}|{device_id}|{sha256(cert_der)}|{nonce}|{timestamp}"

    The rollover TBS hashes the leaf DER (not the multi-KB base64 blob) so it stays inside the
    device's fixed TBS buffer; the device recomputes the hash from the cert it received and rejects
    on mismatch, binding the signature to the exact credentials it is about to install.
    """
    action = payload["action"]
    nonce  = payload["nonce"]
    ts     = payload["timestamp"]
    if action == "ota":
        return f"ota|{payload['version']}|{payload['sha256']}|{nonce}|{ts}"
    if action == "rollover_cert":
        cert_sha = hashlib.sha256(base64.b64decode(payload["device_cert"])).hexdigest()
        return f"rollover_cert|{payload['project_id']}|{payload['device_id']}|{cert_sha}|{nonce}|{ts}"
    return f"{action}|{nonce}|{ts}"


def sign(payload: dict, project_id: str) -> dict:
    priv, _ = _load(project_id)
    der_sig = priv.sign(_build_tbs(payload).encode(), ec.ECDSA(hashes.SHA256()))
    return {**payload, "cmd_sig": der_sig.hex()}


def verify(payload: dict, project_id: str) -> bool:
    _, pub = _load(project_id)
    hex_sig = payload.get("cmd_sig")
    if not hex_sig:
        return False
    try:
        pub.verify(bytes.fromhex(hex_sig), _build_tbs(payload).encode(),
                   ec.ECDSA(hashes.SHA256()))
        return True
    except Exception:
        return False


def public_key_pem(project_id: str) -> str:
    _, pub = _load(project_id)
    return pub.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


def public_key_uncompressed(project_id: str) -> bytes:
    """65-byte uncompressed P-256 point (0x04 || X || Y) for credentials.h."""
    _, pub = _load(project_id)
    return pub.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )


def clear_cache() -> None:
    """Drop cached keys (used by tests)."""
    global _rollover_key
    _keys.clear()
    _rollover_key = None
