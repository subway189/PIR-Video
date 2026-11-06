#!/usr/bin/env python3
"""Non-private baseline streaming client (naive: one full download per chunk).

Playback model
  * A fetcher thread downloads chunks one at a time, in order, and keeps the
    buffer topped up to --target-buffer seconds.
  * The player waits for --startup-chunks, then "plays" one chunk per
    chunk_seconds of wall-clock time. If the buffer runs dry it stalls
    (a rebuffer event) until --resume-chunks are available again.

The fetch step goes through a ChunkSource (fetch(video_id, idx) -> record), so
the PIR backends can later be dropped in behind the same buffer/playback code
and produce metrics with exactly the same fields.

Metrics (all times in seconds unless noted)
  startup_latency_s   session start (before manifest request) -> first chunk starts playing
  rebuffer_count      stalls after playback started
  rebuffer_total_s    total time spent stalled
  rebuffer_ratio      stalled / (stalled + media played)
  fetch_ms_*          per-chunk download time (mean/p50/p95/max)
  total_bytes         bytes downloaded (padded records, i.e. what crosses the wire)
  throughput_kbps     bytes downloaded / time spent downloading
"""
import argparse
import hashlib
import http.client
import json
import statistics
import threading
import time
from collections import deque
from urllib.parse import urlparse


class HTTPChunkSource:
    """Baseline retrieval: plain HTTP GET of the exact chunk wanted (NOT private)."""

    def __init__(self, base_url, timeout=60):
        u = urlparse(base_url)
        self.host, self.port, self.timeout = u.hostname, u.port or 80, timeout
        self.conn = None

    def _get(self, path):
        for attempt in range(2):
            try:
                if self.conn is None:
                    self.conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
                self.conn.request("GET", path)
                resp = self.conn.getresponse()
                body = resp.read()
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status} for {path}")
                return resp, body
            except (http.client.HTTPException, OSError):
                self.close()
                if attempt == 1:
                    raise

    def get_manifest(self):
        return json.loads(self._get("/manifest.json")[1])

    def fetch(self, video_id, idx):
        return self._get(f"/v/{video_id}/{idx}")[1]

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None


def _pct(sorted_vals, p):
    return sorted_vals[int(p * (len(sorted_vals) - 1))] if sorted_vals else 0.0


