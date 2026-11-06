"""TLS helpers for the PIR network layer (TLS 1.3 only; optional mutual authentication).

make_certs(dir) writes a throw-away CA, a server certificate (SAN: localhost, 127.0.0.1, ::1) and a client
certificate, for the lab/demo. In a real deployment use certificates from your own CA and keep each XOR
replica under a different operator with its own key; mutual TLS then also authenticates clients.
Needs the `cryptography` package (pip install cryptography).
"""
import datetime
import ipaddress
import os
import ssl


def server_context(cert, key, client_ca=None):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(cert, key)
    if client_ca:                      # mutual TLS: refuse clients without a certificate signed by this CA
        ctx.load_verify_locations(client_ca)
        ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def client_context(ca, cert=None, key=None):
    """Verifies the server certificate against `ca` and checks the host name (no insecure fallbacks)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_verify_locations(ca)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    if cert:
        ctx.load_cert_chain(cert, key)
    return ctx


def make_certs(out_dir, name="pir-demo"):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID

    os.makedirs(out_dir, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc)

    def key():
        return ec.generate_private_key(ec.SECP256R1())

    def subj(cn):
        return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])

    def write_key(k, path):
        with open(path, "wb") as f:
            f.write(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
        os.chmod(path, 0o600)

    def write_cert(c, path):
        with open(path, "wb") as f:
            f.write(c.public_bytes(serialization.Encoding.PEM))

    def build(cn, pub, issuer_name, issuer_key, ca=False, san=None, eku=None, days=30):
        b = (x509.CertificateBuilder().subject_name(subj(cn)).issuer_name(issuer_name).public_key(pub)
             .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
             .not_valid_after(now + datetime.timedelta(days=days))
             .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
        if san:
            b = b.add_extension(x509.SubjectAlternativeName(san), critical=False)
        if eku:
            b = b.add_extension(x509.ExtendedKeyUsage([eku]), critical=False)
        return b.sign(issuer_key, hashes.SHA256())

    ca_key = key()
    ca = build(f"{name} CA", ca_key.public_key(), subj(f"{name} CA"), ca_key, ca=True, days=60)
    sk = key()
    san = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
           x509.IPAddress(ipaddress.ip_address("::1"))]
    server = build("localhost", sk.public_key(), ca.subject, ca_key, san=san, eku=ExtendedKeyUsageOID.SERVER_AUTH)
    ck = key()
    client = build(f"{name} client", ck.public_key(), ca.subject, ca_key, eku=ExtendedKeyUsageOID.CLIENT_AUTH)
    p = lambda f: os.path.join(out_dir, f)  # noqa: E731
    write_cert(ca, p("ca.pem"))
    write_cert(server, p("server.pem")); write_key(sk, p("server.key"))
    write_cert(client, p("client.pem")); write_key(ck, p("client.key"))
    return {k: p(v) for k, v in dict(ca="ca.pem", cert="server.pem", key="server.key",
                                     client_cert="client.pem", client_key="client.key").items()}


if __name__ == "__main__":
    import sys
    print(make_certs(sys.argv[1] if len(sys.argv) > 1 else "certs"))
