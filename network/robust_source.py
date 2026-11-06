"""Fault-tolerant PIR over TCP: cheating or silent servers can neither feed the player wrong data nor stop it.

What is guaranteed (f = number of faulty servers tolerated, g = f + 1 servers per side):
  * Correctness: every record returned has been checked against the Ed25519-signed manifest, so a wrong record is
    never returned -- either the right record comes back or an error is raised (fail-safe).
  * Liveness: with at most f faulty (lying / refusing / hanging / crashed) units on each side, retrieval still
    succeeds, and without waiting for the faulty ones (answers are accepted as soon as a verifying combination exists).
  * Cheaters are identified (their answer differs from the verified one) and quarantined; dead servers are skipped
    for `cooldown_s`.
What it costs: g x the bandwidth/compute of the plain scheme (every unit on a side gets the same query).
What it still cannot do: force a server to answer (it removes the *effect* of refusing), tolerate more than f faults on
one side (then you get an error, never wrong data), or keep privacy if a unit on side 0 colludes with a unit on side 1.

XOR  (`NetRobustXorBackend`): side 0 = g units that all receive share q0, side 1 = g units that all receive q1 = q0 xor e_i.
     Per record the client tries combinations a xor b over the distinct answers of the two sides (most-voted first;
     normally one combination) and keeps the one whose SHA-256 matches the manifest. One honest unit per side suffices.
     A "unit" is a whole replica: a list of shard addresses, so a bad or dead shard makes its unit faulty.
LWE  (`NetRobustLweBackend`): g independent single-server units; the client queries one, verifies the decoded records,
     and fails over to the next unit on a wrong answer, error or timeout.
"""
import queue
import threading
import time
from collections import Counter

import numpy as np

import integrity
import net_source as ns
from integrity import IntegrityError, ServerUnavailable
from pir_core import LwePIRClient, LweParams, XorPIRClient


def _units(spec):
    """[['h:p','h:p'], 'h:p', ...] -> list of lists of parsed addresses (a bare address is a 1-shard unit)."""
    return [ns._addrs([u] if isinstance(u, str) else u) for u in spec]


class _Unit:
    def __init__(self, addrs, name):
        self.addrs, self.name = addrs, name
        self.down_until = 0.0
        self.banned_until = 0.0
        self.info = None   # filled lazily: ranges for XOR shards / client for LWE

    def usable(self, now):
        return now >= self.down_until and now >= self.banned_until


class _Base:
    def __init__(self, tls, timeout, cooldown_s, verifier, ban_s=120.0):
        self.tls, self.timeout, self.cooldown_s, self.verifier, self.ban_s = tls, timeout, cooldown_s, verifier, ban_s
        self.events = []     # (kind, unit name, detail): 'unavailable' | 'cheater'
        self.stats = ns.ps._Stats()
        self.offline = {"hint_bytes": 0, "setup_s": 0.0}
        self._lock = threading.Lock()

    def _event(self, kind, unit, detail=""):
        with self._lock:
            self.events.append((kind, unit.name, str(detail)[:120]))

    def _mark_down(self, unit, err):
        unit.down_until = time.monotonic() + self.cooldown_s
        self._event("unavailable", unit, repr(err))

    def _mark_cheater(self, unit):
        unit.banned_until = time.monotonic() + self.ban_s   # probation, not for ever: a repaired server is re-admitted
        self._event("cheater", unit, "answer failed verification")

    def _need_verifier(self):
        if self.verifier is None:
            raise RuntimeError("robust backends need a verifier (use robust_source.make_robust_source)")


