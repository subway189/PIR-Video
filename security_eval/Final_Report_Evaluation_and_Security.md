# Evaluation & Security — Final Report Section
**CS670 · PIR-Based Private Video Retrieval · Evaluation & Security Lead: Krishna Chandu Pujari**

## 1. Summary

We built the evaluation layer for the two PIR backends (two-server XOR IT-PIR, single-server LWE) and used it to answer three questions.

1. **Does private retrieval keep up with playback?** Yes for XOR on every link we tested down to 5 Mbit/s (no rebuffering on a 5-minute, 1.5 Mbit/s video). LWE needs about 13 Mbit/s or more; at 10 Mbit/s it stalls for ~74 s per 5-minute session, at 5 Mbit/s for ~490 s. LWE moves ~8× the chunk size per chunk (XOR 2×).
2. **Does PIR itself leak the index?** No, for the cases the schemes cover: a single XOR server guessing from its share does no better than chance (1.06 % vs 1.03 % at N=97, ±0.14 %); the LWE query entry at the wanted column is statistically uniform in our sanity test. But the XOR guarantee collapses completely if both servers see both shares (100 % recovery in 2,000 trials).
3. **Does the traffic pattern leak what PIR hides?** Yes, unless the client is changed. With no countermeasure, an attacker who sees only request counts and batch sizes identifies the watched video 30 % of the time in a 200-video catalog (a popularity-only guess gets 10 %; uniform chance 0.5 %), and detects a seek with AUC 1.00 (98.7 % true-positive, 0 % false-positive). Padding the last batch, bucketing session length, and constant-rate cover traffic remove this leakage at a measured bandwidth and latency cost (§6–§7).

All numbers below come from the code in `eval/` and the result files in `results/`. Costs per batch are taken from Kartik's real-catalog runs (97 chunks × 367 KB); the playback, attacker and countermeasure experiments are simulations driven by those costs, not measurements on a real network.

## 2. Role deliverables and where they are

| Plan item (Evaluation & Security Lead) | Deliverable | File |
|---|---|---|
| 1. Formal threat model | Adversaries, goals, scope, attacks, countermeasures | `threat_model.md` |
| 1. Metrics pipeline (latency, rebuffer, bandwidth) | `simulate_session`, `summarize`, `CostModel` | `eval/metrics.py` |
| 2. Per-query and per-batch timing instrumentation | `TracedBackend` wrapper (query, server, decode time, bytes up/down per `retrieve()`) | `eval/metrics.py`, `results/e1_instrumented_sweep.csv` |
| 2. Real-time-application benchmarks for comparison | Table in §8 (Popcorn, Addra vs ours) | this report |
| 3. Traffic-analysis attacker + baseline privacy/performance curve | Fingerprint, seek and crypto-layer attackers; curve in §6 | `eval/attacks.py`, `results/e4–e6*` |
| 4. Comparative analysis, attacker evaluation, countermeasures | §5–§7 | this report |
| 5. Final report / presentation | This section + slide outline (§10) | |

Reproduce: `python3 -m eval.run_all` (about 5 minutes, 2 cores) and `python3 -m unittest eval.test_eval -v`.

## 3. Method

**Instrumentation (`TracedBackend`).** Wraps any `pir_core` backend and logs, per `retrieve()` call: client query time, slowest-server time, decode time, bytes up/down, batch size, and ms per chunk. Every retrieved batch in the sweep was checked against the original bytes.

**Playback simulator.** Event-driven client with a playback buffer: it fetches a batch when the buffer falls to 12 s, never above 60 s, plays 2 s per chunk, and records startup latency, rebuffer count and time, seek latency, and bytes. Batch policy is fixed k ∈ {1, 4, 8, 16} or adaptive (k doubles when a fetch takes < 25 % of the batch's playback time and halves when it takes > 60 %). Fetch time = compute(k) + RTT + (up + down)·8 / link, with no overlap (the same conservative model as Kartik's report). Compute(k) is interpolated from his measured runs (mean of his two runs per setting); bytes per chunk are XOR 734 KB down, LWE 1,504 KB up + 1,468 KB down, plain 367 KB. The same simulator emits the request trace a server sees.

