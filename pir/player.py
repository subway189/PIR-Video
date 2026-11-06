"""Seekable streaming player on top of any ChunkSource (baseline HTTP or a PIR backend).

A source needs get_manifest(), fetch(video, idx) and optionally fetch_batch(video, [idx...]).

Design
  * One fetcher thread keeps a contiguous window of chunks [lo, fetch_next) in `buf`.
    `pos` is the next chunk to play; chunks in [pos - back_keep, pos) are kept so a short
    backward seek is free. The fetcher pulls `batch` consecutive chunks per round (one PIR
    round = one pass over the database for all of them) while there is room in the buffer.
  * seek(target)
      - target already in the window  -> hit: just move `pos`, no network.
      - otherwise                      -> miss: bump the generation counter, drop the buffer,
        restart fetching at `target`. A round that was already in flight cannot be cancelled
        (a PIR query is atomic), so when it returns its generation is stale and the result is
        discarded and counted in `wasted_fetches`. The fetcher is single-threaded, so a miss
        can wait up to one round for that stale round to finish; this is part of the measured
        seek latency.
      - after a miss playback resumes once `seek_chunks` chunks are buffered (same rule as the
        initial start, so seek latency and startup latency are comparable).
  * Everything is verified against the manifest's SHA-256; the exact sequence of chunks played
    is returned in `play_log` so tests can assert it.
Time model: each chunk "plays" for chunk_seconds of wall time (time.sleep), as in the baseline.
"""
import hashlib
import statistics
import threading
import time


