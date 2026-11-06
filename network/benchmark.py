import subprocess
import time
import statistics
import sys
from pathlib import Path

CLIENT = Path("client.exe")
VIDEO_ID = "sample"
CHUNK_ID = 0
PORT = 54001
RUNS = 10

if not CLIENT.exists():
    print("Error: client.exe not found in project root.")
    sys.exit(1)

if not Path(f"chunks/chunk_{CHUNK_ID}.bin").exists():
    print("Error: requested chunk does not exist.")
    sys.exit(1)

times_ms = []

for i in range(RUNS):
    start = time.perf_counter()

    result = subprocess.run(
        [str(CLIENT.resolve()), "GET_CHUNK", VIDEO_ID,
         str(CHUNK_ID), str(PORT)],
        capture_output=True,
        text=True
    )

    elapsed_ms = (time.perf_counter() - start) * 1000

    if result.returncode != 0:
        print(f"Run {i + 1} failed:")
        print(result.stderr)
        sys.exit(1)

    times_ms.append(elapsed_ms)
    print(f"Run {i + 1}: {elapsed_ms:.2f} ms")

print("\n--- Benchmark Results ---")
print(f"Successful runs: {len(times_ms)}")
print(f"Mean latency:    {statistics.mean(times_ms):.2f} ms")
print(f"Median latency:  {statistics.median(times_ms):.2f} ms")
print(f"Minimum latency: {min(times_ms):.2f} ms")
print(f"Maximum latency: {max(times_ms):.2f} ms")
print(f"Chunk size:      {Path(f'chunks/chunk_{CHUNK_ID}.bin').stat().st_size} bytes")
