#!/usr/bin/env python3
"""Security tests for the network layer: TLS 1.3 / mutual TLS, signed manifest, per-chunk integrity.
Run: PYTHONPATH=<project root> python3 network/test_security.py"""
import os, shutil, socket, ssl, sys, tempfile, threading, unittest
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import stream_over_tcp as so
import net_source as ns
import integrity, tls_util
import pir_network_server as srvmod
import pir_source as ps
from make_catalog import build_catalog
from player import SeekablePlayer
from pir_core import LwePIRServer, LweParams, XorPIRServer
from pir_core.sharded import LweRowShard


class Evil:
    """Delegates to a real PIR server but corrupts answers (first `n` answers, or all if n is None)."""
    def __init__(self, real, n=None, mask=1):
        self.real, self.n, self.mask, self.calls = real, n, mask, 0

    def __getattr__(self, k):
        return getattr(self.real, k)

    def answer(self, q):
        a = self.real.answer(q).copy()
        self.calls += 1
        if self.n is None or self.calls <= self.n:
            a.flat[0] ^= self.mask   # flip a bit (LWE answers need a bit above the 2^24 rounding step to matter)
        return a


def serve(scheme, records, xor_server, lwe_server, shard, port=0):
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port)); s.listen(8)
    def loop():
        while True:
            try:
                c, _ = s.accept()
            except OSError:
                return
            threading.Thread(target=srvmod.handle, args=(c, scheme, records, xor_server, lwe_server, shard), daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()
    return s, f"127.0.0.1:{s.getsockname()[1]}"


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.cat = os.path.join(cls.tmp, "cat")
        build_catalog(cls.cat, 8192, 0.2, synthetic_videos=2, chunks_per_video=8)
        cls.manifest, cls.index, cls.recs = ps.load_catalog_records(cls.cat)
        cls.certs = tls_util.make_certs(os.path.join(cls.tmp, "certs"))
        cls.priv, cls.pub = integrity.keygen(os.path.join(cls.tmp, "keys"))
        integrity.sign_manifest(cls.cat, cls.priv)
        cls.video = sorted(cls.manifest["videos"])[0]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)


class TlsTests(Base):
    def setUp(self):
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            p.kill()

    def addr(self, mtls):
        return so.launch("xor", self.cat, 1, self.procs, self.certs, mtls)[0]

    def test_tls_roundtrip_and_stream(self):
        a0, a1 = self.addr(False), so.launch("xor", self.cat, 1, self.procs, self.certs, False)[0]
        ctx = tls_util.client_context(self.certs["ca"])
        be = ns.NetXorBackend([a0], [a1], tls=ctx)
        src = ns.make_source(self.cat, be, pubkey=self.pub)
        m = SeekablePlayer(src, self.video, batch=2, target_buffer_s=1.0).run(seeks=[(2, 6)])
        self.assertEqual(m["play_log"], [0, 1, 6, 7])

    def test_plaintext_client_is_refused_by_tls_server(self):
        a = self.addr(False)
        with self.assertRaises(Exception):
            ns.request(ns.parse_address(a), {"op": "info"}, tls=None)

    def test_untrusted_server_certificate_is_rejected(self):
        a = self.addr(False)
        other = tls_util.make_certs(os.path.join(self.tmp, "other_ca"))
        with self.assertRaises(ssl.SSLCertVerificationError):
            ns.request(ns.parse_address(a), {"op": "info"}, tls=tls_util.client_context(other["ca"]))

    def test_tls12_client_is_rejected(self):
        a = self.addr(False)
        ctx = tls_util.client_context(self.certs["ca"])
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        with self.assertRaises(ssl.SSLError):
            ns.request(ns.parse_address(a), {"op": "info"}, tls=ctx)

    def test_hostname_is_checked(self):
        a = self.addr(False)
        host, port = ns.parse_address(a)
        # certificate is valid for localhost/127.0.0.1 only; claim a different name -> must fail
        raw = socket.create_connection((host, port), 5)
        with self.assertRaises(ssl.SSLCertVerificationError):
            tls_util.client_context(self.certs["ca"]).wrap_socket(raw, server_hostname="evil.example")

    def test_mutual_tls_requires_client_certificate(self):
        a = self.addr(True)
        no_cert = tls_util.client_context(self.certs["ca"])
        with self.assertRaises(Exception):
            ns.request(ns.parse_address(a), {"op": "info"}, tls=no_cert)
        other = tls_util.make_certs(os.path.join(self.tmp, "rogue"))
        rogue = tls_util.client_context(self.certs["ca"], other["client_cert"], other["client_key"])
        with self.assertRaises(Exception):
            ns.request(ns.parse_address(a), {"op": "info"}, tls=rogue)
        good = tls_util.client_context(self.certs["ca"], self.certs["client_cert"], self.certs["client_key"])
        h, _ = ns.request(ns.parse_address(a), {"op": "info"}, tls=good)
        self.assertTrue(h["ok"])


