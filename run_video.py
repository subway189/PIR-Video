#!/usr/bin/env python3
"""Stream YOUR OWN video(s) through the private-retrieval system, end to end.

    python run_video.py my_movie.mp4                         # 2-server XOR PIR (default)
    python run_video.py a.mp4 b.mp4 --mode robust            # fault tolerant (cheating/dead servers tolerated)
    python run_video.py clip.mp4 --mode lwe                  # single-server lattice PIR
    python run_video.py --videos-dir D:\\videos               # every .mp4/.mkv/.mov/... in a folder
    python run_video.py clip.mp4 --chunk-size 524288         # better picture quality (bigger chunks, more PIR cost)
    python run_video.py --catalog-dir my_catalog             # reuse a catalog built earlier (no re-encoding)

What happens: (1) ffmpeg re-encodes each video and cuts it into fixed-size MPEG-TS chunks (the PIR database record
size), (2) the catalog manifest is Ed25519-signed, (3) TLS 1.3 + mutual-auth PIR servers start on localhost and load the
catalog, (4) the player streams the chosen video privately with seeks, every chunk checked against the signed manifest,
(5) the whole video is retrieved through PIR again and written to output/<name>_pir.ts -- byte-identical to what the
servers hold -- which you can open in VLC / ffplay. Needs ffmpeg on PATH (Windows: `winget install Gyan.FFmpeg`).
"""
import argparse, hashlib, os, shutil, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
for p in ("", "pir", "baseline", "network", "demo"):
    sys.path.insert(0, os.path.join(HERE, p))
import integrity, tls_util, net_source as ns, robust_source as rs        # noqa: E402
import pir_source as ps                                                  # noqa: E402
from make_catalog import build_catalog                                   # noqa: E402
from player import SeekablePlayer                                        # noqa: E402
from live_demo import Servers, Cheat, Tap                                # noqa: E402

