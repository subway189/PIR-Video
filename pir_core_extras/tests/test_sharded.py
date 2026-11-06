import unittest
import numpy as np
from pir_core import (XorPIRClient, XorPIRServer, LwePIRClient, LwePIRServer,
                      ShardedXorServer, ShardedLweServer)


class ShardedTests(unittest.TestCase):
    def test_xor_sharded_equals_unsharded_and_decodes(self):
        rng = np.random.default_rng(1)
        for N, B, S in [(50, 100, 1), (50, 100, 3), (67, 31, 4), (9, 8, 5), (200, 64, 7)]:
            recs = rng.integers(0, 256, (N, B), dtype=np.uint8)
            full, sh = XorPIRServer(recs), ShardedXorServer(recs, S)
            cli = XorPIRClient(N, B)
            idx = [0, N - 1, N // 2]
            state, (q0, q1) = cli.query(idx)
            for q in (q0, q1):
                self.assertTrue(np.array_equal(full.answer(q), sh.answer(q)), (N, B, S))
            out = cli.decode(state, [sh.answer(q0), sh.answer(q1)])
            self.assertTrue(np.array_equal(out, recs[idx]))

    def test_xor_shard_gets_only_its_slice_uniform(self):
        recs = np.zeros((64, 16), dtype=np.uint8)
        sh = ShardedXorServer(recs, 4)
        self.assertEqual([(s.a, s.b) for s in sh.shards], [(0, 16), (16, 32), (32, 48), (48, 64)])

    def test_lwe_sharded_hint_answer_and_decode(self):
        rng = np.random.default_rng(2)
        for N, B, S in [(40, 300, 1), (40, 300, 3), (97, 128, 4)]:
            recs = rng.integers(0, 256, (N, B), dtype=np.uint8)
            full, sh = LwePIRServer(recs), ShardedLweServer(recs, S)
            self.assertTrue(np.array_equal(full.hint(), sh.hint()))
            cli = LwePIRClient(N, B, sh.hint())
            idx = [1, N - 1, N // 2]
            state, Q = cli.query(idx)
            self.assertTrue(np.array_equal(full.answer(Q), sh.answer(Q)))
            self.assertTrue(np.array_equal(cli.decode(state, sh.answer(Q)), recs[idx]))


if __name__ == "__main__":
    unittest.main()
