#!/usr/bin/env python3
"""TCP transport wrapper for Member A's XOR/LWE PIR split APIs."""
import argparse
import json
import os
import socket
import struct
import sys
import ssl
import threading
from pathlib import Path

import numpy as np
from pir_core import XorPIRServer, LwePIRServer, LweParams
from pir_core.sharded import LweRowShard, _bounds

MAX_HEADER = 1 << 20
MAX_PAYLOAD = 1 << 31


def recv_exact(sock, n):
    chunks = []
    while n:
        b = sock.recv(n)
        if not b:
            raise ConnectionError("peer closed connection early")
        chunks.append(b)
        n -= len(b)
    return b"".join(chunks)


def recv_msg(sock):
    hlen = struct.unpack("!I", recv_exact(sock, 4))[0]
    if hlen <= 0 or hlen > MAX_HEADER:
        raise ValueError("invalid header length")
    header = json.loads(recv_exact(sock, hlen).decode("utf-8"))
    nbytes = int(header.get("nbytes", 0))
    if nbytes < 0 or nbytes > MAX_PAYLOAD:
        raise ValueError("invalid payload size")
    payload = recv_exact(sock, nbytes) if nbytes else b""
    return header, payload


def send_msg(sock, header, array=None):
    payload = b""
    header = dict(header)
    if array is not None:
        array = np.ascontiguousarray(array)
        payload = array.tobytes(order="C")
        header.update(dtype=array.dtype.str, shape=list(array.shape), nbytes=len(payload))
    else:
        header["nbytes"] = 0
    h = json.dumps(header, separators=(",", ":")).encode("utf-8")
    sock.sendall(struct.pack("!I", len(h)) + h + payload)


def array_from(header, payload, expected_dtype):
    if header.get("dtype") != np.dtype(expected_dtype).str:
        raise ValueError("unexpected array dtype")
    shape = tuple(int(x) for x in header.get("shape", []))
    if not shape or any(x < 0 for x in shape):
        raise ValueError("invalid array shape")
    expected = int(np.prod(shape, dtype=np.int64)) * np.dtype(expected_dtype).itemsize
    if expected != len(payload):
        raise ValueError("array shape/payload length mismatch")
    return np.frombuffer(payload, dtype=expected_dtype).reshape(shape).copy()


def load_records(folder):
    folder = Path(folder)
    files = []
    i = 0
    while (folder / f"chunk_{i}.bin").is_file():
        files.append(folder / f"chunk_{i}.bin")
        i += 1
    if not files:
        raise FileNotFoundError(f"No sequential chunk_0.bin, chunk_1.bin, ... files in {folder}")
    data = [p.read_bytes() for p in files]
    B = max(map(len, data))
    if B == 0:
        raise ValueError("chunks must not be empty")
    records = np.zeros((len(data), B), dtype=np.uint8)
    for i, raw in enumerate(data):
        records[i, :len(raw)] = np.frombuffer(raw, dtype=np.uint8)
    print(f"Loaded {len(data)} chunks; fixed record size {B} bytes", flush=True)
    return records


def load_catalog(catalog_dir):
    """Baseline-format catalog (manifest.json + per-video chunk files) -> (N, R) records in manifest order."""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pir"))
    import pir_source
    _, _, recs = pir_source.load_catalog_records(catalog_dir)
    print(f"Loaded catalog {catalog_dir}: {recs.shape[0]} records x {recs.shape[1]} bytes", flush=True)
    return recs