class NetRobustXorBackend(_Base):
    name = "robust two-group XOR (TCP)"

    def __init__(self, side0, side1, tls=None, timeout=5.0, cooldown_s=30.0, verifier=None, fanout=None, ban_s=120.0):
        # fanout: units asked per side in the first wave (default: all). List spares after the primaries and set
        # fanout=F+1: spares are only contacted when the first wave cannot produce a verified answer.
        super().__init__(tls, timeout, cooldown_s, verifier, ban_s)
        self.fanout = fanout
        self.sides = [[_Unit(a, f"xor{s}.{i}@{a[0][0]}:{a[0][1]}") for i, a in enumerate(_units(sd))]
                      for s, sd in enumerate((side0, side1))]
        self.N = self.B = None
        for s in self.sides:                 # learn catalog dimensions from the first unit that answers
            for u in s:
                try:
                    self._load(u)
                    break
                except Exception as e:  # noqa: BLE001
                    self._mark_down(u, e)
            if self.N is not None:
                break
        if self.N is None:
            raise ServerUnavailable("no XOR server reachable")
        self.client = XorPIRClient(self.N, self.B)

    def _load(self, u):
        if u.info is None:
            infos = ns._parallel([lambda a=a: ns.request(a, {"op": "info"}, tls=self.tls, timeout=self.timeout)[0]
                                  for a in u.addrs])
            N, B = int(infos[0]["N"]), int(infos[0]["B"])
            if self.N is not None and (N, B) != (self.N, self.B):
                raise ValueError("unit disagrees about catalog dimensions")
            rs = [tuple(i["shard"]) for i in infos]
            srt = sorted(rs)
            if srt[0][0] != 0 or srt[-1][1] != N or any(x[1] != y[0] for x, y in zip(srt, srt[1:])):
                raise ValueError(f"shards do not tile the catalog: {srt}")
            if self.N is None:
                self.N, self.B = N, B
            u.info = rs
        return u.info

    def _ask(self, u, q):
        rs = self._load(u)
        sl = [np.ascontiguousarray(q[:, a // 8:(b + 7) // 8]) for a, b in rs]
        outs = ns._parallel([lambda a=a, x=x: ns.request(a, {"op": "xor_answer"}, x, tls=self.tls, timeout=self.timeout)[1]
                             for a, x in zip(u.addrs, sl)])
        ans = outs[0]
        for o in outs[1:]:
            ans = ans ^ o
        return ans, sum(x.nbytes for x in sl), sum(o.nbytes for o in outs)

    def _resolve(self, idx, got):
        """got[side] = list of (unit, answer (k,B)). Returns {row: (a, b, record)} for verified rows."""
        done = {}
        for j, gi in enumerate(idx):
            cands = []
            for side in (0, 1):
                cnt = Counter(ans[j].tobytes() for _, ans in got[side])
                cands.append([np.frombuffer(v, dtype=np.uint8) for v, _ in cnt.most_common()])
            for a in cands[0]:
                for b in cands[1]:
                    rec = a ^ b
                    if self.verifier(gi, rec.tobytes()):
                        done[j] = (a, b, rec)
                        break
                if j in done:
                    break
        return done

    def retrieve(self, indices):
        self._need_verifier()
        idx = list(indices)
        t0 = time.perf_counter()
        _, qs = self.client.query(idx)
        t1 = time.perf_counter()
        now = time.monotonic()
        # per side: usable units first (in the order given: primaries, then spares); if none is usable try everyone
        order = []
        for side in (0, 1):
            pool = [u for u in self.sides[side] if u.usable(now)] or list(self.sides[side])
            order.append(pool)
        results = queue.Queue()

        def job(side, u):
            try:
                results.put((side, u, self._ask(u, qs[side]), None))
            except Exception as e:  # noqa: BLE001
                results.put((side, u, None, e))
        got, done, up, down, asked = [[], []], {}, 0, 0, 0
        fan = self.fanout or max(len(order[0]), len(order[1]))
        wave = 0
        while len(done) < len(idx):
            todo = []
            for side in (0, 1):
                chunk = order[side][:fan] if wave == 0 else order[side][fan:]
                todo += [(side, u) for u in chunk]
            if not todo:
                break
            for side, u in todo:
                threading.Thread(target=job, args=(side, u), daemon=True).start()
            asked += len(todo)
            pending, deadline = len(todo), time.monotonic() + self.timeout + 2
            while pending and len(done) < len(idx):
                try:
                    side, u, res, err = results.get(timeout=max(0.01, deadline - time.monotonic()))
                except queue.Empty:
                    break
                pending -= 1
                if err is not None:
                    self._mark_down(u, err)
                    continue
                ans, u_up, u_down = res
                up, down = up + u_up, down + u_down
                got[side].append((u, ans))
                if got[0] and got[1]:
                    done = self._resolve(idx, got)
            if wave == 1:
                break
            wave += 1
        if len(done) < len(idx):
            if not got[0] or not got[1]:
                raise ServerUnavailable("no usable answer from at least one side (%d/%d units responded)"
                                        % (len(got[0]) + len(got[1]), asked))
            raise IntegrityError("no combination of answers verifies: more XOR servers misbehave than the pool can absorb")
        # identify cheaters among the answers we saw: anything that differs from the verified answer of its side
        for side in (0, 1):
            for u, ans in got[side]:
                if any(not np.array_equal(ans[j], done[j][side]) for j in range(len(idx))):
                    self._mark_cheater(u)
        t2 = time.perf_counter()
        s = self.stats
        s.rounds += 1
        s.records += len(idx)
        s.up_bytes += up
        s.down_bytes += down
        s.server_s += t2 - t1
        s.client_s += t1 - t0
        return [done[j][2].tobytes() for j in range(len(idx))]


class NetRobustLweBackend(_Base):
    name = "robust single-server lattice (TCP, failover)"

    def __init__(self, units, tls=None, timeout=5.0, cooldown_s=30.0, verifier=None, ban_s=120.0):
        super().__init__(tls, timeout, cooldown_s, verifier, ban_s)
        self.units = [_Unit(a, f"lwe{i}@{a[0][0]}:{a[0][1]}") for i, a in enumerate(_units(units))]
        self.N = self.B = None
        self.last_good = 0
        for u in self.units:
            try:
                self._load(u)
                break
            except Exception as e:  # noqa: BLE001
                self._mark_down(u, e)
        if self.N is None:
            raise ServerUnavailable("no LWE server reachable")

    def _load(self, u):
        if u.info is None:
            t = time.perf_counter()
            infos = ns._parallel([lambda a=a: ns.request(a, {"op": "info"}, tls=self.tls, timeout=self.timeout)[0]
                                  for a in u.addrs])
            order = sorted(range(len(infos)), key=lambda i: infos[i]["shard"][0])
            addrs = [u.addrs[i] for i in order]
            N, B = int(infos[0]["N"]), int(infos[0]["B"])
            if self.N is not None and (N, B) != (self.N, self.B):
                raise ValueError("unit disagrees about catalog dimensions")
            hint = np.concatenate(ns._parallel([lambda a=a: ns.request(a, {"op": "lwe_hint"}, tls=self.tls,
                                                                       timeout=max(self.timeout, 60))[1] for a in addrs]), axis=0)
            if self.N is None:
                self.N, self.B = N, B
                self.offline = {"hint_bytes": int(hint.nbytes), "setup_s": round(time.perf_counter() - t, 3)}
            u.info = (addrs, LwePIRClient(N, B, hint, LweParams()))
        return u.info

    def _ask(self, u, idx):
        addrs, cli = self._load(u)
        state, Q = cli.query(idx)
        parts = ns._parallel([lambda a=a: ns.request(a, {"op": "lwe_answer"}, Q, tls=self.tls, timeout=self.timeout)[1]
                              for a in addrs])
        ans = np.concatenate(parts, axis=0)
        out = cli.decode(state, ans)
        return [out[j].tobytes() for j in range(len(idx))], Q.nbytes * len(addrs), ans.nbytes

    def retrieve(self, indices):
        self._need_verifier()
        idx = list(indices)
        t0 = time.perf_counter()
        now = time.monotonic()
        order = [self.units[(self.last_good + k) % len(self.units)] for k in range(len(self.units))]
        pool = [u for u in order if u.usable(now)] or order          # last resort: try everyone
        up = down = 0
        unavailable = cheaters = 0
        for u in pool:
            try:
                recs, u_up, u_down = self._ask(u, idx)
            except Exception as e:  # noqa: BLE001
                self._mark_down(u, e)
                unavailable += 1
                continue
            up, down = up + u_up, down + u_down
            if all(self.verifier(gi, r) for gi, r in zip(idx, recs)):
                self.last_good = self.units.index(u)
                s = self.stats
                s.rounds += 1
                s.records += len(idx)
                s.up_bytes += up
                s.down_bytes += down
                s.server_s += time.perf_counter() - t0
                return recs
            self._mark_cheater(u)
            cheaters += 1
        if cheaters:
            raise IntegrityError(f"{cheaters} LWE server(s) returned wrong answers and {unavailable} were unavailable")
        raise ServerUnavailable("no LWE server answered")


class FailoverBackend:
    """Chain of backends tried in order, e.g. [robust XOR groups, robust LWE servers]: if the XOR deployment cannot
    produce a verified answer (whole side down / too many cheaters), the LWE servers -- independent machines with a
    different trust assumption -- serve the request, so the stream survives the loss of an entire scheme."""
    name = "failover chain"

    def __init__(self, backends):
        assert len({b.N for b in backends}) == 1, "backends serve catalogs of different sizes"
        self.backends, self.N, self.B = list(backends), backends[0].N, backends[0].B
        self.active = self.backends[0]
        self.events = []
        self._verifier = None

    @property
    def verifier(self):
        return self._verifier

    @verifier.setter
    def verifier(self, v):
        self._verifier = v
        for b in self.backends:
            b.verifier = v

    @property
    def stats(self):
        return self.active.stats

    @property
    def offline(self):
        return self.active.offline

    def retrieve(self, indices):
        last = None
        for b in self.backends:
            try:
                out = b.retrieve(indices)
                if b is not self.active:
                    self.events.append(("failover", b.name, "serving after earlier backend failed"))
                self.active = b
                return out
            except (ServerUnavailable, IntegrityError) as e:
                self.events.append(("backend_failed", b.name, str(e)[:100]))
                last = e
        raise last


class RobustVerifiedSource(ns.VerifiedPIRChunkSource):
    """Never gives up at the first failure: while no verified answer is obtainable (outage, too many faulty servers)
    it keeps re-probing every server -- including quarantined ones -- with exponential backoff for `patience_s`, so
    playback just rebuffers and resumes when enough honest servers are back. Only after `patience_s` does the error
    surface (the player will never be handed unverified data either way)."""

    def __init__(self, manifest, index, backend, retries=0, patience_s=20.0):
        super().__init__(manifest, index, backend, retries)
        self.patience_s, self.waits = patience_s, 0

    def _verified(self, video_id, indices):
        t0, delay = time.monotonic(), 0.25
        while True:
            try:
                return super()._verified(video_id, indices)
            except (ServerUnavailable, IntegrityError):
                if time.monotonic() - t0 + delay > self.patience_s:
                    raise
                self.waits += 1
                time.sleep(delay)
                delay = min(delay * 2, 2.0)


def make_robust_source(catalog_dir, backend, pubkey, retries=0, patience_s=20.0):
    """Signed manifest (required) + per-record verification wired into a fault-tolerant backend."""
    manifest = integrity.load_verified_manifest(catalog_dir, pubkey)
    index, inv, n = {}, {}, 0
    for vid, v in manifest["videos"].items():
        for i, c in enumerate(v["chunks"]):
            index[(vid, i)] = n
            inv[n] = (vid, i, c["sha256"])
            n += 1
    assert n == backend.N, f"manifest has {n} chunks but servers hold {backend.N}"
    import hashlib
    backend.verifier = lambda gi, rec: hashlib.sha256(rec).hexdigest() == inv[gi][2]
    return RobustVerifiedSource(manifest, index, backend, retries, patience_s)
