#!/usr/bin/env python3
"""Fault tolerance: cheating / refusing / hanging / crashed servers must neither corrupt playback nor stop it.
Run: PYTHONPATH=<project root> python3 network/test_robust.py"""
import os, socket, sys, threading, time, unittest
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_security import Base, Evil, serve
import integrity, net_source as ns, robust_source as rs
import pir_network_server as srvmod
from pir_core import LwePIRServer, LweParams, XorPIRServer
from pir_core.sharded import LweRowShard, _bounds
from player import SeekablePlayer


class RowEvil(Evil):
    def __init__(self, real, row):
        super().__init__(real)
        self.row = row

    def answer(self, q):
        a = self.real.answer(q).copy()
        self.calls += 1
        if a.shape[0] > self.row:
            a[self.row, 0] ^= 1
        return a


class Slow:
    def __init__(self, real, delay):
        self.real, self.delay = real, delay

    def __getattr__(self, k):
        return getattr(self.real, k)

    def answer(self, q):
        time.sleep(self.delay)
        return self.real.answer(q)


class Fault(Base):
    def setUp(self):
        self.socks, self.holds = [], []
        self.N = self.recs.shape[0]

    def tearDown(self):
        for s in self.socks:
            s.close()
        for c in self.holds:
            c.close()

    # --- server factories (all in-process, each on its own port) ---
    def xor_unit(self, wrap=None, shards=1):
        addrs, wrapped = [], []
        for a, b in _bounds(self.N, shards, 8):
            real = XorPIRServer(self.recs[a:b].copy())
            srv = wrap(real) if wrap else real
            wrapped.append(srv)
            s, ad = serve("xor", self.recs, srv, None, {"range": (a, b), "unit": "records"})
            self.socks.append(s); addrs.append(ad)
        return addrs, wrapped

    def lwe_unit(self, wrap=None):
        full = LwePIRServer(self.recs, LweParams())
        band = LweRowShard(full, 0, full.db.shape[0]).server
        srv = wrap(band) if wrap else band
        s, ad = serve("lwe", self.recs, None, srv, {"range": (0, full.db.shape[0]), "unit": "db_rows"})
        self.socks.append(s)
        return [ad], srv

    def silent(self):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(16); self.socks.append(s)
        def loop():
            while True:
                try:
                    c, _ = s.accept()
                except OSError:
                    return
                self.holds.append(c)          # accept and never answer
        threading.Thread(target=loop, daemon=True).start()
        return [f"127.0.0.1:{s.getsockname()[1]}"]

    def refusing(self):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(16); self.socks.append(s)
        def loop():
            while True:
                try:
                    c, _ = s.accept()
                except OSError:
                    return
                try:
                    srvmod.recv_msg(c); srvmod.send_msg(c, {"ok": False, "error": "refused"})
                except Exception:
                    pass
                c.close()
        threading.Thread(target=loop, daemon=True).start()
        return [f"127.0.0.1:{s.getsockname()[1]}"]

    def crashed(self):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()   # nothing listens
        return [f"127.0.0.1:{p}"]

    # --- helpers ---
    def xor_source(self, side0, side1, timeout=3.0):
        be = rs.NetRobustXorBackend(side0, side1, timeout=timeout)
        return rs.make_robust_source(self.cat, be, self.pub), be

    def lwe_source(self, units, timeout=2.0):
        be = rs.NetRobustLweBackend(units, timeout=timeout)
        return rs.make_robust_source(self.cat, be, self.pub), be

    def play(self, src):
        return SeekablePlayer(src, self.video, batch=2, target_buffer_s=1.0).run(seeks=[(3, 6)])

    def ok(self, m):
        self.assertEqual(m["play_log"][:3], [0, 1, 2])
        self.assertEqual(m["play_log"][3:], [6, 7])   # video has 8 chunks


