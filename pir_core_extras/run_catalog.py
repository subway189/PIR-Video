"""Run PIR on a real video catalog.

Usage (from the pir_core/ folder):
    python3 run_catalog.py xor seg/video1 seg/video2
    python3 run_catalog.py lwe seg/video1 seg/video2
The first word picks the scheme (xor = two-server, lwe = single-server); the rest are
folders that each hold one video's .ts segments (made with ffmpeg).
Optional settings via environment variables:  TAU (seconds of video per chunk, default 2)
and BW_MBPS (your internet speed in Mbit/s, default 50).
"""
import glob
import json
import os
import sys
import time

import numpy as np

from pir_core import LwePIR, XorPIR

if len(sys.argv) < 3 or sys.argv[1] not in ("xor", "lwe"):
    sys.exit(__doc__)
scheme, dirs = sys.argv[1], sys.argv[2:]
TAU = float(os.environ.get("TAU", 2))
BW = float(os.environ.get("BW_MBPS", 50)) * 1e6

# 1. collect all chunks of all videos into one long list
files, offsets = [], {}
for d in dirs:
    found = sorted(glob.glob(os.path.join(d, "*.ts")))
    if not found:
        sys.exit(f"No .ts files found in '{d}'. Did the ffmpeg step finish?")
    offsets[d] = len(files)          # chunk number c of this video has global index offsets[d] + c
    files += found

# 2. make every chunk the same size (pad with zeros), as PIR requires
sizes = [os.path.getsize(f) for f in files]
B = -(-max(sizes) // 1024) * 1024
records = np.zeros((len(files), B), np.uint8)
for i, f in enumerate(files):
    data = np.fromfile(f, np.uint8)
    records[i, : len(data)] = data
json.dump({"B": B, "sizes": sizes, "offsets": offsets}, open("manifest.json", "w"))
print(f"Catalog: {len(files)} chunks of {B // 1024} KB each = {records.nbytes / 2**20:.0f} MB")
if scheme == "lwe" and records.nbytes > 1.5 * 2**30:
    print("WARNING: the lwe scheme needs about 10x this much RAM; consider a smaller catalog.")

# 3. load into the PIR scheme (for lwe this also computes the one-time 'hint')
backend = XorPIR("optimized") if scheme == "xor" else LwePIR()
t = time.perf_counter()
backend.setup(records)
print(f"Setup took {time.perf_counter() - t:.1f} s")

# 4. fetch random batches privately, check they are right, and time them
print(f"\n(assuming each chunk is {TAU:g} s of video and your internet is {BW / 1e6:g} Mbit/s)")
for k in (1, 4, 8, 16):
    if k > len(files):
        break
    idx = [int(i) for i in np.random.randint(0, len(files), k)]
    out = backend.retrieve(idx)
    assert np.array_equal(out, records[idx]), "WRONG RESULT - please report this"
    s = backend.last_stats
    net = s.comm_bytes * 8 / BW
    ok = s.t_compute_latency + net <= k * TAU
    print(f"batch of {k:2d} chunks: compute {s.t_compute_latency * 1e3:8.1f} ms | "
          f"transfer (estimated) {net * 1e3:8.1f} ms | upload {s.bytes_up / 1024:9.0f} KB | "
          f"download {s.bytes_down / 1024:9.0f} KB | "
          f"{'FAST ENOUGH' if ok else 'TOO SLOW'} to keep up with {k * TAU:g} s of video")