def run_session(source, video_id, *, target_buffer_s=8.0, startup_chunks=2, resume_chunks=1,
                start_chunk=0, max_chunks=None, verify=True, save_stream=None, verbose=True):
    t0 = time.monotonic()
    manifest = source.get_manifest()
    manifest_s = time.monotonic() - t0
    video = manifest["videos"][video_id]
    chunk_seconds = manifest["chunk_seconds"]
    end = video["num_chunks"] if max_chunks is None else min(video["num_chunks"], start_chunk + max_chunks)
    total = end - start_chunk
    startup_chunks = min(startup_chunks, total)

    cv = threading.Condition()
    buf = deque()  # (idx, record); excludes the chunk currently "playing"
    state = {"done": False, "error": None, "stop": False}
    fetch_times, fetch_bytes = [], []

    def fetcher():
        try:
            for idx in range(start_chunk, end):
                with cv:
                    while len(buf) * chunk_seconds >= target_buffer_s and not state["stop"]:
                        cv.wait(0.05)
                    if state["stop"]:
                        return
                t = time.monotonic()
                rec = source.fetch(video_id, idx)
                dt = time.monotonic() - t
                if verify and hashlib.sha256(rec).hexdigest() != video["chunks"][idx]["sha256"]:
                    raise ValueError(f"integrity check failed for {video_id} chunk {idx}")
                fetch_times.append(dt)
                fetch_bytes.append(len(rec))
                with cv:
                    buf.append((idx, rec))
                    cv.notify_all()
        except Exception as e:  # surfaced to the player thread
            state["error"] = e
        finally:
            with cv:
                state["done"] = True
                cv.notify_all()

    th = threading.Thread(target=fetcher, daemon=True)
    th.start()

    out = open(save_stream, "wb") if save_stream else None
    stalls, stall_total, played = 0, 0.0, 0
    try:
        with cv:  # startup: wait for the initial buffer
            while len(buf) < startup_chunks and not state["done"]:
                cv.wait(0.05)
        if state["error"]:
            raise state["error"]
        play_start = time.monotonic()
        startup_latency = play_start - t0
        if verbose:
            print(f"[play] started after {startup_latency:.2f}s (manifest {manifest_s * 1000:.0f} ms)")

        while played < total:
            with cv:
                if not buf:
                    if state["error"]:
                        raise state["error"]
                    s0 = time.monotonic()
                    stalls += 1
                    while len(buf) < resume_chunks and not state["done"]:
                        cv.wait(0.05)
                    if state["error"]:
                        raise state["error"]
                    stall = time.monotonic() - s0
                    stall_total += stall
                    if verbose:
                        print(f"[stall] chunk {start_chunk + played} late, stalled {stall:.2f}s")
                if not buf:
                    break
                idx, rec = buf.popleft()
                cv.notify_all()
            if out:
                out.write(rec[: video["chunks"][idx]["len"]])  # strip padding
            time.sleep(chunk_seconds)  # "play" this chunk in real time
            played += 1
    finally:
        state["stop"] = True
        if out:
            out.close()
        th.join(timeout=5)

    wall = time.monotonic() - t0
    ft = sorted(fetch_times)
    dl_time = sum(fetch_times)
    media_s = played * chunk_seconds
    metrics = {
        "video": video_id, "chunks_played": played, "chunk_seconds": chunk_seconds,
        "manifest_s": round(manifest_s, 4), "startup_latency_s": round(startup_latency, 4),
        "rebuffer_count": stalls, "rebuffer_total_s": round(stall_total, 4),
        "rebuffer_ratio": round(stall_total / (stall_total + media_s), 4) if media_s else 0.0,
        "requests": len(fetch_times), "total_bytes": sum(fetch_bytes),
        "fetch_ms_mean": round(statistics.fmean(ft) * 1000, 2) if ft else 0.0,
        "fetch_ms_p50": round(_pct(ft, 0.50) * 1000, 2),
        "fetch_ms_p95": round(_pct(ft, 0.95) * 1000, 2),
        "fetch_ms_max": round(ft[-1] * 1000, 2) if ft else 0.0,
        "throughput_kbps": round(sum(fetch_bytes) * 8 / 1000 / dl_time, 1) if dl_time else 0.0,
        "session_wall_s": round(wall, 3),
    }
    return metrics


def print_metrics(m):
    print("\n=== baseline session metrics ===")
    for k, v in m.items():
        print(f"{k:>20}: {v}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="http://127.0.0.1:8080")
    ap.add_argument("--video", required=True, help="video id from the manifest, e.g. video_000")
    ap.add_argument("--target-buffer", type=float, default=8.0, help="seconds of media to keep buffered")
    ap.add_argument("--startup-chunks", type=int, default=2)
    ap.add_argument("--resume-chunks", type=int, default=1)
    ap.add_argument("--start-chunk", type=int, default=0)
    ap.add_argument("--max-chunks", type=int, default=None)
    ap.add_argument("--no-verify", action="store_true", help="skip SHA-256 check of each chunk")
    ap.add_argument("--save-stream", default=None, help="write the reassembled stream (e.g. out.ts)")
    ap.add_argument("--metrics-out", default=None, help="write metrics JSON here")
    a = ap.parse_args()

    src = HTTPChunkSource(a.server)
    try:
        m = run_session(src, a.video, target_buffer_s=a.target_buffer, startup_chunks=a.startup_chunks,
                        resume_chunks=a.resume_chunks, start_chunk=a.start_chunk, max_chunks=a.max_chunks,
                        verify=not a.no_verify, save_stream=a.save_stream)
    except KeyError:
        raise SystemExit(f"error: video '{a.video}' is not in the server's manifest")
    except (ValueError, RuntimeError, OSError) as e:
        raise SystemExit(f"error: {e}")
    finally:
        src.close()
    print_metrics(m)
    if a.metrics_out:
        with open(a.metrics_out, "w") as f:
            json.dump(m, f, indent=2)


if __name__ == "__main__":
    main()
