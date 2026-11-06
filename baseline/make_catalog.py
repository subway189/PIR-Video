#!/usr/bin/env python3
"""Build a chunked video catalog made of FIXED-SIZE records.

Why fixed-size records? A PIR database needs uniform entries, so the baseline
uses exactly the same layout the PIR backends will use later. Every chunk is
zero-padded up to --chunk-size; the manifest keeps the true length and a
SHA-256 of the padded record so clients can verify what they download.

Two sources of content:
  --synthetic-videos N   deterministic pseudo-random bytes (no ffmpeg needed;
                         ideal for benchmarking the transport/buffering logic)
  --from-video A.mp4 ... real videos, re-encoded and cut into MPEG-TS segments
                         with ffmpeg (playable once the client reassembles them)
"""
import argparse
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

from common import MANIFEST_NAME, chunk_path


def _write_record(catalog_dir, video_id, idx, payload, record_size):
    if len(payload) > record_size:
        raise ValueError(
            f"{video_id} chunk {idx} is {len(payload)} bytes > record size {record_size}"
        )
    padded = payload + bytes(record_size - len(payload))
    os.makedirs(os.path.join(catalog_dir, video_id), exist_ok=True)
    with open(chunk_path(catalog_dir, video_id, idx), "wb") as f:
        f.write(padded)
    return {"len": len(payload), "sha256": hashlib.sha256(padded).hexdigest()}


def build_synthetic(catalog_dir, n_videos, chunks_per_video, chunk_size, seed="pir"):
    videos = {}
    for v in range(n_videos):
        vid = f"video_{v:03d}"
        chunks = []
        for i in range(chunks_per_video):
            payload = hashlib.shake_256(f"{seed}:{vid}:{i}".encode()).digest(chunk_size)
            chunks.append(_write_record(catalog_dir, vid, i, payload, chunk_size))
        videos[vid] = {"title": f"Synthetic video {v}", "num_chunks": len(chunks), "chunks": chunks}
    return videos


def build_from_videos(catalog_dir, paths, chunk_size, chunk_seconds, bitrate_kbps):
    if shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg not found on PATH (needed for --from-video)")
    videos = {}
    for path in paths:
        vid = "".join(c if c.isalnum() or c in "_-" else "_" for c in os.path.splitext(os.path.basename(path))[0])
        with tempfile.TemporaryDirectory() as tmp:
            cmd = [
                "ffmpeg", "-y", "-loglevel", "error", "-i", path,
                "-c:v", "libx264", "-preset", "veryfast",
                "-b:v", f"{bitrate_kbps}k", "-maxrate", f"{bitrate_kbps}k",
                "-bufsize", f"{bitrate_kbps}k",
                "-force_key_frames", f"expr:gte(t,n_forced*{chunk_seconds})",
                "-c:a", "aac", "-b:a", "64k",
                "-f", "segment", "-segment_time", str(chunk_seconds),
                "-segment_format", "mpegts",
                os.path.join(tmp, "seg_%06d.ts"),
            ]
            subprocess.run(cmd, check=True)
            segs = sorted(glob.glob(os.path.join(tmp, "seg_*.ts")))
            sizes = [os.path.getsize(s) for s in segs]
            if max(sizes) > chunk_size:
                sys.exit(
                    f"{vid}: largest segment is {max(sizes)} bytes but --chunk-size is {chunk_size}. "
                    f"Raise --chunk-size (e.g. {((max(sizes) // 1024) + 1) * 1024}) or lower --bitrate-kbps."
                )
            chunks = []
            for i, seg in enumerate(segs):
                with open(seg, "rb") as f:
                    chunks.append(_write_record(catalog_dir, vid, i, f.read(), chunk_size))
        videos[vid] = {"title": os.path.basename(path), "num_chunks": len(chunks), "chunks": chunks}
        print(f"  {vid}: {len(chunks)} chunks, max segment {max(sizes)} B (record size {chunk_size} B)")
    return videos


def build_catalog(catalog_dir, chunk_size, chunk_seconds, synthetic_videos=0,
                  chunks_per_video=30, from_videos=(), bitrate_kbps=None, seed="pir"):
    # Only ever wipe a directory that is already one of our catalogs.
    if os.path.exists(os.path.join(catalog_dir, MANIFEST_NAME)):
        shutil.rmtree(catalog_dir)
    os.makedirs(catalog_dir, exist_ok=True)
    videos = {}
    if synthetic_videos:
        videos.update(build_synthetic(catalog_dir, synthetic_videos, chunks_per_video, chunk_size, seed))
    if from_videos:
        if bitrate_kbps is None:
            # Budget = record size per chunk. Keep 25% headroom for MPEG-TS overhead and
            # rate-control overshoot, and subtract the 64 kbps audio track from video.
            bitrate_kbps = max(100, int(chunk_size * 8 / 1000 / chunk_seconds * 0.75) - 64)
        videos.update(build_from_videos(catalog_dir, from_videos, chunk_size, chunk_seconds, bitrate_kbps))
    if not videos:
        sys.exit("nothing to build: pass --synthetic-videos N and/or --from-video FILE...")
    manifest = {"version": 1, "chunk_size": chunk_size, "chunk_seconds": chunk_seconds, "videos": videos}
    with open(os.path.join(catalog_dir, MANIFEST_NAME), "w") as f:
        json.dump(manifest, f)
    total = sum(v["num_chunks"] for v in videos.values())
    print(f"catalog '{catalog_dir}': {len(videos)} videos, {total} chunks, "
          f"{total * chunk_size / 2**20:.1f} MiB, record size {chunk_size} B, {chunk_seconds}s per chunk")
    return manifest


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--catalog-dir", default="catalog")
    ap.add_argument("--chunk-size", type=int, default=262144, help="fixed record size in bytes (default 256 KiB)")
    ap.add_argument("--chunk-seconds", type=float, default=2.0, help="playback duration of one chunk")
    ap.add_argument("--synthetic-videos", type=int, default=0)
    ap.add_argument("--chunks-per-video", type=int, default=30)
    ap.add_argument("--seed", default="pir")
    ap.add_argument("--from-video", nargs="+", default=[], metavar="FILE")
    ap.add_argument("--bitrate-kbps", type=int, default=None,
                    help="video bitrate for --from-video (default: derived from the record size with 25%% headroom)")
    a = ap.parse_args()
    build_catalog(a.catalog_dir, a.chunk_size, a.chunk_seconds, a.synthetic_videos,
                  a.chunks_per_video, a.from_video, a.bitrate_kbps, a.seed)


if __name__ == "__main__":
    main()
