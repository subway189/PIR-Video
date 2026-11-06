#!/usr/bin/env python3
"""Tests for the seekable player.  Run:  python3 test_player.py
Uses an in-memory source with tiny chunk durations so the whole file runs in a few seconds."""
import hashlib
import os
import tempfile
import time
import unittest

import numpy as np

import player as pl
import pir_source as ps

CS = 0.03  # seconds of "playback" per chunk in the tests


class FakeSource:
    def __init__(self, n=20, delay=0.0, corrupt=None):
        self.n, self.delay, self.corrupt = n, delay, corrupt
        self.recs = [bytes([i]) * 64 for i in range(n)]
        self.manifest = {"chunk_size": 64, "chunk_seconds": CS, "videos": {"v": {
            "num_chunks": n, "chunks": [{"len": 64, "sha256": hashlib.sha256(r).hexdigest()} for r in self.recs]}}}
        self.calls = []

    def get_manifest(self):
        return self.manifest

    def fetch(self, v, i):
        self.calls.append([i])
        time.sleep(self.delay)
        return b"bad" * 21 + b"!" if i == self.corrupt else self.recs[i]

    def fetch_batch(self, v, idxs):
        self.calls.append(list(idxs))
        time.sleep(self.delay)
        return [self.recs[i] for i in idxs]


def play(src, seeks=(), **kw):
    run_kw = {k: kw.pop(k) for k in ("max_chunks", "start_chunk") if k in kw}
    return pl.SeekablePlayer(src, "v", **kw).run(seeks=seeks, **run_kw)


class PlayerTests(unittest.TestCase):
    def test_plain_playback_in_order(self):
        m = play(FakeSource(20))
        self.assertEqual(m["play_log"], list(range(20)))
        self.assertEqual(m["rebuffer_count"], 0)

    def test_start_in_the_middle(self):
        m = play(FakeSource(20), start_chunk=12)
        self.assertEqual(m["play_log"], list(range(12, 20)))

    def test_forward_seek_miss_skips_the_gap(self):
        m = play(FakeSource(40), seeks=[(3, 35)], target_buffer_s=0.3)  # buffer holds ~10 chunks
        self.assertEqual(m["play_log"], [0, 1, 2, 35, 36, 37, 38, 39])
        self.assertFalse(m["seek_log"][0]["hit"])

    def test_forward_seek_inside_buffer_is_a_hit_with_no_new_fetch(self):
        src = FakeSource(20)
        m = play(src, seeks=[(2, 8)], target_buffer_s=0.6)  # buffer holds 20 chunks, fills instantly
        self.assertEqual(m["play_log"], [0, 1] + list(range(8, 20)))
        self.assertTrue(m["seek_log"][0]["hit"])
        self.assertLess(m["seek_hit_latency_s_max"], 0.1)
        fetched = sorted(i for c in src.calls for i in c)
        self.assertEqual(fetched, sorted(set(fetched)))  # nothing fetched twice

    def test_short_backward_seek_uses_the_back_buffer(self):
        m = play(FakeSource(20), seeks=[(5, 3)], target_buffer_s=0.6, back_keep=2)
        self.assertEqual(m["play_log"], [0, 1, 2, 3, 4, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19])
        self.assertTrue(m["seek_log"][0]["hit"])

    def test_far_backward_seek_is_a_miss_and_refetches(self):
        m = play(FakeSource(20), seeks=[(8, 1)], target_buffer_s=0.6, back_keep=2)
        self.assertEqual(m["play_log"], list(range(8)) + list(range(1, 20)))
        self.assertFalse(m["seek_log"][0]["hit"])

    def test_seek_to_last_chunk(self):
        m = play(FakeSource(20), seeks=[(2, 19)], target_buffer_s=0.15)
        self.assertEqual(m["play_log"], [0, 1, 19])

    def test_seek_to_start_of_a_batch_boundary_and_batched_rounds(self):
        src = FakeSource(40)
        m = play(src, seeks=[(3, 30)], batch=4, target_buffer_s=0.3)
        self.assertEqual(m["play_log"][:3], [0, 1, 2])
        self.assertEqual(m["play_log"][3:], list(range(30, 40)))
        self.assertTrue(all(len(c) <= 4 for c in src.calls))
        self.assertTrue(any(len(c) == 4 for c in src.calls))

    def test_stale_inflight_round_is_discarded_not_played(self):
        src = FakeSource(40, delay=0.08)  # fetch slower than playback: seek lands during a round
        m = play(src, seeks=[(2, 30)], target_buffer_s=0.3)
        self.assertEqual(m["play_log"][2:], list(range(30, 40)))
        self.assertGreaterEqual(m["wasted_fetches"], 1)
        self.assertNotIn(5, m["play_log"][2:])

    def test_multiple_seeks_in_one_session(self):
        m = play(FakeSource(60), seeks=[(2, 40), (5, 10), (7, 50)], target_buffer_s=0.3)
        self.assertEqual(m["play_log"][:2], [0, 1])
        self.assertEqual(m["play_log"][2:5], [40, 41, 42])
        self.assertEqual(m["play_log"][5:7], [10, 11])
        self.assertEqual(m["play_log"][7:], list(range(50, 60)))
        self.assertEqual(m["n_seeks"], 3)

    def test_rebuffers_are_counted_when_fetch_is_slower_than_playback(self):
        m = play(FakeSource(12, delay=0.09), max_chunks=12)  # 0.09 s per chunk vs 0.03 s playback
        self.assertGreater(m["rebuffer_count"], 0)
        self.assertGreater(m["rebuffer_ratio"], 0)

    def test_bad_seek_target_raises(self):
        with self.assertRaises(ValueError):
            play(FakeSource(10), seeks=[(1, 10)])
        with self.assertRaises(ValueError):
            play(FakeSource(10), seeks=[(1, -1)])

    def test_integrity_failure_raises(self):
        with self.assertRaises(ValueError):
            play(FakeSource(10, corrupt=4))

    def test_no_background_thread_left_behind(self):
        import threading
        before = threading.active_count()
        play(FakeSource(10), seeks=[(2, 7)])
        time.sleep(0.2)
        self.assertLessEqual(threading.active_count(), before)


