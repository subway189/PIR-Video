"""Single-server computational PIR from LWE, in the style of SimplePIR
(Henzinger, Hong, Corrigan-Gibbs, Meiklejohn, Vaikuntanathan; USENIX Sec '23).

Setting: q = 2^32 (native uint32 wrap-around), plaintext modulus p = 2^8, so one
database entry is exactly one byte and Delta = q/p = 2^24.

Database: a matrix DB in Z_p^{l x m} (l rows, m columns), see Layout.
Public:   A in Z_q^{m x n}, expanded from a short seed (never transmitted).

Offline (once per DB):  server computes hint H = DB * A  (l x n);  client downloads it.
Online, to read column c:
  client: s <- {-1,0,1}^n, e <- rounded Gaussian(sigma);
          query = A*s + e + Delta*u_c      (m entries of Z_q; u_c is a unit vector)
  server: answer = DB * query              (l entries of Z_q)
  client: answer - H*s = DB*e + Delta*DB[:,c];  rounding by Delta gives column c.
Correctness needs |DB*e| < Delta/2 (checked by noise_margin_sigmas()).
Privacy: the query is LWE-pseudorandom, so the server learns nothing about c.
"""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from typing import Iterator, Sequence

import numpy as np

from .base import PIRBackend, RetrievalStats

MASK = (1 << 32) - 1
P = 256                    # plaintext modulus (one byte per entry)
DELTA = (1 << 32) // P     # 2^24
A_BLOCK_ROWS = 2048        # rows of A generated per PRG call


@dataclass(frozen=True)
class LweParams:
    n: int = 1024          # LWE dimension (SimplePIR default for q=2^32)
    sigma: float = 6.4     # error standard deviation (SimplePIR default)
    seed_A: int = 0x5EED_A  # public; fixes the matrix A


