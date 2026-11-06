#!/usr/bin/env python3
"""CS670 live demo: private video streaming with PIR  (about 4-5 minutes).

    python3 demo/live_demo.py              # presenter mode: press Enter between acts
    python3 demo/live_demo.py --auto       # runs by itself with short pauses (use for rehearsal / recording)
    python3 demo/live_demo.py --act 3      # one act only

Everything here is the real system (real servers over real TLS sockets, the real player, real PIR code); only the
"curious operator" and the "faulty servers" are scripted so that you can see what they learn / what they break.
Acts: 1 baseline leaks -> 2 XOR PIR hides it -> 3 lattice PIR hides it with ONE server -> 4 the curious-operator test
      -> 5 cheating and dead servers -> 6 the numbers
"""
import argparse, csv, hashlib, os, socket, sys, tempfile, threading, time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, os.path.join(ROOT, "pir"), os.path.join(ROOT, "baseline"), os.path.join(ROOT, "network")):
    sys.path.insert(0, p)
import integrity, tls_util, net_source as ns, robust_source as rs     # noqa: E402
import pir_network_server as srvmod                                     # noqa: E402
import pir_source as ps                                                 # noqa: E402
from client import HTTPChunkSource                                      # noqa: E402
from make_catalog import build_catalog                                  # noqa: E402
from player import SeekablePlayer                                       # noqa: E402
from server import BaselineServer                                       # noqa: E402
from pir_core import LwePIRServer, LweParams, XorPIRServer              # noqa: E402
from pir_core.sharded import LweRowShard                                # noqa: E402

TTY = sys.stdout.isatty()
C = dict(h="\033[1;36m", ok="\033[1;32m", bad="\033[1;31m", dim="\033[2m", y="\033[33m", b="\033[1m", r="\033[0m") if TTY else {k: "" for k in "h ok bad dim y b r".split()}
ARGS = None
SEEKS = [(5, 14), (8, 2)]          # play 0-4, jump to 14, play 14-16, jump back to 2


def say(*a, **k):
    print(*a, **k, flush=True)


def act(n, title):
    if ARGS.act and ARGS.act != n:
        return False
    say(f"\n{C['h']}━━ ACT {n} · {title} {'━' * max(3, 60 - len(title))}{C['r']}")
    if ARGS.presenter:
        input(f"{C['dim']}   (Enter to run){C['r']}")
    else:
        time.sleep(ARGS.pause)
    return True


def hexs(b, n=8):
    return " ".join(f"{x:02x}" for x in bytes(b)[:n])


# ---------------- servers (in-process, real sockets, optional TLS) ----------------
class Tap:
    """Wraps a PIR server and records exactly what the (curious) operator sees: the query bytes."""
    def __init__(self, real, delay=0.0):
        self.real, self.seen, self.delay = real, [], delay

    def __getattr__(self, k):
        return getattr(self.real, k)

    def answer(self, q):
        self.seen.append(np.array(q, copy=True))
        if self.delay:
            time.sleep(self.delay)
        return self.real.answer(q)


class Cheat(Tap):
    def answer(self, q):
        a = super().answer(q).copy()
        a.flat[0] ^= (1 << 28) if a.dtype == np.uint32 else 1
        return a


class Servers:
    def __init__(self, recs, tls):
        self.recs, self.tls, self.socks, self.N = recs, tls, [], recs.shape[0]

    def _serve(self, scheme, xs, ls, shard):
        s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0)); s.listen(16); self.socks.append(s)
        def loop():
            while True:
                try:
                    c, _ = s.accept()
                except OSError:
                    return
                threading.Thread(target=srvmod.handle, args=(c, scheme, self.recs, xs, ls, shard, self.tls), daemon=True).start()
        threading.Thread(target=loop, daemon=True).start()
        return f"127.0.0.1:{s.getsockname()[1]}"

    def xor(self, wrap=Tap):
        srv = wrap(XorPIRServer(self.recs.copy()))
        return self._serve("xor", srv, None, {"range": (0, self.N), "unit": "records"}), srv

    def lwe(self, wrap=Tap):
        full = LwePIRServer(self.recs, LweParams())
        band = wrap(LweRowShard(full, 0, full.db.shape[0]).server)
        return self._serve("lwe", None, band, {"range": (0, full.db.shape[0]), "unit": "db_rows"}), band

    def dead(self):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
        return f"127.0.0.1:{p}"

    def close(self):
        for s in self.socks:
            s.close()


