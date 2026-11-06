#!/usr/bin/env python3
"""Benchmark: single-server lattice PIR (SimplePIR-style) vs catalog size.

For each catalog size N it reports
  setup_s          offline hint computation (once per catalog version)
  hint_mib         what a new client downloads once
  k                columns per record, chosen as the smallest k whose hint fits --max-hint-mib
  server_ms        ONE server answering B queries (one pass over the DB)
  ms_per_record    server_ms / B
  scan_gbps        DB bytes / server time
  overhead_x       ms_per_record / memcpy of one record
  client_ms        query generation + decoding
  up/down bytes    per record fetched
Every retrieved record is checked against the database.
"""
import argparse
import csv
import os
import statistics
import time

import numpy as np

import kernels
import lattice_pir as lp


def run(record_size, sizes, batches, reps, max_hint, lwe_n, seed=1):
    rng = np.random.default_rng(seed)
    rows = []
    for n in sizes:
        recs = rng.integers(0, 256, size=(n, record_size), dtype=np.uint8)
        srv = lp.LatticePIRServer(recs, max_hint_bytes=max_hint, lwe_n=lwe_n)
        hint = srv.setup()
        cli = lp.LatticePIRClient(srv.params, hint)
        p = srv.params
        t = []
        for _ in range(200):
            i = int(rng.integers(0, n))
            t0 = time.perf_counter()
            recs[i].tobytes()
            t.append(time.perf_counter() - t0)
        plain_ms = statistics.median(t) * 1000
        for b in batches:
            srv_t, cli_t = [], []
            for rep in range(reps + 1):
                idx = rng.integers(0, n, size=b)
                c0 = time.perf_counter()
                Q, st = cli.make_queries(idx)
                c1 = time.perf_counter()
                ans = srv.answer(Q)
                c2 = time.perf_counter()
                out = cli.decode(ans, st)
                c3 = time.perf_counter()
                assert np.array_equal(out, recs[idx]), "lattice PIR returned a wrong record"
                if rep:
                    srv_t.append(c2 - c1)
                    cli_t.append((c1 - c0) + (c3 - c2))
            s = statistics.median(srv_t)
            row = {
                "record_size": record_size, "n_records": n, "db_mib": round(n * record_size / 2**20, 2),
                "k": p.k, "l": p.l, "m": p.m, "lwe_n": lwe_n,
                "setup_s": round(srv.setup_s, 3), "hint_mib": round(p.hint_bytes / 2**20, 2),
                "batch": b, "reps": reps,
                "server_ms": round(s * 1000, 3), "server_ms_per_record": round(s * 1000 / (b), 3),
                "server_ms_min": round(min(srv_t) * 1000, 3), "server_ms_max": round(max(srv_t) * 1000, 3),
                "scan_gbps": round(p.db_bytes * b * 1.0 / 1e9 / s, 2),  # DB bytes touched per query-column set
                "plain_ms": round(plain_ms, 4), "overhead_x": round(s * 1000 / b / plain_ms),
                "client_ms": round(statistics.median(cli_t) * 1000, 3),
                "up_bytes_per_record": p.upload_per_record, "down_bytes_per_record": p.download_per_record,
                "noise_margin_sigmas": round(p.noise_margin_sigmas, 1),
            }
            rows.append(row)
            print(f"N={n:6d} ({row['db_mib']:7.1f} MiB) k={p.k:3d} hint {row['hint_mib']:6.1f} MiB "
                  f"setup {srv.setup_s:6.1f}s | B={b:2d} server {row['server_ms']:8.1f} ms "
                  f"({row['server_ms_per_record']:7.1f}/rec) client {row['client_ms']:8.1f} ms "
                  f"up {p.upload_per_record / 2**20:6.2f} MiB down {p.download_per_record / 2**20:5.2f} MiB", flush=True)
        del srv, cli, recs
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--record-size", type=int, default=65536)
    ap.add_argument("--min-n", type=int, default=16)
    ap.add_argument("--max-mib", type=int, default=256)
    ap.add_argument("--batches", default="1,4")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--max-hint-mib", type=int, default=64)
    ap.add_argument("--lwe-n", type=int, default=lp.LWE_N_DEFAULT)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    n_max = a.max_mib * 2**20 // a.record_size
    sizes, n = [], a.min_n
    while n <= n_max:
        sizes.append(n)
        n *= 2
    batches = [int(x) for x in a.batches.split(",")]
    print(f"kernels: {kernels.BUILD_NOTE}; R={a.record_size} B; N in {sizes}; hint cap {a.max_hint_mib} MiB")
    rows = run(a.record_size, sizes, batches, a.reps, a.max_hint_mib << 20, a.lwe_n)
    out = a.out or os.path.join("results", f"lattice_R{a.record_size // 1024}KiB.csv")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print("wrote", out)


if __name__ == "__main__":
    main()
