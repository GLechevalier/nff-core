from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.exceptions import InvalidSignature

_private_key: ec.EllipticCurvePrivateKey | None = None
_public_key: ec.EllipticCurvePublicKey | None = None


def load_key(path: Path) -> None:
    global _private_key, _public_key
    pem = path.read_bytes()
    _private_key = serialization.load_pem_private_key(pem, password=None)
    _public_key = _private_key.public_key()


def _build_tbs(payload: dict) -> str:
    """Build the to-be-signed string matching the device's nff_security.c build_tbs().

    Format (from nff.h):
      ping/reboot/diag/custom: "{action}|{nonce}|{timestamp}"
      ota:                     "ota|{version}|{sha256}|{nonce}|{timestamp}"
    """
    action = payload["action"]
    nonce  = payload["nonce"]
    ts     = payload["timestamp"]
    if action == "ota":
        return f"ota|{payload['version']}|{payload['sha256']}|{nonce}|{ts}"
    return f"{action}|{nonce}|{ts}"


def sign(payload: dict) -> dict:
    if _private_key is None:
        raise RuntimeError("Signing key not loaded — call load_key() at startup")
    tbs = _build_tbs(payload).encode()
    der_sig = _private_key.sign(tbs, ec.ECDSA(hashes.SHA256()))
    return {**payload, "cmd_sig": der_sig.hex()}


def verify(payload: dict) -> bool:
    if _public_key is None:
        raise RuntimeError("Signing key not loaded")
    hex_sig = payload.get("cmd_sig")
    if not hex_sig:
        return False
    try:
        _public_key.verify(bytes.fromhex(hex_sig), _build_tbs(payload).encode(),
                           ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, Exception):
        return False


def public_key_pem() -> str:
    if _public_key is None:
        raise RuntimeError("Signing key not loaded")
    return _public_key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
