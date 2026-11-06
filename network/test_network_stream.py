#!/usr/bin/env python3
"""Player over real TCP servers, unsharded and sharded (XOR: 2 replicas x S shards; LWE: S shards).
Chunks are SHA-256 verified inside the player; asserts the exact play sequence incl. seeks and adaptive batching.
Run: PYTHONPATH=<project root> python3 network/test_network_stream.py"""
import os, sys, tempfile, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import stream_over_tcp as so
import net_source as ns
from make_catalog import build_catalog
from player import SeekablePlayer


class NetworkStream(unittest.TestCase):
    def test_sharded_and_unsharded(self):
        procs = []
        with tempfile.TemporaryDirectory() as tmp:
            cat = os.path.join(tmp, "cat")
            build_catalog(cat, 16384, 0.25, synthetic_videos=2, chunks_per_video=12)
            try:
                for S in (1, 3):
                    xor = ns.NetXorBackend(so.launch("xor", cat, S, procs), so.launch("xor", cat, S, procs))
                    lwe = ns.NetLweBackend(so.launch("lwe", cat, S, procs))
                    for be in (xor, lwe):
                        src = ns.make_source(cat, be)
                        for batch in ("adaptive", 3):
                            m = SeekablePlayer(src, sorted(src.manifest["videos"])[1], batch=batch,
                                               target_buffer_s=1.0).run(seeks=[(3, 9), (5, 1)])
                            self.assertEqual(m["play_log"][:3], [0, 1, 2], (S, be.name))
                            self.assertEqual(m["play_log"][3:5], [9, 10])
                            self.assertEqual(m["play_log"][5:], list(range(1, 12)))
                    self.assertGreater(lwe.offline["hint_bytes"], 0)
            finally:
                for p in procs:
                    p.kill()


if __name__ == "__main__":
    unittest.main(verbosity=2)