class XorFaults(Fault):
    def test_all_honest(self):
        src, be = self.xor_source([self.xor_unit()[0], self.xor_unit()[0]], [self.xor_unit()[0], self.xor_unit()[0]])
        self.ok(self.play(src)); self.assertEqual(be.events, [])

    def test_cheating_server_is_corrected_identified_and_quarantined(self):
        slow = lambda r: Slow(r, 0.15)                       # honest answers arrive after the cheater's
        bad_addr, bad = self.xor_unit(wrap=lambda r: Evil(r))
        src, be = self.xor_source([self.xor_unit(wrap=slow)[0], self.xor_unit(wrap=slow)[0]],
                                  [bad_addr, self.xor_unit(wrap=slow)[0]])
        self.ok(self.play(src))
        self.assertTrue(any(e[0] == "cheater" for e in be.events), be.events)
        self.assertEqual(bad[0].calls, 1, "quarantined cheater must not be asked again")

    def test_one_cheater_on_each_side(self):
        src, be = self.xor_source([self.xor_unit(wrap=Evil)[0], self.xor_unit()[0]],
                                  [self.xor_unit()[0], self.xor_unit(wrap=Evil)[0]])
        self.ok(self.play(src))

    def test_row_level_cheating_in_a_batch(self):
        src, be = self.xor_source([self.xor_unit(wrap=lambda r: RowEvil(r, 1))[0], self.xor_unit()[0]],
                                  [self.xor_unit()[0], self.xor_unit()[0]])
        self.ok(self.play(src))

    def test_silent_server_does_not_slow_or_stop_playback(self):
        src, be = self.xor_source([self.silent(), self.xor_unit()[0]], [self.xor_unit()[0], self.silent()], timeout=3.0)
        t = time.time(); m = self.play(src)
        self.ok(m)
        self.assertLess(time.time() - t, 3.0, "hanging servers must not add their timeout to every round")

    def test_refusing_and_crashed_servers(self):
        src, be = self.xor_source([self.refusing(), self.xor_unit()[0]], [self.crashed(), self.xor_unit()[0]], timeout=2.0)
        self.ok(self.play(src))
        self.assertTrue(any(e[0] == "unavailable" for e in be.events))

    def test_sharded_unit_with_one_bad_shard_is_treated_as_faulty(self):
        bad_addrs, _ = self.xor_unit(wrap=Evil, shards=2)
        # only the 2nd shard cheats
        addrs, wrapped = [], []
        for k, (a, b) in enumerate(_bounds(self.N, 2, 8)):
            real = XorPIRServer(self.recs[a:b].copy())
            s, ad = serve("xor", self.recs, Evil(real) if k == 1 else real, None, {"range": (a, b), "unit": "records"})
            self.socks.append(s); addrs.append(ad)
        src, be = self.xor_source([addrs, self.xor_unit(shards=2)[0]], [self.xor_unit()[0], self.xor_unit()[0]])
        self.ok(self.play(src))

    def test_too_many_faults_fail_safe_never_wrong_data(self):
        src, be = self.xor_source([self.xor_unit()[0], self.xor_unit()[0]],
                                  [self.xor_unit(wrap=Evil)[0], self.xor_unit(wrap=Evil)[0]])
        with self.assertRaises(integrity.IntegrityError):
            self.play(src)

    def test_whole_side_down_is_reported_as_unavailable(self):
        src, be = self.xor_source([self.xor_unit()[0], self.xor_unit()[0]], [self.silent(), self.crashed()], timeout=1.0)
        with self.assertRaises(integrity.ServerUnavailable):
            self.play(src)


