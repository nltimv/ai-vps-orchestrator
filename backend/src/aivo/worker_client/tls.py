"""Per-machine TLS credentials.

For every new machine we mint a throw-away CA, sign one leaf certificate for the
synthetic name ``i-<instance-uuid>.worker.aivo.invalid`` and discard the CA's
private key. The backend pins exactly that CA and sends the synthetic name as
SNI, so if a destroyed machine's public IP is recycled to someone else, the TLS
handshake fails and no prompt or token is ever sent there.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


@dataclass(frozen=True)
class WorkerCredentials:
    ca_pem: str
    cert_pem: str
    key_pem: str
    sni_name: str


def sni_for(instance_id: uuid.UUID) -> str:
    return f"i-{instance_id}.worker.aivo.invalid"


def make_worker_credentials(instance_id: uuid.UUID, valid_days: int = 7) -> WorkerCredentials:
    now = dt.datetime.now(dt.UTC)
    sni = sni_for(instance_id)

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"aivo ephemeral CA {instance_id}")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=valid_days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, sni)]))
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=valid_days))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(sni)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    del ca_key  # never stored anywhere

    return WorkerCredentials(
        ca_pem=ca_cert.public_bytes(serialization.Encoding.PEM).decode(),
        cert_pem=leaf_cert.public_bytes(serialization.Encoding.PEM).decode(),
        key_pem=leaf_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode(),
        sni_name=sni,
    )
