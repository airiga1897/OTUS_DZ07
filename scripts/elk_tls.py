import datetime
import ipaddress
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

def certificates(local, addresses):
    certdir = local / 'certs'
    if certdir.exists():
        for name in ['ca.crt', 'ca.key']:
            if not (certdir / name).exists():
                raise RuntimeError('Incomplete certificate set; do not silently rotate CA')
        key = serialization.load_pem_private_key((certdir / 'ca.key').read_bytes(), password=None)
        ca = x509.load_pem_x509_certificate((certdir / 'ca.crt').read_bytes())
        if key.public_key().public_numbers() != ca.public_key().public_numbers():
            raise RuntimeError('CA key mismatch')
    else:
        certdir.mkdir()
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'OTUS DZ07 CA')])
        now = datetime.datetime.now(datetime.timezone.utc)
        ca = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
              .public_key(key.public_key()).serial_number(x509.random_serial_number())
              .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=365))
              .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
              .sign(key, hashes.SHA256()))
    now = datetime.datetime.now(datetime.timezone.utc)
    subject = ca.subject
    def write_key(path, private):
        path.write_bytes(private.private_bytes(serialization.Encoding.PEM,
                         serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    if not (certdir / 'ca.key').exists():
        write_key(certdir / 'ca.key', key)
        (certdir / 'ca.crt').write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    for name, ip in addresses.items():
        cert_path, key_path = certdir / (name + '.crt'), certdir / (name + '.key')
        if cert_path.exists() or key_path.exists():
            if not cert_path.exists() or not key_path.exists():
                raise RuntimeError('Incomplete node certificate pair')
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            private = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
            cert.verify_directly_issued_by(ca)
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            if (ipaddress.ip_address(ip) not in san.get_values_for_type(x509.IPAddress)
                    or name not in san.get_values_for_type(x509.DNSName)
                    or private.public_key().public_numbers() != cert.public_key().public_numbers()):
                raise RuntimeError('Existing node certificate identity changed')
            continue
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
                .issuer_name(subject).public_key(private.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=180))
                .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(ip)), x509.DNSName(name)]), False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
                .sign(key, hashes.SHA256()))
        write_key(certdir / (name + '.key'), private)
        (certdir / (name + '.crt')).write_bytes(cert.public_bytes(serialization.Encoding.PEM))

