# pir_core: PIR backends for private video retrieval (CS670, Member A)

```
pir_core/      xor_pir.py (2-server IT-PIR), lwe_pir.py (1-server LWE PIR), base.py (interface)
tests/         python3 -m unittest discover -s tests -v
bench/         run_bench.py (synthetic sweeps, optional), make_assets.py (figures from its CSV)
run_catalog.py run PIR on a real video catalog (this produced results/real_catalog_run.txt)
report/        pointer to the final report (numbers come from results/real_catalog_run.txt)
```
Needs only `numpy` (benchmarks/plots also use `pandas`, `matplotlib`, `tabulate`).

## For the streaming layer

```python
from pir_core import XorPIR, LwePIR
backend = LwePIR()                  # or XorPIR(); same interface
backend.setup(records)              # records: (N, B) uint8, one row per chunk
chunks = backend.retrieve([5, 6, 7, 8])      # (4, B) uint8, fetched privately as one batch
backend.last_stats                  # query/server/decode time, bytes up/down, offline hint size
```

## For the server/infra layer (split API, messages are numpy arrays)

Two-server XOR (one server object per replica, identical data):
```python
srv = XorPIRServer(records);  cli = XorPIRClient(N, B)
state, (q0, q1) = cli.query(indices)      # send q0 to server 0, q1 to server 1   (uint8, shape (k, ceil(N/8)))
a0, a1 = srv0.answer(q0), srv1.answer(q1) # each (k, B) uint8
chunks = cli.decode(state, [a0, a1])
```
Single-server LWE:
```python
srv = LwePIRServer(records);  hint = srv.hint()       # offline: client downloads `hint` once (uint32, (l, n))
cli = LwePIRClient(N, B, hint)                         # same params/rows as the server
state, Q = cli.query(indices)                          # Q: uint32 (m, K), K = columns queried
ans = srv.answer(Q)                                    # uint32 (l, K)
chunks = cli.decode(state, ans)
```
To shard the catalog, build one backend per shard; each shard's `retrieve` is independent.