**Attackers.** The server or network observer sees `[(time, k)]` per session. The fingerprint attacker knows the catalog lengths, a Zipf(0.8) popularity prior and the viewing model (50 % watch to the end, 50 % abandon at a uniform point, 30 % of sessions contain 1–2 seeks, links drawn from 10/25/50/100 Mbit/s). It learns P(features | video) by simulating the client (200–250 training sessions per video) and returns the maximum-posterior video. Features: total chunks requested T and number of requests R. Tested on fresh sessions.

## 4. Crypto-layer results (`results/e3_crypto_attacks.json`)

| Test | Result | Chance / expected |
|---|---|---|
| XOR, one server, guess a random set bit (N=97, 20,000 trials) | 1.06 % | 1.031 % (95 % CI ±0.14 %) |
| same, guess a random unset bit | 1.01 % | 1.031 % |
| XOR, one server, N=1000 | 0.13 % | 0.100 % (±0.044 %) |
| XOR, largest per-position bit bias over 20,000 queries (N=97) | 0.007 | ~0.007 s.d. per position |
| **XOR, both shares combined (collusion / same server)** | **100 %** (2,000 trials) | 1.03 % |
| LWE, χ² of query top byte at the wanted column (3,000 queries) | 287.4 | 255 ± 22.6 expected |
| LWE, χ² at an unrelated column | 262.5 | 255 ± 22.6 |
| LWE, argmax-guessing attacker | 1.4 % | 1.67 % |

The XOR numbers are consistent with its information-theoretic guarantee. The LWE test is a sanity check only: it ran with a reduced dimension (n=128) on a small catalog (m=60 columns), and a statistical test cannot establish LWE security. For the full parameters (n=1024) we rely on the SimplePIR parameter choice; the team's implementation has not been run through a lattice estimator and uses a non-cryptographic PRNG for secrets and noise.

## 5. Comparative analysis

### 5.1 Playback quality vs. network (simulation, 150 chunks = 5 min, RTT 20 ms, fixed batch k=4, mean of 12 sessions per cell, `results/e2_playback_grid.csv`, rtt_s = 0.02)

| Link | Non-private: startup / rebuffer | XOR: startup / rebuffer | LWE: startup / rebuffer |
|---|---|---|---|
| 5 Mbit/s | 2.4 s / 0 s | 5.1 s / 0 s | 21.7 s / 490 s (36 events) |
| 10 Mbit/s | 1.3 s / 0 s | 2.9 s / 0 s | 10.0 s / 74 s (33 events) |
| 25 Mbit/s | 0.5 s / 0 s | 1.0 s / 0 s | 4.5 s / 0 s |
| 50 Mbit/s | 0.3 s / 0 s | 0.5 s / 0 s | 2.5 s / 0 s |
| 100 Mbit/s | 0.1 s / 0 s | 0.3 s / 0 s | 1.5 s / 0 s |

