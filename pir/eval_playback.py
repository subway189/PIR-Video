#!/usr/bin/env python3
"""Playback-quality evaluation: startup latency, rebuffering and seek latency for
  {non-private HTTP baseline, two-server XOR PIR, single-server lattice PIR}
  x batch size (chunks per PIR round)  x  network condition.
Uses player.SeekablePlayer for every scheme, so only the transport differs.

Scenarios
  play : play chunks 0..(--play-chunks-1) with no seeks                  (startup + rebuffering)
  seek : play 0-3, seek->16 (cold), play 16-19, seek->22 (inside buffer?), play 22-23,
         seek->5 (cold, backwards), play 5-7                              (seek latency)
Network is emulated: latency + bytes/rate sleep after real compute (PIR backends) or
server-side throttling (HTTP baseline). The two XOR servers run one after the other
in-process, so XOR compute here is up to 2x what two machines would show (negligible at this
catalog size). first_visit_startup_s adds the lattice hint download (analytic).
Writes results_playback.csv (one row per session) -- run on an otherwise idle machine.
"""
import argparse
import csv
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
for cand in (os.path.join(HERE, "..", "baseline"), "/mnt/user-data/outputs/baseline", "/home/claude/baseline"):
    if os.path.exists(os.path.join(cand, "client.py")):
        sys.path.insert(0, os.path.abspath(cand))
        break

from client import HTTPChunkSource  # noqa: E402
from make_catalog import build_catalog  # noqa: E402
from server import BaselineServer  # noqa: E402

import pir_source as ps  # noqa: E402
from player import SeekablePlayer  # noqa: E402

SEEK_SCRIPT = [(4, 16), (8, 22), (10, 5)]
SEEK_MAX = 13


def parse_links(s):
    out = []
    for tok in s.split(","):
        rate = float(tok)
        out.append((f"{tok} Mbps" if rate else "unthrottled", int(rate * 1000), 40 if rate else 0))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--videos", type=int, default=10)
    ap.add_argument("--chunks", type=int, default=30)
    ap.add_argument("--chunk-size", type=int, default=131072)
    ap.add_argument("--play-chunks", type=int, default=16)
    ap.add_argument("--batches", default="1,2,4")
    ap.add_argument("--links", default="20,10,5,3", help="Mbps list (0 = unthrottled)")
    ap.add_argument("--seek-links", default="10,5")
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--seek-reps", type=int, default=None, help="default: max(1, reps//2)")
    ap.add_argument("--scenarios", default="play,seek")
    ap.add_argument("--max-hint-mib", type=int, default=32)
    ap.add_argument("--csv", default="results/playback.csv")
    a = ap.parse_args()
    batches = [x if x == "adaptive" else int(x) for x in a.batches.split(",")]

    with tempfile.TemporaryDirectory() as tmp:
        cat = os.path.join(tmp, "catalog")
        build_catalog(cat, a.chunk_size, 1.0, synthetic_videos=a.videos, chunks_per_video=a.chunks)
        manifest, index, recs = ps.load_catalog_records(cat)
        video = sorted(manifest["videos"])[a.videos // 2]
        print(f"catalog {len(recs)} records x {a.chunk_size} B = {recs.nbytes / 2**20:.1f} MiB; "
              f"stream ~{a.chunk_size * 8 / 1000:.0f} kbps; video {video}", flush=True)

        srv = BaselineServer(("127.0.0.1", 0), cat)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        xor = ps.XorBackend(recs)
        lat = ps.LatticeBackend(recs, max_hint_bytes=a.max_hint_mib << 20)
        print(f"lattice: k={lat.params.k} hint {lat.params.hint_bytes / 2**20:.1f} MiB setup {lat.offline['setup_s']}s "
              f"up {lat.params.upload_per_record / 1024:.0f} KiB down {lat.params.download_per_record / 1024:.0f} KiB per chunk", flush=True)

        schemes = [("baseline (non-private)", None, [1])] + [("two-server XOR", xor, batches), ("single-server lattice", lat, batches)]
        rows = []

        def one(scenario, scheme, backend, b, label, rate, latency, rep):
            if backend is None:
                srv.set_conditions(rate, latency)
                src = HTTPChunkSource(url)
            else:
                backend.link = (rate, latency) if (rate or latency) else None
                backend.stats = ps._Stats()
                src = ps.PIRChunkSource(manifest, index, backend)
            p = SeekablePlayer(src, video, batch=b)
            if scenario == "play":
                m = p.run(max_chunks=a.play_chunks)
            else:
                m = p.run(seeks=SEEK_SCRIPT, max_chunks=SEEK_MAX)
            if backend is None:
                src.close()
            hint_s = (backend.offline["hint_bytes"] * 8 / (rate * 1000) + latency / 1000) if (backend and rate) else 0.0
            st = backend.stats.as_dict() if backend else {}
            row = {k: v for k, v in m.items() if k not in ("play_log", "seek_log")}
            for i, sk in enumerate(m["seek_log"], 1):  # per-seek detail: target, hit, latency
                row[f"seek{i}_target"], row[f"seek{i}_hit"], row[f"seek{i}_latency_s"] = sk["target"], int(sk["hit"]), round(sk["latency_s"], 4)
            row.update(scenario=scenario, scheme=scheme, batch=b, link=label, rate_kbps=rate, rep=rep,
                       first_visit_startup_s=round(m["startup_latency_s"] + hint_s, 2),
                       hint_mib=round(backend.offline["hint_bytes"] / 2**20, 1) if backend else 0,
                       up_kib_per_chunk=round(st.get("up_bytes_per_record", 0) / 1024, 1),
                       down_kib_per_chunk=round(st.get("down_bytes_per_record", 131072) / 1024, 1),
                       server_ms_per_chunk=st.get("server_ms_per_record", 0.0))
            rows.append(row)
            print(f"{scenario:4s} {scheme[:14]:14s} B={b} {label:>11s} rep{rep}: startup {m['startup_latency_s']:6.2f}s "
                  f"rebuf {m['rebuffer_count']:2d} ({m['rebuffer_total_s']:5.2f}s) rounds {m['rounds']:2d} "
                  f"wasted {m['wasted_fetches']} seekmiss {m['seek_miss_latency_s_mean']:.2f}s", flush=True)

        t_start = time.time()
        scen = a.scenarios.split(",")
        for rep in range(a.reps if "play" in scen else 0):
            for label, rate, latency in parse_links(a.links):
                for scheme, backend, bs in schemes:
                    for b in bs:
                        one("play", scheme, backend, b, label, rate, latency, rep)
        for rep in range((a.seek_reps or max(1, a.reps // 2)) if "seek" in scen else 0):
            for label, rate, latency in parse_links(a.seek_links):
                for scheme, backend, bs in schemes:
                    for b in bs:
                        one("seek", scheme, backend, b, label, rate, latency, rep)
        srv.shutdown()
        print(f"total {time.time() - t_start:.0f}s", flush=True)

    os.makedirs(os.path.dirname(a.csv) or ".", exist_ok=True)
    cols = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    with open(a.csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print("wrote", a.csv)


if __name__ == "__main__":
    main()