def handle(conn, scheme, records, xor_server, lwe_server, shard=None, tls=None):
    try:
        if tls is not None:
            conn.settimeout(30)
            conn = tls.wrap_socket(conn, server_side=True)   # handshake here, in the worker thread
        header, payload = recv_msg(conn)
        op = header.get("op")
        if op == "info":
            send_msg(conn, {"ok": True, "N": int(records.shape[0]), "B": int(records.shape[1]), "scheme": scheme,
                            "shard": list(shard["range"]), "shard_unit": shard["unit"]})
        elif scheme == "xor" and op == "xor_answer":
            # a shard receives only the byte-aligned slice of the bit-packed query for its record range
            q = array_from(header, payload, np.uint8)
            if q.ndim != 2 or q.shape[1] != xor_server.query_bytes:
                raise ValueError("XOR query has incorrect shape")
            send_msg(conn, {"ok": True}, xor_server.answer(q))
        elif scheme == "lwe" and op == "lwe_hint":
            send_msg(conn, {"ok": True}, lwe_server.hint())
        elif scheme == "lwe" and op == "lwe_answer":
            q = array_from(header, payload, np.uint32)
            if q.ndim != 2 or q.shape[0] != lwe_server.db.shape[1]:
                raise ValueError("LWE query has incorrect shape")
            send_msg(conn, {"ok": True}, lwe_server.answer(q))
        else:
            send_msg(conn, {"ok": False, "error": "unsupported operation for this server"})
    except Exception as exc:
        try:
            send_msg(conn, {"ok": False, "error": str(exc)})
        except Exception:
            pass
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scheme", choices=("xor", "lwe"))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--chunks", default="chunks", help="folder with chunk_0.bin, chunk_1.bin, ...")
    ap.add_argument("--catalog", help="baseline-format catalog folder (manifest.json + videos); overrides --chunks")
    ap.add_argument("--tls-cert", help="server certificate (PEM); with --tls-key enables TLS 1.3")
    ap.add_argument("--tls-key", help="server private key (PEM)")
    ap.add_argument("--tls-client-ca", help="require client certificates signed by this CA (mutual TLS)")
    ap.add_argument("--shard", default="0/1",
                    help="I/S: this process serves shard I of S (XOR: a range of records; LWE: a band of DB rows)")
    args = ap.parse_args()
    si, sn = (int(x) for x in args.shard.split("/"))
    tls = None
    if args.tls_cert or args.tls_key:
        if not (args.tls_cert and args.tls_key):
            raise SystemExit("--tls-cert and --tls-key must be given together")
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from tls_util import server_context
        tls = server_context(args.tls_cert, args.tls_key, args.tls_client_ca)
    elif args.tls_client_ca:
        raise SystemExit("--tls-client-ca needs --tls-cert/--tls-key")
    records = load_catalog(args.catalog) if args.catalog else load_records(args.chunks)
    N = records.shape[0]
    if args.scheme == "xor":
        bounds = _bounds(N, sn, 8)
        if not 0 <= si < len(bounds):
            raise SystemExit(f"shard {si}/{sn}: catalog of {N} records only splits into {len(bounds)} shards")
        a, b = bounds[si]
        xor_server = XorPIRServer(np.ascontiguousarray(records[a:b]))
        lwe_server, shard = None, {"range": (a, b), "unit": "records"}
        print(f"XOR shard {si}/{sn}: records [{a}, {b})", flush=True)
    else:
        xor_server = None
        full = LwePIRServer(records, LweParams())
        bounds = _bounds(full.db.shape[0], sn)
        if not 0 <= si < len(bounds):
            raise SystemExit(f"shard {si}/{sn}: DB has {full.db.shape[0]} rows, only {len(bounds)} shards")
        r0, r1 = bounds[si]
        band = LweRowShard(full, r0, r1)
        lwe_server = band.server          # an LwePIRServer over rows [r0, r1)
        shard = {"range": (r0, r1), "unit": "db_rows"}
        del full
        print(f"LWE shard {si}/{sn}: DB rows [{r0}, {r1}) of {bounds[-1][1]}", flush=True)
    if lwe_server is not None:
        print("Computing LWE offline hint (one-time setup)...", flush=True)
        lwe_server.hint()
        print(f"LWE hint ready; shape={lwe_server.hint().shape}", flush=True)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((args.host, args.port))
        server.listen(16)
        print(f"{args.scheme.upper()} PIR server listening on {args.host}:{args.port}"
              f"{' (TLS 1.3' + (', client auth' if args.tls_client_ca else '') + ')' if tls else ' (PLAINTEXT, lab use only)'}", flush=True)
        while True:
            conn, _ = server.accept()
            threading.Thread(target=handle, args=(conn, args.scheme, records, xor_server, lwe_server, shard, tls),
                             daemon=True).start()


if __name__ == "__main__":
    main()
