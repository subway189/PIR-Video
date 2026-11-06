"""TCP-backed PIR backends for the streaming player: the player's ChunkSource talks to the real
network servers (pir_network_server.py), including sharded deployments.

    xor = NetXorBackend([[host0:p, host0:p2], [host1:p, host1:p2]])   # replica 0 shards, replica 1 shards
    lwe = NetLweBackend([host:p_shard0, host:p_shard1])
    src = net_source.make_source(catalog_dir_or_manifest, xor)        # -> pir_source.PIRChunkSource
    SeekablePlayer(src, video, batch="adaptive").run(...)

Both backends expose .retrieve(indices) -> list[bytes], .stats, .offline like pir/pir_source backends.
Shards of one replica are queried in parallel threads. The client sends each XOR shard only the
byte-aligned slice of the query for that shard's record range (a slice of a uniform vector is uniform).
"""
import json
import os
import sys
import threading
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pir"))
sys.path.insert(0, os.path.join(HERE, ".."))
import pir_source as ps                                   # noqa: E402
from pir_network_client import parse_address, request as _request  # noqa: E402
import integrity                                          # noqa: E402
from integrity import IntegrityError, ManifestError       # noqa: E402,F401
from pir_core import LwePIRClient, LweParams, XorPIRClient  # noqa: E402


