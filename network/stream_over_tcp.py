#!/usr/bin/env python3
"""End-to-end: stream a video through the real TCP PIR servers (sharded) with the seekable player.

  python3 network/stream_over_tcp.py            # synthetic catalog, XOR (2 replicas) and LWE, S shards each
  options: --shards S  --chunks N  --chunk-size B  --batch adaptive|1|2|4  --csv results/net_stream.csv

Starts the servers as subprocesses on localhost, plays the same session (with seeks) over each scheme,
verifies every chunk's SHA-256, prints metrics and writes a CSV. Note: localhost + 2 CPU cores, so
shards compete for the same cores; see pir/bench_sharding.py for the per-shard compute scaling.
"""
import argparse, csv, os, socket, subprocess, sys, tempfile, time
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "pir"))
sys.path.insert(0, os.path.join(ROOT, "baseline"))
sys.path.insert(0, ROOT)
from make_catalog import build_catalog   # noqa: E402
from player import SeekablePlayer       # noqa: E402
import net_source as ns                 # noqa: E402
import robust_source as rsrc           # noqa: E402
import integrity, tls_util            # noqa: E402


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def wait(port, timeout=240):
    t = time.time()
    while time.time() - t < timeout:
        try:
            socket.create_connection(("127.0.0.1", port), 0.5).close(); return
        except OSError:
            time.sleep(0.3)
    raise SystemExit(f"server on {port} did not start")


def launch(scheme, catalog, S, procs, certs=None, mtls=False):
    ports = [free_port() for _ in range(S)]
    for i, p in enumerate(ports):
        procs.append(subprocess.Popen([sys.executable, os.path.join(HERE, "pir_network_server.py"), scheme,
                                       "--port", str(p), "--catalog", catalog, "--shard", f"{i}/{S}"]
                                      + (["--tls-cert", certs["cert"], "--tls-key", certs["key"]] if certs else [])
                                      + (["--tls-client-ca", certs["ca"]] if certs and mtls else []),
                                      env=dict(os.environ, PYTHONPATH=ROOT), stdout=subprocess.DEVNULL))
    for p in ports:
        wait(p)
    return [f"127.0.0.1:{p}" for p in ports]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shards", type=int, default=2)
    ap.add_argument("--videos", type=int, default=3)
    ap.add_argument("--chunks", type=int, default=20)
    ap.add_argument("--chunk-size", type=int, default=65536)
    ap.add_argument("--batch", default="adaptive")
    ap.add_argument("--fault-tolerance", type=int, default=1, metavar="F",
                    help="tolerate F cheating/silent servers per side (F+1 XOR units per side, F+1 LWE units); 0 = plain; needs secure mode")
    ap.add_argument("--insecure", action="store_true", help="plaintext + unsigned manifest (default: TLS 1.3 + mutual auth + signed manifest)")
    ap.add_argument("--csv", default=os.path.join(ROOT, "network", "results", "net_stream.csv"))
    a = ap.parse_args()
    batch = a.batch if a.batch == "adaptive" else int(a.batch)
    procs, rows = [], []
    with tempfile.TemporaryDirectory() as tmp:
        cat = os.path.join(tmp, "catalog")
        build_catalog(cat, a.chunk_size, 1.0, synthetic_videos=a.videos, chunks_per_video=a.chunks)
        certs = tls = pub = None
        if not a.insecure:
            certs = tls_util.make_certs(os.path.join(tmp, "certs"))
            tls = tls_util.client_context(certs["ca"], certs["client_cert"], certs["client_key"])
            priv, pub = integrity.keygen(os.path.join(tmp, "keys"))
            integrity.sign_manifest(cat, priv)
        try:
            F = 0 if a.insecure else a.fault_tolerance
            g = F + 1
            xs = [[launch("xor", cat, a.shards, procs, certs, True) for _ in range(g)] for _ in range(2)]
            ls = [launch("lwe", cat, a.shards, procs, certs, True) for _ in range(g)]
            if F:
                backends = [rsrc.NetRobustXorBackend(xs[0], xs[1], tls=tls), rsrc.NetRobustLweBackend(ls, tls=tls)]
            else:
                backends = [ns.NetXorBackend(xs[0][0], xs[1][0], tls=tls), ns.NetLweBackend(ls[0], tls=tls)]
            for be in backends:
                src = rsrc.make_robust_source(cat, be, pub) if F else ns.make_source(cat, be, pubkey=pub)
                video = sorted(src.manifest["videos"])[0]
                p = SeekablePlayer(src, video, batch=batch, target_buffer_s=6.0)
                m = p.run(seeks=[(6, 15), (9, 2)], max_chunks=14)
                st = be.stats.as_dict()
                row = dict(scheme=be.name, shards=a.shards, secure=not a.insecure, fault_tolerance=F, events=len(getattr(be, 'events', [])), integrity_failures=src.integrity_failures, batch=m["batch"], startup_s=m["startup_latency_s"],
                           rebuffers=m["rebuffer_count"], rebuffer_s=m["rebuffer_total_s"], rounds=m["rounds"],
                           batch_sizes=" ".join(map(str, m["batch_sizes"])), seeks=m["n_seeks"], seek_hits=m["seek_hits"],
                           seek_miss_latency_s=m["seek_miss_latency_s_mean"], wasted=m["wasted_fetches"],
                           hint_mib=round(be.offline["hint_bytes"] / 2**20, 2),
                           up_kib_per_chunk=round(st["up_bytes_per_record"] / 1024, 1),
                           down_kib_per_chunk=round(st["down_bytes_per_record"] / 1024, 1),
                           played=m["chunks_played"])
                rows.append(row)
                print(row, flush=True)
        finally:
            for pr in procs:
                pr.kill()
    os.makedirs(os.path.dirname(a.csv), exist_ok=True)
    with open(a.csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    print("wrote", a.csv)


if __name__ == "__main__":
    main()
