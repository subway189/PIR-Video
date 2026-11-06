# Server Infrastructure — Private Video Retrieval

## 1. Overview

The server infrastructure provides TCP-based communication and binary video-chunk retrieval. It serves as the baseline for integrating and evaluating privacy-preserving information retrieval methods.

## 2. Video Chunking

The input video is split into fixed-size binary chunks of 1 MiB using `split_video.py`. The generated chunks are stored in the `chunks/` directory.

## 3. Client–Server Communication

The server listens on a configurable TCP port and processes client requests. The current implementation supports:

- `PING`: connectivity testing.
- `GET_CHUNK`: retrieval of a specified chunk for the `sample` video.

Chunk responses use a status byte, an eight-byte big-endian payload length, and the binary payload. This framing allows the client to distinguish successful responses from server errors.

## 4. Validation and Error Handling

The server validates video identifiers and chunk IDs, handles missing chunk files, and enforces a maximum chunk-size limit. The client validates the response length and checks for incomplete transfers.

## 5. Testing and Benchmarking

`test_server.py` checks valid chunk retrieval, byte-for-byte integrity, negative chunk IDs, missing chunks, and unknown video IDs.

`benchmark.py` measures end-to-end retrieval latency, including client process startup and connection setup. Results provide a local baseline for later comparison with PIR-enabled retrieval.

### PIR Network Benchmark Results

**Experimental setup**
- Retrieved two chunks per run.
- Each downloaded record was 1 MiB (1,048,576 bytes), with shorter original chunks padded with zeros.
- Measured client-side wall-clock time (`real`) using the `time` command.
- Four runs were recorded for each PIR method.

**Results**

| Run | XOR PIR (seconds) | LWE PIR (seconds) |
|---|---:|---:|
| 1 | 0.749 | 1.780 |
| 2 | 0.641 | 1.528 |
| 3 | 0.542 | 1.796 |
| 4 | 0.512 | 1.901 |
| **Average** | **0.611** | **1.751** |

XOR PIR achieved an average client-side time of 0.611 seconds, compared with 1.751 seconds for LWE PIR. In this experiment, XOR PIR was approximately 2.87 times faster, with an average time reduction of approximately 65.1%.

**Correctness verification**

The downloaded data was checked against the original chunks for both methods. All four checks passed: XOR chunks 0 and 1, and LWE chunks 0 and 1. The verification compared original bytes and confirmed that any remaining padding consisted entirely of zeros.

**Limitations**

These are preliminary measurements from four runs per method on a local setup. They measure client-side workflow time rather than isolated cryptographic computation. The results do not establish performance at larger scales or provide a complete security comparison between the PIR schemes.

## 6. PIR Integration

The infrastructure is intended to support both two-server XOR-based information-theoretic PIR and lattice-based PIR. Their query formats, server-side processing, and response interfaces will be integrated once the corresponding cryptographic implementations and interfaces are finalized.

The existing `GET_CHUNK` operation is an ordinary retrieval baseline; it does not provide query privacy.
