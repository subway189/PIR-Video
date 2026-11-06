#!/usr/bin/env python3
"""Run the baseline end to end under several network conditions.

Builds a small synthetic catalog, starts the server in a background thread,
streams one video under each condition, prints a comparison table, writes
results_baseline.csv (the same columns the PIR runs will produce), and shows
what the server learned about the viewer.
"""
import argparse
import csv
import json
import tempfile
import threading
import os

from client import HTTPChunkSource, run_session
from make_catalog import build_catalog
from server import BaselineServer

# (label, rate_kbps (0 = unlimited), latency_ms)
CONDITIONS = [
    ("unthrottled", 0, 0),
    ("2000 kbps, 40 ms", 2000, 40),
    ("800 kbps, 40 ms", 800, 40),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks", type=int, default=10)
    ap.add_argument("--chunk-size", type=int, default=131072)
    ap.add_argument("--chunk-seconds", type=float, default=1.0)
    ap.add_argument("--catalog-dir", default=None, help="reuse an existing catalog instead of a temp one")
    ap.add_argument("--csv", default="results_baseline.csv")
    a = ap.parse_args()

    tmp = None
    catalog = a.catalog_dir
    if catalog is None:
        tmp = tempfile.TemporaryDirectory()
        catalog = os.path.join(tmp.name, "catalog")
        build_catalog(catalog, a.chunk_size, a.chunk_seconds, synthetic_videos=5, chunks_per_video=a.chunks)
    manifest = json.load(open(os.path.join(catalog, "manifest.json")))
    video = sorted(manifest["videos"])[2]
    stream_kbps = manifest["chunk_size"] * 8 / 1000 / manifest["chunk_seconds"]
    print(f"\nstream bitrate ~{stream_kbps:.0f} kbps; watching {video}\n")

    srv = BaselineServer(("127.0.0.1", 0), catalog)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"

    rows = []
    for label, rate, lat in CONDITIONS:
        srv.set_conditions(rate, lat)
        srv.clear_access()
        src = HTTPChunkSource(url)
        m = run_session(src, video, verbose=False)
        src.close()
        m["condition"] = label
        rows.append(m)
        print(f"done: {label}")

    cols = ["condition", "startup_latency_s", "rebuffer_count", "rebuffer_total_s", "rebuffer_ratio",
            "fetch_ms_mean", "fetch_ms_p95", "throughput_kbps", "total_bytes", "requests"]
    print("\n" + " | ".join(f"{c:>17}" for c in cols))
    for r in rows:
        print(" | ".join(f"{str(r[c]):>17}" for c in cols))

    with open(a.csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["condition"] + [k for k in rows[0] if k != "condition"])
        w.writeheader()
        w.writerows(rows)
    print(f"\nwrote {a.csv}")

    s = srv.summary()
    print("\nWhat the server learned during the last session (this is the leak PIR must remove):")
    print(f"  client {s['first_requests'][0]['client']} requested {s['requests_per_video']} "
          f"-> exact chunk sequence: {[e['chunk'] for e in srv.access]}")
    srv.shutdown()
    if tmp:
        tmp.cleanup()


if __name__ == "__main__":
    main()
