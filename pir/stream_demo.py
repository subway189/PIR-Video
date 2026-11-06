#!/usr/bin/env python3
"""Stream the same video through the baseline HTTP source and the two PIR backends under
several emulated links, using the baseline's run_session() unchanged.

Writes results_pir_stream.csv. Per-chunk compute is real; the network is emulated
(latency + bytes/rate sleep after compute; for XOR PIR the two servers run one after the
other here, so the compute part is ~2x what two real machines would show).
'first_visit_startup_s' = startup + time to download the lattice hint once (analytic).
Run it on an otherwise idle machine: compute time is part of the result.
"""
import argparse
import csv
import json
import os
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
for cand in (os.path.join(HERE, "..", "baseline"), "/mnt/user-data/outputs/baseline", "/home/claude/baseline"):
    if os.path.exists(os.path.join(cand, "client.py")):
        sys.path.insert(0, os.path.abspath(cand))
        break

from client import HTTPChunkSource, run_session  # noqa: E402
from make_catalog import build_catalog  # noqa: E402
from server import BaselineServer  # noqa: E402

import pir_source as ps  # noqa: E402

LINKS = [("unthrottled", 0, 0), ("20 Mbps, 40 ms", 20000, 40), ("5 Mbps, 40 ms", 5000, 40), ("3 Mbps, 40 ms", 3000, 40)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--videos", type=int, default=5)
    ap.add_argument("--chunks", type=int, default=10)
    ap.add_argument("--chunk-size", type=int, default=131072)
    ap.add_argument("--max-hint-mib", type=int, default=32)
    ap.add_argument("--csv", default="results_pir_stream.csv")
    a = ap.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        cat = os.path.join(tmp, "catalog")
        build_catalog(cat, a.chunk_size, 1.0, synthetic_videos=a.videos, chunks_per_video=a.chunks)
        manifest, index, recs = ps.load_catalog_records(cat)
        video = sorted(manifest["videos"])[a.videos // 2]
        print(f"catalog: {len(recs)} records x {a.chunk_size} B = {recs.nbytes / 2**20:.1f} MiB; "
              f"stream ~{a.chunk_size * 8 / 1000:.0f} kbps; watching {video}", flush=True)

        srv = BaselineServer(("127.0.0.1", 0), cat)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_address[1]}"

        rows = []
        for label, rate, lat in LINKS:
            # --- baseline (non-private) ---
            srv.set_conditions(rate, lat)
            srv.clear_access()
            src = HTTPChunkSource(url)
            m = run_session(src, video, verbose=False)
            src.close()
            m.update(scheme="baseline (non-private)", link=label, up_bytes_per_chunk=0, hint_mib=0,
                     down_bytes_per_chunk=m["total_bytes"] // max(m["requests"], 1),
                     first_visit_startup_s=m["startup_latency_s"], server_learns="exact chunk sequence")
            rows.append(m)
            # --- PIR backends ---
            link = (rate, lat) if (rate or lat) else None
            for backend in (ps.XorBackend(recs, link=link),
                            ps.LatticeBackend(recs, max_hint_bytes=a.max_hint_mib << 20, link=link)):
                m = run_session(ps.PIRChunkSource(manifest, index, backend), video, verbose=False)
                st = backend.stats.as_dict()
                hint_mib = backend.offline["hint_bytes"] / 2**20
                hint_s = (backend.offline["hint_bytes"] * 8 / (rate * 1000) + lat / 1000) if rate else 0.0
                m.update(scheme=backend.name, link=label, up_bytes_per_chunk=st["up_bytes_per_record"],
                         down_bytes_per_chunk=st["down_bytes_per_record"], hint_mib=round(hint_mib, 1),
                         first_visit_startup_s=round(m["startup_latency_s"] + hint_s, 2),
                         server_ms_per_chunk=st["server_ms_per_record"],
                         server_learns="nothing (non-collusion)" if "XOR" in backend.name else "nothing (LWE)")
                rows.append(m)
            print(f"done: {label}", flush=True)
        srv.shutdown()

    cols = ["scheme", "link", "startup_latency_s", "first_visit_startup_s", "rebuffer_count", "rebuffer_total_s",
            "fetch_ms_mean", "fetch_ms_p95", "up_bytes_per_chunk", "down_bytes_per_chunk", "hint_mib"]
    print("\n" + " | ".join(f"{c[:16]:>16}" for c in cols))
    for r in rows:
        print(" | ".join(f"{str(r.get(c, ''))[:16]:>16}" for c in cols))
    allcols = []
    for r in rows:
        for k in r:
            if k not in allcols:
                allcols.append(k)
    with open(a.csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=allcols)
        w.writeheader()
        w.writerows(rows)
    print("wrote", a.csv)


if __name__ == "__main__":
    main()
