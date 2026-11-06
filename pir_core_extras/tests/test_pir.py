"""Run with:  python3 -m unittest discover -s tests -v   (from the pir_core/ directory)"""
import unittest

import numpy as np

from pir_core import (LwePIR, LwePIRClient, LwePIRServer, Layout, LweParams, XorPIR,
                      XorPIRClient, XorPIRServer, noise_margin_sigmas)
from pir_core.lwe_pir import a_blocks, MASK

rng = np.random.default_rng(1234)


def db(N, B):
    return rng.integers(0, 256, (N, B), dtype=np.uint8)


class XorTests(unittest.TestCase):
    def test_every_index_small_db(self):
        for N, B in [(1, 1), (2, 3), (7, 5), (16, 8), (33, 13), (100, 64)]:
            r = db(N, B)
            for scan in ("naive", "optimized"):
                p = XorPIR(scan)
                p.setup(r)
                for i in range(N):
                    np.testing.assert_array_equal(p.retrieve([i])[0], r[i], f"N={N} B={B} i={i}")

    def test_batch_with_duplicates_and_naive_equals_optimized(self):
        r = db(1000, 100)
        idx = [0, 999, 500, 500, 7, 123]
        outs = []
        for scan in ("naive", "optimized"):
            p = XorPIR(scan)
            p.setup(r)
            outs.append(p.retrieve(idx))
            np.testing.assert_array_equal(outs[-1], r[idx])
        # server answers themselves must agree, not only the final XOR
        srv = XorPIRServer(r)
        q = XorPIRClient(1000, 100).query(idx)[1][0]
        np.testing.assert_array_equal(srv.answer(q, "naive"), srv.answer(q, "optimized"))

    def test_larger_batch_crosses_cache_blocks(self):
        r = db(5000, 300)   # several 512KB blocks worth of rows is not needed; many row blocks
        p = XorPIR()
        p.setup(r)
        idx = list(rng.integers(0, 5000, 64))
        np.testing.assert_array_equal(p.retrieve(idx), r[idx])

    def test_privacy_each_query_is_uniform_and_shares_xor_to_unit_vector(self):
        N, trials = 64, 4000
        c = XorPIRClient(N, 4)
        counts = {0: np.zeros(N), 1: np.zeros(N)}
        for target in (3, 40):
            ones = np.zeros(N)
            for _ in range(trials // 2):
                _, (q0, q1) = c.query([target])
                b0 = np.unpackbits(q0, axis=1, count=N)[0]
                b1 = np.unpackbits(q1, axis=1, count=N)[0]
                d = b0 ^ b1
                self.assertEqual(d.sum(), 1)
                self.assertEqual(d[target], 1)
                ones += b1
            # server 1's view: every coordinate (including the target's) is a fair coin
            freq = ones / (trials // 2)
            self.assertLess(np.abs(freq - 0.5).max(), 0.06, f"target {target} biased: {freq.max():.3f}")

    def test_padding_bits_are_zero(self):
        c = XorPIRClient(10, 4)
        _, (q0, q1) = c.query([9])
        self.assertEqual(q0[0, -1] & 0b00111111, 0)
        self.assertEqual(q1[0, -1] & 0b00111111, 0)

    def test_bad_input(self):
        p = XorPIR()
        p.setup(db(4, 4))
        with self.assertRaises(IndexError):
            p.retrieve([4])
        with self.assertRaises(ValueError):
            p.retrieve([])


class LweTests(unittest.TestCase):
    SMALL = LweParams(n=64)   # small n only to keep unit tests fast; benchmarks use n=1024

    def _roundtrip(self, N, B, rows=None, idx=None, params=None, scan="optimized"):
        r = db(N, B)
        p = LwePIR(params or self.SMALL, rows, scan)
        p.setup(r)
        idx = idx if idx is not None else list(range(N))
        np.testing.assert_array_equal(p.retrieve(idx), r[idx], f"N={N} B={B} rows={rows}")
        return p

    def test_layout_span1_stacked_records(self):
        lay = Layout.choose(100, 10)           # sqrt(1000)=32 -> 3 records per column
        self.assertEqual((lay.per_col, lay.span, lay.rows), (3, 1, 30))
        self._roundtrip(100, 10)

    def test_layout_one_record_per_column(self):
        self._roundtrip(50, 40, rows=40)

    def test_layout_record_spans_several_columns(self):
        lay = Layout.choose(20, 100, rows=30)
        self.assertEqual(lay.span, 4)
        self._roundtrip(20, 100, rows=30)
        self._roundtrip(7, 33, rows=8)          # B not a multiple of rows -> padding

    def test_odd_sizes_and_default_layout(self):
        for N, B in [(1, 1), (3, 5), (37, 11), (200, 64)]:
            self._roundtrip(N, B)

    def test_batch_with_duplicates(self):
        self._roundtrip(300, 20, idx=[5, 299, 5, 0, 150])

    def test_naive_equals_optimized_server_answers_and_hint(self):
        r = db(80, 16)
        srv = LwePIRServer(r, self.SMALL, scan="optimized")
        # hint via BLAS limbs == plain uint32 matmul against the same A
        A = np.concatenate([b for _, _, b in a_blocks(self.SMALL, srv.layout.cols)])
        ref = (srv.db.astype(np.uint32) @ A)
        np.testing.assert_array_equal(srv.hint(), ref)
        cl = LwePIRClient(80, 16, srv.hint(), self.SMALL)
        _, Q = cl.query([3, 70, 3])
        np.testing.assert_array_equal(srv.answer(Q, "naive"), srv.answer(Q, "optimized"))

    def test_naive_backend_roundtrip(self):
        self._roundtrip(60, 12, scan="naive")

    def test_full_size_params_n1024(self):
        p = self._roundtrip(256, 64, idx=[0, 17, 255], params=LweParams())
        self.assertGreater(noise_margin_sigmas(p.server.layout.cols), 20)

    def test_decryption_noise_far_below_threshold(self):
        r = db(2000, 32)
        p = LwePIR(LweParams(n=256))
        p.setup(r)
        p.retrieve(list(range(0, 2000, 7)))
        self.assertLess(p.client.last_noise_max, (1 << 24) / 2 / 4)   # < 1/4 of the budget

    def test_different_queries_for_same_index(self):
        r = db(50, 8)
        p = LwePIR(self.SMALL)
        p.setup(r)
        _, Q1 = p.client.query([7])
        _, Q2 = p.client.query([7])
        self.assertFalse(np.array_equal(Q1, Q2))   # fresh randomness every time

    def test_query_looks_uniform(self):
        # Statistical sanity check only (the real guarantee is LWE hardness). Over many
        # fresh queries for the same index, the high bit of every query word -- including
        # the entry in the target column -- should be an unbiased coin. Bounds are ~4 sigma.
        r = db(400, 8)
        p = LwePIR(self.SMALL)
        p.setup(r)
        T = 400
        _, Q = p.client.query([399] * T)                 # (m, T): T independent queries
        hi = (Q >> 31).astype(float)
        self.assertLess(abs(hi.mean() - 0.5), 4 * 0.5 / np.sqrt(hi.size))
        col = p.server.layout.columns_for(399)[0]
        self.assertLess(abs(hi[col].mean() - 0.5), 4 * 0.5 / np.sqrt(T))
        other = p.server.layout.columns_for(0)[0]
        self.assertLess(abs(hi[other].mean() - 0.5), 4 * 0.5 / np.sqrt(T))

    def test_noise_margin_rejects_huge_catalogs(self):
        self.assertLess(noise_margin_sigmas(2 ** 40), 6)

    def test_bad_input(self):
        p = LwePIR(self.SMALL)
        p.setup(db(4, 4))
        with self.assertRaises(IndexError):
            p.retrieve([-1])


class InterfaceTests(unittest.TestCase):
    def test_both_backends_same_interface_and_stats(self):
        r = db(120, 24)
        for b in (XorPIR(), LwePIR(LweParams(n=64))):
            b.setup(r)
            out = b.retrieve([1, 2, 3])
            np.testing.assert_array_equal(out, r[[1, 2, 3]])
            s = b.last_stats
            self.assertEqual(s.batch, 3)
            self.assertGreater(s.bytes_up, 0)
            self.assertGreater(s.bytes_down, 0)
            self.assertGreater(s.t_compute_latency, 0)


if __name__ == "__main__":
    unittest.main()
