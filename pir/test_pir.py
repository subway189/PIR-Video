#!/usr/bin/env python3
"""Correctness + sanity tests for the PIR schemes.  Run:  python3 test_pir.py

NOTE on the "privacy" tests: they are statistical sanity checks that the queries have no
obvious structure. They are not a proof; the guarantees come from the schemes (information-
theoretic for the XOR scheme, LWE for the lattice one)."""
import math
import os
import tempfile
import unittest

import numpy as np

import kernels
import lattice_pir as lp
import pir_source as ps
import two_server_pir as tp


def rand_records(n, r, seed=0):
    return np.random.default_rng(seed).integers(0, 256, size=(n, r), dtype=np.uint8)


class KernelTests(unittest.TestCase):
    def _both(self, fn, *args):
        a = fn(*args)
        saved = kernels.HAVE_C
        kernels.HAVE_C = False
        try:
            b = fn(*args)
        finally:
            kernels.HAVE_C = saved
        return a, b

    def test_xor_select_c_matches_numpy(self):
        rng = np.random.default_rng(1)
        db = rng.integers(0, 2**63, size=(70, 33), dtype=np.uint64)
        masks = rng.integers(0, 256, size=(5, 9), dtype=np.uint8)
        a, b = self._both(kernels.xor_select_batch, db, masks)
        self.assertTrue(np.array_equal(a, b))

    def test_lwe_answer_matches_numpy_and_exact_integers(self):
        rng = np.random.default_rng(2)
        D = rng.integers(0, 256, size=(100, 9001), dtype=np.uint8)  # 9001 > one 8192 column block
        Q = rng.integers(0, 2**32, size=(3, 9001), dtype=np.uint32)
        a, b = self._both(kernels.lwe_answer_batch, D, Q)
        exact = (D.astype(object) @ Q.astype(object).T) % 2**32
        self.assertTrue(np.array_equal(a, b))
        self.assertTrue(np.array_equal(a.T.astype(object), exact))

    def test_u32_matvec_matches_numpy_and_exact_integers(self):
        rng = np.random.default_rng(3)
        M = rng.integers(0, 2**32, size=(40, 256), dtype=np.uint32)
        V = rng.integers(0, 2**32, size=(4, 256), dtype=np.uint32)
        a, b = self._both(kernels.u32_matvec_batch, M, V)
        exact = (M.astype(object) @ V.astype(object).T) % 2**32
        self.assertTrue(np.array_equal(a, b))
        self.assertTrue(np.array_equal(a.T.astype(object), exact))