Data per 5-minute session: non-private ≈ 52 MB; XOR ≈ 105 MB (2.0×); LWE ≈ 215 MB down + 220 MB up (≈ 8.3× in total). Break-even link speed (per-chunk fetch time = 2 s deadline): ≈ 3 Mbit/s for XOR, ≈ 13 Mbit/s for LWE (checked against Kartik's derived 3 and 13).

Batch size barely changes these results (per-chunk cost is nearly flat in k, as Kartik found). Adaptive batching gives similar total rebuffer *seconds* (LWE at 5 Mbit/s: 475 s vs 490 s; at 10 Mbit/s it is worse, 107 s vs 74 s) but many more, shorter rebuffer events on slow links (LWE at 5 Mbit/s: 141 vs 36 events) because it shrinks k toward 1 when fetches are slow — it does not rescue LWE, it trades a few long stalls for many short ones. Startup latency grows with k (first batch must arrive); small first batches are better for startup. Full grid: `results/e2_playback_grid.csv`; figure `results/fig_playback.png`.

### 5.2 Catalog size and server cost

Instrumented real runs on this machine (2 cores; 32 KB chunks; batch k=4; best of 3; `results/e1_instrumented_sweep.csv`):

| Catalog | XOR server time | LWE server time | LWE one-time setup (incl. hint) |
|---|---|---|---|
| 3 MB (97 chunks) | 1 ms | 12 ms | 0.17 s |
| 8–16 MB | 3 ms (16 MB) | 21 ms (8 MB) | 0.43 s (8 MB) |
| 32–64 MB | 11 ms (64 MB) | 57 ms (32 MB) | 1.48 s (32 MB) |

XOR server time grows ≈ linearly (~0.17 ms/MB); LWE ≈ 1.8 ms/MB at 32 MB (2.6–3.9 ms/MB on the smaller catalogs, where fixed costs dominate). A straight-line extrapolation to a 1 GB catalog gives roughly 0.17 s (XOR) and 1.8 s (LWE) of server time per 4-chunk batch (8 s of video); the LWE figure would also need ~8× the catalog size in RAM, so this extrapolation is an estimate, not a measurement. These sweeps independently reproduce Kartik's data volumes: XOR downloads 2.0× the chunk size per chunk, LWE moves 8.3× (our 32 KB chunks) vs his 8.1× (367 KB chunks). The sweep covers catalogs up to 64 MB; larger catalogs were not run.

Compute per viewer (Kartik's 97-chunk, 35 MB catalog, k=8): XOR 72 ms per 16 s of video (0.45 % of a core), LWE ≈ 960 ms per 16 s (6 % of a core). Compute alone would let one core serve ~220 XOR viewers vs ~17 LWE viewers of such a small catalog; real capacity is lower once I/O, memory and catalog growth are counted.

## 6. Traffic-analysis attacker and privacy/performance curve

### 6.1 Baseline leak: the trace identifies the video

| Catalog size V | Attacker top-1 (XOR) | Attacker top-1 (LWE) | Popularity-only guess | Uniform chance | Effective anonymity set (XOR) |
|---|---|---|---|---|---|
| 10 | 45.7 % | 54.0 % | 28.0 % | 10 % | 4.4 |
| 50 | 39.1 % | 36.6 % | 15.3 % | 2 % | 12.6 |
| 200 | 29.7 % | 25.6 % | 10.0 % | 0.5 % | 36.0 |
| 1000 | 11.8 % | 9.9 % | 6.5 % | 0.1 % | 161 |

(`results/e4_fingerprint_vs_catalog.csv`, `fig_attack_vs_catalog.png`.) Both backends leak about equally, since the leak is in the trace, not the PIR scheme. Accuracy falls as the catalog grows (more videos share each length) but stays 1.5–3× above the popularity-only guess. The effective anonymity set is the exponentiated entropy of the attacker's posterior; a value of 36 at V=200 means the viewer is hidden among ~36 videos' worth of probability instead of 200.

Why it works: the total number of chunks requested equals (or is bounded by) the video's length, and a short final batch reveals where the video ended.

### 6.2 Countermeasures (V = 200, XOR; `results/e5_countermeasure_curve.csv`, `fig_privacy_vs_overhead.png`)

| Client configuration | Attacker top-1 | Effective anonymity set | Bandwidth overhead (sent / needed) |
|---|---|---|---|
| None (adaptive batches) | 27.5 % | 41.6 | 1.00× |
| Fixed k=8 | 27.5 % | 41.2 | 1.00× |
| Fixed k=8 + pad last batch | 6.9 % | 51.8 | 1.02× |
| + length bucket 25 chunks | 3.2 % | 60.1 | 1.16× |
| + bucket 50 | 2.0 % | 65.7 | 1.30× |
| + bucket 100 | 1.7 % | 72.4 | 1.65× |
| + bucket 200 | 1.1 % | 86.0 | 2.65× |
| + bucket 400 | 0.8 % | 104.7 | 4.98× |
| + bucket 1600 (all sessions look equal) | 0.5 % | 104.0 | 20.1× |
| Constant-rate cover traffic, no bucket | 9.6 % | 47.2 | 1.71× |
| Constant-rate + bucket 25 slots | 1.7 % | 73.0 | 2.87× |
| Constant-rate + bucket 100 slots | 0.7 % | 105.0 | 8.75× |
| Constant-rate + bucket 400 slots | 0.5 % | 104.0 | 34.6× |

How to read it. Fixed batch size alone does nothing. **Padding the final batch** is nearly free (2 %) and cuts the attacker from 27.5 % to below the 10 % popularity-only guess. Bucketing adds more protection at growing cost. An effective anonymity set of ≈ 104 is the prior's own value for this catalog, i.e. the trace gives the attacker nothing beyond popularity; buckets of 400 chunks reach it at 5× bandwidth. The attacker's accuracy dropping *below* the popularity-only guess in the padded rows means its learned likelihoods are noise there (it has no information to learn from); it is not evidence of a weaker attacker being "better off".

Caveats: (i) the attacker only uses T and R; timing of the last request and finer inter-request patterns could leak more and were not modelled; (ii) the padding schemes pad the *requests*; a real client must keep sending after the user leaves, which is the source of the overhead.

### 6.3 Seek privacy (`results/e6_seek_attack.csv`, 300 sessions with and 300 without one seek, 200-chunk video, 50 Mbit/s)

| Client | Seek-detection AUC | TPR / FPR | Mean seek latency |
|---|---|---|---|
| Buffered, immediate refetch | 1.00 | 98.7 % / 0 % | 2.0 s |
| Buffered, fixed k=8 | 1.00 | 98.3 % / 0 % | 1.1 s |
| Constant-rate (k=8, one batch every 8 s) | 0.50 | 0 % / 0 % | 5.0 s |

A buffered client that flushes its buffer on a seek sends the next request earlier than the steady-state schedule of k·2 s, which is visible even though PIR hides the target. A constant-rate client is undetectable (AUC 0.5, which is chance) but pays ≈ 2.5–5× the seek latency and ≈ 1.9× the bandwidth.

### 6.4 Playback cost of constant-rate cover traffic (`results/e2b_constant_rate_cost.csv`)

| Backend / link | Buffered: startup / seek latency / data down | Constant-rate k=8: startup / seek latency / data down |
|---|---|---|
| XOR, 50 Mbit/s | 0.50 s / 0.6 s / 105 MB | 1.1 s / 1.5 s / 217 MB (1.9×) |
| XOR, 10 Mbit/s | 2.6 s / 0.9 s / 101 MB | 5.2 s / 2.5 s / 212 MB |
| LWE, 50 Mbit/s | 2.3 s / 1.2 s / 212 MB | 5.4 s / 2.7 s / 433 MB |
| LWE, 10 Mbit/s | 9.9 s / 0.8 s, still rebuffers | 19.7 s / 7.6 s, still rebuffers |

Constant-rate is affordable with XOR on any link tested; with LWE it doubles an already large bandwidth bill and is unusable below ≈ 25 Mbit/s. Seek latency here is a mean over sessions that contain a seek, so it varies run to run.

## 7. Countermeasure discussion

* Cheap and worth always doing: fixed batch size and padding the final batch (≈ 2 % overhead).
* A tunable middle: length bucketing. Pick the bucket from the privacy target; 100–200 chunks (≈ 3–7 min of video) costs 1.7–2.7× bandwidth in this catalog.
* Strongest, expensive: constant-rate cover traffic. Needed only if seeks and session timing must also be hidden; best paired with XOR.
* Adaptive batch sizing leaks the viewer's network speed (batch sequence) and is the practical enemy of uniform traces; fixed k in the padded modes avoids it at the cost of the adaptivity the project added for rebuffer control.
* None of this addresses collusion, integrity or IP-level metadata (see `threat_model.md`).

## 8. Comparison with Popcorn and Addra (reported numbers vs ours)

| | Popcorn (NSDI '16) | Addra / FastPIR (OSDI '21) | Ours, XOR | Ours, LWE |
|---|---|---|---|---|
| Workload | ~8,000 titles, 10,000 concurrent clients | 32,000 users, 96-byte voice messages | 1 viewer, 97 chunks × 367 KB | same |
| Reported overhead vs non-private | 1080× compute, 14× I/O, 3.87× cost | 682× ciphertext expansion; 1.46 Mbit/s per user | 2.0× bandwidth; compute 10–128 ms per batch of 1–16 | ≈ 8.1× bandwidth; compute 162–2,274 ms per batch of 1–16 |
| Latency | — | 726 ms 99th-percentile (80 machines) | startup 0.5 s at 50 Mbit/s | startup 2.5 s at 50 Mbit/s |

These are **not like-for-like**: Popcorn and Addra report large multi-user deployments and different metrics, we report a single-viewer prototype on one machine, and our non-private baseline has near-zero compute, so a "×" compute overhead is undefined. The defensible reading is about bandwidth: on a single viewer with 367 KB chunks, modern PIR costs 2× (two non-colluding servers) to ~8× (one server) in data, against 14× I/O and ~682× expansion for the earlier systems on their workloads; server compute per viewer-second is small for XOR and substantial for LWE.

## 9. Limitations

* Timing and transfer are modelled from measured compute and byte counts; no real network, no real two-machine latency, and the two XOR servers ran in one process in the source measurements.
* Per-batch costs come from one small catalog (97 chunks, 35 MB) measured once per setting with ~±50 % noise on the LWE compute figures (Kartik's own caveat). Catalog sizes above 64 MB were not run here.
* The attacker is a fixed, reasonably strong statistical attacker, not a worst case: it uses only (T, R). Real adversaries could add finer timing, cross-session linking (adaptive batch sequences reveal link speed), and external information.
* The simulated viewing model (50 % complete views, Zipf popularity, lognormal lengths around 5 minutes) is an assumption; leakage numbers change with it.
* The LWE statistical test uses reduced parameters; LWE security and the PRNG are not independently verified. No integrity checks and no TLS in the prototype.
* A single bitrate is assumed; multi-bitrate catalogs (as in Popcorn) add another distinguishing feature.

## 10. Presentation outline (evaluation part, ~6 slides)

1. **Threat model** — who we defend against; what PIR does and does not hide (table from §3 of `threat_model.md`).
2. **Does it play?** — `fig_playback.png`; XOR keeps up from 5 Mbit/s, LWE needs ≥ 13 Mbit/s; 2× vs 8× data.
3. **PIR itself is private** — single-server guess = chance; collusion = 100 % (warn about deployment).
4. **But the trace leaks** — `fig_attack_vs_catalog.png`; 30 % vs 10 % at V=200; seek AUC 1.0.
5. **Privacy vs cost** — `fig_privacy_vs_overhead.png`; padding is nearly free, buckets/cover traffic cost 1.7–35×.
6. **Comparison and limits** — table from §8, limitations from §9, future work (real network test, larger catalogs, integrity, stronger attackers).

## References for numbers
Real-catalog costs: `real_catalog_run.txt` and "PIR Core: Implementation and Evaluation on a Real Video Catalog" (Kartik). Popcorn and Addra figures: the literature survey (Gupta et al., NSDI 2016; Ahmad et al., OSDI 2021).
