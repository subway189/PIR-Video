"""Run every experiment of the Evaluation & Security track.  From the project root:
    python3 -m eval.run_all            -> writes results/*.csv, results/*.json, results/*.png
"""
import json
import os
import sys
import time

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from eval.attacks import (fingerprint_attack, lwe_query_distinguisher, seek_attack,  # noqa: E402
                          xor_colluding_attack, xor_single_server_attack)
from eval.metrics import CostModel, TracedBackend, simulate_session, summarize  # noqa: E402
from pir_core import LwePIR, XorPIR  # noqa: E402

RES = os.path.join(os.path.dirname(__file__), "..", "results")
os.makedirs(RES, exist_ok=True)
KB = 1024
T0 = time.time()


def log(msg):
    print(f"[{time.time() - T0:6.1f}s] {msg}", flush=True)


# ------------------------------------------------------------------ E1: instrumented real backends
def e1_real_instrumented():
    rng = np.random.default_rng(11)
    rows = []
    plan = [("xor", N, 32 * KB) for N in (97, 512, 2048)] + [("lwe", N, 32 * KB) for N in (97, 256, 1024)]
    for scheme, N, B in plan:
        recs = rng.integers(0, 256, (N, B), dtype=np.uint8)
        tb = TracedBackend(XorPIR() if scheme == "xor" else LwePIR())
        tb.setup(recs)
        for k in (1, 4, 8):
            for _ in range(3):
                idx = [int(i) for i in rng.integers(0, N, k)]
                out = tb.retrieve(idx)
                assert np.array_equal(out, recs[idx]), "WRONG RESULT"
        df = pd.DataFrame(tb.log)
        g = df.groupby("k").agg(t_query=("t_query", "min"), t_server=("t_server_max", "min"),
                                t_decode=("t_decode", "min"), t_total=("t_compute_latency", "min"),
                                up_kb=("bytes_up", "first"), down_kb=("bytes_down", "first")).reset_index()
        g[["up_kb", "down_kb"]] /= KB
        g.insert(0, "scheme", scheme); g.insert(1, "N", N); g.insert(2, "db_MB", N * B / 2**20)
        g["setup_s"] = tb.setup_seconds
        rows.append(g)
        log(f"E1 {scheme} N={N} ok")
    out = pd.concat(rows)
    out.to_csv(os.path.join(RES, "e1_instrumented_sweep.csv"), index=False)
    return out


# ------------------------------------------------------------------ E2: playback grid
def e2_playback():
    rng = np.random.default_rng(5)
    rows = []
    for scheme in ("plain", "xor", "lwe"):
        for rtt in (0.02, 0.1):
            for bw in (5, 10, 25, 50, 100):
                for pol in (1, 4, 8, 16, "adaptive"):
                    rs = []
                    for rep in range(12):
                        cost = CostModel(scheme, bw * float(rng.lognormal(0, 0.2)), rtt)
                        seeks = [(float(rng.uniform(40, 250)), int(rng.integers(0, 150)))] if rep % 3 == 0 else []
                        rs.append(simulate_session(150, cost, pol, seeks=seeks, rng=rng))
                    d = summarize(rs)
                    d.update(scheme=scheme, rtt_s=rtt, bw_mbps=bw, policy=str(pol))
                    rows.append(d)
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(RES, "e2_playback_grid.csv"), index=False)
    # privacy-motivated variant: constant-rate cover traffic vs buffered, same network
    rows = []
    for scheme in ("xor", "lwe"):
        for bw in (10, 25, 50, 100):
            for name, kw in (("buffered-adaptive", dict(policy="adaptive")),
                             ("constant-rate k=8", dict(mode="constant", k_const=8)),
                             ("constant-rate k=4", dict(mode="constant", k_const=4))):
                rs = [simulate_session(150, CostModel(scheme, bw * float(rng.lognormal(0, 0.2)), 0.05),
                                       seeks=[(float(rng.uniform(40, 250)), int(rng.integers(0, 150)))] if r % 3 == 0 else [],
                                       rng=rng, **kw) for r in range(12)]
                d = summarize(rs); d.update(scheme=scheme, bw_mbps=bw, mode=name)
                rows.append(d)
    pd.DataFrame(rows).to_csv(os.path.join(RES, "e2b_constant_rate_cost.csv"), index=False)
    return df


# ------------------------------------------------------------------ E3: crypto-layer attacks
def e3_crypto():
    res = dict(xor_single=xor_single_server_attack(97, 20000), xor_single_N1000=xor_single_server_attack(1000, 20000),
               xor_colluding=xor_colluding_attack(97, 2000), lwe=lwe_query_distinguisher(trials=3000))
    json.dump(res, open(os.path.join(RES, "e3_crypto_attacks.json"), "w"), indent=2)
    return res


# ------------------------------------------------------------------ E4/E5: fingerprint attack + countermeasures
def e4_vsweep():
    rows = []
    for scheme in ("xor", "lwe"):
        for V in (10, 50, 200, 1000):
            r = fingerprint_attack(V, dict(scheme=scheme), n_train=200 if V < 1000 else 80,
                                   n_test=30 if V < 1000 else 10, seed=V)
            r.update(scheme=scheme); rows.append(r)
            log(f"E4 {scheme} V={V} top1={r['top1']:.3f}")
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(RES, "e4_fingerprint_vs_catalog.csv"), index=False)
    return df