# --------------------------------------------------------------------------- layout
@dataclass(frozen=True)
class Layout:
    """How N records of B bytes are laid out in the l x m byte matrix.

    span == 1: each column holds `per_col` whole records stacked vertically.
    span  > 1: a record is longer than a column; it occupies `span` adjacent
               columns and the client queries each of them.
    """
    N: int
    B: int
    rows: int
    cols: int
    per_col: int
    span: int

    @staticmethod
    def choose(N: int, B: int, rows: int | None = None) -> "Layout":
        # default: roughly square matrix over bytes, which balances query size (4m)
        # against answer size (4l); an explicit `rows` trades hint size for query size.
        target = rows if rows is not None else max(1, round(math.sqrt(N * B)))
        if target >= B:
            r = max(1, target // B)
            return Layout(N, B, r * B, -(-N // r), r, 1)
        s = -(-B // target)
        l = -(-B // s)
        return Layout(N, B, l, N * s, 1, s)

    def columns_for(self, i: int) -> list[int]:
        if self.span == 1:
            return [i // self.per_col]
        return list(range(i * self.span, (i + 1) * self.span))

    def build_matrix(self, records: np.ndarray) -> np.ndarray:
        """records (N,B) uint8 -> DB matrix (rows, cols) uint8, C-contiguous."""
        if self.span == 1:
            padded = np.zeros((self.cols * self.per_col, self.B), dtype=np.uint8)
            padded[: self.N] = records
            mat = padded.reshape(self.cols, self.per_col * self.B)
        else:
            padded = np.zeros((self.N, self.span * self.rows), dtype=np.uint8)
            padded[:, : self.B] = records
            mat = padded.reshape(self.N * self.span, self.rows)
        return np.ascontiguousarray(mat.T)

    def extract(self, i: int, columns: np.ndarray) -> np.ndarray:
        """columns: (rows, len(columns_for(i))) uint8 decoded columns -> record i."""
        if self.span == 1:
            off = (i % self.per_col) * self.B
            return columns[off : off + self.B, 0]
        return columns.T.reshape(-1)[: self.B]


def noise_margin_sigmas(m: int, params: LweParams = LweParams()) -> float:
    """How many standard deviations of DB*e fit inside Delta/2 (worst case:
    DB entries uniform in [0,255], rms ~147). Want >> 6 for negligible failure."""
    rms = math.sqrt((P - 1) * (2 * P - 1) / 6)
    return (DELTA / 2) / (math.sqrt(m) * rms * params.sigma)


# --------------------------------------------------------------------------- the public matrix A
def a_blocks(params: LweParams, m: int) -> Iterator[tuple[int, int, np.ndarray]]:
    """Deterministically expand A (m x n, uint32) in row blocks from the seed."""
    for b, c0 in enumerate(range(0, m, A_BLOCK_ROWS)):
        c1 = min(m, c0 + A_BLOCK_ROWS)
        rng = np.random.default_rng([params.seed_A, b])
        yield c0, c1, rng.integers(0, 1 << 32, size=(c1 - c0, params.n), dtype=np.uint32)


def _fmod32(x: np.ndarray) -> np.ndarray:
    """float64 array of exact integers (possibly negative) -> int64 in [0, 2^32)."""
    return x.astype(np.int64) & MASK


# --------------------------------------------------------------------------- server
class LwePIRServer:
    def __init__(self, records: np.ndarray, params: LweParams = LweParams(),
                 rows: int | None = None, scan: str = "optimized"):
        records = np.ascontiguousarray(records, dtype=np.uint8)
        N, B = records.shape
        if scan not in ("naive", "optimized"):
            raise ValueError("scan must be 'naive' or 'optimized'")
        self.params, self.scan = params, scan
        self.layout = Layout.choose(N, B, rows)
        self.db = self.layout.build_matrix(records)          # (l, m) uint8
        self._dbf: np.ndarray | None = None                 # float64 copy for BLAS
        self._db32: np.ndarray | None = None                # uint32 copy for naive
        self._hint: np.ndarray | None = None
        self.hint_seconds = 0.0

    @property
    def dbf(self) -> np.ndarray:
        if self._dbf is None:
            self._dbf = self.db.astype(np.float64)
        return self._dbf

    # offline ---------------------------------------------------------------
    def hint(self) -> np.ndarray:
        """H = DB * A mod 2^32, shape (l, n), uint32. Cached."""
        if self._hint is None:
            t = time.perf_counter()
            l, m = self.db.shape
            lo_acc = np.zeros((l, self.params.n))
            hi_acc = np.zeros((l, self.params.n))
            dbf = self.dbf
            for c0, c1, blk in a_blocks(self.params, m):
                sub = dbf[:, c0:c1]
                lo_acc += sub @ (blk & 0xFFFF).astype(np.float64)   # entries <= 255*65535*m < 2^53
                hi_acc += sub @ (blk >> 16).astype(np.float64)
            h = (lo_acc.astype(np.int64) + (hi_acc.astype(np.int64) << 16)) & MASK
            self._hint = h.astype(np.uint32)
            self.hint_seconds = time.perf_counter() - t
        return self._hint

    # online ----------------------------------------------------------------
    def answer(self, Q: np.ndarray, scan: str | None = None) -> np.ndarray:
        """Q: (m, K) uint32 queries (one per column). Returns (l, K) uint32 = DB*Q mod 2^32."""
        Q = np.asarray(Q, dtype=np.uint32)
        l, m = self.db.shape
        if Q.ndim != 2 or Q.shape[0] != m:
            raise ValueError(f"query matrix must have shape ({m}, K)")
        if (scan or self.scan) == "naive":
            if self._db32 is None:
                self._db32 = self.db.astype(np.uint32)
            return self._db32 @ Q  # numpy integer matmul wraps mod 2^32; slow, no BLAS
        # optimized: exact float64 BLAS. Split Q into 16-bit limbs so that
        # 255 * 65535 * m < 2^53 and every product/sum is an exactly-represented integer.
        K = Q.shape[1]
        limbs = np.concatenate([(Q & 0xFFFF), (Q >> 16)], axis=1).astype(np.float64)  # (m, 2K)
        res = self.dbf @ limbs                                                        # one pass over DB
        out = (res[:, :K].astype(np.int64) + (res[:, K:].astype(np.int64) << 16)) & MASK
        return out.astype(np.uint32)


# --------------------------------------------------------------------------- client
@dataclass
class _QueryState:
    indices: list[int]
    S: np.ndarray            # (n, K) float64 secrets in {-1,0,1}
    spans: list[int]         # columns per index


class LwePIRClient:
    def __init__(self, num_records: int, record_bytes: int, hint: np.ndarray,
                 params: LweParams = LweParams(), rows: int | None = None,
                 seed: bytes | None = None, cache_A: bool = True):
        self.params = params
        self.layout = Layout.choose(num_records, record_bytes, rows)
        if hint.shape != (self.layout.rows, params.n):
            raise ValueError("hint shape does not match layout/params")
        self.hintf = hint.astype(np.float64)
        # NOTE: numpy's PCG64 is not a cryptographic RNG; a production client
        # would draw s and e from a CSPRNG (e.g. ChaCha20). Seeded from os.urandom.
        self.rng = np.random.default_rng(int.from_bytes(seed or os.urandom(32), "little"))
        m = self.layout.cols
        self._A = None
        if cache_A and m * params.n * 8 <= 600 * 2**20:
            self._A = [(c0, c1, blk.astype(np.float64)) for c0, c1, blk in a_blocks(params, m)]
        self.last_noise_max = 0.0

    def _A_times(self, S: np.ndarray) -> np.ndarray:
        """A @ S, exact in float64 (|A|<2^32, |S|<=1, n terms => < 2^42)."""
        m = self.layout.cols
        out = np.empty((m, S.shape[1]))
        blocks = self._A if self._A is not None else (
            (c0, c1, blk.astype(np.float64)) for c0, c1, blk in a_blocks(self.params, m))
        for c0, c1, blk in blocks:
            out[c0:c1] = blk @ S
        return out

    def query(self, indices: Sequence[int]):
        lay, n = self.layout, self.params.n
        cols = [c for i in indices for c in lay.columns_for(i)]
        K, m = len(cols), lay.cols
        S = self.rng.integers(-1, 2, size=(n, K)).astype(np.float64)
        E = np.rint(self.rng.normal(0.0, self.params.sigma, size=(m, K))).astype(np.int64)
        Q = _fmod32(self._A_times(S)) + E
        Q[np.asarray(cols), np.arange(K)] += DELTA
        state = _QueryState(list(indices), S, [len(lay.columns_for(i)) for i in indices])
        return state, (Q & MASK).astype(np.uint32)

    def decode(self, state: _QueryState, ans: np.ndarray) -> np.ndarray:
        d = (ans.astype(np.int64) - _fmod32(self.hintf @ state.S)) & MASK     # (l, K)
        vals = (((d + DELTA // 2) & MASK) >> 24).astype(np.uint8)
        # observed noise = distance from the nearest multiple of Delta (diagnostics)
        resid =((d - (vals.astype(np.int64) << 24) + (1 << 31)) & MASK) - (1 << 31)
        self.last_noise_max = float(np.abs(resid).max())
        out, col = [], 0
        for i, s in zip(state.indices, state.spans):
            out.append(self.layout.extract(i, vals[:, col : col + s]))
            col += s
        return np.stack(out)


# --------------------------------------------------------------------------- backend wrapper
class LwePIR(PIRBackend):
    def __init__(self, params: LweParams = LweParams(), rows: int | None = None,
                 scan: str = "optimized", seed: bytes | None = None):
        self.params, self.rows, self.scan, self._seed = params, rows, scan, seed
        self.name = f"lwe-1server-{scan}"

    def setup(self, records: np.ndarray) -> None:
        records = self._check_records(records)
        self.N, self.B = records.shape
        self.server = LwePIRServer(records, self.params, self.rows, self.scan)
        t = time.perf_counter()
        hint = self.server.hint()
        self.offline_server_s = time.perf_counter() - t
        t = time.perf_counter()
        self.client = LwePIRClient(self.N, self.B, hint, self.params, self.rows, self._seed)
        self.offline_client_s = time.perf_counter() - t
        if noise_margin_sigmas(self.server.layout.cols, self.params) < 6:
            raise ValueError("catalog too large for p=256 / q=2^32: decryption noise margin < 6 sigma")

    def retrieve(self, indices: Sequence[int]) -> np.ndarray:
        idx = self._check_indices(indices, self.N)
        st = RetrievalStats(self.name, self.N, self.B, len(idx))
        st.offline_bytes = self.server.hint().nbytes
        st.t_offline_server, st.t_offline_client = self.offline_server_s, self.offline_client_s
        t = time.perf_counter()
        state, Q = self.client.query(idx)
        st.t_query = time.perf_counter() - t
        t = time.perf_counter()
        ans = self.server.answer(Q)
        st.t_server = st.t_server_max = time.perf_counter() - t
        t = time.perf_counter()
        out = self.client.decode(state, ans)
        st.t_decode = time.perf_counter() - t
        st.bytes_up, st.bytes_down = Q.nbytes, ans.nbytes
        self.last_stats = st
        return out
