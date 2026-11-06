"""Glue between the PIR schemes and the baseline playback client.

The baseline's run_session() only needs a ChunkSource with
    get_manifest() -> dict,  fetch(video_id, idx) -> bytes,  close()
PIRChunkSource provides exactly that on top of a PIR backend, so the same buffer /
playback / metrics code runs unchanged and the only thing that differs is how a
chunk is retrieved. fetch_batch() is the hook for the batching layer.

The manifest (video -> chunk count, SHA-256 of every padded record) is public, as in
the baseline, so mapping (video, chunk) -> global record index is public too; what PIR
hides is WHICH record the client asks for.

Backends run the servers in-process. 'link' optionally emulates a network path by
sleeping latency_ms + (upload + download) / rate after the compute; the two XOR servers
are run one after the other (so a fetch costs t1 + t2 here, where two machines would
cost max(t1, t2)).
"""
import json
import os
import time

import numpy as np

import lattice_pir as lp
import two_server_pir as tp


def chunk_file(catalog_dir, video_id, idx):  # same layout as baseline/common.chunk_path
    return os.path.join(catalog_dir, video_id, f"{idx:06d}.chunk")


def load_catalog_records(catalog_dir):
    """Read a baseline catalog -> (manifest, {(video, idx): global_index}, (N, R) uint8 array)."""
    with open(os.path.join(catalog_dir, "manifest.json")) as f:
        manifest = json.load(f)
    index, recs = {}, []
    for vid, v in manifest["videos"].items():
        for i in range(v["num_chunks"]):
            index[(vid, i)] = len(recs)
            recs.append(np.fromfile(chunk_file(catalog_dir, vid, i), dtype=np.uint8))
    return manifest, index, np.stack(recs)


class _Stats:
    def __init__(self):
        self.rounds = self.records = 0
        self.up_bytes = self.down_bytes = 0
        self.server_s = self.client_s = self.wire_s = 0.0

    def as_dict(self):
        r = max(self.records, 1)
        return {
            "rounds": self.rounds, "records": self.records,
            "up_bytes_per_record": round(self.up_bytes / r), "down_bytes_per_record": round(self.down_bytes / r),
            "server_ms_per_record": round(self.server_s * 1000 / r, 2),
            "client_ms_per_record": round(self.client_s * 1000 / r, 2),
        }


def _wire(link, up, down):
    if not link:
        return 0.0
    rate_kbps, latency_ms = link
    t = latency_ms / 1000 + ((up + down) * 8 / (rate_kbps * 1000) if rate_kbps else 0.0)
    time.sleep(t)
    return t


class XorBackend:
    name = "two-server XOR"

    def __init__(self, records, link=None):
        self.db = tp.XorPIRDatabase(records)
        self.server1 = tp.XorPIRServer(self.db)
        self.server2 = tp.XorPIRServer(self.db)  # a real deployment holds a second, independent copy
        self.client = tp.XorPIRClient(self.db.n_records, self.db.record_size)
        self.link = link
        self.stats = _Stats()
        self.offline = {"hint_bytes": 0, "setup_s": 0.0}

    def retrieve(self, indices):
        s = self.stats
        t0 = time.perf_counter()
        q1, q2 = self.client.make_queries(indices)
        t1 = time.perf_counter()
        a1 = self.server1.answer(q1)
        t2 = time.perf_counter()
        a2 = self.server2.answer(q2)
        t3 = time.perf_counter()
        out = self.client.reconstruct(a1, a2)
        t4 = time.perf_counter()
        n = len(indices)
        up, down = self.client.query_bytes(n), self.client.answer_bytes(n)
        s.rounds += 1
        s.records += n
        s.up_bytes += up
        s.down_bytes += down
        s.server_s += max(t2 - t1, t3 - t2)
        s.client_s += (t1 - t0) + (t4 - t3)
        s.wire_s += _wire(self.link, up, down)
        return [out[j].tobytes() for j in range(n)]


class LatticeBackend:
    name = "single-server lattice"

    def __init__(self, records, max_hint_bytes=32 << 20, lwe_n=lp.LWE_N_DEFAULT, k=None, link=None):
        self.server = lp.LatticePIRServer(records, max_hint_bytes=max_hint_bytes, k=k, lwe_n=lwe_n)
        hint = self.server.setup()
        self.params = self.server.params
        self.client = lp.LatticePIRClient(self.params, hint)  # in practice: downloads (params, hint) once
        self.link = link
        self.stats = _Stats()
        self.offline = {"hint_bytes": self.params.hint_bytes, "setup_s": round(self.server.setup_s, 3)}

    def retrieve(self, indices):
        s = self.stats
        p = self.params
        t0 = time.perf_counter()
        Q, st = self.client.make_queries(indices)
        t1 = time.perf_counter()
        ans = self.server.answer(Q)
        t2 = time.perf_counter()
        out = self.client.decode(ans, st)
        t3 = time.perf_counter()
        n = len(indices)
        up, down = n * p.upload_per_record, n * p.download_per_record
        s.rounds += 1
        s.records += n
        s.up_bytes += up
        s.down_bytes += down
        s.server_s += t2 - t1
        s.client_s += (t1 - t0) + (t3 - t2)
        s.wire_s += _wire(self.link, up, down)
        return [out[j].tobytes() for j in range(n)]


class PIRChunkSource:
    """Drop-in replacement for the baseline HTTPChunkSource."""

    def __init__(self, manifest, index, backend):
        self.manifest, self.index, self.backend = manifest, index, backend

    def get_manifest(self):
        return self.manifest

    def fetch(self, video_id, idx):
        return self.backend.retrieve([self.index[(video_id, idx)]])[0]

    def fetch_batch(self, video_id, indices):
        return self.backend.retrieve([self.index[(video_id, i)] for i in indices])

    def close(self):
        pass