class SeekablePlayer:
    def __init__(self, source, video_id, *, batch=1, target_buffer_s=8.0, startup_chunks=2,
                 resume_chunks=1, seek_chunks=None, back_keep=2, chunk_seconds=None, verify=True,
                 batch_min=1, batch_max=8, grow_below=0.25, shrink_above=0.6):
        # batch=int -> fixed chunks per PIR round.  batch="adaptive" -> starts at batch_min, doubles
        # (up to batch_max) when a round took < grow_below of the playback time it bought, halves
        # (down to batch_min) when it took > shrink_above, and resets to batch_min after a cold seek.
        self.source, self.video_id = source, video_id
        self.manifest = source.get_manifest()
        self.video = self.manifest["videos"][video_id]
        self.n = self.video["num_chunks"]
        self.cs = chunk_seconds if chunk_seconds is not None else self.manifest["chunk_seconds"]
        self.adaptive = batch == "adaptive"
        self.batch_min, self.batch_max = max(1, int(batch_min)), max(1, int(batch_max))
        self.grow_below, self.shrink_above = grow_below, shrink_above
        self.batch = self.batch_max if self.adaptive else max(1, int(batch))   # self.batch = largest round
        self.cur_batch = self.batch_min if self.adaptive else self.batch
        self.startup_chunks = startup_chunks
        self.resume_chunks = resume_chunks
        self.seek_chunks = seek_chunks if seek_chunks is not None else startup_chunks
        self.back_keep = back_keep
        self.verify = verify
        # buffer cap in chunks; always large enough for one batch round
        self.cap = max(self.batch, int(target_buffer_s / self.cs))
        self.cv = threading.Condition()
        self.buf = {}
        self.pos = 0
        self.fetch_next = 0
        self.gen = 0
        self.stop = False
        self.error = None
        self.wasted = 0
        self.round_times, self.round_sizes = [], []

    # ---------------- fetcher ----------------
    def _fetch_round(self, idxs):
        if len(idxs) > 1 and hasattr(self.source, "fetch_batch"):
            recs = self.source.fetch_batch(self.video_id, idxs)
        else:
            recs = [self.source.fetch(self.video_id, i) for i in idxs]
        if self.verify:
            for i, r in zip(idxs, recs):
                if hashlib.sha256(r).hexdigest() != self.video["chunks"][i]["sha256"]:
                    raise ValueError(f"integrity check failed for {self.video_id} chunk {i}")
        return recs

    def _fetcher(self):
        try:
            while True:
                with self.cv:
                    while not self.stop and not (self.fetch_next < self.n and
                                                 self.fetch_next - self.pos + min(self.cur_batch, self.n - self.fetch_next) <= self.cap):
                        self.cv.wait(0.05)
                    if self.stop:
                        return
                    g, start = self.gen, self.fetch_next
                    idxs = list(range(start, min(start + self.cur_batch, self.n)))
                t = time.monotonic()
                recs = self._fetch_round(idxs)
                dt = time.monotonic() - t
                with self.cv:
                    self.round_times.append(dt)
                    self.round_sizes.append(len(idxs))
                    if g != self.gen:
                        self.wasted += len(idxs)  # a seek happened while this round was in flight
                        continue
                    if self.adaptive:
                        ratio = dt / (len(idxs) * self.cs)
                        if ratio < self.grow_below and self.cur_batch < self.batch_max:
                            self.cur_batch = min(self.batch_max, self.cur_batch * 2)
                        elif ratio > self.shrink_above and self.cur_batch > self.batch_min:
                            self.cur_batch = max(self.batch_min, self.cur_batch // 2)
                    for i, r in zip(idxs, recs):
                        self.buf[i] = r
                    self.fetch_next = idxs[-1] + 1
                    self._evict()
                    self.cv.notify_all()
        except Exception as e:  # surfaced to the player thread
            with self.cv:
                self.error = e
                self.cv.notify_all()

    def _evict(self):  # caller holds the lock
        lo = self.pos - self.back_keep
        for i in [k for k in self.buf if k < lo]:
            del self.buf[i]

    # ---------------- seek ----------------
    def _seek(self, target):
        """Returns True on a buffer hit. Caller holds the lock."""
        if not 0 <= target < self.n:
            raise ValueError(f"seek target {target} outside 0..{self.n - 1}")
        if target in self.buf:
            self.pos = target
            self._evict()
            self.cv.notify_all()
            return True
        self.gen += 1
        if self.adaptive:
            self.cur_batch = self.batch_min  # small first round after a cold seek -> fast resume
        self.buf.clear()
        self.pos = target
        self.fetch_next = target
        self.cv.notify_all()
        return False

    def _wait_ready(self, need):
        """Block until `need` chunks from pos are buffered (or the video ends). Holds the lock."""
        need = min(need, self.n - self.pos)
        while not all((self.pos + j) in self.buf for j in range(need)):
            if self.error:
                raise self.error
            self.cv.wait(0.02)
        if self.error:
            raise self.error

    # ---------------- session ----------------
    def run(self, *, start_chunk=0, max_chunks=None, seeks=()):
        """Play from start_chunk; `seeks` = [(played_count, target_chunk), ...] fires when that many
        chunks have been played in total. Returns a metrics dict."""
        script = sorted(seeks)
        t0 = time.monotonic()
        th = threading.Thread(target=self._fetcher, daemon=True)
        th.start()
        play_log, seek_log = [], []
        stalls, stall_total, played = 0, 0.0, 0
        try:
            with self.cv:
                self.pos = self.fetch_next = start_chunk
                self.cv.notify_all()
                self._wait_ready(self.startup_chunks)
            startup = time.monotonic() - t0
            limit = max_chunks if max_chunks is not None else 10 ** 9
            while played < limit:
                with self.cv:
                    while script and script[0][0] == played:
                        _, target = script.pop(0)
                        s0 = time.monotonic()
                        hit = self._seek(target)
                        self._wait_ready(self.seek_chunks)
                        seek_log.append({"target": target, "hit": hit, "latency_s": time.monotonic() - s0})
                    if self.pos >= self.n:
                        break
                    if self.pos not in self.buf:  # underrun: rebuffer
                        s0 = time.monotonic()
                        stalls += 1
                        self._wait_ready(self.resume_chunks)
                        stall_total += time.monotonic() - s0
                    idx = self.pos
                    self.pos += 1
                    self._evict()
                    self.cv.notify_all()
                play_log.append(idx)
                time.sleep(self.cs)
                played += 1
        finally:
            with self.cv:
                self.stop = True
                self.cv.notify_all()
            th.join(timeout=30)
        media_s = played * self.cs
        misses = [s for s in seek_log if not s["hit"]]
        return {
            "video": self.video_id, "batch": "adaptive" if self.adaptive else self.batch,
            "batch_sizes": list(self.round_sizes), "chunks_played": played, "chunk_seconds": self.cs,
            "startup_latency_s": round(startup, 4),
            "rebuffer_count": stalls, "rebuffer_total_s": round(stall_total, 4),
            "rebuffer_ratio": round(stall_total / (stall_total + media_s), 4) if media_s else 0.0,
            "rebuffers_per_min": round(stalls / (media_s + stall_total) * 60, 2) if media_s else 0.0,
            "rounds": len(self.round_times), "chunks_fetched": sum(self.round_sizes), "wasted_fetches": self.wasted,
            "round_ms_mean": round(statistics.fmean(self.round_times) * 1000, 2) if self.round_times else 0.0,
            "n_seeks": len(seek_log), "seek_hits": len(seek_log) - len(misses),
            "seek_miss_latency_s_mean": round(statistics.fmean(s["latency_s"] for s in misses), 4) if misses else 0.0,
            "seek_miss_latency_s_max": round(max((s["latency_s"] for s in misses), default=0.0), 4),
            "seek_hit_latency_s_max": round(max((s["latency_s"] for s in seek_log if s["hit"]), default=0.0), 5),
            "play_log": play_log, "seek_log": seek_log,
        }