def e5_countermeasures(V=200):
    cfgs = [("none (adaptive batches)", dict(sim=dict(policy="adaptive"))),
            ("fixed k=8", dict(sim=dict(policy=8))),
            ("fixed k=8 + pad last batch", dict(sim=dict(policy=8, pad_batch=True)))]
    cfgs += [(f"pad + length bucket {b}", dict(sim=dict(policy=8, pad_batch=True, bucket_chunks=b)))
             for b in (25, 50, 100, 200, 400, 1600)]
    cfgs += [(f"constant-rate + bucket {s} slots", dict(sim=dict(mode="constant", k_const=8, bucket_slots=s)))
             for s in (0, 25, 100, 400)]
    rows = []
    for name, cfg in cfgs:
        r = fingerprint_attack(V, cfg, n_train=250, n_test=40, seed=7)
        r["config"] = name
        rows.append(r)
        log(f"E5 {name}: top1={r['top1']:.3f} overhead={r['overhead']:.2f}")
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(RES, "e5_countermeasure_curve.csv"), index=False)
    return df


def e6_seek():
    rows = []
    for name, sim in (("buffered, immediate refetch", dict(policy="adaptive")),
                      ("buffered, fixed k=8", dict(policy=8)),
                      ("constant-rate k=8", dict(mode="constant", k_const=8))):
        r = seek_attack(dict(sim=sim), n=300); r["config"] = name; rows.append(r)
        log(f"E6 {name}: auc={r['auc']:.3f}")
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(RES, "e6_seek_attack.csv"), index=False)
    return df


# ------------------------------------------------------------------ figures
def figures(e1, e2, e4, e5, e6):
    plt.rcParams.update({"font.size": 9})
    # Fig 1 playback: rebuffer seconds and startup vs link speed (rtt=0.02, adaptive)
    d = e2[(e2.rtt_s == 0.02)]
    fig, ax = plt.subplots(1, 2, figsize=(9, 3.4))
    for sch, c in (("plain", "gray"), ("xor", "tab:blue"), ("lwe", "tab:red")):
        for pol, ls in (("adaptive", "-"), ("4", "--")):
            g = d[(d.scheme == sch) & (d.policy == pol)].sort_values("bw_mbps")
            ax[0].plot(g.bw_mbps, g.rebuffer_s, c=c, ls=ls, marker="o", label=f"{sch}/{pol}")
            ax[1].plot(g.bw_mbps, g.startup_s, c=c, ls=ls, marker="o")
    ax[0].set(xscale="log", xlabel="link (Mbit/s)", ylabel="mean rebuffer time per 5-min session (s)")
    ax[1].set(xscale="log", yscale="log", xlabel="link (Mbit/s)", ylabel="startup latency (s)")
    ax[0].legend(fontsize=7); [a.grid(alpha=.3) for a in ax]
    fig.tight_layout(); fig.savefig(os.path.join(RES, "fig_playback.png"), dpi=160); plt.close(fig)
    # Fig 2 attack vs catalog size
    fig, ax = plt.subplots(figsize=(4.8, 3.4))
    for sch, c in (("xor", "tab:blue"), ("lwe", "tab:red")):
        g = e4[e4.scheme == sch]
        ax.plot(g.V, g.top1, c=c, marker="o", label=f"attacker top-1 ({sch})")
    g = e4[e4.scheme == "xor"]
    ax.plot(g.V, g.prior_only, c="k", ls=":", marker="x", label="popularity-prior guess")
    ax.plot(g.V, g.uniform_chance, c="gray", ls="--", label="uniform chance 1/V")
    ax.set(xscale="log", yscale="log", xlabel="catalog size V (videos)", ylabel="identification accuracy")
    ax.legend(fontsize=7); ax.grid(alpha=.3); fig.tight_layout()
    fig.savefig(os.path.join(RES, "fig_attack_vs_catalog.png"), dpi=160); plt.close(fig)
    # Fig 3 privacy vs overhead
    fig, ax = plt.subplots(figsize=(6.2, 3.8))
    ax.scatter(e5.overhead, e5.top1, c="tab:purple")
    for _, r in e5.iterrows():
        if r.config == "fixed k=8":
            continue
        ax.annotate(r.config.replace("none (adaptive batches)", "none / fixed k=8").replace("pad + length ", "").replace("constant-rate + ", "CR+"), (r.overhead, r.top1),
                    fontsize=6, xytext=(3, 3), textcoords="offset points")
    ax.axhline(e5.prior_only.iloc[0], c="k", ls=":", label="popularity-prior guess (no traffic info)")
    ax.set(xscale="log", xlabel="bandwidth overhead (chunks sent / chunks needed)",
           ylabel="attacker top-1 accuracy (V=200)")
    ax.legend(fontsize=7); ax.grid(alpha=.3); fig.tight_layout()
    fig.savefig(os.path.join(RES, "fig_privacy_vs_overhead.png"), dpi=160); plt.close(fig)
    # Fig 4 instrumented scaling
    fig, ax = plt.subplots(figsize=(4.8, 3.4))
    for sch, c in (("xor", "tab:blue"), ("lwe", "tab:red")):
        g = e1[(e1.scheme == sch) & (e1.k == 4)].sort_values("db_MB")
        ax.plot(g.db_MB, g.t_server * 1e3, c=c, marker="o", label=sch)
    ax.set(xscale="log", yscale="log", xlabel="catalog size (MB, 32 KB chunks)", ylabel="server time, batch of 4 (ms)")
    ax.legend(); ax.grid(alpha=.3); fig.tight_layout()
    fig.savefig(os.path.join(RES, "fig_scaling_instrumented.png"), dpi=160); plt.close(fig)


if __name__ == "__main__":
    e1 = e1_real_instrumented(); log("E1 done")
    e2 = e2_playback(); log("E2 done")
    e3 = e3_crypto(); log("E3 done")
    e4 = e4_vsweep(); log("E4 done")
    e5 = e5_countermeasures(); log("E5 done")
    e6 = e6_seek(); log("E6 done")
    figures(e1, e2, e4, e5, e6)
    log("figures done")
