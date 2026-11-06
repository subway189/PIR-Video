# CS670 — PIR-Based Private Video Retrieval, practical for real-time playback

Merged deliverable of the four team parts. Run `./run_all_checks.sh` to verify everything (≈4 min;
needs python3 + `pip install -r requirements.txt`; gcc optional — C kernels fall back to numpy).

| Member | Part | Folder | What is there |
|---|---|---|---|
| A (PIR core) | Shared PIR package: two-server XOR IT-PIR and single-server LWE (SimplePIR-style), split client/server API | `pir_core/`, `pir_core_extras/` | `tests/` (23, incl. sharding), `bench/`, `sharded.py` (server-side sharding), `run_catalog.py`, real-catalog results (97 chunks × 367 KB), report pointer/docx |
| A + B (extra) | Independent PIR implementation with C kernels, server-time benchmarks vs catalog size, hint-capped lattice PIR | `pir/` (`two_server_pir.py`, `lattice_pir.py`, `pir_kernels.c`, `bench_*.py`) | 16 tests, `results/` (+ `bench_sharding.py`, `sharding_large_catalog.csv`) |
| B (client/streaming) | Non-private baseline, seekable player (buffer, seek, stale-round discard, batching), playback evaluation | `baseline/`, `pir/` (`player.py`, `eval_playback.py`, `plot_playback.py`) | 18 player tests (incl. adaptive batching), `pir/results/playback_summary.md`, `playback_adaptive.csv` |
| C (server/infra) | TCP chunk server (C++), Python PIR network servers/client for XOR (2 replicas) and LWE, now **sharded** and threaded | `network/` | `README_NETWORK.md`, `SERVER_INFRASTRUCTURE.md`, `benchmark.py`, `loopback_check.py`, `net_source.py` (player ↔ TCP), `stream_over_tcp.py`, `tls_util.py`, `integrity.py`, `robust_source.py`, tests `test_network_stream.py` / `test_security.py` (12) / `test_robust.py` (18), `results/net_stream.csv` |
| D (eval/security) | Threat model, metrics/simulator, attackers, countermeasures, final report section | `security_eval/` | `threat_model.md`, `Final_Report_Evaluation_and_Security.md`, `results/` |
| — | Literature survey report, slides 18–22 + script, 4-member plan | `docs/` | |

## Presentation demo
`PYTHONPATH=. python3 demo/live_demo.py` — 6 acts, ~5 min (baseline leak → XOR PIR → lattice PIR → curious-operator test → cheating/dead servers → numbers). Presenter notes and fallback transcript: `demo/DEMO_GUIDE.md`, `demo/demo_transcript.txt`.

## Status
All suites pass from a fresh unzip (`./run_all_checks.sh`): pir_core 23, own PIR core 16, player 18, adapter 1, evaluation 6,
TCP streaming 1, security 12, fault tolerance 18, plus plaintext and TLS/mTLS loopback runs. Reports: Member A's report is in
`pir_core_extras/report/`, Member D's in `security_eval/Final_Report_Evaluation_and_Security.md`, the client/streaming
sections in `pir/results/playback_summary.md` and `docs/`.

