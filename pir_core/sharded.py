"""Server-side sharding for both PIR backends. Each shard can live on its own machine; the
coordinator (or the client, over the network) combines the shard answers. Sharding only splits
*compute and memory*; the client's query is the same as for an unsharded server, so privacy is
unchanged and a shard learns nothing more than a full server would.

XOR (two replicas, each sharded the same way)
    Shard s holds the records in [a_s, b_s) (a_s a multiple of 8). It receives the byte-aligned
    slice of the bit-packed query for its range and returns the XOR of its selected rows;
    the XOR of all shard answers equals the unsharded answer.
LWE (SimplePIR layout, DB is l x m bytes)
    Shard s holds a band of rows [r_s, r_s') of the DB matrix. All shards get the *same* query Q;
    shard answers (rows of D*Q) and hint bands (rows of D*A) are concatenated, so the client is the
    unmodified LwePIRClient.
"""
from __future__ import annotations

import numpy as np

from .lwe_pir import LwePIRServer, LweParams
from .xor_pir import XorPIRServer


def _bounds(total: int, parts: int, align: int = 1) -> list[tuple[int, int]]:
    parts = max(1, min(parts, max(1, total // align)))
    step = -(-total // parts)
    step = -(-step // align) * align
    return [(a, min(total, a + step)) for a in range(0, total, step)]


class XorShard:
    """Holds records[a:b]. `answer` takes the FULL packed query and slices its own range."""

    def __init__(self, records: np.ndarray, a: int, b: int, scan: str = "optimized"):
        assert a % 8 == 0
        self.a, self.b = a, b
        self.server = XorPIRServer(np.ascontiguousarray(records[a:b]), scan=scan)

    def answer(self, q_full: np.ndarray) -> np.ndarray:
        q = np.asarray(q_full, dtype=np.uint8)[:, self.a // 8: (self.b + 7) // 8]
        return self.server.answer(q)


class ShardedXorServer:
    def __init__(self, records: np.ndarray, n_shards: int, scan: str = "optimized"):
        records = np.ascontiguousarray(records, dtype=np.uint8)
        self.N, self.B = records.shape
        self.shards = [XorShard(records, a, b, scan) for a, b in _bounds(self.N, n_shards, 8)]

    def answer(self, q: np.ndarray) -> np.ndarray:
        out = None
        for sh in self.shards:
            a = sh.answer(q)
            out = a if out is None else out ^ a
        return out


class LweRowShard:
    """Rows [r0, r1) of the DB matrix; same public API as LwePIRServer (hint/answer)."""

    def __init__(self, full: LwePIRServer, r0: int, r1: int):
        s = object.__new__(LwePIRServer)
        s.params, s.scan, s.layout = full.params, full.scan, full.layout
        s.db = np.ascontiguousarray(full.db[r0:r1])
        s._dbf = s._db32 = s._hint = None
        s.hint_seconds = 0.0
        self.r0, self.r1, self.server = r0, r1, s

    def hint(self) -> np.ndarray:
        return self.server.hint()

    def answer(self, Q: np.ndarray) -> np.ndarray:
        return self.server.answer(Q)


class ShardedLweServer:
    def __init__(self, records: np.ndarray, n_shards: int, params: LweParams = LweParams(),
                 rows: int | None = None, scan: str = "optimized"):
        full = LwePIRServer(records, params, rows, scan)
        self.layout, self.params = full.layout, params
        self.shards = [LweRowShard(full, a, b) for a, b in _bounds(full.db.shape[0], n_shards)]
        self.db = full.db  # only for shape checks by callers; shards own their own copies

    def hint(self) -> np.ndarray:
        return np.concatenate([s.hint() for s in self.shards], axis=0)

    def answer(self, Q: np.ndarray) -> np.ndarray:
        return np.concatenate([s.answer(Q) for s in self.shards], axis=0)
