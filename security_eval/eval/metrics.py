"""Metrics pipeline for the PIR video project (Evaluation & Security Lead).

Three parts:
  1. TracedBackend   - wraps any pir_core backend; logs per-query / per-batch timing + bandwidth.
  2. CostModel       - fetch latency for a batch of k chunks, calibrated from the real-catalog
                       runs in real_catalog_run.txt (97 chunks x 367 KB, Kartik's machine).
  3. simulate_session - event-driven playback simulator that produces BOTH quality metrics
                       (startup latency, rebuffer count/time, seek latency, bandwidth) AND the
                       observable request trace (what a server / network observer sees).
"""
from __future__ import annotations

import csv
import time
from dataclasses import dataclass, field

import numpy as np

KB = 1024

# --------------------------------------------------------------------------- 1. instrumentation


class TracedBackend:
    """Drop-in wrapper around a PIRBackend; records one row per retrieve() call."""

    def __init__(self, backend):
        self.backend = backend
        self.log: list[dict] = []

    @property
    def name(self):
        return self.backend.name

    def setup(self, records):
        t = time.perf_counter()
        self.backend.setup(records)
        self.setup_seconds = time.perf_counter() - t

    def retrieve(self, indices):
        t0 = time.time()
        t = time.perf_counter()
        out = self.backend.retrieve(indices)
        wall = time.perf_counter() - t
        row = self.backend.last_stats.as_dict()
        row.update(t_wall_start=t0, t_wall_total=wall, k=len(indices))
        row["ms_per_chunk"] = 1e3 * row["t_compute_latency"] / len(indices)
        self.log.append(row)
        return out

    def to_csv(self, path):
        if not self.log:
            return
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, list(self.log[0].keys()))
            w.writeheader()
            w.writerows(self.log)


# --------------------------------------------------------------------------- 2. cost model
# Measured by Kartik (run_catalog.py, 97 chunks x 367 KB). Compute = mean of the two runs
# (50 and 10 Mbit/s settings); bytes are deterministic functions of k.
_K = np.array([1, 4, 8, 16], float)
_MEAS = {
    "xor": dict(compute_ms=np.array([(10.3 + 10.2) / 2, (37.1 + 39.9) / 2,
                                     (72.9 + 71.2) / 2, (122.6 + 134.3) / 2]),
                up_kb_per_chunk=0.0, down_kb_per_chunk=734.0),
    "lwe": dict(compute_ms=np.array([(158.2 + 166.0) / 2, (479.6 + 498.7) / 2,
                                     (1100.3 + 820.8) / 2, (2706.9 + 1841.6) / 2]),
                up_kb_per_chunk=1504.0, down_kb_per_chunk=1468.2),
}
CHUNK_KB = 367.0          # useful payload per chunk
NONPRIVATE = dict(compute_ms_per_chunk=0.0, up_kb_per_chunk=0.0, down_kb_per_chunk=CHUNK_KB)
_MEAS["plain"] = dict(compute_ms=np.zeros(4), up_kb_per_chunk=0.0, down_kb_per_chunk=CHUNK_KB)


@dataclass
class CostModel:
    scheme: str            # 'xor' | 'lwe' | 'plain' (non-private baseline)
    bw_mbps: float = 50.0
    rtt_s: float = 0.05

    def compute_s(self, k: int) -> float:
        m = _MEAS[self.scheme]["compute_ms"]
        if k <= 0:
            return 0.0
        if k <= _K[-1]:
            return float(np.interp(k, _K, m)) / 1e3
        slope = (m[-1] - m[-2]) / (_K[-1] - _K[-2])      # extrapolate linearly
        return float(m[-1] + slope * (k - _K[-1])) / 1e3

    def bytes(self, k: int) -> tuple[float, float]:
        m = _MEAS[self.scheme]
        return m["up_kb_per_chunk"] * k * KB, m["down_kb_per_chunk"] * k * KB

    def fetch_time(self, k: int) -> float:
        up, down = self.bytes(k)
        return self.compute_s(k) + self.rtt_s + (up + down) * 8 / (self.bw_mbps * 1e6)


# --------------------------------------------------------------------------- 3. session simulator


@dataclass
class SessionResult:
    startup_s: float = 0.0
    rebuffer_n: int = 0
    rebuffer_s: float = 0.0
    seek_latency_s: float = 0.0
    n_seeks: int = 0
    real_chunks: int = 0
    sent_chunks: int = 0               # real + dummy (what the server is asked to serve)
    bytes_up: float = 0.0
    bytes_down: float = 0.0
    duration_s: float = 0.0
    trace: list = field(default_factory=list)   # [(t_issue_observed, k_observed)]

    @property
    def overhead(self) -> float:
        """sent / real chunks (1.0 = no cover traffic)."""
        return self.sent_chunks / max(1, self.real_chunks)

    @property
    def T(self) -> int:
        return sum(k for _, k in self.trace)

    @property
    def R(self) -> int:
        return len(self.trace)


