#!/usr/bin/env python3
"""Non-private baseline chunk server.

Endpoints
  GET /manifest.json        catalog description (public)
  GET /v/<video_id>/<idx>   one fixed-size chunk record (raw bytes)
  GET /stats                summary of everything this server has learned

The access log is the point of this baseline: every request names the exact
video and chunk, so the server (or anyone who can read its logs) can
reconstruct what each client watched and when. PIR is meant to remove that.

Network emulation (so benchmarks are reproducible on localhost):
  --rate-kbps    per-response bandwidth cap (0 = unlimited)
  --latency-ms   fixed delay added before every response (RTT-ish)
"""
import argparse
import json
import re
import socket
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from common import chunk_path, load_manifest

CHUNK_RE = re.compile(r"^/v/([A-Za-z0-9_\-]+)/(\d+)$")
BLOCK = 16384


class BaselineServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, catalog_dir, rate_kbps=0, latency_ms=0, access_log=None):
        super().__init__(addr, Handler)
        self.catalog_dir = catalog_dir
        self.manifest = load_manifest(catalog_dir)
        self.manifest_bytes = json.dumps(self.manifest).encode()
        self.set_conditions(rate_kbps, latency_ms)
        self._lock = threading.Lock()
        self.access = []  # what the server learns, one entry per chunk request
        self._log_fh = open(access_log, "a") if access_log else None

    def set_conditions(self, rate_kbps, latency_ms):
        self.rate_bps = rate_kbps * 1000 / 8
        self.latency_s = latency_ms / 1000

    def record_access(self, client, video, chunk):
        entry = {"t": round(time.time(), 3), "client": client, "video": video, "chunk": chunk}
        with self._lock:
            self.access.append(entry)
            if self._log_fh:
                self._log_fh.write(json.dumps(entry) + "\n")
                self._log_fh.flush()

    def clear_access(self):
        with self._lock:
            self.access.clear()

    def summary(self):
        with self._lock:
            acc = list(self.access)
        per_video = Counter(e["video"] for e in acc)
        return {
            "total_chunk_requests": len(acc),
            "distinct_clients": len({e["client"] for e in acc}),
            "requests_per_video": dict(per_video),
            "first_requests": acc[:5],
            "last_requests": acc[-5:],
        }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive, so clients reuse one TCP connection
    server_version = "BaselineVOD/1.0"

    def setup(self):
        super().setup()
        self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def log_message(self, *args):  # silence default stderr logging; we keep our own access log
        pass

    def _reply(self, code, body, ctype, headers=None, paced=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        rate = self.server.rate_bps if paced else 0
        if not rate:
            self.wfile.write(body)
            return
        start, sent = time.monotonic(), 0
        for i in range(0, len(body), BLOCK):
            block = body[i:i + BLOCK]
            self.wfile.write(block)
            sent += len(block)
            delay = start + sent / rate - time.monotonic()
            if delay > 0:
                time.sleep(delay)

    def do_GET(self):
        srv = self.server
        path = urlparse(self.path).path
        try:
            if srv.latency_s:
                time.sleep(srv.latency_s)
            if path == "/manifest.json":
                return self._reply(200, srv.manifest_bytes, "application/json")
            if path == "/stats":
                return self._reply(200, json.dumps(srv.summary(), indent=2).encode(), "application/json")
            m = CHUNK_RE.match(path)
            if not m:
                return self._reply(404, b"not found", "text/plain")
            vid, idx = m.group(1), int(m.group(2))
            video = srv.manifest["videos"].get(vid)
            if video is None or idx >= video["num_chunks"]:
                return self._reply(404, b"no such chunk", "text/plain")
            srv.record_access(self.client_address[0], vid, idx)  # <- the privacy leak
            with open(chunk_path(srv.catalog_dir, vid, idx), "rb") as f:
                data = f.read()
            self._reply(200, data, "application/octet-stream",
                        {"X-Chunk-Len": str(video["chunks"][idx]["len"])}, paced=True)
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--catalog-dir", default="catalog")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--rate-kbps", type=int, default=0)
    ap.add_argument("--latency-ms", type=int, default=0)
    ap.add_argument("--access-log", default=None, help="append JSONL access log to this file")
    a = ap.parse_args()
    try:
        load_manifest(a.catalog_dir)
    except FileNotFoundError:
        raise SystemExit(f"no manifest.json in '{a.catalog_dir}' - build one first with make_catalog.py")
    srv = BaselineServer((a.host, a.port), a.catalog_dir, a.rate_kbps, a.latency_ms, a.access_log)
    print(f"serving '{a.catalog_dir}' on http://{a.host}:{a.port}  "
          f"(rate={a.rate_kbps or 'unlimited'} kbps, latency={a.latency_ms} ms)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nWhat this server learned:\n" + json.dumps(srv.summary(), indent=2))


if __name__ == "__main__":
    main()
