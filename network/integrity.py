"""Integrity for the PIR video stack.

Layers (what each one stops):
  1. TLS (tls_util.py)        : tampering/eavesdropping on the wire; server authentication (+ client auth with mTLS).
  2. Signed manifest          : the manifest holds the SHA-256 of every padded chunk record. It is public, so it is
                                signed (Ed25519) by the publisher; clients refuse a manifest whose signature does
                                not verify. A malicious PIR server cannot forge a manifest.
  3. Per-chunk verification   : every retrieved record is hashed and compared with the signed manifest; a server
                                that returns a wrong PIR answer (either XOR replica, or the LWE server) yields a
                                record whose hash does not match -> IntegrityError, never silently played.
What it does NOT do: say *which* XOR replica cheated (needs >= 3 replicas or per-answer proofs), prevent a server
from refusing to answer (availability), or authenticate that the publisher's key is the right one (distribute
the public key out of band).

CLI:  python3 integrity.py keygen KEYDIR | sign CATALOG_DIR KEYDIR/manifest_ed25519.key | verify CATALOG_DIR PUB.pem
"""
import hashlib
import json
import os
import sys

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class IntegrityError(ValueError):
    """A retrieved chunk does not match the signed manifest."""


class ServerUnavailable(RuntimeError):
    """Too many servers are down/unresponsive/misbehaving to produce a verified answer."""


class ManifestError(ValueError):
    """Manifest missing its signature, or the signature does not verify."""


def canonical(manifest) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()


def keygen(key_dir):
    os.makedirs(key_dir, exist_ok=True)
    k = Ed25519PrivateKey.generate()
    priv, pub = os.path.join(key_dir, "manifest_ed25519.key"), os.path.join(key_dir, "manifest_ed25519.pub")
    with open(priv, "wb") as f:
        f.write(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    os.chmod(priv, 0o600)
    with open(pub, "wb") as f:
        f.write(k.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    return priv, pub


def sign_manifest(catalog_dir, priv_path):
    with open(os.path.join(catalog_dir, "manifest.json")) as f:
        manifest = json.load(f)
    with open(priv_path, "rb") as f:
        k = serialization.load_pem_private_key(f.read(), None)
    sig = k.sign(canonical(manifest))
    with open(os.path.join(catalog_dir, "manifest.sig"), "wb") as f:
        f.write(sig)
    return sig


def verify_manifest(manifest, sig: bytes, pub_path):
    with open(pub_path, "rb") as f:
        pub = serialization.load_pem_public_key(f.read())
    try:
        pub.verify(sig, canonical(manifest))
    except InvalidSignature:
        raise ManifestError("manifest signature does not verify") from None


def load_verified_manifest(catalog_dir, pub_path):
    with open(os.path.join(catalog_dir, "manifest.json")) as f:
        manifest = json.load(f)
    sig_path = os.path.join(catalog_dir, "manifest.sig")
    if not os.path.exists(sig_path):
        raise ManifestError("manifest.sig is missing")
    with open(sig_path, "rb") as f:
        verify_manifest(manifest, f.read(), pub_path)
    return manifest


def expected_hashes(manifest):
    """Global-index-ordered list of (video, chunk_idx, sha256 of the padded record)."""
    return [(vid, i, c["sha256"]) for vid, v in manifest["videos"].items() for i, c in enumerate(v["chunks"])]


def check_record(record: bytes, video, idx, sha256_hex):
    if hashlib.sha256(record).hexdigest() != sha256_hex:
        raise IntegrityError(f"integrity check failed for {video} chunk {idx}: "
                             "a PIR server returned a wrong or tampered answer")


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) == 2 and a[0] == "keygen":
        print(keygen(a[1]))
    elif len(a) == 3 and a[0] == "sign":
        sign_manifest(a[1], a[2]); print("signed", a[1])
    elif len(a) == 3 and a[0] == "verify":
        load_verified_manifest(a[1], a[2]); print("manifest signature OK")
    else:
        print(__doc__)
