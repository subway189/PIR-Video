# Network transport for Member A's PIR core

This package wraps the existing `pir_core` split APIs in a small TCP protocol. It does not change either PIR algorithm.

## Requirements

Python 3.10+ and NumPy:

```bash
python -m pip install numpy
```

Run commands from the directory containing `pir_network_server.py`, `pir_network_client.py`, `pir_core/`, and your `chunks/` folder. The server expects sequential files named `chunk_0.bin`, `chunk_1.bin`, etc. Short final chunks are zero-padded to the largest chunk size, consistent with fixed-size PIR records.

## XOR PIR: two server processes

Open two terminals and start one server per terminal:

```bash
python pir_network_server.py xor --port 54100 --chunks chunks
python pir_network_server.py xor --port 54101 --chunks chunks
```

Each server must have the same database, and the two server operators must not collude for the two-server privacy guarantee.

In a third terminal, request global indices 0 and 1:

```bash
python pir_network_client.py xor 0,1 --server0 127.0.0.1:54100 --server1 127.0.0.1:54101
```

## LWE PIR: one server

Start the server:

```bash
python pir_network_server.py lwe --port 54102 --chunks chunks
```

Wait for `LWE hint ready`, then in another terminal:

```bash
python pir_network_client.py lwe 0,1 --server 127.0.0.1:54102
```

The client downloads the offline hint for each invocation in this simple prototype. A later improvement is to cache the hint client-side.

## Important limitations

- Plaintext mode is for a trusted lab network only. For anything else enable TLS 1.3 (`--tls-cert/--tls-key`, optionally `--tls-client-ca` for client authentication) and verify chunks against the signed manifest; see `tls_util.py` and `integrity.py`. There is still no replay protection beyond TLS and no rate limiting.
- XOR requests are sent to separate server addresses; do not send both query shares to the same server if claiming two-server privacy.
- LWE server setup computes a potentially expensive offline hint. The core's randomness note says its NumPy PCG64 generator is not a cryptographic RNG, so do not claim production security.
- `chunks/` contains raw byte slices. They are not independently playable video segments; reconstruct the original file by concatenating chunks in index order.
- Retrieved records are padded to fixed record size. Store original byte lengths in a public manifest to trim the final chunk correctly.
- The original C++ server/client remain unchanged and can still be used as a direct-retrieval baseline.

## Sharding, catalog mode and streaming (added during the merge)
```bash
# serve a baseline-format catalog (manifest.json + videos) split over 2 shards per scheme
python pir_network_server.py xor --port 54100 --catalog CAT --shard 0/2   # replica 0, shard 0
python pir_network_server.py xor --port 54101 --catalog CAT --shard 1/2   # replica 0, shard 1
#   ... same again on other hosts for replica 1;  LWE: python pir_network_server.py lwe --port P --catalog CAT --shard I/S
python stream_over_tcp.py --shards 2          # spawns everything on localhost and plays a video with seeks
```
`net_source.NetXorBackend(replica0_addrs, replica1_addrs)` / `NetLweBackend(shard_addrs)` give the player a `ChunkSource`.
Servers now handle each connection in its own thread. `--shard 0/1` (default) behaves exactly as before.

## TLS and integrity
```bash
python tls_util.py certs/                                   # demo CA + server + client certs (needs `pip install cryptography`)
python integrity.py keygen keys/ && python integrity.py sign CATALOG keys/manifest_ed25519.key
python pir_network_server.py xor --port 54100 --catalog CATALOG --tls-cert certs/server.pem --tls-key certs/server.key --tls-client-ca certs/ca.pem
python pir_network_client.py xor 0,1 --server0 localhost:54100 --server1 localhost:54101 \
       --tls-ca certs/ca.pem --tls-cert certs/client.pem --tls-key certs/client.key --manifest CATALOG/manifest.json --pubkey keys/manifest_ed25519.pub
```
Use the host name from the certificate (`localhost`, `127.0.0.1`); the client verifies it.

## Fault tolerance (cheating / refusing servers)
```python
import robust_source as rs, integrity
be  = rs.NetRobustXorBackend(side0=[unitA, unitB], side1=[unitC, unitD], tls=ctx, timeout=5)   # unit = 'host:port' or [shard addrs]
src = rs.make_robust_source(CATALOG, be, 'keys/manifest_ed25519.pub')                            # signed manifest required
# LWE: rs.NetRobustLweBackend([unit1, unit2], tls=ctx, timeout=5);  inspect be.events for 'cheater' / 'unavailable'
```
Side 0 units all receive the same share, side 1 units the other; one honest unit per side is enough.

Beyond F faults: `NetRobustXorBackend(..., fanout=F+1, ban_s=120)` with spares listed after the primaries;
`robust_source.make_robust_source(..., patience_s=20)` stalls and retries instead of failing at the first outage;
`robust_source.FailoverBackend([xor_backend, lwe_backend])` falls back from XOR to LWE servers.
