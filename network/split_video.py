
from pathlib import Path

VIDEO = Path("videos/sample.mp4")
OUTPUT = Path("chunks")
CHUNK_SIZE = 1024 * 1024  # 1 MiB

OUTPUT.mkdir(parents=True, exist_ok=True)

with VIDEO.open("rb") as video:
    chunk_id = 0

    while True:
        data = video.read(CHUNK_SIZE)

        if not data:
            break

        output_file = OUTPUT / f"chunk_{chunk_id}.bin"
        output_file.write_bytes(data)

        print(f"Created {output_file} ({len(data)} bytes)")
        chunk_id += 1

print(f"Finished: {chunk_id} chunks created.")
