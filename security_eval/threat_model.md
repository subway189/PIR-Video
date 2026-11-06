# Threat Model — PIR-Based Private Video Retrieval (CS670)

Owner: Evaluation & Security Lead (Krishna Chandu Pujari). Status: final, matches the code in `eval/` and `pir_core/`.

## 1. System and assets

A client plays a video from a catalog held by the service. The catalog is cut into fixed-size records (chunks, padded to one catalog-wide size B). The client fetches batches of k upcoming chunks through `retrieve(indices)`; the manifest (video → global-index offsets, true chunk lengths) is public.

| Backend | Servers | Client → server | Server → client |
|---|---|---|---|
| Two-server XOR (IT-PIR) | 2 identical replicas | one bit-packed selection vector per chunk, shape (k, ⌈N/8⌉), to each server | (k, B) XOR sum |
| Single-server LWE (SimplePIR-style) | 1 | uint32 matrix (m, K), K = k × span columns; one-time hint download (~23 MB for the real catalog) | uint32 matrix (l, K) |

**Assets to protect**
1. A1 — which video is being watched.
2. A2 — which chunk/timestamp inside it is being fetched (including seek targets).
3. A3 — (secondary, added after the traffic-analysis study) coarse session properties that identify a video indirectly: total length, seek behaviour.

## 2. Adversaries

| ID | Adversary | Capabilities |
|---|---|---|
| ADV1 | Honest-but-curious single server | Sees every query it receives, with arrival times; runs the protocol correctly; unbounded history across sessions; knows the public manifest/catalog and its video lengths. |
| ADV2 | Passive network observer (ISP, on-path) | Sees packet sizes and timing between client and the server(s); cannot read content it cannot decrypt (the network layer now uses TLS 1.3, see below; without it this adversary reads everything on the wire). |
| ADV3 | Colluding servers (XOR backend only) | Both replicas pool their views. |
| ADV4 | Active/malicious server | May return wrong answers, delay, or drop. |

Computational power: ADV1/ADV2 are polynomial-time for the LWE backend; the XOR backend's guarantee against a single server holds against unbounded adversaries.

## 3. Security goals and what each scheme provides

| Goal | XOR (2-server) | LWE (1-server) |
|---|---|---|
| Index privacy against ADV1 (one server) | Information-theoretic: each query vector is uniform | Computational, under LWE (n=1024, q=2³², σ=6.4 per SimplePIR defaults) |
| Index privacy against ADV3 (collusion) | **None** — `q0 ⊕ q1` is the unit vector (verified: 100 % recovery, `results/e3_crypto_attacks.json`) | n/a (single server) |
| Index privacy against ADV2 | Same as ADV1 per share; but an observer of **both** links is ADV3 | Same as ADV1 |
| Integrity against ADV4 | **Detected and tolerated** — with F+1 units per side (`network/robust_source.py`) up to F cheating units per side cannot change the output: a corrupted share/answer yields a record whose SHA-256 differs from the Ed25519-signed manifest, so the client raises `IntegrityError` (`network/integrity.py`, `net_source.VerifiedPIRChunkSource`); the cheater is identified by comparison with the verified answer and quarantined; with more than F faulty units per side the client fails safe | **Detected, not prevented** — same check on the decoded record (note: tampering below the 2^24 rounding step is absorbed by the LWE noise tolerance and is harmless) |
| Hiding request metadata (batch size, timing, session length) | **No** | **No** |

Deployment requirement for XOR privacy: the two servers must be operated by independent parties and `q0`, `q1` must never reach the same server (README_NETWORK warning).

## 4. Scope

**In scope (provided by the PIR layer):** per-request index privacy (A1, A2) against ADV1/ADV3 as stated above.

