"""Benchmark both PIR backends. Usage (from pir_core/):  python3 bench/run_bench.py [exp ...]
Experiments: scaling, batch, recsize.   Results are appended to results/bench.csv.
Every measured retrieval is also checked for correctness."""
import csv
import os
import sys
import time
import statistics

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pir_core import LwePIR, XorPIR  # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "..", "results", "bench.csv")
FIELDS = ["exp", "scheme", "scan", "N", "B", "db_mb", "k", "t_query", "t_server", "t_decode",
          "t_latency", "ms_per_record", "bytes_up", "bytes_down", "offline_bytes",
          "t_offline_server", "t_offline_client", "lwe_rows", "lwe_cols"]
KB, MB = 1024, 1024 * 1024
rng = np.random.default_rng(7)


def make(scheme, scan):
    return XorPIR(scan) if scheme == "xor" else LwePIR(scan=scan)


def run(exp, scheme, scan, N, B, ks, writer, reps=3, records=None):
    records = records if records is not None else rng.integers(0, 256, (N, B), dtype=np.uint8)
    p = make(scheme, scan)
    t0 = time.perf_counter()
    p.setup(records)
    setup_s = time.perf_counter() - t0
    for k in ks:
        runs = []
        for _ in range(reps):
            idx = [int(i) for i in rng.integers(0, N, k)]
            out = p.retrieve(idx)
            assert np.array_equal(out, records[idx]), "WRONG RESULT"
            runs.append(p.last_stats)
        st = min(runs, key=lambda s: s.t_compute_latency)  # best of reps, standard for timing
        lay = getattr(getattr(p, "server", None), "layout", None)
        row = dict(exp=exp, scheme=scheme, scan=scan, N=N, B=B, db_mb=round(N * B / MB, 2), k=k,
                   t_query=st.t_query, t_server=st.t_server_max, t_decode=st.t_decode,
                   t_latency=st.t_compute_latency, ms_per_record=1e3 * st.t_compute_latency / k,
                   bytes_up=st.bytes_up, bytes_down=st.bytes_down, offline_bytes=st.offline_bytes,
                   t_offline_server=st.t_offline_server, t_offline_client=st.t_offline_client,
                   lwe_rows=lay.rows if lay else "", lwe_cols=lay.cols if lay else "")
        writer.writerow(row)
        print(f"{exp:8s} {scheme:3s}/{scan:9s} N={N:<6d} B={B//KB:>4d}KB db={N*B/MB:7.1f}MB k={k:<3d} "
              f"server={st.t_server_max*1e3:9.1f}ms total={st.t_compute_latency*1e3:9.1f}ms "
              f"up={st.bytes_up/KB:9.1f}KB down={st.bytes_down/KB:9.1f}KB", flush=True)
    print(f"   (setup incl. offline hint: {setup_s:.1f}s)", flush=True)


def main(exps):
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    new = not os.path.exists(OUT)
    with open(OUT, "a", newline="") as f:
        w = csv.DictWriter(f, FIELDS)
        if new:
            w.writeheader()
        if "scaling" in exps:   # catalog size sweep, 16 KB chunks, single chunk vs batch of 16
            for N in (256, 1024, 4096):
                for scheme, scans in (("xor", ("naive", "optimized")), ("lwe", ("optimized",))):
                    for scan in scans:
                        run("scaling", scheme, scan, N, 16 * KB, (1, 16), w)
        if "batch" in exps:     # batch-size sweep on a fixed 16 MB catalog (1024 x 16 KB)
            for scheme, scan in (("xor", "naive"), ("xor", "optimized"), ("lwe", "optimized")):
                run("batch", scheme, scan, 1024, 16 * KB, (1, 2, 4, 8, 16, 32, 64), w)
        if "recsize" in exps:   # record-size sweep at constant 16 MB catalog
            for B in (4 * KB, 16 * KB, 64 * KB, 256 * KB):
                N = 16 * MB // B
                for scheme in ("xor", "lwe"):
                    run("recsize", scheme, "optimized", N, B, (1, 8), w)
        if "naive_lwe" in exps:  # how much BLAS helps the LWE server (small DB only)
            run("naive_lwe", "lwe", "naive", 256, 16 * KB, (1, 4, 16), w)


if __name__ == "__main__":
    main(sys.argv[1:] or ["scaling", "batch", "recsize"])
