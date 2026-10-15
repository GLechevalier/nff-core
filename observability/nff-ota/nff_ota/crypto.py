import base64
import hashlib
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

_CHUNK = 65536  # 64 KB


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def sign_ed25519(private_key_pem: str, data: bytes) -> str:
    key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    assert isinstance(key, Ed25519PrivateKey)
    sig = key.sign(data)
    return base64.b64encode(sig).decode()


def verify_ed25519(public_key_pem: str, data: bytes, signature_b64: str) -> bool:
    try:
        key = serialization.load_pem_public_key(public_key_pem.encode())
        assert isinstance(key, Ed25519PublicKey)
        sig = base64.b64decode(signature_b64)
        key.verify(sig, data)
        return True
    except (InvalidSignature, Exception):
        return False


def verify_rsa_pss(public_key_pem: str, data: bytes, signature_b64: str) -> bool:
    try:
        key = serialization.load_pem_public_key(public_key_pem.encode())
        assert isinstance(key, RSAPublicKey)
        sig = base64.b64decode(signature_b64)
        key.verify(sig, data, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH), hashes.SHA256())
        return True
    except (InvalidSignature, Exception):
        return False