def simulate_session(L, cost: CostModel, policy="adaptive", tau=2.0, b_low=12.0, b_max=60.0,
                     watch_s=None, seeks=(), mode="buffered", k_const=8, rho=2.0,
                     pad_batch=False, bucket_chunks=0, bucket_slots=0, jitter=0.05, kmax=16,
                     rng=None) -> SessionResult:
    """Simulate one viewing session of a video with L chunks (tau seconds each).

    mode 'buffered': client fetches a batch whenever buffer <= b_low (batch size fixed or adaptive).
    mode 'constant': client sends exactly one batch of k_const chunks every k_const*tau/rho seconds,
                     substituting dummy batches when the buffer is full / video exhausted.
    seeks: list of (played_seconds_at_which_seek_happens, target_chunk_index).
    pad_batch: always send full k chunks (dummies for a short final batch).
    bucket_chunks / bucket_slots: after the session ends keep sending dummies until the total
                     number of chunks (buffered) / slots (constant) is a multiple of the bucket.
    """
    rng = rng or np.random.default_rng()
    watch_s = L * tau if watch_s is None else watch_s
    seeks = sorted(seeks)
    S = SessionResult(n_seeks=len(seeks))
    st = dict(t=0.0, buf=0.0, consumed=0.0, nxt=0, started=False, stalled=False, recovering=False,
              t_seek=0.0)
    k = k_const if mode == "constant" else (4 if policy == "adaptive" else int(policy))

    def ended():
        return st["consumed"] >= watch_s - 1e-9 or (st["nxt"] >= L and st["buf"] <= 1e-9)

    def fire_seek():
        if seeks and st["started"] and st["consumed"] >= seeks[0][0] - 1e-9:
            _, target = seeks.pop(0)
            st.update(buf=0.0, nxt=int(target), stalled=False, recovering=True, t_seek=st["t"])
            return True
        return False

    def play(dt, allow_seek=True):
        """advance wall clock by dt; returns True if a seek fired (caller should react)."""
        while dt > 1e-12:
            if not st["started"] or st["consumed"] >= watch_s - 1e-9:
                st["t"] += dt
                return False
            avail = min(st["buf"], watch_s - st["consumed"])
            if seeks:
                avail = min(avail, max(0.0, seeks[0][0] - st["consumed"]))
            if avail <= 1e-12:
                if allow_seek and fire_seek():
                    return True
                if seeks and st["consumed"] >= seeks[0][0] - 1e-9:   # seek pending but disallowed
                    st["t"] += dt
                    return False
                if st["nxt"] >= L:                                   # nothing left to play
                    st["t"] += dt
                    st["consumed"] = watch_s
                    return False
                if st["recovering"]:
                    S.seek_latency_s += dt
                else:
                    S.rebuffer_s += dt
                    if not st["stalled"]:
                        S.rebuffer_n += 1
                        st["stalled"] = True
                st["t"] += dt
                return False
            s = min(dt, avail)
            st["consumed"] += s
            st["buf"] -= s
            st["t"] += s
            dt -= s
            st["stalled"] = False
        return False

    def issue(n_real, k_send):
        """send one batch (k_send chunks of which n_real are real) and wait for it."""
        t_obs = st["t"] + rng.normal(0, jitter) if jitter else st["t"]
        S.trace.append((t_obs, k_send))
        up, down = cost.bytes(k_send)
        S.bytes_up += up
        S.bytes_down += down
        S.sent_chunks += k_send
        S.real_chunks += n_real
        f = cost.fetch_time(k_send)
        play(f, allow_seek=False)
        if n_real:
            st["buf"] += n_real * tau
            st["nxt"] += n_real
            if not st["started"]:
                st["started"] = True
                S.startup_s = st["t"]
            st["recovering"] = False      # stall time since the seek was booked in play()
        return f

    if mode == "buffered":
        guard = 0
        while not ended():
            guard += 1
            if guard > 100000:
                raise RuntimeError("buffered-mode session did not terminate")
            if fire_seek():
                pass
            if st["nxt"] >= L:
                if play(st["buf"]):
                    continue
                break
            dt = max(0.0, st["buf"] - b_low) if st["started"] else 0.0
            if play(dt):
                continue                       # seek fired -> react immediately (the leak)
            if ended():
                break
            n = min(k, L - st["nxt"])
            f = issue(n, k if pad_batch else n)
            if policy == "adaptive":
                if f < 0.25 * k * tau:
                    k = min(kmax, 2 * k)
                elif f > 0.6 * k * tau:
                    k = max(1, k // 2)
        if bucket_chunks:
            extra = (-S.T) % bucket_chunks
            while extra > 0:
                kk = min(k, extra)
                S.trace.append((st["t"] + k * tau * 0 + rng.normal(0, jitter), kk))
                S.sent_chunks += kk
                up, down = cost.bytes(kk)
                S.bytes_up += up
                S.bytes_down += down
                st["t"] += k * tau
                extra -= kk
    else:  # constant-rate cover traffic
        P = k_const * tau / rho
        slot = 0
        while True:
            t_slot = slot * P
            if slot > 100000:
                raise RuntimeError("constant-mode session did not terminate")
            fire_seek()
            if t_slot > st["t"]:
                if play(t_slot - st["t"]):
                    continue
            if ended() and (not bucket_slots or slot % bucket_slots == 0):
                break
            real_possible = (st["nxt"] < L and st["buf"] + k_const * tau <= b_max
                             and st["consumed"] < watch_s - 1e-9)
            n = min(k_const, L - st["nxt"]) if real_possible else 0
            issue(n, k_const)
            slot += 1

    S.duration_s = st["t"]
    return S


def summarize(results: list[SessionResult]) -> dict:
    a = lambda f: float(np.mean([f(r) for r in results]))
    return dict(startup_s=a(lambda r: r.startup_s), rebuffer_n=a(lambda r: r.rebuffer_n),
                rebuffer_s=a(lambda r: r.rebuffer_s), seek_latency_s=a(lambda r: r.seek_latency_s),
                overhead=a(lambda r: r.overhead),
                mb_down=a(lambda r: r.bytes_down / 2**20), mb_up=a(lambda r: r.bytes_up / 2**20))