## How the pieces connect
```
baseline/ (catalog + HTTP server, non-private)            security_eval/  (imports pir_core; simulates sessions,
pir/player.py  --ChunkSource-->  pir/pir_source.py           attackers, countermeasures)
                                 pir/pircore_backend.py --> pir_core/ (XorPIR, LwePIR)   <-- network/ wraps the
                                 (own XorBackend/LatticeBackend)                            split Server/Client API in TCP
```
* `pir_core/` is the **canonical shared package**: the network layer and the evaluation code both import it
  (the copy in Member C's zip was byte-identical, so there is one copy here).
* `pir/pircore_backend.py` is the new glue written during the merge: it lets `XorPIR`/`LwePIR` drive the
  seekable player (`test_pircore_adapter.py` plays and seeks through both, SHA-256 verified).
* Member B's `pir/` also keeps its own PIR implementation. It was used for the playback and server-scaling
  measurements, so those results stay reproducible; swap in `PirCoreBackend(LwePIR(), recs)` to rerun them on Member A's core.

## New in the completed version
* **Adaptive batching** (`pir/player.py`, `batch="adaptive"`): starts at 1 chunk/round, doubles up to `batch_max`
  when a round took < 25 % of the playback time it bought, halves above 60 %, resets to 1 after a cold seek.
  Finding (`pir/results/playback_adaptive.csv`, 37.5 MiB catalog, ~1 Mbps stream): on these links the per-chunk cost
  is transfer-bound, so adaptive stays at 1 and matches B=1 (XOR 5 Mbps startup 0.93 s vs 0.99 s; lattice 3.0 s,
  seek miss 3.8 s vs 8.7 s for fixed B=4, which also wastes 12 chunks per seek vs 3). It protects startup/seek
  latency like B=1 and can batch when the server is the bottleneck (unit-tested with fast sources); it does not reduce
  rebuffering for lattice PIR on slow links (13 stalls, 6.3 s, same as B=1) — only more bandwidth does.
* **Sharding** (`pir_core/sharded.py`, `pir_network_server.py --shard I/S`): XOR shards hold record ranges and get
  only their byte-aligned slice of the query; LWE shards hold bands of DB rows and get the same query, the client is
  unchanged. Exactness is tested against the unsharded servers. Measured (`pir/results/sharding_large_catalog.csv`,
  64 KiB records, k=4): XOR 1 GiB 391 ms → 52 ms slowest shard at S=8 (7.6×); LWE 256 MiB 720 ms → 126 ms at S=8 (5.7×).
* **Player over TCP** (`network/net_source.py`, `network/stream_over_tcp.py`): `NetXorBackend` / `NetLweBackend` plug
  the real servers into `PIRChunkSource`; run `python3 network/stream_over_tcp.py --shards 2`.

## Security layer (TLS + integrity)
* **TLS 1.3 only**, server certificate + host-name verification on the client, optional **mutual TLS**
  (`network/tls_util.py`; servers: `--tls-cert/--tls-key/--tls-client-ca`; client: `--tls-ca/--tls-cert/--tls-key`;
  `make_certs()` creates a demo CA + server + client certificates). Plaintext remains only as an explicit lab mode.
* **Signed manifest** (`network/integrity.py`): Ed25519 signature over the canonical manifest (which holds every
  chunk's SHA-256); `integrity.py keygen|sign|verify`. Clients given a public key refuse a missing/forged/edited manifest.
* **Per-chunk verification** (`net_source.VerifiedPIRChunkSource`, CLI `--manifest --pubkey`): a wrong XOR share or
  wrong LWE answer decodes to a record with a different hash -> one retry with fresh query randomness, then
  `IntegrityError`; bad data is never played. Detects but does not attribute cheating (which XOR replica), and a
  server can still refuse to answer. Tests: `network/test_security.py` (12 tests: untrusted CA, TLS 1.2, wrong host name,
  plaintext client, rogue client cert, tampered answers for XOR and LWE, transient fault retried, forged manifest/key/missing sig).
* Needs `pip install cryptography`. `stream_over_tcp.py` runs secure by default (`--insecure` to disable).
* Not covered: hiding request sizes/timing from a network observer (TLS does not; see `security_eval/`), certificate
  revocation, key distribution for the manifest public key, and the non-cryptographic RNG note for the LWE client.

## Fault tolerance: cheating and refusing servers (`network/robust_source.py`)
You cannot *force* a server to answer or to be honest; the system makes both **harmless** by redundancy + verification.
* **XOR, `NetRobustXorBackend`:** instead of one server per share, each share goes to a *group* of F+1 independent units
  (a unit = a replica, possibly several shards). The client accepts the first combination `a ⊕ b` of side-0/side-1 answers
  whose SHA-256 matches the signed manifest, so one honest unit per side is enough; answers that differ from the verified
  one are flagged **cheater** and quarantined, dead/refusing/hanging units are skipped for `cooldown_s`, and a hanging server
  never delays a round (the round returns as soon as a verifying combination exists).
* **LWE, `NetRobustLweBackend`:** F+1 independent servers; query one, verify, fail over on a wrong answer, error or timeout.
* **Guarantees (tested, `network/test_robust.py`, 18 tests):** with ≤ F faulty units per side — lying (whole answer, or one
  row of a batch, or one shard of a sharded unit), refusing (error reply), hanging, or crashed — playback is correct
  and not slowed; with more faults it first uses spares/waits/fails over (below) and only then **fails safe** (`IntegrityError` / `ServerUnavailable`), never returning wrong data.
* **Beyond F faults (so the error case is as rare as possible):** (1) **spare units** — list extra units after the
  primaries and set `fanout=F+1`; spares are contacted only if the first wave cannot produce a verified answer, so
  they cost nothing while the primaries are healthy; (2) **probation, not exile** — a quarantined cheater is re-admitted
  after `ban_s`, dead servers after `cooldown_s`, and when nothing is usable every server is re-probed; (3) **patience** —
  `RobustVerifiedSource` retries with exponential backoff for `patience_s` (default 20 s), so an outage or a burst of
  faults is a *stall* (the player rebuffers and resumes) instead of a crash; (4) **scheme failover** —
  `FailoverBackend([robust XOR, robust LWE])` falls back to the lattice servers if the whole XOR deployment is lost
  (tests: spares absorb 2 cheaters on one side, probation re-admits a repaired server, outage then recovery plays through,
  XOR side lost -> LWE serves, no honest server -> bounded-time error).
* **What cannot be promised:** if *no* honest server of a needed kind is reachable for longer than `patience_s`, no
  protocol can produce the record — the client then stops with `IntegrityError`/`ServerUnavailable` after a bounded wait
  (never wrong data). Size the pool (spares, operators, both schemes) for the failures you want to survive.
* **Costs / limits:** the first wave costs (F+1)× bandwidth and compute; spares and the second scheme cost nothing until
  used. Privacy requires that **no unit of side 0 colludes with any unit of side 1** (units on the same side only see the
  same uniform share) — deploy the two sides under different operators; LWE servers add only computational privacy.
  A cheater that is always slower than the honest units is not observed, but also cannot do any harm.
  Demo: `python3 network/stream_over_tcp.py --fault-tolerance 1` (default; needs secure mode).

## Run
```bash
export PYTHONPATH=$PWD
cd baseline && python3 run_demo.py                       # non-private baseline end to end
cd ../pir && python3 eval_playback.py                    # playback grid (startup/rebuffer/seek) -> results/
cd ../security_eval && python3 -m eval.run_all           # attackers + countermeasures (~5 min)
cd .. && python3 pir_core_extras/run_catalog.py          # needs a chunk folder (see script header)
python3 network/stream_over_tcp.py --shards 2            # player over real TCP PIR servers (sharded)
cd pir && PYTHONPATH=.. python3 bench_sharding.py         # large-catalog + sharding benchmark (~8 min)
# network: see network/README_NETWORK.md (start 2 XOR servers + 1 LWE server, then pir_network_client.py)
```

## Cross-checks and honest caveats (read before quoting numbers)
* **Consistent across members:** XOR moves ≈2× the chunk size per chunk, lattice PIR several times more
  (D: 8.3× at 32 KB chunks / 8.1× in A's 367 KB run; B's hint-capped variant: 4× download and 4× upload per chunk at k=4). XOR keeps up with playback on every tested link down to 3–5 Mbit/s; lattice PIR needs roughly
  13 Mbit/s (D, 367 KB chunks / 2 s) and rebuffers on 3–5 Mbit/s links in B's lower-bitrate test.
* **Numbers are not directly comparable between members:** B measured a ~1 Mbps stream with 128 KiB chunks
  on an emulated link; D simulated 2 s chunks of 367 KB using A's measured costs; A/C ran on different machines
  (C's wall-clock figures include client process start-up). Compare trends, not absolute values.
* **Security status:** XOR is information-theoretically private only if the two servers do not collude
  (D shows 100 % recovery if shares meet). The LWE implementations use NumPy's non-cryptographic PRNG and
  were not checked with a lattice estimator. TLS, integrity and fault tolerance (cheating/refusing servers) are now implemented (see below).
  D's traffic analysis shows request counts/batch sizes still identify the video (30 % top-1 at 200 videos)
  unless last-batch padding / bucketing / constant-rate traffic is used.
* **Previously open items, now done (details below):** adaptive batch sizing in the real player; server-side
  sharding for both schemes (library, network servers, benchmark); a TCP-backed `ChunkSource` so the player streams
  from the real servers; measured large-catalog runs (up to 1 GiB XOR, 256 MiB LWE).
* **Still not done / limits:** (1) Sharding was benchmarked per shard on one 2-core machine: "parallel latency"
  is the slowest shard's time, a projection, not a measured cluster. (2) The C++ chunk server (`network/server`) is
  the non-private baseline and was not changed; sharding lives in the Python PIR servers. (3) XOR sharding by
  record range costs S× download (every shard returns a full-width answer); a per-replica aggregator that XORs
  shard answers locally would remove this. LWE sharding by DB rows costs S× upload (same query sent to each shard).
  (4) LWE was measured up to 256 MiB (hint 64 MiB, setup 27 s) and XOR up to 1 GiB; beyond that is extrapolation,
  and LWE at p=256, q=2^32 stops being correct at very large catalogs (noise margin 69σ at 256 MiB, so fine here).
  (5) The hint is re-downloaded per client start unless `hint_cache` is used.
* Removed from Member C's zip as build/runtime artefacts: `.exe` binaries, `__pycache__`, downloaded `.bin`
  chunks and download folders (listed in its `.gitignore`); `network/videos/sample.mp4` kept for `split_video.py`.
