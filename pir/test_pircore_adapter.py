#!/usr/bin/env python3
"""Integration test: the team's pir_core backends (XorPIR, LwePIR) drive the seekable player over a
real baseline-format catalog; every chunk is SHA-256 verified inside the player.
Run from the project root:  PYTHONPATH=. python3 -m unittest discover -s pir -p "test_pircore*.py" """
import hashlib, json, os, sys, tempfile, unittest
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, ".."))
import pir_source as ps
import player as pl
from pircore_backend import PirCoreBackend
from pir_core import LwePIR, XorPIR


class PirCoreIntegration(unittest.TestCase):
    def test_pir_core_backends_play_and_seek(self):
        with tempfile.TemporaryDirectory() as tmp:
            cat, R, n = os.path.join(tmp, "cat"), 2048, 14
            os.makedirs(os.path.join(cat, "v"))
            ch = []
            for i in range(n):
                d = np.random.default_rng(i).integers(0, 256, R, dtype=np.uint8).tobytes()
                open(ps.chunk_file(cat, "v", i), "wb").write(d)
                ch.append({"len": R, "sha256": hashlib.sha256(d).hexdigest()})
            json.dump({"chunk_size": R, "chunk_seconds": 0.03, "videos": {"v": {"num_chunks": n, "chunks": ch}}},
                      open(os.path.join(cat, "manifest.json"), "w"))
            manifest, index, recs = ps.load_catalog_records(cat)
            for core in (XorPIR(), LwePIR()):
                be = PirCoreBackend(core, recs)
                for batch in (1, 3):
                    m = pl.SeekablePlayer(ps.PIRChunkSource(manifest, index, be), "v", batch=batch,
                                          target_buffer_s=0.15).run(seeks=[(3, 10), (5, 2)])
                    self.assertEqual(m["play_log"][:3], [0, 1, 2])
                    self.assertEqual(m["play_log"][3:5], [10, 11])
                    self.assertEqual(m["play_log"][5:], list(range(2, 14)))
                self.assertGreater(be.stats.up_bytes, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
