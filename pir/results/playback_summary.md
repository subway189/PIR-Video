### Playback (no seeks): mean over reps

| Link | Scheme | B | Startup s | First-visit s | Rebuffers | Stall s | Ratio | /min |
|---|---|---|---|---|---|---|---|---|
| 20 Mbps | baseline | 1 | 0.18 | 0.2 | 0.0 | 0.00 | 0.000 | 0.0 |
| 20 Mbps | XOR PIR | 1 | 0.30 | 0.3 | 0.0 | 0.00 | 0.000 | 0.0 |
| 20 Mbps | XOR PIR | 2 | 0.26 | 0.3 | 0.0 | 0.00 | 0.000 | 0.0 |
| 20 Mbps | XOR PIR | 4 | 0.47 | 0.5 | 0.0 | 0.00 | 0.000 | 0.0 |
| 20 Mbps | lattice PIR | 1 | 0.93 | 14.4 | 0.0 | 0.00 | 0.000 | 0.0 |
| 20 Mbps | lattice PIR | 2 | 0.90 | 14.4 | 0.0 | 0.00 | 0.000 | 0.0 |
| 20 Mbps | lattice PIR | 4 | 1.76 | 15.2 | 0.0 | 0.00 | 0.000 | 0.0 |
| 10 Mbps | baseline | 1 | 0.28 | 0.3 | 0.0 | 0.00 | 0.000 | 0.0 |
| 10 Mbps | XOR PIR | 1 | 0.51 | 0.6 | 0.0 | 0.00 | 0.000 | 0.0 |
| 10 Mbps | XOR PIR | 2 | 0.47 | 0.5 | 0.0 | 0.00 | 0.000 | 0.0 |
| 10 Mbps | XOR PIR | 4 | 0.90 | 0.9 | 0.0 | 0.00 | 0.000 | 0.0 |
| 10 Mbps | lattice PIR | 1 | 1.58 | 28.5 | 0.0 | 0.00 | 0.000 | 0.0 |
| 10 Mbps | lattice PIR | 2 | 1.54 | 28.4 | 0.0 | 0.00 | 0.000 | 0.0 |
| 10 Mbps | lattice PIR | 4 | 3.09 | 30.0 | 0.0 | 0.00 | 0.000 | 0.0 |
| 5 Mbps | baseline | 1 | 0.48 | 0.5 | 0.0 | 0.00 | 0.000 | 0.0 |
| 5 Mbps | XOR PIR | 1 | 0.93 | 1.0 | 0.0 | 0.00 | 0.000 | 0.0 |
| 5 Mbps | XOR PIR | 2 | 0.89 | 0.9 | 0.0 | 0.00 | 0.000 | 0.0 |
| 5 Mbps | XOR PIR | 4 | 1.73 | 1.8 | 0.0 | 0.00 | 0.000 | 0.0 |
| 5 Mbps | lattice PIR | 1 | 2.94 | 56.7 | 12.0 | 5.54 | 0.257 | 33.4 |
| 5 Mbps | lattice PIR | 2 | 2.86 | 56.6 | 7.0 | 6.21 | 0.279 | 18.9 |
| 5 Mbps | lattice PIR | 4 | 5.70 | 59.4 | 3.0 | 5.11 | 0.242 | 8.5 |
| 3 Mbps | baseline | 1 | 0.74 | 0.7 | 0.0 | 0.00 | 0.000 | 0.0 |
| 3 Mbps | XOR PIR | 1 | 1.49 | 1.5 | 0.0 | 0.00 | 0.000 | 0.0 |
| 3 Mbps | XOR PIR | 2 | 1.45 | 1.5 | 0.0 | 0.00 | 0.000 | 0.0 |
| 3 Mbps | XOR PIR | 4 | 2.85 | 2.9 | 0.0 | 0.00 | 0.000 | 0.0 |
| 3 Mbps | lattice PIR | 1 | 4.71 | 94.2 | 14.0 | 17.98 | 0.529 | 24.7 |
| 3 Mbps | lattice PIR | 2 | 4.64 | 94.2 | 7.0 | 18.64 | 0.538 | 12.1 |
| 3 Mbps | lattice PIR | 4 | 9.26 | 98.8 | 3.0 | 15.82 | 0.497 | 5.7 |

### Seeks (per session: play 0-3 -> seek to 16 (cold) -> play 4 -> seek +2 -> play 2 -> seek back to 5 (cold)); mean over reps

| Link | Scheme | B | Cold seek 0->16 s | Cold seek back ->5 s | Near-forward seek (+2): buffer hit | its latency s | Wasted chunks | Rebuffers |
|---|---|---|---|---|---|---|---|---|
| 10 Mbps | baseline | 1 | 0.28 | 0.28 | 100% | 0.00 | 0.0 | 0.0 |
| 10 Mbps | XOR PIR | 1 | 0.51 | 0.51 | 100% | 0.00 | 0.0 | 0.0 |
| 10 Mbps | XOR PIR | 2 | 0.47 | 0.47 | 100% | 0.00 | 0.0 | 0.0 |
| 10 Mbps | XOR PIR | 4 | 0.89 | 0.89 | 100% | 0.00 | 0.0 | 0.0 |
| 10 Mbps | lattice PIR | 1 | 1.62 | 2.01 | 0% | 1.66 | 3.0 | 0.0 |
| 10 Mbps | lattice PIR | 2 | 2.25 | 2.66 | 0% | 2.21 | 6.0 | 0.0 |
| 10 Mbps | lattice PIR | 4 | 5.23 | 3.26 | 100% | 0.00 | 8.0 | 0.0 |
| 5 Mbps | baseline | 1 | 0.48 | 0.48 | 100% | 0.00 | 0.0 | 0.0 |
| 5 Mbps | XOR PIR | 1 | 1.12 | 0.93 | 100% | 0.00 | 1.0 | 0.0 |
| 5 Mbps | XOR PIR | 2 | 1.32 | 0.89 | 100% | 0.00 | 2.0 | 0.0 |
| 5 Mbps | XOR PIR | 4 | 2.46 | 1.73 | 100% | 0.00 | 4.0 | 0.0 |
| 5 Mbps | lattice PIR | 1 | 3.34 | 3.90 | 0% | 3.36 | 3.0 | 0.0 |
| 5 Mbps | lattice PIR | 2 | 3.75 | 3.76 | 0% | 3.77 | 6.0 | 3.0 |
| 5 Mbps | lattice PIR | 4 | 7.41 | 9.47 | 0% | 7.41 | 12.0 | 0.0 |
