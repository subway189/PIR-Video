"""Single-server lattice PIR in the style of SimplePIR (Henzinger-Hong-Corrigan-Gibbs-
Meiklejohn-Vaikuntanathan, USENIX Security 2023); FrodoPIR (Davidson-Pestana-Celi,
PoPETs 2023) has the same structure with the roles of rows/columns swapped.

Setting: Z_q with q = 2^32 (so uint32 arithmetic wraps for free), plaintext modulus
p = 256 (one database byte per matrix entry), LWE dimension n = 1024, error ~ rounded
Gaussian with sigma = 6.4 (SimplePIR's parameter choice; see README for caveats).

Database layout
  N records of R bytes. Each record is cut into k slices of l = ceil(R/k) bytes; slice t
  of record i is column c = i*k + t of the l x m matrix D (m = N*k):  D[r, i*k+t] =
  record_i[t*l + r].  Retrieving a record = retrieving its k columns.

Protocol
  Setup (server, offline, client-independent):  A <- Z_q^{m x n} from a public seed,
      hint H = D*A in Z_q^{l x n}.  The client downloads (seed, H) ONCE and reuses it for
      every query against this database version.
  Query  (client), for column c:  s <- Z_q^n, e <- chi^m, send  b = A*s + e + Delta*u_c
      where Delta = q/p = 2^24 and u_c is the c-th unit vector. A fresh s per query is
      mandatory: re-using s across queries would leak the difference of the targets.
  Answer (server):  ans = D*b in Z_q^l          (one pass over D, cheap integer MACs)
  Decode (client):  ans - H*s = D*e + Delta*D[:,c];  round to a multiple of Delta, giving
      column c exactly as long as |D*e| < Delta/2 (checked at setup: see noise_margin).

Trade-off controlled by k:  hint size = l*n*4 bytes (shrinks as 1/k) versus upload per
record = k * m * 4 = 4*N*k^2 bytes (grows as k^2).  choose_layout() picks the smallest k
whose hint fits a budget.
"""
import math
import os
import time
from dataclasses import dataclass

import numpy as np

import kernels

Q_BITS = 32
P = 256                      # plaintext modulus: one byte per entry
DELTA = 1 << (Q_BITS - 8)    # q / p
SIGMA = 6.4
LWE_N_DEFAULT = 1024
A_BLOCK_ROWS = 4096          # rows of A generated per PRG block (part of the protocol)
MIN_MARGIN_SIGMAS = 6.0


@dataclass(frozen=True)
class LatticeParams:
    """Everything the client must know in the clear (besides the hint)."""
    n_records: int
    record_size: int
    k: int               # columns per record
    l: int               # rows of D (bytes per column)
    m: int               # columns of D = n_records * k
    lwe_n: int
    seed: bytes          # public seed expanding to A
    noise_margin_sigmas: float = float("nan")

    @property
    def hint_bytes(self):
        return self.l * self.lwe_n * 4

    @property
    def query_bytes(self):          # one query (one column)
        return self.m * 4

    @property
    def answer_bytes(self):         # one answer (one column)
        return self.l * 4

    @property
    def upload_per_record(self):
        return self.k * self.query_bytes

    @property
    def download_per_record(self):
        return self.k * self.answer_bytes

    @property
    def db_bytes(self):
        return self.l * self.m


def choose_layout(n_records, record_size, lwe_n=LWE_N_DEFAULT, max_hint_bytes=64 << 20):
    """Smallest power-of-two k whose hint (l*n*4 bytes) fits the budget; returns (k, l, m)."""
    k = 1
    while math.ceil(record_size / k) * lwe_n * 4 > max_hint_bytes and k < record_size:
        k *= 2
    l = math.ceil(record_size / k)
    return k, l, n_records * k


def noise_margin(D, sigma=SIGMA):
    """Decoding succeeds iff |sum_j D[r,j] e_j| < Delta/2 for every row r. That sum is
    (sub-)Gaussian with std sigma*||D[r,:]||_2, so return (Delta/2) / (sigma * max_r ||D[r,:]||_2),
    i.e. the number of standard deviations of headroom for the worst row of THIS database."""
    worst = 0
    for r0 in range(0, D.shape[0], 256):
        blk = D[r0:r0 + 256].astype(np.uint64)
        worst = max(worst, int(np.einsum("ij,ij->i", blk, blk).max()))
    return (DELTA / 2) / (sigma * math.sqrt(worst))


def _seed_int(seed):
    return int.from_bytes(seed, "big")


def gen_A_block(seed, block, rows, lwe_n):
    """Deterministic block of the public matrix A (numpy PCG64 here; a deployment should
    expand the seed with AES-CTR/ChaCha as FrodoPIR does)."""
    ss = np.random.SeedSequence(entropy=_seed_int(seed), spawn_key=(block,))
    return np.random.Generator(np.random.PCG64(ss)).integers(0, 1 << 32, size=(rows, lwe_n), dtype=np.uint32)


def _gaussian_noise(shape, sigma=SIGMA):
    rng = np.random.Generator(np.random.PCG64(int.from_bytes(os.urandom(16), "big")))
    return np.rint(rng.normal(0.0, sigma, size=shape)).astype(np.int32).view(np.uint32)