EXT = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".ts", ".flv", ".wmv")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("videos", nargs="*", help="video files to publish")
    ap.add_argument("--videos-dir", help="publish every video file in this folder")
    ap.add_argument("--catalog-dir", default="video_catalog", help="where the catalog is built / reused")
    ap.add_argument("--watch", help="video id to stream (default: the first one; ids are the file names without extension)")
    ap.add_argument("--mode", choices=["xor", "lwe", "robust"], default="xor")
    ap.add_argument("--chunk-size", type=int, default=262144, help="record size in bytes (default 256 KiB)")
    ap.add_argument("--chunk-seconds", type=float, default=2.0)
    ap.add_argument("--play-chunks", type=int, default=12, help="how many chunks to play live (default 12; 0 = all)")
    ap.add_argument("--no-seek", action="store_true", help="play straight through without the scripted seeks")
    ap.add_argument("--no-download", action="store_true", help="skip writing the reassembled video file")
    ap.add_argument("--out-dir", default="output")
    ap.add_argument("--inject-cheater", action="store_true", help="(robust mode) one server corrupts its answers")
    a = ap.parse_args()

    files = list(a.videos)
    if a.videos_dir:
        files += [os.path.join(a.videos_dir, f) for f in sorted(os.listdir(a.videos_dir)) if f.lower().endswith(EXT)]
    missing = [f for f in files if not os.path.isfile(f)]
    if missing:
        sys.exit(f"video file not found: {missing[0]}")

    cat = a.catalog_dir
    if files:
        if not shutil.which("ffmpeg"):
            sys.exit("ffmpeg is needed to prepare videos and was not found on PATH.\n"
                     "  Windows: winget install Gyan.FFmpeg   (or download from ffmpeg.org and add the bin folder to PATH)\n"
                     "  then open a NEW terminal and retry.")
        print(f"[1/5] encoding + chunking {len(files)} video(s) into {a.chunk_size // 1024} KiB records ...", flush=True)
        build_catalog(cat, a.chunk_size, a.chunk_seconds, from_videos=files)
    elif not os.path.exists(os.path.join(cat, "manifest.json")):
        sys.exit("give at least one video file (or --videos-dir), or --catalog-dir of an existing catalog")
    else:
        print(f"[1/5] reusing catalog '{cat}'", flush=True)

    manifest, index, recs = ps.load_catalog_records(cat)
    vids = sorted(manifest["videos"])
    video = a.watch or vids[0]
    if video not in manifest["videos"]:
        sys.exit(f"unknown video '{video}'; available: {vids}")
    n = manifest["videos"][video]["num_chunks"]
    cs = manifest["chunk_seconds"]
    print(f"      catalog: {recs.shape[0]} records of {recs.shape[1] // 1024} KiB ({recs.nbytes / 2**20:.1f} MiB) · "
          f"videos {vids} · watching '{video}' = {n} chunks ≈ {n * cs:.0f} s", flush=True)

    print("[2/5] signing manifest, creating TLS certificates ...", flush=True)
    keydir = os.path.join(cat, "_keys")
    certdir = os.path.join(cat, "_certs")
    priv, pub = integrity.keygen(keydir)
    integrity.sign_manifest(cat, priv)
    certs = tls_util.make_certs(certdir)
    ctx_c = tls_util.client_context(certs["ca"], certs["client_cert"], certs["client_key"])
    ctx_s = tls_util.server_context(certs["cert"], certs["key"], certs["ca"])

    print(f"[3/5] starting {a.mode.upper()} PIR servers (TLS 1.3, client certificates required) ...", flush=True)
    S = Servers(recs, ctx_s)
    taps = []
    try:
        if a.mode == "xor":
            (a0, t0), (a1, t1) = S.xor(), S.xor()
            taps = [t0, t1]
            be = ns.NetXorBackend([a0], [a1], tls=ctx_c)
            src = ns.make_source(cat, be, pubkey=pub)
        elif a.mode == "lwe":
            ad, t0 = S.lwe()
            taps = [t0]
            t = time.time()
            be = ns.NetLweBackend([ad], tls=ctx_c)
            print(f"      lattice hint {be.offline['hint_bytes'] / 2**20:.1f} MiB downloaded in {time.time() - t:.1f} s (one-time)")
            src = ns.make_source(cat, be, pubkey=pub)
        else:
            bad_a = S.xor(Cheat)[0] if a.inject_cheater else None
            side1 = [bad_a or S.xor()[0], S.xor()[0]]
            be = rs.NetRobustXorBackend([S.xor()[0], S.xor()[0]], side1, tls=ctx_c, timeout=3.0)
            src = rs.make_robust_source(cat, be, pub)
            print("      robust mode: 2 servers per share (tolerates 1 faulty per side)")

        print(f"[4/5] streaming '{video}' privately ...", flush=True)
        seeks = []
        if not a.no_seek and n >= 12:
            seeks = [(4, int(n * 0.6)), (8, 1)]
        limit = n if a.play_chunks == 0 else min(n, a.play_chunks + (n // 2 if seeks else 0))
        m = SeekablePlayer(src, video, batch="adaptive", target_buffer_s=min(8.0, n * cs)).run(
            seeks=seeks, max_chunks=None if a.play_chunks == 0 else a.play_chunks + len(seeks) * 2)
        print(f"      played {m['chunks_played']} chunks | startup {m['startup_latency_s']:.2f}s | rebuffers {m['rebuffer_count']} "
              f"({m['rebuffer_total_s']:.1f}s) | seeks {m['n_seeks']} | rounds {m['rounds']} batches {m['batch_sizes'][:10]}")
        print(f"      play order: {m['play_log']}   (every chunk SHA-256-verified)")
        st = be.stats.as_dict()
        print(f"      per chunk: {st["up_bytes_per_record"]:.0f} B up, {st['down_bytes_per_record'] / 1024:.1f} KiB down "
              f"(record = {recs.shape[1] / 1024:.0f} KiB)")
        for i, tp in enumerate(taps):
            if tp.seen:
                print(f"      server {i} saw {len(tp.seen)} requests, each a random-looking vector — no video id, no chunk number")
        if hasattr(be, "events") and be.events:
            print(f"      fault events: {[(e[0], e[1]) for e in be.events][:4]}")

        if not a.no_download:
            print("[5/5] retrieving the entire video through PIR and writing the playable file ...", flush=True)
            os.makedirs(a.out_dir, exist_ok=True)
            out = os.path.join(a.out_dir, f"{video}_pir.ts")
            info = manifest["videos"][video]["chunks"]
            t = time.time()
            h, got = hashlib.sha256(), 0
            with open(out, "wb") as f:
                for lo in range(0, n, 4):
                    idx = list(range(lo, min(n, lo + 4)))
                    for i, rec in zip(idx, src.fetch_batch(video, idx)):
                        piece = rec[:info[i]["len"]]            # strip the zero padding
                        f.write(piece); h.update(piece); got += len(piece)
            ref = hashlib.sha256()
            for i, c in enumerate(info):
                with open(ps.chunk_file(cat, video, i), "rb") as f:
                    ref.update(f.read()[:c["len"]])
            same = h.digest() == ref.digest()
            print(f"      wrote {out}  ({got / 2**20:.1f} MiB in {time.time() - t:.1f} s) · identical to the server's copy: {same}")
            print(f"      play it:  ffplay \"{out}\"   (or open in VLC)")
    finally:
        S.close()


if __name__ == "__main__":
    main()