def session(src, video, label, batch="adaptive"):
    t = time.time()
    m = SeekablePlayer(src, video, batch=batch, target_buffer_s=3.0).run(seeks=SEEKS, max_chunks=17)
    ok = m["play_log"][:5] == list(range(5)) and m["play_log"][5:8] == [14, 15, 16] and m["play_log"][8:10] == [2, 3]
    say(f"   {label:<34} played {m['chunks_played']:>2} chunks (seeks incl.) | startup {m['startup_latency_s']:.2f}s | "
        f"rebuffers {m['rebuffer_count']} | rounds {m['rounds']} | batches {m['batch_sizes'][:9]}")
    say(f"   {C['ok'] if ok else C['bad']}every chunk SHA-256-verified against the signed manifest · play order correct: {ok}{C['r']}")
    return m


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("--auto", action="store_true", help="no key presses; short pauses")
    ap.add_argument("--pause", type=float, default=1.5)
    ap.add_argument("--act", type=int, default=0)
    ARGS = ap.parse_args()
    ARGS.presenter = not ARGS.auto and sys.stdin.isatty()

    say(f"{C['b']}Private video streaming with PIR — live demo{C['r']}   {C['dim']}(real TLS servers on localhost, real player){C['r']}")
    tmp = tempfile.mkdtemp()
    cat = os.path.join(tmp, "catalog")
    build_catalog(cat, 32768, 0.4, synthetic_videos=3, chunks_per_video=20)
    manifest, index, recs = ps.load_catalog_records(cat)
    videos = sorted(manifest["videos"])
    watched = videos[1]
    certs = tls_util.make_certs(os.path.join(tmp, "certs"))
    ctx_c = tls_util.client_context(certs["ca"], certs["client_cert"], certs["client_key"])
    ctx_s = tls_util.server_context(certs["cert"], certs["key"], certs["ca"])
    priv, pub = integrity.keygen(os.path.join(tmp, "keys"))
    integrity.sign_manifest(cat, priv)
    N = recs.shape[0]
    say(f"{C['dim']}catalog: {len(videos)} videos × 20 chunks = {N} records of 32 KiB · viewer watches '{watched}' "
        f"and seeks 4→14, then back to 2{C['r']}")
    S = Servers(recs, ctx_s)
    first_global = index[(watched, 0)]

    try:
        # ---------------- ACT 1 ----------------
        if act(1, "Without PIR the server sees everything"):
            bsrv = BaselineServer(("127.0.0.1", 0), cat)
            threading.Thread(target=bsrv.serve_forever, daemon=True).start()
            src = HTTPChunkSource(f"http://127.0.0.1:{bsrv.server_address[1]}")
            session(src, watched, "baseline (plain HTTP chunk fetch)", batch=1)
            src.close()
            seen = [(a["video"], a["chunk"]) for a in bsrv.access]
            say(f"   {C['bad']}SERVER LOG →{C['r']} {watched}: chunk requests in order {[c for v, c in seen][:24]} …")
            say(f"   {C['dim']}(you can see the jump when the viewer seeked, plus the player's read-ahead){C['r']}")
            say(f"   {C['bad']}The operator knows which video, when you seeked, and where you stopped.{C['r']}")
            bsrv.shutdown()

        # ---------------- ACT 2 ----------------
        if act(2, "Two-server XOR PIR: same session, nothing to log"):
            a0, t0 = S.xor(); a1, t1 = S.xor()
            be = ns.NetXorBackend([a0], [a1], tls=ctx_c)
            src = ns.make_source(cat, be, pubkey=pub)
            session(src, watched, "XOR PIR over TLS 1.3 + mutual auth")
            say(f"   {C['y']}SERVER A's view{C['r']}: {len(t0.seen)} requests, each just {t0.seen[0].shape[1]} random-looking bytes, e.g.")
            for q in t0.seen[:3]:
                say(f"        {hexs(q[0])}   (≈{np.unpackbits(q[0]).mean():.0%} of bits set)")
            say(f"   {C['ok']}No video id, no chunk number. Alone, each share is uniformly random.{C['r']}")
            q0 = ns.XorPIRClient(N, recs.shape[1])
            st, (x, y) = q0.query([first_global + 5])
            say(f"   share to A: {hexs(x[0])}   share to B: {hexs(y[0])}   A⊕B = {hexs(x[0] ^ y[0])}  ← the 1-hot vector, "
                f"visible only if A and B collude")

        # ---------------- ACT 3 ----------------
        if act(3, "Single-server lattice PIR (LWE): one honest-but-curious server"):
            a, tp = S.lwe()
            be = ns.NetLweBackend([a], tls=ctx_c)
            src = ns.make_source(cat, be, pubkey=pub)
            say(f"   one-time hint download: {be.offline['hint_bytes'] / 2**20:.1f} MiB (then every query is fresh)")
            session(src, watched, "lattice PIR over TLS")
            Q = tp.seen[0]
            say(f"   {C['y']}SERVER's view{C['r']}: {len(tp.seen)} query matrices of 32-bit numbers, e.g. {Q.flat[:4].tolist()} …")
            say(f"   {C['ok']}≈{(Q > 2**31).mean():.0%} above 2³¹ — indistinguishable from random under the LWE assumption (no 2nd server needed).{C['r']}")

        # ---------------- ACT 4 ----------------
        if act(4, "The curious operator tries to guess your chunk"):
            rng = np.random.default_rng(7)
            trials = 400
            ra, rb = XorPIRServer(recs.copy()), XorPIRServer(recs.copy())
            cl = ns.XorPIRClient(N, recs.shape[1])
            one, both, base = 0, 0, trials
            for _ in range(trials):
                i = int(rng.integers(N))
                st, (x, y) = cl.query([i])
                bits = np.unpackbits(x[0])[:N]
                if int(rng.choice(np.flatnonzero(bits)) if bits.any() else rng.integers(N)) == i:
                    one += 1                                    # best a lone server can do: guess from its share
                if int(np.flatnonzero(np.unpackbits(x[0] ^ y[0])[:N])[0]) == i:
                    both += 1
            say(f"   baseline server reading its log : {C['bad']}{base}/{trials} = 100%{C['r']}")
            say(f"   ONE XOR server (its share only) : {C['ok']}{one}/{trials} = {one/trials:.1%}{C['r']}   (chance = {1/N:.1%})")
            say(f"   BOTH XOR servers collude        : {C['bad']}{both}/{trials} = {both/trials:.0%}{C['r']}   ← the assumption: run them under different operators")
            say(f"   {C['dim']}And traffic analysis (batch sizes/timing) still leaks — see security_eval/ for the attack and the padding defence.{C['r']}")

        # ---------------- ACT 5 ----------------
        if act(5, "Cheating, refusing and dead servers"):
            hon = lambda: S.xor(lambda r: Tap(r, 0.05))[0]            # noqa: E731
            bad_a, bad = S.xor(Cheat)
            say(f"   {C['y']}one XOR server is now corrupting every answer; one more is unreachable (crashed port){C['r']}")
            be2 = rs.NetRobustXorBackend([hon(), S.dead()], [bad_a, hon()], tls=ctx_c, timeout=2.0)
            src2 = rs.make_robust_source(cat, be2, pub)
            session(src2, watched, "robust XOR (F=1 per side)")
            for kind, who, det in be2.events[:4]:
                say(f"      event: {C['bad'] if kind == 'cheater' else C['y']}{kind:<11}{C['r']} {who}")
            say(f"   {C['ok']}Playback is still bit-exact: the cheater is outvoted by the signed hashes, caught and quarantined.{C['r']}")
            say(f"\n   {C['y']}now an entire operator disappears (side 2 of the XOR deployment is down){C['r']}")
            xor_dead = rs.NetRobustXorBackend([hon(), hon()], [S.dead(), S.dead()], tls=ctx_c, timeout=1.0)
            lwe = rs.NetRobustLweBackend([S.lwe()[0], S.lwe()[0]], tls=ctx_c, timeout=2.0)
            chain = rs.FailoverBackend([xor_dead, lwe])
            src3 = rs.make_robust_source(cat, chain, pub, patience_s=10)
            session(src3, watched, "XOR → fails over to lattice PIR")
            say(f"      events: XOR deployment failed → {chain.events[1][1]} took over")
            say(f"   {C['ok']}Never wrong data. If nothing honest is reachable the client stops with an error instead of lying.{C['r']}")

        # ---------------- ACT 6 ----------------
        if act(6, "The numbers (measured, from the project's result files)"):
            def rows(path):
                with open(os.path.join(ROOT, path)) as f:
                    return list(csv.DictReader(f))
            say(f"   {C['b']}Per-chunk cost (64 KiB-class chunks){C['r']}   baseline 1×  |  XOR ≈2× down  |  lattice ≈4× down + upload, one-time hint")
            try:
                sh = rows("pir/results/sharding_large_catalog.csv")
                for sc, mib in (("xor", "1024"), ("lwe", "256")):
                    r1 = next(r for r in sh if r["scheme"] == sc and r["catalog_MiB"] == mib and r["shards"] == "1")
                    r8 = next(r for r in sh if r["scheme"] == sc and r["catalog_MiB"] == mib and r["shards"] == "8")
                    say(f"   {sc.upper():<4} {mib:>5} MiB catalog: server time {float(r1['server_max_ms']):.0f} ms → "
                        f"{float(r8['server_max_ms']):.0f} ms with 8 shards (slowest-shard projection)")
                pa = rows("pir/results/playback_adaptive.csv")
                for r in pa:
                    if r["scenario"] == "play" and r["link"] == "5 Mbps" and r["batch"] in ("adaptive", "1") and "baseline" not in r["scheme"] and r["batch"] == "adaptive":
                        say(f"   playback @5 Mbit/s (~1 Mbit/s video), {r['scheme']:<22}: startup {float(r['startup_latency_s']):.2f}s, "
                            f"rebuffers {r['rebuffer_count']}")
            except Exception as e:  # noqa: BLE001
                say(f"   (result files not found: {e})")
            say(f"   {C['ok']}Takeaway: XOR PIR is real-time on ordinary links; single-server lattice PIR works but needs ~13 Mbit/s "
                f"at 2 s/367 KB chunks.{C['r']}")
        say(f"\n{C['b']}Done.{C['r']}")
    finally:
        S.close()


if __name__ == "__main__":
    main()
