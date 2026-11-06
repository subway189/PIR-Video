# Live demo — presenter guide (≈ 5 min)

**Run:** `PYTHONPATH=. python3 demo/live_demo.py` (from the project root). Press **Enter** to start each act.
`--auto` runs hands-free (rehearsal/recording), `--act N` runs one act. `demo/demo_transcript.txt` is a recorded run —
**fallback**: if the laptop misbehaves, put that file on screen and narrate the same lines.
Setup before the talk: `pip install -r requirements.txt`, run once with `--auto` to confirm, use a terminal with a dark
background and a large font (the demo uses colours: red = bad/leak, green = good).

| Act | Time | What is on screen | What to say |
|---|---|---|---|
| intro | 0:20 | catalog line | "Three videos, one viewer. They watch video_001, jump ahead to chunk 14, then back to chunk 2." |
| 1 Baseline | 0:40 | server log with the chunk numbers in request order | "A normal streaming server logs exactly this: the video, the seek, the stop point. This is what we remove." |
| 2 XOR PIR | 0:60 | same session, then the server's view: 8 random bytes per request | "Same playback, same chunks (every chunk is hash-checked). Server A only ever sees random-looking bits. Down at the bottom: A⊕B is the real index — that's why the two servers must be different operators." |
| 3 Lattice PIR | 0:40 | hint size, queries of 32-bit numbers | "One server instead of two: security from LWE. Price: a one-time 5 MiB hint here and bigger queries." |
| 4 Curious operator | 0:40 | 100 % / 2.2 % / 100 % | "Log reader: 100 %. One XOR server: chance level (1.7 %). Both colluding: 100 % — that is the trust assumption, not a bug. Traffic analysis still leaks; security_eval/ has the attack and the padding defence." |
| 5 Faults | 1:00 | cheater + crashed server events; then operator loss → failover | "One server corrupts answers, one is dead: playback is still bit-exact because the manifest is signed and every chunk is checked. Kill a whole operator and the lattice servers take over. If nothing honest is left it stops with an error rather than lying." |
| 6 Numbers | 0:40 | sharding speed-up, playback at 5 Mbit/s | "XOR is real-time on ordinary links. Lattice needs about 13 Mbit/s at our chunk size — at 5 Mbit/s it rebuffers (13 stalls). Sharding cuts server time 6–8× on 8 machines (projected from per-shard timings)." |

## What is real and what is staged
Real: TLS 1.3 + mutual-auth sockets, the PIR servers and clients, the seekable player with adaptive batching, signed
manifest and per-chunk SHA-256 checks. Staged: the "curious operator" (a tap that records exactly what a server receives)
and the faulty servers (a wrapper that corrupts answers / a closed port). Say so if asked.

## Likely questions
* *Why 2.2 % and not exactly 1.7 %?* 400 trials; the lone-server guess is statistically indistinguishable from chance.
* *Is this practical?* XOR yes at video bitrates (2× download); lattice PIR needs ~4–8× bandwidth and a hint — fine on fast links, not on 3–5 Mbit/s.
* *What if both XOR servers collude?* Privacy is lost (Act 4 shows it) — deploy them under different operators; the lattice scheme avoids this assumption.
* *What does PIR not hide?* Request counts, batch sizes and timing (traffic analysis) and that you are watching at all — see `security_eval/`.
* *Can a server just refuse?* Not usefully: spares, retries, probation and scheme failover; beyond the pool you get a bounded wait then an error, never wrong data.