class AdaptiveBatching(unittest.TestCase):
    def test_fast_source_grows_batch_to_max(self):
        src = FakeSource(60)  # instant fetches
        m = play(src, batch="adaptive", batch_max=8, target_buffer_s=1.0)
        self.assertEqual(m["play_log"], list(range(60)))
        self.assertEqual(m["batch"], "adaptive")
        self.assertEqual(m["batch_sizes"][0], 1)
        self.assertEqual(max(m["batch_sizes"]), 8)
        self.assertTrue(all(len(c) <= 8 for c in src.calls))

    def test_slow_source_keeps_batch_small(self):
        src = FakeSource(16, delay=0.05)  # 0.05 s per round vs 0.03 s of video per chunk -> ratio > 0.6 at k=1
        m = play(src, batch="adaptive", batch_max=8, target_buffer_s=0.5, max_chunks=16)
        self.assertEqual(max(m["batch_sizes"]), 1)

    def test_cold_seek_resets_to_small_batch(self):
        src = FakeSource(60)
        m = play(src, seeks=[(20, 50)], batch="adaptive", batch_max=8, target_buffer_s=0.3)
        self.assertEqual(m["play_log"][-10:], list(range(50, 60)))
        self.assertFalse(m["seek_log"][0]["hit"])
        self.assertEqual(src.calls[[c[0] for c in src.calls].index(50)], [50])  # first round after seek is 1 chunk


class PIRIntegration(unittest.TestCase):
    def test_xor_and_lattice_with_batches_and_seeks_return_correct_chunks(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            cat, R, chunks = os.path.join(tmp, "cat"), 2048, 14
            videos = {}
            for v in ("v",):
                os.makedirs(os.path.join(cat, v))
                ch = []
                for i in range(chunks):
                    data = np.random.default_rng(i).integers(0, 256, R, dtype=np.uint8).tobytes()
                    with open(ps.chunk_file(cat, v, i), "wb") as fh:
                        fh.write(data)
                    ch.append({"len": R, "sha256": hashlib.sha256(data).hexdigest()})
                videos[v] = {"num_chunks": chunks, "chunks": ch}
            with open(os.path.join(cat, "manifest.json"), "w") as fh:
                json.dump({"chunk_size": R, "chunk_seconds": CS, "videos": videos}, fh)
            manifest, index, recs = ps.load_catalog_records(cat)
            for backend in (ps.XorBackend(recs), ps.LatticeBackend(recs, max_hint_bytes=1 << 20)):
                for batch in (1, 3):
                    m = pl.SeekablePlayer(ps.PIRChunkSource(manifest, index, backend), "v", batch=batch,
                                          target_buffer_s=0.15).run(seeks=[(3, 10), (5, 2)])
                    self.assertEqual(m["play_log"][:3], [0, 1, 2])
                    self.assertEqual(m["play_log"][3:5], [10, 11])
                    self.assertEqual(m["play_log"][5:], list(range(2, 14)))  # SHA-256 verified inside the player


if __name__ == "__main__":
    unittest.main(verbosity=2)
