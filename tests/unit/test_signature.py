import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import hashes
import base64

from nff_ota.crypto import sign_ed25519, verify_ed25519, verify_rsa_pss


def _gen_ed25519_pem_pair() -> tuple[str, str]:
    priv = Ed25519PrivateKey.generate()
    priv_pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub_pem = priv.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return priv_pem, pub_pem


def _gen_rsa_pem_pair() -> tuple[str, str]:
    from cryptography.hazmat.backends import default_backend
    priv = generate_private_key(65537, 2048, default_backend())
    priv_pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    pub_pem = priv.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return priv_pem, pub_pem


class TestEd25519:
    def test_round_trip(self):
        priv_pem, pub_pem = _gen_ed25519_pem_pair()
        data = b"firmware payload"
        sig = sign_ed25519(priv_pem, data)
        assert verify_ed25519(pub_pem, data, sig) is True

    def test_wrong_key_rejected(self):
        priv_pem, _ = _gen_ed25519_pem_pair()
        _, other_pub_pem = _gen_ed25519_pem_pair()
        data = b"firmware payload"
        sig = sign_ed25519(priv_pem, data)
        assert verify_ed25519(other_pub_pem, data, sig) is False

    def test_tampered_data_rejected(self):
        priv_pem, pub_pem = _gen_ed25519_pem_pair()
        data = b"original firmware"
        sig = sign_ed25519(priv_pem, data)
        assert verify_ed25519(pub_pem, b"tampered firmware", sig) is False

    def test_corrupted_signature_returns_false(self):
        _, pub_pem = _gen_ed25519_pem_pair()
        bad_sig = base64.b64encode(b"not a real signature at all 1234567").decode()
        assert verify_ed25519(pub_pem, b"data", bad_sig) is False

    def test_empty_signature_returns_false(self):
        _, pub_pem = _gen_ed25519_pem_pair()
        assert verify_ed25519(pub_pem, b"data", "") is False

    def test_sign_returns_base64_string(self):
        priv_pem, _ = _gen_ed25519_pem_pair()
        sig = sign_ed25519(priv_pem, b"data")
        assert isinstance(sig, str)
        decoded = base64.b64decode(sig)
        assert len(decoded) == 64  # Ed25519 signature is always 64 bytes


class TestRsaPss:
    def test_round_trip(self):
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives.asymmetric import padding as apad
        priv_pem, pub_pem = _gen_rsa_pem_pair()
        priv = serialization.load_pem_private_key(priv_pem.encode(), password=None)
        data = b"rsa firmware"
        sig = priv.sign(data, apad.PSS(mgf=apad.MGF1(hashes.SHA256()), salt_length=apad.PSS.MAX_LENGTH), hashes.SHA256())
        sig_b64 = base64.b64encode(sig).decode()
        assert verify_rsa_pss(pub_pem, data, sig_b64) is True

    def test_wrong_key_rejected(self):
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives.asymmetric import padding as apad
        priv_pem, _ = _gen_rsa_pem_pair()
        _, other_pub_pem = _gen_rsa_pem_pair()
        priv = serialization.load_pem_private_key(priv_pem.encode(), password=None)
        data = b"firmware"
        sig = priv.sign(data, apad.PSS(mgf=apad.MGF1(hashes.SHA256()), salt_length=apad.PSS.MAX_LENGTH), hashes.SHA256())
        sig_b64 = base64.b64encode(sig).decode()
        assert verify_rsa_pss(other_pub_pem, data, sig_b64) is False

    def test_corrupted_signature_returns_false(self):
        _, pub_pem = _gen_rsa_pem_pair()
        assert verify_rsa_pss(pub_pem, b"data", base64.b64encode(b"garbage").decode()) is False
