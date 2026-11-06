# PIR core: two-server XOR PIR + single-server lattice PIR

Real, tested implementations plus benchmarks, plugged into the baseline streaming player (`../baseline`).

| File | What |
|---|---|
| `two_server_pir.py` | Chor et al. XOR PIR (client, 2 servers) |
| `lattice_pir.py` | SimplePIR/FrodoPIR-style single-server LWE PIR (q=2^32, p=256, n=1024, sigma=6.4) |
| `pir_kernels.c`, `kernels.py` | C inner loops (compiled on import with gcc; numpy fallback; `PIR_NO_C=1` forces it) |
| `pir_source.py` | `PIRChunkSource` adapter: PIR backends plug into the baseline `run_session()` |
| `bench_two_server.py`, `bench_lattice.py`, `stream_demo.py`, `plot_results.py` | benchmarks, streaming comparison, charts |
| `test_pir.py` | 16 tests (`python3 test_pir.py`) |
| `results/` | CSVs, logs, chart |

Run: `python3 test_pir.py`; `python3 bench_two_server.py --max-mib 1024`; `python3 bench_lattice.py --max-mib 256`; `python3 stream_demo.py`.
(Run scripts normally, not with `python -I`.)

## Results (2-vCPU cloud VM, single-threaded kernels)

**Two-server XOR, server time per query (one server)**
| Catalog | R=256 KiB, B=1 | B=8 (per record) |
|---|---|---|
| 256 MiB | 16.9 ms | |
| 1 GiB | 69.6 ms | 29.5 ms |

R=64 KiB, 1 GiB: 62.7 ms (B=1). Cost is linear in catalog size (~15 GB/s scan); batching helps ~2x via shared DB reads. Upload is 2*N/8 bytes per record, download 2R.

**Single-server lattice (R=64 KiB, hint cap 64 MiB -> k=4)**
256 MiB catalog: 147 ms/record server, setup 17.7 s, hint 64 MiB (one-time download), up 256 KiB, down 256 KiB (4x overhead from k).
About 9x the server time of XOR at equal catalog size, because k column-queries each scan the DB.

**Real-time budget (1 s chunks, one core, B=1, linear extrapolation):** XOR about 14-16 GiB of catalog per core; lattice about 1.7 GiB per core.

**Streaming (50 chunks x 128 KiB, ~1.05 Mbps stream, 40 ms latency, `results_pir_stream.csv`)**
No rebuffering for either PIR scheme at 20 and 5 Mbps. At 3 Mbps the lattice scheme (4x download, 512 KiB/chunk) rebuffers 7 times (3.9 s); XOR (2x) does not. Lattice first-visit startup includes the 32 MiB hint: 14 s at 20 Mbps, 56 s at 5 Mbps, 93 s at 3 Mbps.

## Threat model and caveats
- XOR PIR is information-theoretically private only if the two servers do **not** collude (q1 xor q2 = e_i).
- Lattice PIR is private under LWE against one server; LWE parameters follow SimplePIR, **no independent security estimate was run**.
- Public matrix A and noise come from numpy PCG64; a deployment must use AES-CTR/ChaCha (and a CSPRNG for noise/secret sampling beyond os.urandom).
- A fresh secret per query is mandatory (`test_reusing_the_secret_would_leak` shows why).
- The manifest and (video, chunk) -> record mapping are public; PIR hides which record is fetched. Fetch timing/count still leaks session length.
- Servers run in-process and sequentially; the network is emulated (sleep). Two real XOR servers would halve the XOR compute shown in `stream_demo`.
- "Overhead vs plain" is measured against an in-memory memcpy (pessimistic; Popcorn reports ~1080x compute overhead against real storage-backed serving).
- Small catalogs fit in cache and look faster than the linear trend.
- Possible upload optimization for XOR: send a PRG seed instead of a full mask for one server (about 2x less upload).

## Client: seek handling and playback evaluation
- `player.py`: transport-agnostic seekable player (buffer window, back buffer, batch rounds, generation-counter seeks that discard stale in-flight rounds, SHA-256 verification). `python3 test_player.py` runs 15 tests.
- `eval_playback.py`: 3 transports x batch 1/2/4 x 20/10/5/3 Mbps (40 ms) playback grid, plus a seek scenario; `plot_playback.py` makes `results/playback_*.png` and `results/playback_summary.md`.
- Headline (37.5 MiB catalog, ~1.05 Mbps stream): XOR PIR never rebuffered at 3-20 Mbps; lattice PIR rebuffered 26% of the time at 5 Mbps and 53% at 3 Mbps (it needs 6.65 Mbps per chunk); batch 4 doubles startup; a cold seek costs about one startup plus the discarded round. See the report sections for method and limits.

## Added after the merge
`player.py` adaptive batching (`batch="adaptive"`), `bench_sharding.py` (large catalogs, sharded servers),
`pircore_backend.py` (adapter for the shared pir_core package). Results: `results/playback_adaptive.csv`,
`results/sharding_large_catalog.csv`.
