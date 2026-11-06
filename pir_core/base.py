"""Common interface shared by every PIR backend.

The streaming layer (Sabyasachi) only ever calls::

    backend.setup(records)          # records: (N, B) uint8 array, one row per chunk
    chunks = backend.retrieve([i, j, k, ...])   # -> (len(indices), B) uint8
    backend.last_stats              # RetrievalStats of the most recent retrieve()

Each backend also exposes a *split* client/server API (see xor_pir.py and
lwe_pir.py) whose messages are plain numpy arrays, so the server/infra layer
(Lokesh) can put them on a socket / gRPC without touching the cryptography.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from typing import Sequence

import numpy as np


@dataclass
class RetrievalStats:
    """Cost of one retrieve() call. All times in seconds, sizes in bytes."""

    scheme: str = ""
    num_records: int = 0
    record_bytes: int = 0
    batch: int = 0
    # --- online phase ---
    t_query: float = 0.0        # client: build the query
    t_server: float = 0.0       # server compute, summed over all servers
    t_server_max: float = 0.0   # slowest single server (latency if servers run in parallel)
    t_decode: float = 0.0       # client: recover records from the answer
    bytes_up: int = 0           # client -> server(s), all servers summed
    bytes_down: int = 0         # server(s) -> client, all servers summed
    # --- offline phase (0 for schemes with no preprocessing) ---
    offline_bytes: int = 0      # one-time download (LWE hint)
    t_offline_server: float = 0.0
    t_offline_client: float = 0.0

    @property
    def t_compute_latency(self) -> float:
        """Client + (parallel) server compute, excluding the network."""
        return self.t_query + self.t_server_max + self.t_decode

    @property
    def comm_bytes(self) -> int:
        return self.bytes_up + self.bytes_down

    def as_dict(self) -> dict:
        d = asdict(self)
        d["t_compute_latency"] = self.t_compute_latency
        d["comm_bytes"] = self.comm_bytes
        return d


class PIRBackend(ABC):
    """A PIR scheme with both parties simulated in-process (for benchmarking
    and for the streaming layer's first integration)."""

    name: str = "abstract"
    last_stats: RetrievalStats

    @abstractmethod
    def setup(self, records: np.ndarray) -> None:
        """Load a database of N equal-size records, shape (N, B), dtype uint8."""

    @abstractmethod
    def retrieve(self, indices: Sequence[int]) -> np.ndarray:
        """Privately fetch records[indices]; returns shape (len(indices), B)."""

    # helpers ---------------------------------------------------------------
    @staticmethod
    def _check_records(records: np.ndarray) -> np.ndarray:
        records = np.ascontiguousarray(records)
        if records.ndim != 2 or records.dtype != np.uint8:
            raise ValueError("records must be a 2-D uint8 array of shape (N, B)")
        if records.shape[0] == 0 or records.shape[1] == 0:
            raise ValueError("database must be non-empty")
        return records

    @staticmethod
    def _check_indices(indices: Sequence[int], n: int) -> list[int]:
        idx = [int(i) for i in indices]
        if not idx:
            raise ValueError("need at least one index")
        for i in idx:
            if not 0 <= i < n:
                raise IndexError(f"index {i} out of range [0, {n})")
        return idx
