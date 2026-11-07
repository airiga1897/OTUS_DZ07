"""Existing certificate algorithms are checked without rotating private material."""

import datetime
from pathlib import Path

import elk_tls
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import dsa, ec, ed25519, rsa
from cryptography.x509.oid import NameOID


def encode(key: elk_tls.SigningKey | ed25519.Ed25519PrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


@pytest.mark.parametrize("algorithm", ["rsa", "dsa", "ec"])
def test_existing_signing_key_is_preserved(tmp_path: Path, algorithm: str) -> None:
    key: elk_tls.SigningKey
    if algorithm == "rsa":
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    elif algorithm == "dsa":
        key = dsa.generate_private_key(key_size=2048)
    else:
        key = ec.generate_private_key(ec.SECP256R1())
    path = tmp_path / "ca.key"
    material = encode(key)
    path.write_bytes(material)
    loaded = elk_tls._signing_key(path)
    assert encode(loaded) == material
    assert path.read_bytes() == material


def test_unsupported_ca_does_not_rotate_keys_or_create_node_files(tmp_path: Path) -> None:
    folder = tmp_path / "certs"
    folder.mkdir()
    original = encode(ed25519.Ed25519PrivateKey.generate())
    (folder / "ca.key").write_bytes(original)
    (folder / "ca.crt").write_bytes(b"untouched existing certificate")
    with pytest.raises(RuntimeError, match="Unsupported existing CA key"):
        elk_tls.certificates(tmp_path, {"es1": "10.77.0.31"})
    assert (folder / "ca.key").read_bytes() == original
    assert (folder / "ca.crt").read_bytes() == b"untouched existing certificate"
    assert sorted(path.name for path in folder.iterdir()) == ["ca.crt", "ca.key"]


def test_existing_ecdsa_ca_can_sign_and_reuse_rsa_nodes(tmp_path: Path) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic CA")])
    now = datetime.datetime.now(datetime.UTC)
    ca = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    folder = tmp_path / "certs"
    folder.mkdir()
    (folder / "ca.key").write_bytes(encode(key))
    (folder / "ca.crt").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    elk_tls.certificates(tmp_path, {"es1": "10.77.0.31"})
    original = {p.name: p.read_bytes() for p in folder.iterdir()}
    elk_tls.certificates(tmp_path, {"es1": "10.77.0.31", "es4": "10.77.0.34"})
    assert all((folder / name).read_bytes() == content for name, content in original.items())
    assert (folder / "es4.crt").is_file()
