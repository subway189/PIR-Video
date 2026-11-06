"""python3 -m unittest eval.test_eval -v   (from the project root)"""
import unittest
import numpy as np
from eval.metrics import CostModel, simulate_session
from eval.attacks import xor_colluding_attack, seek_features


class SimTests(unittest.TestCase):
    def test_fast_link_never_rebuffers(self):
        r = simulate_session(100, CostModel("xor", 1000, 0.001), policy=4, jitter=0, rng=np.random.default_rng(0))
        self.assertEqual(r.rebuffer_n, 0)
        self.assertEqual(r.real_chunks, 100)

    def test_slow_lwe_rebuffers(self):
        r = simulate_session(100, CostModel("lwe", 5, 0.05), policy=4, jitter=0, rng=np.random.default_rng(0))
        self.assertGreater(r.rebuffer_s, 10)

    def test_steady_state_gap_is_k_tau(self):
        r = simulate_session(200, CostModel("xor", 50, 0.02), policy=8, jitter=0, rng=np.random.default_rng(0))
        t = np.array([a for a, _ in r.trace])
        gaps = np.diff(t)[3:-2]
        self.assertTrue(np.allclose(gaps, 16.0, atol=1e-6), gaps[:5])

    def test_seek_changes_gap_pattern_buffered_only(self):
        rng = np.random.default_rng(0)
        c = CostModel("xor", 50, 0.02)
        s = simulate_session(200, c, policy=8, jitter=0.02, seeks=[(150.0, 20)], rng=rng)
        n = simulate_session(200, c, policy=8, jitter=0.02, rng=rng)
        self.assertLess(seek_features(s.trace), 0.75)
        self.assertGreater(seek_features(n.trace), 0.9)
        cs = simulate_session(200, c, mode="constant", k_const=8, jitter=0.0, seeks=[(150.0, 20)], rng=rng)
        gaps = np.diff([a for a, _ in cs.trace])
        self.assertTrue(np.allclose(gaps, 8.0, atol=1e-6))
        self.assertEqual({k for _, k in cs.trace}, {8})

    def test_bucket_padding(self):
        r = simulate_session(77, CostModel("xor", 50, 0.02), policy=8, pad_batch=True, bucket_chunks=100,
                             rng=np.random.default_rng(1))
        self.assertEqual(r.T % 100, 0)
        self.assertGreaterEqual(r.T, 77)

    def test_collusion_breaks_xor(self):
        self.assertEqual(xor_colluding_attack(N=40, trials=200)["acc"], 1.0)


if __name__ == "__main__":
    unittest.main()
