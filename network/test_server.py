import subprocess
import time
from pathlib import Path

CLIENT = Path("client.exe").resolve()
CHUNKS = Path("chunks")
PORT = "54001"

def run_client(*args):
    return subprocess.run(
        [str(CLIENT), *args],
        capture_output=True,
        text=True,
        timeout=10
    )

def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"PASS: {message}")

def main():
    original = CHUNKS / "chunk_0.bin"
    downloaded = Path("downloaded_chunk.bin")

    check(CLIENT.exists(), "Client executable exists")
    check(original.exists(), "Test chunk exists")

    # Valid chunk retrieval
    result = run_client("GET_CHUNK", "sample", "0", PORT)
    check(result.returncode == 0, "Valid chunk request succeeds")
    check(downloaded.exists(), "Downloaded file is created")
    check(original.read_bytes() == downloaded.read_bytes(),
          "Downloaded chunk matches original")

    # Invalid chunk ID
    result = run_client("GET_CHUNK", "sample", "-1", PORT)
    check(result.returncode != 0, "Negative chunk ID is rejected")

    # Missing chunk
    result = run_client("GET_CHUNK", "sample", "999999", PORT)
    check(result.returncode != 0, "Missing chunk is rejected")

    # Invalid video ID
    result = run_client("GET_CHUNK", "unknown", "0", PORT)
    check(result.returncode != 0, "Unknown video ID is rejected")

    print("\nAll tests passed.")

if __name__ == "__main__":
    main()