class LatticePIRServer:
    def __init__(self, records, max_hint_bytes=64 << 20, k=None, lwe_n=LWE_N_DEFAULT, seed=None):
        records = np.ascontiguousarray(records, dtype=np.uint8)
        n_records, record_size = records.shape
        if k is None:
            k, l, m = choose_layout(n_records, record_size, lwe_n, max_hint_bytes)
        else:
            l, m = math.ceil(record_size / k), n_records * k
        seed = seed if seed is not None else os.urandom(16)
        padded = records
        if k * l != record_size:
            padded = np.zeros((n_records, k * l), dtype=np.uint8)
            padded[:, :record_size] = records
        # D[r, i*k + t] = record_i[t*l + r]
        self.D = np.ascontiguousarray(padded.reshape(n_records, k, l).transpose(2, 0, 1).reshape(l, m))
        margin = noise_margin(self.D)
        if margin < MIN_MARGIN_SIGMAS:
            raise ValueError(
                f"decoding-noise margin is only {margin:.1f} sigma for m={m} columns; shrink the database "
                f"per PIR instance (shard it) or reduce k")
        self.params = LatticeParams(n_records, record_size, k, l, m, lwe_n, seed, margin)
        self.hint = None
        self.setup_s = None

    def setup(self):
        """Offline phase: H = D*A mod 2^32, computed exactly with float64 BLAS by splitting A into
        16-bit halves (every partial sum stays below 2^53). Returns the hint (l x n uint32)."""
        p = self.params
        l, m, n = p.l, p.m, p.lwe_n
        t0 = time.perf_counter()
        h_lo = np.zeros((l, n), dtype=np.float64)
        h_hi = np.zeros((l, n), dtype=np.float64)
        row_blk = 2048
        for blk, j0 in enumerate(range(0, m, A_BLOCK_ROWS)):
            rows = min(A_BLOCK_ROWS, m - j0)
            A = gen_A_block(p.seed, blk, rows, n)
            a_lo = (A & 0xFFFF).astype(np.float64)
            a_hi = (A >> 16).astype(np.float64)
            for i0 in range(0, l, row_blk):
                d = self.D[i0:i0 + row_blk, j0:j0 + rows].astype(np.float64)
                h_lo[i0:i0 + row_blk] += d @ a_lo
                h_hi[i0:i0 + row_blk] += d @ a_hi
        lo = h_lo.astype(np.uint64)
        hi = h_hi.astype(np.uint64)
        self.hint = ((lo + (hi << np.uint64(16))) & np.uint64(0xFFFFFFFF)).astype(np.uint32)
        self.setup_s = time.perf_counter() - t0
        return self.hint

    def answer(self, queries):
        """queries: (Q, m) uint32 -> (Q, l) uint32. One pass over D for all Q queries."""
        return kernels.lwe_answer_batch(self.D, queries)


class LatticePIRClient:
    def __init__(self, params, hint, cache_A_bytes=256 << 20):
        self.p = params
        self.hint = np.ascontiguousarray(hint, dtype=np.uint32)
        if self.hint.shape != (params.l, params.lwe_n):
            raise ValueError("hint has the wrong shape for these parameters")
        self._A = None
        if params.m * params.lwe_n * 4 <= cache_A_bytes:
            self._A = [gen_A_block(params.seed, b, min(A_BLOCK_ROWS, params.m - j0), params.lwe_n)
                       for b, j0 in enumerate(range(0, params.m, A_BLOCK_ROWS))]

    def _A_blocks(self):
        if self._A is not None:
            yield from self._A
        else:
            for b, j0 in enumerate(range(0, self.p.m, A_BLOCK_ROWS)):
                yield gen_A_block(self.p.seed, b, min(A_BLOCK_ROWS, self.p.m - j0), self.p.lwe_n)

    def make_queries(self, indices):
        """One query per (record, slice): returns (queries (B*k, m) uint32, state)."""
        p = self.p
        idx = np.asarray(indices, dtype=np.int64)
        if idx.size and (idx.min() < 0 or idx.max() >= p.n_records):
            raise IndexError("record index out of range")
        cols = (idx[:, None] * p.k + np.arange(p.k)[None, :]).reshape(-1)  # (B*k,)
        nq = len(cols)
        S = np.frombuffer(os.urandom(nq * p.lwe_n * 4), dtype=np.uint32).reshape(nq, p.lwe_n).copy()
        parts = [kernels.u32_matvec_batch(A, S) for A in self._A_blocks()]   # each (nq, rows)
        Q = np.concatenate(parts, axis=1) + _gaussian_noise((nq, p.m))
        Q[np.arange(nq), cols] += np.uint32(DELTA)
        return Q, {"S": S, "n_records": len(idx)}

    def decode(self, answers, state):
        """answers: (B*k, l) uint32 -> (B, R) uint8."""
        p = self.p
        hs = kernels.u32_matvec_batch(self.hint, state["S"])                 # (B*k, l)
        diff = answers - hs                                                  # uint32 wraps mod 2^32
        cols = ((diff + np.uint32(DELTA // 2)) >> np.uint32(Q_BITS - 8)).astype(np.uint8)  # round to byte
        out = cols.reshape(state["n_records"], p.k * p.l)                    # slice t occupies [t*l, (t+1)*l)
        return np.ascontiguousarray(out[:, : p.record_size])


def retrieve(client, server, indices):
    Q, st = client.make_queries(indices)
    return client.decode(server.answer(Q), st)
