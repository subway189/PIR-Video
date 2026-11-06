#!/usr/bin/env python3
"""Benchmark: two-server XOR PIR server-side compute as a function of catalog size.

For every catalog size N (records) and batch size B (records fetched in one pass
over the database) it measures, over several repetitions with random targets:
  * server_ms        wall time of ONE server answering B queries (the two servers
                     do identical work, so this is also the per-server latency
                     when they run on two machines)
  * scan_gbps        database bytes the server actually XORed / server time
  * plain_ms         what a non-private server pays to fetch one record (a memcpy)
  * overhead_x       server_ms per record / plain_ms   (cf. Popcorn's reported 1080x)
  * client_ms        query generation + reconstruction
  * up/down bytes    per record fetched, both servers combined
Every retrieved record is checked against the database.
"""
import argparse
import csv
import os
import statistics
import time

import numpy as np

import kernels
import two_server_pir as tp


def popcount_rows(masks, n):
    return int(np.unpackbits(masks, axis=1, bitorder="little")[:, :n].sum())


def run(record_size, sizes, batches, reps, seed=1):
    n_max = max(sizes)
    rng = np.random.default_rng(seed)
    big = rng.integers(0, 256, size=(n_max, record_size), dtype=np.uint8)
    rows = []
    for n in sizes:
        db = tp.XorPIRDatabase(big[:n])
        server = tp.XorPIRServer(db)
        client = tp.XorPIRClient(n, record_size)
        # non-private reference: copy one record out of memory
        t = []
        for _ in range(200):
            i = int(rng.integers(0, n))
            t0 = time.perf_counter()
            db.record(i)
            t.append(time.perf_counter() - t0)
        plain_ms = statistics.median(t) * 1000
        for b in batches:
            srv_t, cli_t, scanned = [], [], []
            for rep in range(reps + 1):  # first iteration is warm-up
                idx = rng.integers(0, n, size=b)
                c0 = time.perf_counter()
                q1, q2 = client.make_queries(idx)
                c1 = time.perf_counter()
                a1 = server.answer(q1)
                s1 = time.perf_counter()
                a2 = server.answer(q2)  # identical work on the second server
                s2 = time.perf_counter()
                out = client.reconstruct(a1, a2)
                c2 = time.perf_counter()
                for j in range(b):
                    assert out[j].tobytes() == db.record(int(idx[j])), "PIR returned a wrong record"
                if rep == 0:
                    continue
                srv_t.append((s1 - c1 + s2 - s1) / 2)
                cli_t.append((c1 - c0) + (c2 - s2))
                scanned.append(popcount_rows(q1, n) * record_size)
            server_s = statistics.median(srv_t)
            row = {
                "record_size": record_size, "n_records": n, "db_mib": round(n * record_size / 2**20, 2),
                "batch": b, "reps": reps,
                "server_ms": round(server_s * 1000, 3),
                "server_ms_per_record": round(server_s * 1000 / b, 3),
                "server_ms_min": round(min(srv_t) * 1000, 3), "server_ms_max": round(max(srv_t) * 1000, 3),
                "scan_gbps": round(statistics.median(scanned) / 1e9 / server_s, 2),
                "full_db_equiv_gbps": round(n * record_size / 1e9 / server_s, 2),
                "plain_ms": round(plain_ms, 4),
                "overhead_x": round(server_s * 1000 / b / plain_ms),
                "client_ms": round(statistics.median(cli_t) * 1000, 3),
                "up_bytes_per_record": client.query_bytes(1),
                "down_bytes_per_record": client.answer_bytes(1),
            }
            rows.append(row)
            print(f"N={n:6d} ({row['db_mib']:8.1f} MiB) B={b:2d}  server {row['server_ms']:9.2f} ms "
                  f"({row['server_ms_per_record']:8.2f} ms/rec)  {row['scan_gbps']:5.2f} GB/s scanned  "
                  f"plain {plain_ms * 1000:7.1f} us  overhead {row['overhead_x']}x", flush=True)
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--record-size", type=int, default=262144)
    ap.add_argument("--max-mib", type=int, default=1024, help="largest database to test")
    ap.add_argument("--min-n", type=int, default=16)
    ap.add_argument("--batches", default="1,8")
    ap.add_argument("--reps", type=int, default=7)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    n_max = a.max_mib * 2**20 // a.record_size
    sizes, n = [], a.min_n
    while n <= n_max:
        sizes.append(n)
        n *= 2
    batches = [int(x) for x in a.batches.split(",")]
    print(f"kernels: {kernels.BUILD_NOTE}; record {a.record_size} B; N in {sizes}; batches {batches}")
    rows = run(a.record_size, sizes, batches, a.reps)
    out = a.out or os.path.join("results", f"two_server_R{a.record_size // 1024}KiB.csv")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print("wrote", out)


if __name__ == "__main__":
    main()
