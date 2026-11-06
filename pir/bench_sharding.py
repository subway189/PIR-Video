#!/usr/bin/env python3
"""Large-catalog + sharding benchmark for the shared pir_core backends (run from the project root with
PYTHONPATH=. or via: cd pir && PYTHONPATH=.. python3 bench_sharding.py).

For each catalog size and shard count S, builds the sharded servers, answers one k-record query, checks the
decoded records against the originals, and reports
  server_max_ms : slowest shard  = online latency if the S shards run on S machines in parallel
  server_sum_ms : total CPU work (what this single 2-core box actually spent, shards run one after another)
  setup_s       : one-time cost (LWE: hint = D*A);  hint_MiB : one-time download
Parallel latency is a projection from per-shard timings (this machine has 2 cores), not a cluster measurement.
"""
import argparse, csv, os, sys, time
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from pir_core import (LwePIRClient, ShardedLweServer, ShardedXorServer, XorPIRClient, noise_margin_sigmas)


def make(N, B, seed=0):
    rng = np.random.default_rng(seed)
    r = np.empty((N, B), dtype=np.uint8)
    for lo in range(0, N, 1024):
        r[lo:lo + 1024] = rng.integers(0, 256, (min(1024, N - lo), B), dtype=np.uint8)
    return r


def xor_case(recs, S, k):
    N, B = recs.shape
    srv = ShardedXorServer(recs, S)
    cli = XorPIRClient(N, B)
    idx = [int(i) for i in np.random.default_rng(1).integers(0, N, k)]
    st, qs = cli.query(idx)
    best = None
    for _ in range(2):
        times = [[], []]
        ans = []
        for r, q in enumerate(qs):
            outs = []
            for sh in srv.shards:
                t = time.perf_counter(); o = sh.answer(q); times[r].append(time.perf_counter() - t); outs.append(o)
            a = outs[0]
            for o in outs[1:]:
                a = a ^ o
            ans.append(a)
        mx, sm = max(times[0]), sum(times[0])  # one replica's shards
        best = (mx, sm) if best is None or sm < best[1] else best
    assert np.array_equal(cli.decode(st, ans), recs[idx])
    return dict(server_max_ms=best[0] * 1e3, server_sum_ms=best[1] * 1e3, setup_s=0.0, hint_MiB=0.0,
                up_KiB=sum(q.nbytes for q in qs) / 1024, down_KiB=2 * S * k * B / 1024)


def lwe_case(recs, S, k):
    N, B = recs.shape
    t = time.perf_counter(); srv = ShardedLweServer(recs, S)
    hint = srv.hint(); setup = time.perf_counter() - t
    cli = LwePIRClient(N, B, hint)
    idx = [int(i) for i in np.random.default_rng(1).integers(0, N, k)]
    st, Q = cli.query(idx)
    best = None
    for _ in range(2):
        times, outs = [], []
        for sh in srv.shards:
            t = time.perf_counter(); o = sh.answer(Q); times.append(time.perf_counter() - t); outs.append(o)
        best = (max(times), sum(times)) if best is None or sum(times) < best[1] else best
    assert np.array_equal(cli.decode(st, np.concatenate(outs, axis=0)), recs[idx])
    return dict(server_max_ms=best[0] * 1e3, server_sum_ms=best[1] * 1e3, setup_s=setup, hint_MiB=hint.nbytes / 2**20,
                up_KiB=Q.nbytes * S / 1024, down_KiB=sum(o.nbytes for o in outs) / 1024,
                noise_margin_sigmas=noise_margin_sigmas(srv.layout.cols))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--record-kib", type=int, default=64)
    ap.add_argument("--xor-mib", default="64,256,1024")
    ap.add_argument("--lwe-mib", default="64,128,256")
    ap.add_argument("--shards", default="1,2,4,8")
    ap.add_argument("--k", type=int, default=4)
    ap.add_argument("--csv", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "sharding_large_catalog.csv"))
    a = ap.parse_args()
    B = a.record_kib * 1024
    rows = []
    for scheme, sizes, fn in (("xor", a.xor_mib, xor_case), ("lwe", a.lwe_mib, lwe_case)):
        for mib in (int(x) for x in sizes.split(",")):
            recs = make(mib * 2**20 // B, B)
            for S in (int(x) for x in a.shards.split(",")):
                r = dict(scheme=scheme, catalog_MiB=mib, record_KiB=a.record_kib, N=recs.shape[0], shards=S, k=a.k, **fn(recs, S, a.k))
                rows.append(r)
                print({k: (round(v, 2) if isinstance(v, float) else v) for k, v in r.items()}, flush=True)
            del recs
    cols = []
    for r in rows:
        for c in r:
            if c not in cols:
                cols.append(c)
    os.makedirs(os.path.dirname(a.csv), exist_ok=True)
    with open(a.csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(rows)
    print("wrote", a.csv)


if __name__ == "__main__":
    main()