def _parallel(fns):
    """Run callables in threads; return results in order, re-raise the first error."""
    res, errs = [None] * len(fns), []

    def run(i, f):
        try:
            res[i] = f()
        except Exception as e:  # noqa: BLE001
            errs.append(e)
    ths = [threading.Thread(target=run, args=(i, f)) for i, f in enumerate(fns)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    if errs:
        raise errs[0]
    return res


def _addrs(lst):
    return [parse_address(a) if isinstance(a, str) else tuple(a) for a in lst]


def request(addr, header, array=None, tls=None, timeout=120):
    return _request(addr, header, array, tls=tls, timeout=timeout)


class NetXorBackend:
    name = "two-server XOR (TCP)"

    def __init__(self, replica0, replica1, tls=None):
        self.tls = tls
        self.reps = [_addrs(replica0), _addrs(replica1)]
        infos = [[request(a, {"op": "info"}, tls=tls)[0] for a in rep] for rep in self.reps]
        self.N, self.B = int(infos[0][0]["N"]), int(infos[0][0]["B"])
        self.ranges = []
        for rep in infos:
            assert all((int(i["N"]), int(i["B"])) == (self.N, self.B) for i in rep), "servers disagree on catalog"
            rs = sorted(tuple(i["shard"]) for i in rep)
            assert rs[0][0] == 0 and rs[-1][1] == self.N and all(x[1] == y[0] for x, y in zip(rs, rs[1:])), \
                f"shards do not tile 0..{self.N}: {rs}"
            self.ranges.append([tuple(i["shard"]) for i in rep])
        self.client = XorPIRClient(self.N, self.B)
        self.stats, self.offline = ps._Stats(), {"hint_bytes": 0, "setup_s": 0.0}

    def retrieve(self, indices):
        t0 = time.perf_counter()
        state, qs = self.client.query(list(indices))
        t1 = time.perf_counter()
        jobs, up = [], 0
        for r in range(2):
            for addr, (a, b) in zip(self.reps[r], self.ranges[r]):
                sl = np.ascontiguousarray(qs[r][:, a // 8:(b + 7) // 8])
                up += sl.nbytes
                jobs.append((r, addr, sl))
        outs = _parallel([lambda addr=addr, sl=sl: request(addr, {"op": "xor_answer"}, sl, tls=self.tls)[1] for _, addr, sl in jobs])
        t2 = time.perf_counter()
        ans = [None, None]
        for (r, _, _), o in zip(jobs, outs):
            ans[r] = o if ans[r] is None else ans[r] ^ o
        out = self.client.decode(state, ans)
        t3 = time.perf_counter()
        s = self.stats
        s.rounds += 1
        s.records += len(indices)
        s.up_bytes += up
        s.down_bytes += sum(o.nbytes for o in outs)
        s.server_s += t2 - t1   # wall time of the parallel requests: network + server compute
        s.client_s += (t1 - t0) + (t3 - t2)
        return [out[j].tobytes() for j in range(len(indices))]


class NetLweBackend:
    name = "single-server lattice (TCP)"

    def __init__(self, shards, hint_cache=None, tls=None):
        self.tls = tls
        self.addrs = _addrs(shards)
        infos = [request(a, {"op": "info"}, tls=tls)[0] for a in self.addrs]
        self.N, self.B = int(infos[0]["N"]), int(infos[0]["B"])
        order = sorted(range(len(infos)), key=lambda i: infos[i]["shard"][0])
        self.addrs = [self.addrs[i] for i in order]
        t = time.perf_counter()
        if hint_cache and os.path.exists(hint_cache):
            hint = np.load(hint_cache)
        else:
            hint = np.concatenate(_parallel([lambda a=a: request(a, {"op": "lwe_hint"}, tls=tls)[1] for a in self.addrs]), axis=0)
            if hint_cache:
                np.save(hint_cache, hint)
        self.client = LwePIRClient(self.N, self.B, hint, LweParams())
        self.stats = ps._Stats()
        self.offline = {"hint_bytes": int(hint.nbytes), "setup_s": round(time.perf_counter() - t, 3)}

    def retrieve(self, indices):
        t0 = time.perf_counter()
        state, Q = self.client.query(list(indices))
        t1 = time.perf_counter()
        parts = _parallel([lambda a=a: request(a, {"op": "lwe_answer"}, Q, tls=self.tls)[1] for a in self.addrs])
        t2 = time.perf_counter()
        ans = np.concatenate(parts, axis=0)
        out = self.client.decode(state, ans)
        t3 = time.perf_counter()
        s = self.stats
        s.rounds += 1
        s.records += len(indices)
        s.up_bytes += Q.nbytes * len(self.addrs)   # the same Q goes to every shard
        s.down_bytes += ans.nbytes
        s.server_s += t2 - t1
        s.client_s += (t1 - t0) + (t3 - t2)
        return [out[j].tobytes() for j in range(len(indices))]


class VerifiedPIRChunkSource(ps.PIRChunkSource):
    """PIRChunkSource that checks every record against the (signed) manifest before returning it, so a server
    that answers wrongly can never feed bad data to the player. One transparent retry (fresh query randomness)
    covers a transient glitch; a persistent mismatch raises IntegrityError."""

    def __init__(self, manifest, index, backend, retries=1):
        super().__init__(manifest, index, backend)
        self.retries = retries
        self.integrity_failures = 0

    def _verified(self, video_id, indices):
        last = None
        for _ in range(self.retries + 1):
            recs = self.backend.retrieve([self.index[(video_id, i)] for i in indices])
            try:
                for i, r in zip(indices, recs):
                    integrity.check_record(r, video_id, i, self.manifest["videos"][video_id]["chunks"][i]["sha256"])
                return recs
            except IntegrityError as e:
                self.integrity_failures += 1
                last = e
        raise last

    def fetch(self, video_id, idx):
        return self._verified(video_id, [idx])[0]

    def fetch_batch(self, video_id, indices):
        return self._verified(video_id, list(indices))


def make_source(catalog_or_manifest, backend, pubkey=None, retries=1):
    """Verified PIR source over a network backend. Only the public manifest is read locally (no chunk data);
    global indices follow the manifest order, which the servers use when started with --catalog.
    pubkey: Ed25519 public key (PEM). When given, the manifest MUST carry a valid manifest.sig
    (written by `integrity.py sign`), otherwise ManifestError is raised."""
    if isinstance(catalog_or_manifest, str):
        if pubkey:
            manifest = integrity.load_verified_manifest(catalog_or_manifest, pubkey)
        else:
            with open(os.path.join(catalog_or_manifest, "manifest.json")) as f:
                manifest = json.load(f)
    else:
        if pubkey:
            raise ValueError("signature verification needs the catalog directory, not a manifest dict")
        manifest = catalog_or_manifest
    index, n = {}, 0
    for vid, v in manifest["videos"].items():
        for i in range(v["num_chunks"]):
            index[(vid, i)] = n
            n += 1
    assert n == backend.N, f"manifest has {n} chunks but servers hold {backend.N}"
    return VerifiedPIRChunkSource(manifest, index, backend, retries)