class Beyond(Fault):
    """Situations that used to end in an error (more faults than F per side): spares, probation, patience, failover."""

    def test_spare_units_absorb_more_than_F_faults(self):
        # side 1 = two cheating primaries + one honest spare; first wave (fanout=2) fails, the spare saves the round
        spare_addr, spare = self.xor_unit(wrap=lambda r: Evil(r, 0))       # n=0: never corrupts, only counts calls
        be = rs.NetRobustXorBackend([self.xor_unit()[0], self.xor_unit()[0]],
                                    [self.xor_unit(wrap=Evil)[0], self.xor_unit(wrap=Evil)[0], spare_addr],
                                    timeout=3.0, fanout=2)
        src = rs.make_robust_source(self.cat, be, self.pub)
        self.ok(self.play(src))
        self.assertGreaterEqual(sum(1 for e in be.events if e[0] == "cheater"), 2)
        self.assertGreaterEqual(spare[0].calls, 1)

    def test_spares_cost_nothing_while_primaries_are_healthy(self):
        spare_addr, spare = self.xor_unit(wrap=lambda r: Evil(r, 0))
        be = rs.NetRobustXorBackend([self.xor_unit()[0], self.xor_unit()[0], self.xor_unit(wrap=lambda r: Evil(r, 0))[0]],
                                    [self.xor_unit()[0], self.xor_unit()[0], spare_addr], fanout=2)
        self.ok(self.play(rs.make_robust_source(self.cat, be, self.pub)))
        self.assertEqual(spare[0].calls, 0)

    def test_quarantine_is_probation_not_for_ever(self):
        addr, bad = self.xor_unit(wrap=lambda r: Evil(r, 1))              # corrupts only its first answer, then repairs
        be = rs.NetRobustXorBackend([addr, self.xor_unit()[0]], [self.xor_unit()[0], self.xor_unit()[0]],
                                    timeout=3.0, ban_s=0.4)
        rs.make_robust_source(self.cat, be, self.pub)
        for i in range(3):
            be.retrieve([i])                                              # first call: cheater caught and banned
        self.assertEqual(bad[0].calls, 1)
        time.sleep(0.6)
        for i in range(3):
            be.retrieve([i])
        self.assertGreater(bad[0].calls, 1, "repaired server must be re-admitted after probation")

    def test_outage_then_recovery_is_a_stall_not_a_failure(self):
        ports = []
        for _ in range(2):
            s = socket.socket(); s.bind(("127.0.0.1", 0)); ports.append(s.getsockname()[1]); s.close()
        be = rs.NetRobustXorBackend([self.xor_unit()[0], self.xor_unit()[0]],
                                    [[f"127.0.0.1:{p}"] for p in ports], timeout=1.0, cooldown_s=0.3)
        src = rs.make_robust_source(self.cat, be, self.pub, patience_s=15.0)

        def bring_up():
            time.sleep(1.5)
            for p in ports:
                real = XorPIRServer(self.recs.copy())
                s, _ = serve("xor", self.recs, real, None, {"range": (0, self.N), "unit": "records"}, port=p)
                self.socks.append(s)
        threading.Thread(target=bring_up, daemon=True).start()
        m = self.play(src)
        self.ok(m)
        self.assertGreater(src.waits, 0)
        self.assertTrue(m["startup_latency_s"] > 1.0 or m["rebuffer_count"] > 0, "the outage must show up as a stall")

    def test_whole_scheme_loss_fails_over_to_lattice_servers(self):
        xor = rs.NetRobustXorBackend([self.xor_unit()[0], self.xor_unit()[0]], [self.silent(), self.crashed()], timeout=1.0)
        lwe = rs.NetRobustLweBackend([self.lwe_unit()[0], self.lwe_unit()[0]], timeout=2.0)
        chain = rs.FailoverBackend([xor, lwe])
        src = rs.make_robust_source(self.cat, chain, self.pub, patience_s=5.0)
        self.ok(self.play(src))
        self.assertTrue(any(e[0] == "failover" for e in chain.events))

    def test_no_honest_server_anywhere_ends_in_bounded_time_with_an_error(self):
        be = rs.NetRobustXorBackend([self.xor_unit()[0]], [self.xor_unit(wrap=Evil)[0]], timeout=1.0)
        src = rs.make_robust_source(self.cat, be, self.pub, patience_s=1.5)
        t = time.time()
        with self.assertRaises(integrity.IntegrityError):
            self.play(src)
        self.assertLess(time.time() - t, 6.0)


class LweFaults(Fault):
    def test_failover_from_cheater_and_quarantine(self):
        bad_addr, bad = self.lwe_unit(wrap=lambda r: Evil(r, None, mask=1 << 28))
        src, be = self.lwe_source([bad_addr, self.lwe_unit()[0]])
        self.ok(self.play(src))
        self.assertTrue(any(e[0] == "cheater" for e in be.events))
        self.assertEqual(bad.calls, 1)

    def test_failover_from_silent_refusing_and_crashed(self):
        for first in (self.silent(), self.refusing(), self.crashed()):
            src, be = self.lwe_source([first, self.lwe_unit()[0]], timeout=1.0)
            self.ok(self.play(src))
            self.assertTrue(any(e[0] == "unavailable" for e in be.events))

    def test_all_bad_fails_safe(self):
        src, be = self.lwe_source([self.lwe_unit(wrap=lambda r: Evil(r, None, mask=1 << 28))[0], self.silent()], timeout=1.0)
        with self.assertRaises((integrity.IntegrityError, integrity.ServerUnavailable)):
            self.play(src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
