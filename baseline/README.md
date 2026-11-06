# Non-private baseline: naive per-chunk streaming

The comparison point for the PIR video system. A plain HTTP server hands out
fixed-size video chunks; a playback client downloads each chunk in full, in
order, and measures how well playback holds up. Every later PIR variant
should be measured with the same client logic, the same catalog and the same
metrics, so the only thing that changes is the retrieval step.

Python 3.9+, standard library only (ffmpeg is needed only for `--from-video`).

## Files

| File | Purpose |
|---|---|
| `make_catalog.py` | Builds a catalog of **fixed-size chunk records** (synthetic data, or real videos cut with ffmpeg into MPEG-TS segments) plus `manifest.json` |
| `server.py` | HTTP chunk server with bandwidth/latency emulation and an access log |
| `client.py` | Playback client: fetcher thread + buffer + player, rebuffer detection, metrics |
| `run_demo.py` | Runs everything end to end under 3 network conditions, prints a table, writes `results_baseline.csv` |
| `common.py` | Shared path/manifest helpers |

## Quick start

```bash
# 1. quickest check: end-to-end demo (temp catalog, ~25 s)
python3 run_demo.py

# 2. separate processes
python3 make_catalog.py --catalog-dir catalog --synthetic-videos 5 --chunks-per-video 30
python3 server.py --catalog-dir catalog --port 8080 --rate-kbps 2000 --latency-ms 40
python3 client.py --server http://127.0.0.1:8080 --video video_002 --metrics-out m.json

# 3. real video: chunk it, stream it, and play the reassembled result
python3 make_catalog.py --catalog-dir catalog --chunk-seconds 2 --from-video my_video.mp4
python3 client.py --video my_video --save-stream out.ts     # open out.ts in VLC/ffplay
```

## Design decisions

- **Fixed-size records.** A PIR database needs uniform entries, so chunks are
  zero-padded to `--chunk-size` (default 256 KiB, about 1.05 Mbps at 2 s per
  chunk). The manifest stores each chunk's true length and the SHA-256 of the
  padded record. The baseline transfers the padded record too, so byte counts
  are directly comparable with PIR (which cannot reveal the true length).
- **One request per chunk, fetched in order.** That is the "naive" baseline.
  The server learns the exact video, chunk index and time of every request.
- **Integrity check.** The client verifies each record's hash against the
  manifest (`--no-verify` to skip). This is also how we will confirm that PIR
  reconstruction returns byte-identical chunks.
- **Playback is simulated in real time** (a chunk "plays" for `chunk_seconds`
  of wall-clock time); nothing is rendered. `--save-stream` writes the
  reassembled stream so you can check it really plays.

## Metrics (identical field names for the PIR runs)

`startup_latency_s`, `rebuffer_count`, `rebuffer_total_s`, `rebuffer_ratio`,
`fetch_ms_mean/p50/p95/max`, `total_bytes`, `requests`, `throughput_kbps`,
`session_wall_s`. Definitions are in the `client.py` docstring.
Startup latency is measured from the start of the session (before the
manifest request) to the first chunk starting to play.

## What this baseline leaks

`server.py` keeps an access log (`--access-log file.jsonl`, and `GET /stats`).
After a session it contains the viewer's address, the video watched and the
exact chunk sequence with timestamps. `run_demo.py` prints this. The PIR
system's job is to make that log uninformative.

## Reference results (localhost, 1 s chunks of 128 KiB ≈ 1.05 Mbps, 10 chunks)

| Condition | Startup | Stalls | Stall time | Mean fetch |
|---|---|---|---|---|
| unthrottled | ~0 s | 0 | 0 s | 0.4 ms |
| 2000 kbps, 40 ms | 1.1 s | 0 | 0 s | 558 ms |
| 800 kbps, 40 ms | 2.6 s | 6 | 1.8 s | 1335 ms |

800 kbps is below the stream bitrate, so it stalls; that is expected. See
`results_baseline.csv` for the full columns from the latest run.

## Plugging in the PIR backends

`run_session()` only touches the network through a **ChunkSource**:

```python
class ChunkSource:
    def get_manifest(self) -> dict: ...
    def fetch(self, video_id, idx) -> bytes: ...   # returns one padded record
    def close(self): ...
```

`HTTPChunkSource` is the baseline implementation. The two-server IT-PIR and
single-server PIR clients should implement the same three methods, and then
`run_session()` and `run_demo.py` work unchanged. The batching task adds a
`fetch_batch(video_id, indices)` method next to `fetch`.

## Limitations to keep in mind

- Rate limiting is per response, not a shared link, and latency is a fixed
  delay. This suffices for reproducible comparisons, not for modelling
  congestion. Use Linux `tc`/containers later if needed.
- One viewer, one quality level, no adaptive bitrate and no seeking yet
  (`--start-chunk` jumps in at a chunk, but there is no mid-stream seek).
- It is Python, so absolute server CPU numbers say nothing about the PIR
  servers. The transport, buffering and stall metrics are the comparable part.
- The server reads each chunk from disk per request (the OS page cache makes
  that fast); there is no encryption/TLS.