class IntegrityTests(Base):
    def setUp(self):
        self.socks = []

    def tearDown(self):
        for s in self.socks:
            s.close()

    def xor_pair(self, evil1=None):
        N, B = self.recs.shape
        shard = {"range": (0, N), "unit": "records"}
        addrs = []
        for ev in (None, evil1):
            real = XorPIRServer(self.recs)
            srv = Evil(real, ev) if ev != "no" and ev is not None else real
            s, a = serve("xor", self.recs, srv, None, shard)
            self.socks.append(s); addrs.append(a)
        return addrs

    def lwe_addr(self, evil_n="honest"):
        full = LwePIRServer(self.recs, LweParams())
        band = LweRowShard(full, 0, full.db.shape[0]).server
        srv = band if evil_n == "honest" else Evil(band, evil_n, mask=1 << 28)
        s, a = serve("lwe", self.recs, None, srv, {"range": (0, full.db.shape[0]), "unit": "db_rows"})
        self.socks.append(s)
        return a

    def play(self, src):
        return SeekablePlayer(src, self.video, batch=2, target_buffer_s=1.0).run()

    def test_honest_servers_pass(self):
        a0, a1 = self.xor_pair(None)
        src = ns.make_source(self.cat, ns.NetXorBackend([a0], [a1]), pubkey=self.pub)
        self.assertEqual(self.play(src)["play_log"], list(range(8)))
        self.assertEqual(src.integrity_failures, 0)

    def test_corrupt_xor_replica_is_detected(self):
        a0, a1 = self.xor_pair(evil1=10**9)   # corrupts every answer
        src = ns.make_source(self.cat, ns.NetXorBackend([a0], [a1]), pubkey=self.pub)
        with self.assertRaises(integrity.IntegrityError):
            self.play(src)
        self.assertGreaterEqual(src.integrity_failures, 2)       # first try + one retry

    def test_corrupt_lwe_server_is_detected(self):
        a = self.lwe_addr(evil_n=None)
        src = ns.make_source(self.cat, ns.NetLweBackend([a]), pubkey=self.pub)
        with self.assertRaises(integrity.IntegrityError):
            self.play(src)

    def test_transient_corruption_is_retried(self):
        a0, a1 = self.xor_pair(evil1=1)                          # only the first answer is corrupted
        src = ns.make_source(self.cat, ns.NetXorBackend([a0], [a1]), pubkey=self.pub)
        self.assertEqual(self.play(src)["play_log"], list(range(8)))
        self.assertEqual(src.integrity_failures, 1)

    def test_without_manifest_check_corruption_would_be_played(self):
        # the unverified source returns the wrong bytes: this is what the verification prevents
        a0, a1 = self.xor_pair(evil1=10**9)
        be = ns.NetXorBackend([a0], [a1])
        wrong = be.retrieve([0])[0]
        self.assertNotEqual(wrong, self.recs[0].tobytes())

    def test_manifest_signature(self):
        self.assertEqual(integrity.load_verified_manifest(self.cat, self.pub)["chunk_size"], 8192)
        # tampered manifest (a hash swapped by an attacker)
        bad = tempfile.mkdtemp(dir=self.tmp)
        for f in ("manifest.json", "manifest.sig"):
            shutil.copy(os.path.join(self.cat, f), bad)
        import json
        m = json.load(open(os.path.join(bad, "manifest.json")))
        m["videos"][self.video]["chunks"][0]["sha256"] = "0" * 64
        json.dump(m, open(os.path.join(bad, "manifest.json"), "w"))
        with self.assertRaises(integrity.ManifestError):
            integrity.load_verified_manifest(bad, self.pub)
        # wrong publisher key
        _, other_pub = integrity.keygen(os.path.join(self.tmp, "other_keys"))
        with self.assertRaises(integrity.ManifestError):
            integrity.load_verified_manifest(self.cat, other_pub)
        # missing signature
        os.remove(os.path.join(bad, "manifest.sig"))
        with self.assertRaises(integrity.ManifestError):
            integrity.load_verified_manifest(bad, self.pub)


if __name__ == "__main__":
    unittest.main(verbosity=2)
