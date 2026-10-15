#!/usr/bin/env python3
"""
gen_credentials.py — Generate credentials.h for an nff device.

Usage (called by `nff provision new-device`):
    python gen_credentials.py --id device-42 --project <project_id>
                               [--ca ca.pem] [--cmd-key cmd_verify.bin]
                               [--out credentials.h] [--fw-version 1.0.0]
                               [--build-id aabbccdd11223344]

What it generates:
    - P-256 key pair (device private key + CSR)
    - Client certificate signed by the nff CA (requires --ca and --ca-key)
    - credentials.h with all four DER byte arrays + macros

If no CA is provided, a self-signed dev cert is generated (NOT for production).

WARNING: The output file contains the device private key.
Add credentials.h to .gitignore and treat it as a secret.
"""

import argparse
import os
import sys
import datetime

try:
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.backends import default_backend
except ImportError:
    print("ERROR: cryptography library not found.")
    print("Install with: pip install cryptography")
    sys.exit(1)


# Custom X.509 extension OID carrying the device's project_id (must match
# nff-fleet/nff_fleet/provisioning/ca.py:NFF_PROJECT_OID).
NFF_PROJECT_OID = x509.ObjectIdentifier("1.3.6.1.4.1.61226.1.1")


def bytes_to_c_array(name: str, data: bytes) -> str:
    """Render a bytes object as a C byte array declaration."""
    hex_vals = ", ".join(f"0x{b:02x}" for b in data)
    base = name[:-4]  # strip "_DER" suffix to get the base name
    return (
        f"static const uint8_t {name}[] = {{\n"
        f"    {hex_vals}\n"
        f"}};\n"
        f"#define {base}_LEN sizeof({name})\n"
    )


def gen_device_keypair():
    """Generate an ECDSA P-256 key pair."""
    private_key = ec.generate_private_key(ec.SECP256R1(), default_backend())
    return private_key


def gen_self_signed_cert(private_key, device_id: str, project_id: str) -> x509.Certificate:
    """Generate a self-signed certificate (dev only)."""
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, device_id),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "nff-dev"),
    ])
    now = datetime.datetime.utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.UnrecognizedExtension(NFF_PROJECT_OID, project_id.encode()), critical=False)
        .sign(private_key, hashes.SHA256(), default_backend())
    )
    return cert


def sign_csr_with_ca(csr, ca_cert_path: str, ca_key_path: str, device_id: str, project_id: str):
    """Sign the device CSR with the nff CA, binding project_id in a signed extension."""
    with open(ca_cert_path, "rb") as f:
        ca_cert = x509.load_pem_x509_certificate(f.read(), default_backend())
    with open(ca_key_path, "rb") as f:
        ca_key = serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())

    now = datetime.datetime.utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(csr.subject)
        .issuer_name(ca_cert.subject)
        .public_key(csr.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.UnrecognizedExtension(NFF_PROJECT_OID, project_id.encode()), critical=False)
        .sign(ca_key, hashes.SHA256(), default_backend())
    )
    return cert


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id",         required=True,        help="Device ID (CN of cert)")
    parser.add_argument("--project",    required=True,        help="Project/tenant ID (bound in signed cert; NFF_PROJECT_ID)")
    parser.add_argument("--ca",         default=None,         help="CA certificate PEM (optional)")
    parser.add_argument("--ca-key",     default=None,         help="CA private key PEM (optional)")
    parser.add_argument("--cmd-key",    default=None,         help="Fleet command-verify public key (65-byte binary)")
    parser.add_argument("--out",        default="credentials.h", help="Output path")
    parser.add_argument("--fw-version", default="1.0.0",      help="Firmware version string")
    parser.add_argument("--build-id",   default="0000000000000000", help="16-char build ID hex")
    args = parser.parse_args()

    device_id = args.id
    project_id = args.project

    # Generate device key pair
    private_key = gen_device_keypair()

    # Generate or sign certificate
    if args.ca and args.ca_key:
        csr = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, device_id)]))
            .sign(private_key, hashes.SHA256(), default_backend())
        )
        cert = sign_csr_with_ca(csr, args.ca, args.ca_key, device_id, project_id)
        with open(args.ca, "rb") as f:
            ca_cert = x509.load_pem_x509_certificate(f.read(), default_backend())
        ca_cert_der = ca_cert.public_bytes(serialization.Encoding.DER)
        print(f"Signed by CA: {args.ca}")
    else:
        # Self-signed dev certificate
        cert = gen_self_signed_cert(private_key, device_id, project_id)
        ca_cert_der = cert.public_bytes(serialization.Encoding.DER)
        print("WARNING: Self-signed certificate — NOT for production. Provide --ca and --ca-key.")

    # Serialise to DER
    cert_der = cert.public_bytes(serialization.Encoding.DER)
    key_der  = private_key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()
    )

    # Fleet command-verify key
    if args.cmd_key:
        with open(args.cmd_key, "rb") as f:
            cmd_key_bytes = f.read()
        assert len(cmd_key_bytes) == 65, f"cmd-key must be 65 bytes (0x04 || X || Y), got {len(cmd_key_bytes)}"
    else:
        # Placeholder — replace with the real fleet key in production
        cmd_key_bytes = bytes([0x04] + [0x00] * 64)
        print("WARNING: Using zero cmd-verify key. Run `nff provision export-cmd-key` and pass --cmd-key.")

    # Build output
    lines = [
        f"// credentials.h — generated by gen_credentials.py for device: {device_id}",
        f"// Add to .gitignore. Contains the device private key — treat as a secret.",
        f"// Generated: {datetime.datetime.utcnow().isoformat()}",
        "",
        "#pragma once",
        "#include <stdint.h>",
        "#include <stddef.h>",
        "",
        "// Tenant scope (bound in the signed device cert; topics namespace under it)",
        f'#define NFF_PROJECT_ID "{project_id}"',
        "",
        "// Per-device mTLS client certificate (CN = device_id, DER-encoded)",
        bytes_to_c_array("NFF_CLIENT_CERT_DER", cert_der),
        "",
        "// Per-device private key (P-256, PKCS#8 DER)",
        bytes_to_c_array("NFF_CLIENT_KEY_DER", key_der),
        "",
        "// nff CA certificate — pinned; device rejects servers not signed by this CA",
        bytes_to_c_array("NFF_CA_CERT_DER", ca_cert_der),
        "",
        "// Fleet command-verify key (P-256 public, 65-byte uncompressed point)",
        bytes_to_c_array("NFF_CMD_VERIFY_KEY_DER", cmd_key_bytes),
        "",
        "// Firmware identity (injected by build system; override here for manual builds)",
        f'#ifndef NFF_FW_VERSION',
        f'#  define NFF_FW_VERSION  "{args.fw_version}"',
        f'#endif',
        f'#ifndef NFF_BUILD_ID',
        f'#  define NFF_BUILD_ID    "{args.build_id}"',
        f'#endif',
    ]

    out_path = args.out
    with open(out_path, "w") as f:
        f.write("\n".join(lines) + "\n")

    print(f"Wrote {out_path} for device '{device_id}'")
    print(f"  Client cert: {len(cert_der)} bytes DER")
    print(f"  Private key: {len(key_der)} bytes DER")
    print(f"  CA cert:     {len(ca_cert_der)} bytes DER")
    print(f"  Cmd key:     {len(cmd_key_bytes)} bytes")


if __name__ == "__main__":
    main()
