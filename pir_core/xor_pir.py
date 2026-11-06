"""Two-server information-theoretic PIR (Chor-Goldreich-Kushilevitz-Sudan '95,
the basic linear XOR scheme).

Protocol, for a database of N records and a wanted index i:
  client : r0 <- uniform {0,1}^N ;  r1 = r0 XOR e_i       (e_i = i-th unit vector)
  server b: returns A_b = XOR of all records j with r_b[j] = 1
  client : record_i = A_0 XOR A_1
Each r_b on its own is uniformly random, so a single server learns nothing
about i (information-theoretically). Privacy is lost if the two servers collude.

Cost: upload N bits per server per query, download one record per server,
server work ~ N*B/2 bytes XORed per query.
"""
from __future__ import annotations

import os
import time
from typing import Callable, Sequence

import numpy as np

from .base import PIRBackend, RetrievalStats

# Rows processed per cache block ~ BLOCK_BYTES / record size (tuned on 2-core box).
BLOCK_BYTES = 512 * 1024


def _round8(x: int) -> int:
    return (x + 7) // 8 * 8


class XorPIRServer:
    """One of the two replicas. Stateless between queries."""

    def __init__(self, records: np.ndarray, scan: str = "optimized"):
        records = np.ascontiguousarray(records, dtype=np.uint8)
        self.N, self.B = records.shape
        self.Bp = _round8(self.B)  # pad so rows can be XORed as uint64 words
        if self.Bp != self.B:
            padded = np.zeros((self.N, self.Bp), dtype=np.uint8)
            padded[:, : self.B] = records
            records = padded
        self.words = records.view(np.uint64)  # (N, Bp/8), shares memory
        if scan not in ("naive", "optimized"):
            raise ValueError("scan must be 'naive' or 'optimized'")
        self.scan = scan

    @property
    def query_bytes(self) -> int:
        return (self.N + 7) // 8

    def answer(self, qpacked: np.ndarray, scan: str | None = None) -> np.ndarray:
        """qpacked: (k, ceil(N/8)) uint8 bit-packed selection vectors.
        Returns (k, B) uint8, one XOR-sum per query."""
        qpacked = np.asarray(qpacked, dtype=np.uint8)
        if qpacked.ndim != 2 or qpacked.shape[1] != self.query_bytes:
            raise ValueError(f"query must have shape (k, {self.query_bytes})")
        k = qpacked.shape[0]
        masks = np.unpackbits(qpacked, axis=1, count=self.N).astype(bool)  # (k, N)
        out = np.zeros((k, self.words.shape[1]), dtype=np.uint64)
        if (scan or self.scan) == "naive":
            # one full pass over the database per query
            for j in range(k):
                out[j] = np.bitwise_xor.reduce(self.words[masks[j]], axis=0)
        else:
            # cache-blocked: each block of rows is applied to all k queries while
            # it is still resident in cache, so the DB is streamed from RAM once.
            rows = max(8, BLOCK_BYTES // self.Bp)
            for lo in range(0, self.N, rows):
                blk = self.words[lo : lo + rows]
                for j in range(k):
                    out[j] ^= np.bitwise_xor.reduce(blk[masks[j, lo : lo + blk.shape[0]]], axis=0)
        return np.ascontiguousarray(out.view(np.uint8)[:, : self.B])


class XorPIRClient:
    def __init__(self, num_records: int, record_bytes: int,
                 randbytes: Callable[[int], bytes] = os.urandom):
        self.N, self.B = num_records, record_bytes
        self._randbytes = randbytes  # injectable only so tests can be deterministic

    def query(self, indices: Sequence[int]):
        """Returns (state, [query_for_server0, query_for_server1])."""
        idx = np.asarray(list(indices), dtype=np.int64)
        k, nb = len(idx), (self.N + 7) // 8
        q0 = np.frombuffer(self._randbytes(k * nb), dtype=np.uint8).reshape(k, nb).copy()
        # packed bits are big-endian within a byte (numpy.packbits default)
        pad = nb * 8 - self.N
        if pad:  # zero the unused padding bits so queries are canonical
            q0[:, -1] &= np.uint8((0xFF << pad) & 0xFF)
        q1 = q0.copy()
        q1[np.arange(k), idx // 8] ^= (0x80 >> (idx % 8)).astype(np.uint8)
        return idx, [q0, q1]

    def decode(self, state, answers: Sequence[np.ndarray]) -> np.ndarray:
        a0, a1 = answers
        return a0 ^ a1


class XorPIR(PIRBackend):
    """In-process simulation of the two-server protocol (both replicas are the
    same object, since they hold identical data; timings are taken per call)."""

    def __init__(self, scan: str = "optimized", randbytes: Callable[[int], bytes] = os.urandom):
        self.scan = scan
        self.name = f"xor-2server-{scan}"
        self._randbytes = randbytes

    def setup(self, records: np.ndarray) -> None:
        records = self._check_records(records)
        self.N, self.B = records.shape
        self.server = XorPIRServer(records, scan=self.scan)
        self.client = XorPIRClient(self.N, self.B, self._randbytes)

    def retrieve(self, indices: Sequence[int]) -> np.ndarray:
        idx = self._check_indices(indices, self.N)
        st = RetrievalStats(self.name, self.N, self.B, len(idx))
        t = time.perf_counter()
        state, queries = self.client.query(idx)
        st.t_query = time.perf_counter() - t
        answers, times = [], []
        for q in queries:  # in a real deployment these run on two machines in parallel
            t = time.perf_counter()
            answers.append(self.server.answer(q))
            times.append(time.perf_counter() - t)
        st.t_server, st.t_server_max = sum(times), max(times)
        t = time.perf_counter()
        out = self.client.decode(state, answers)
        st.t_decode = time.perf_counter() - t
        st.bytes_up = sum(q.nbytes for q in queries)
        st.bytes_down = sum(a.nbytes for a in answers)
        self.last_stats = st
        return out