class XorPIRTests(unittest.TestCase):
    def test_every_record_single_and_batched(self):
        for n, r in [(1, 8), (37, 104), (37, 100), (64, 4096), (130, 33)]:
            db = tp.XorPIRDatabase(rand_records(n, r, seed=n))
            s1, s2 = tp.XorPIRServer(db), tp.XorPIRServer(db)
            c = tp.XorPIRClient(n, r)
            for i in range(n):
                self.assertEqual(tp.retrieve(c, s1, s2, [i])[0].tobytes(), db.record(i))
            idx = [n - 1, 0, n // 2, 0]  # includes duplicates
            out = tp.retrieve(c, s1, s2, idx)
            for j, i in enumerate(idx):
                self.assertEqual(out[j].tobytes(), db.record(i))

    def test_query_structure_and_uniformity(self):
        n = 100
        c = tp.XorPIRClient(n, 16)
        target = 37
        ones1 = ones2 = 0
        trials = 2000
        for _ in range(trials):
            q1, q2 = c.make_queries([target])
            diff = np.unpackbits(q1 ^ q2, bitorder="little")[:n]
            self.assertEqual(int(diff.sum()), 1)       # the two queries differ in exactly one position...
            self.assertEqual(int(diff[target]), 1)     # ...which is the target
            ones1 += int(np.unpackbits(q1, bitorder="little")[target])
            ones2 += int(np.unpackbits(q2, bitorder="little")[target])
        sd = math.sqrt(trials * 0.25)
        # each server, on its own, sees the target bit as a fair coin
        self.assertLess(abs(ones1 - trials / 2), 5 * sd)
        self.assertLess(abs(ones2 - trials / 2), 5 * sd)

    def test_out_of_range(self):
        c = tp.XorPIRClient(10, 8)
        with self.assertRaises(IndexError):
            c.make_queries([10])


class LatticePIRTests(unittest.TestCase):
    def _roundtrip(self, n, r, k, lwe_n=1024, seed=0):
        recs = rand_records(n, r, seed)
        srv = lp.LatticePIRServer(recs, k=k, lwe_n=lwe_n)
        cli = lp.LatticePIRClient(srv.params, srv.setup())
        out = lp.retrieve(cli, srv, list(range(n)))
        self.assertTrue(np.array_equal(out, recs), f"mismatch n={n} r={r} k={k}")
        return recs, srv, cli

    def test_layouts(self):
        for n, r, k in [(20, 1000, 1), (20, 1000, 4), (13, 777, 3), (7, 64, 8), (1, 33, 2), (50, 512, 16)]:
            self._roundtrip(n, r, k, seed=n + r)

    def test_batch_with_duplicates_and_order(self):
        recs, srv, cli = self._roundtrip(30, 500, 5)
        idx = [29, 0, 14, 14, 3]
        out = lp.retrieve(cli, srv, idx)
        self.assertTrue(np.array_equal(out, recs[idx]))

    def test_wrong_hint_does_not_decode(self):
        recs, srv, cli = self._roundtrip(10, 300, 2)
        bad = lp.LatticePIRClient(srv.params, srv.hint ^ np.uint32(1 << 20))
        self.assertFalse(np.array_equal(lp.retrieve(bad, srv, [4]), recs[[4]]))

    def test_independent_client_regenerates_same_A(self):
        # the client only ever receives (params incl. seed, hint): A must expand identically
        recs = rand_records(25, 400, 9)
        srv = lp.LatticePIRServer(recs, k=4)
        hint = srv.setup()
        cli = lp.LatticePIRClient(srv.params, hint.copy(), cache_A_bytes=0)  # streaming-A path
        self.assertTrue(np.array_equal(lp.retrieve(cli, srv, [0, 24, 11]), recs[[0, 24, 11]]))

    def test_noise_is_far_below_the_decoding_threshold(self):
        recs = rand_records(40, 600, 4)
        srv = lp.LatticePIRServer(recs, k=3)
        cli = lp.LatticePIRClient(srv.params, srv.setup())
        idx = list(range(40))
        Q, st = cli.make_queries(idx)
        ans = srv.answer(Q)
        diff = ans - kernels.u32_matvec_batch(cli.hint, st["S"])
        # remove the encoded bytes: what is left is exactly D*e (as signed ints)
        rounded = (diff + np.uint32(lp.DELTA // 2)) >> np.uint32(24)
        resid = (diff - (rounded << np.uint32(24))).view(np.int32)
        self.assertLess(int(np.abs(resid.astype(np.int64)).max()), lp.DELTA // 2 // 20)
        self.assertGreater(srv.params.noise_margin_sigmas, 50)

    def test_query_coordinates_look_uniform_even_at_the_target(self):
        recs = rand_records(30, 200, 6)
        srv = lp.LatticePIRServer(recs, k=1)
        cli = lp.LatticePIRClient(srv.params, srv.setup())
        target, trials = 17, 400
        top = np.empty(trials, dtype=np.float64)
        other = np.empty(trials, dtype=np.float64)
        for t in range(trials):
            Q, _ = cli.make_queries([target])
            top[t] = Q[0, target] >> 24
            other[t] = Q[0, (target + 5) % 30] >> 24
        sd_mean = 73.9 / math.sqrt(trials)  # std of a uniform byte is ~73.9
        self.assertLess(abs(top.mean() - 127.5), 5 * sd_mean)
        self.assertLess(abs(other.mean() - 127.5), 5 * sd_mean)

    def test_reusing_the_secret_would_leak(self):
        # Why every query needs a fresh s: with a shared s the *difference* of two queries is
        # Delta*(u_c1 - u_c2) + small noise, which exposes both targets immediately.
        recs = rand_records(30, 200, 7)
        srv = lp.LatticePIRServer(recs, k=1)
        cli = lp.LatticePIRClient(srv.params, srv.setup())
        s = np.frombuffer(os.urandom(srv.params.lwe_n * 4), dtype=np.uint32).reshape(1, -1)
        As = np.concatenate([kernels.u32_matvec_batch(A, s) for A in cli._A_blocks()], axis=1)[0]
        c1, c2 = 5, 21
        q1 = As + lp._gaussian_noise((srv.params.m,))
        q1[c1] += np.uint32(lp.DELTA)
        q2 = As + lp._gaussian_noise((srv.params.m,))
        q2[c2] += np.uint32(lp.DELTA)
        d = (q1 - q2).view(np.int32)
        big = np.flatnonzero(np.abs(d.astype(np.int64)) > lp.DELTA // 2)
        self.assertEqual(set(big.tolist()), {c1, c2})

    def test_choose_layout_and_guard(self):
        k, l, m = lp.choose_layout(100, 262144, 1024, 64 << 20)
        self.assertLessEqual(l * 1024 * 4, 64 << 20)
        self.assertEqual((k, m), (k, 100 * k))
        self.assertEqual(lp.choose_layout(10, 1000, 1024, 1 << 30)[0], 1)
        # worst case: every byte 0xFF and a huge m push |D*e| past Delta/2, so the constructor must refuse
        recs = np.full((2**20, 8), 255, dtype=np.uint8)
        with self.assertRaises(ValueError):
            lp.LatticePIRServer(recs, k=8)

    def test_out_of_range(self):
        recs = rand_records(5, 64, 1)
        srv = lp.LatticePIRServer(recs, k=1)
        cli = lp.LatticePIRClient(srv.params, srv.setup())
        with self.assertRaises(IndexError):
            cli.make_queries([5])


class SourceAdapterTests(unittest.TestCase):
    def test_backends_return_the_catalog_bytes(self):
        import hashlib
        import json
        with tempfile.TemporaryDirectory() as tmp:
            cat = os.path.join(tmp, "cat")
            R, chunks = 4096, 6
            videos = {}
            for v in ("va", "vb"):
                os.makedirs(os.path.join(cat, v))
                ch = []
                for i in range(chunks):
                    data = np.random.default_rng(hash((v, i)) % 2**32).integers(0, 256, R, dtype=np.uint8).tobytes()
                    with open(ps.chunk_file(cat, v, i), "wb") as fh:
                        fh.write(data)
                    ch.append({"len": R, "sha256": hashlib.sha256(data).hexdigest()})
                videos[v] = {"num_chunks": chunks, "chunks": ch}
            with open(os.path.join(cat, "manifest.json"), "w") as fh:
                json.dump({"chunk_size": R, "chunk_seconds": 1.0, "videos": videos}, fh)
            manifest, index, recs = ps.load_catalog_records(cat)
            for backend in (ps.XorBackend(recs), ps.LatticeBackend(recs, max_hint_bytes=1 << 20)):
                src = ps.PIRChunkSource(manifest, index, backend)
                for v in videos:
                    for i in range(chunks):
                        rec = src.fetch(v, i)
                        self.assertEqual(hashlib.sha256(rec).hexdigest(), videos[v]["chunks"][i]["sha256"])
                batch = src.fetch_batch("vb", [5, 0, 3])
                self.assertEqual([hashlib.sha256(b).hexdigest() for b in batch],
                                 [videos["vb"]["chunks"][i]["sha256"] for i in (5, 0, 3)])
                self.assertEqual(backend.stats.as_dict()["rounds"], 2 * chunks + 1)


if __name__ == "__main__":
    print("kernels:", kernels.BUILD_NOTE)
    unittest.main(verbosity=2)
