"""Two-server information-theoretic PIR (Chor-Goldreich-Kushilevitz-Sudan 1995).

Both servers hold the same database of N fixed-size records. To fetch record i
the client picks a uniformly random N-bit vector q1, sets q2 = q1 XOR e_i, and
sends q1 to server 1 and q2 to server 2. Each server returns the XOR of the
records its vector selects. XORing the two answers cancels everything except
record i.

Privacy: each query on its own is a uniformly random bit string, independent of
i, so a single server learns nothing about i *with unconditional security*.
The scheme is broken if the two servers collude (q1 XOR q2 = e_i).

Cost (this is what the benchmark measures):
  upload   2 * ceil(N/8) bytes per record fetched
  download 2 * R bytes   (R = record size; a 2x download overhead)
  server   each server XORs ~N/2 records: linear in the catalog size N*R
"""
import os

import numpy as np

import kernels


class XorPIRDatabase:
    """N records of R bytes, stored as uint64 words (rows padded to 8 bytes)."""

    def __init__(self, records):
        records = np.ascontiguousarray(records, dtype=np.uint8)
        if records.ndim != 2:
            raise ValueError("records must be a 2-D (N, R) uint8 array")
        self.n_records, self.record_size = records.shape
        self.row_bytes = (self.record_size + 7) // 8 * 8
        if self.row_bytes == self.record_size:
            padded = records
        else:
            padded = np.zeros((self.n_records, self.row_bytes), dtype=np.uint8)
            padded[:, : self.record_size] = records
        self.words = padded.view(np.uint64)  # (N, row_bytes / 8), shares memory with `padded`
        self._keepalive = padded

    @classmethod
    def random(cls, n_records, record_size, seed=0):
        rng = np.random.default_rng(seed)
        return cls(rng.integers(0, 256, size=(n_records, record_size), dtype=np.uint8))

    @property
    def size_bytes(self):
        return self.n_records * self.record_size

    def record(self, i):
        return self._keepalive[i, : self.record_size].tobytes()


class XorPIRServer:
    def __init__(self, db):
        self.db = db

    def answer(self, masks):
        """masks: (B, ceil(N/8)) uint8 -> (B, R) uint8, one answer per query."""
        out = kernels.xor_select_batch(self.db.words, masks)  # (B, row_bytes/8) uint64
        return out.view(np.uint8)[:, : self.db.record_size]


class XorPIRClient:
    def __init__(self, n_records, record_size):
        self.n = n_records
        self.record_size = record_size
        self.mask_bytes = (n_records + 7) // 8

    def make_queries(self, indices):
        """Return (masks_for_server1, masks_for_server2), each (B, mask_bytes) uint8."""
        idx = np.asarray(indices, dtype=np.int64)
        if idx.size and (idx.min() < 0 or idx.max() >= self.n):
            raise IndexError("record index out of range")
        b = len(idx)
        q1 = np.frombuffer(os.urandom(b * self.mask_bytes), dtype=np.uint8).reshape(b, self.mask_bytes).copy()
        if self.n % 8:  # keep the unused tail bits zero (cosmetic: the kernel ignores them)
            q1[:, -1] &= (1 << (self.n % 8)) - 1
        q2 = q1.copy()
        q2[np.arange(b), idx >> 3] ^= (1 << (idx & 7)).astype(np.uint8)
        return q1, q2

    def reconstruct(self, ans1, ans2):
        return np.bitwise_xor(ans1, ans2)  # (B, R) uint8

    def query_bytes(self, n_queries=1):
        return 2 * n_queries * self.mask_bytes

    def answer_bytes(self, n_queries=1):
        return 2 * n_queries * self.record_size


def retrieve(client, server1, server2, indices):
    """Convenience: run the whole protocol in-process; returns (B, R) uint8."""
    q1, q2 = client.make_queries(indices)
    return client.reconstruct(server1.answer(q1), server2.answer(q2))