**In scope (evaluated in this project, not provided by PIR):** leakage through the *observable trace* — request count, batch sizes, request timing, total volume, session duration, and seek-induced irregularities (A3). The original plan listed traffic-volume analysis and buffer-refill timing as out of scope; the final project **moves them in scope for measurement** because no surveyed system tested them (literature survey, §9) and Addra reports that PIR alone does not hide them.

**Out of scope:** IP address / connection metadata (needs an anonymity network), TLS and client authentication, denial of service, malicious-server integrity (listed as a limitation), side channels in the client's local machine, and multi-session intersection attacks beyond what is stated in §6.

## 5. Observables

What each adversary sees per session (this is exactly what `eval/attacks.py` consumes):

* trace = [(t_i, k_i)]: time of each request and its batch size k_i (XOR: array shape; LWE: K = k_i × span ⇒ k_i is recoverable);
* derived: R = number of requests, T = Σk_i total chunks requested, session duration, inter-request gaps;
* for LWE only: a one-time hint download at session start (reveals that a session began, not which video).

PIR does not hide any of these because the server needs k and N to answer.

## 6. Attacks implemented and evaluated

| Attack | Adversary | Model | Result file |
|---|---|---|---|
| Single-server index guessing from XOR query | ADV1 | guess among set/unset bits of one share | `e3_crypto_attacks.json` |
| Colluding-server recovery | ADV3 | XOR the two shares | `e3_crypto_attacks.json` |
| LWE query distinguisher (statistical sanity check, reduced n) | ADV1 | χ² on the query entry at the wanted column; guessing attacker | `e3_crypto_attacks.json` |
| Video-length fingerprinting | ADV1/ADV2 | Bayes-style attacker that knows the catalog lengths, a popularity prior and the viewing model; learns P(T,R \| video) by simulating the client; picks argmax posterior | `e4_*.csv`, `e5_*.csv` |
| Seek detection | ADV1/ADV2 | flags a session as containing a seek if a request arrives well before the steady-state schedule (gap < 0.75·k_prev·τ) | `e6_seek_attack.csv` |

## 7. Countermeasures evaluated

1. **Fixed batch size + padding of the final batch** (dummy indices) — removes the "short last batch" leak.
2. **Length bucketing** — after playback ends keep sending dummy batches until the total chunk count is a multiple of b.
3. **Constant-rate cover traffic** — exactly one batch of k chunks every kτ/ρ seconds, dummies when the buffer is full; seeks wait for the next slot, so a flushed buffer is not visible.
4. Cost of each: bandwidth overhead = chunks sent / chunks needed, and playback cost (startup, rebuffer, seek latency).

## 8. Assumptions and residual risks

* Chunk records are the same size for every video; a multi-bitrate catalog would add another distinguishing feature (Popcorn/Addra caveat).
* The attacker's training model assumes it knows the viewer population's behaviour; our attacker is a realistic but not worst-case one — numbers are an upper-bound-ish estimate for a simple attacker and a lower bound for a stronger one.
* The team's LWE backend uses a non-cryptographic PRNG (NumPy PCG64) for the secret and noise and its parameters were not re-checked with a lattice estimator; claims for it are limited to correctness and the statistical sanity test above.
* Transport security added after this analysis: `pir_network_server.py`/`pir_network_client.py` support TLS 1.3 with server-certificate and host-name verification and optional mutual TLS (`network/tls_util.py`); plaintext remains available for lab use and is not secure. The signing key's public half must reach clients out of band; an adversary who controls both the manifest key and a server can forge content. Availability: refusing/hanging/crashed servers are tolerated up to F per side by redundancy (`robust_source.py`), at (F+1)× cost; if every unit on a side refuses, the client gets `ServerUnavailable`. The robust XOR mode requires that no unit of one side colludes with a unit of the other. TLS does not hide request sizes/timing (§5).
* Cover traffic hides the *length and seeks of the session as seen by the server*; it does not hide that the user is streaming at all.
* Results come from simulation driven by measured per-batch costs for the real 97-chunk catalog; timing/transfer are modelled, not measured on a real network.
