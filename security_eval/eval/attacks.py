"""Traffic-analysis attackers and crypto-layer distinguishers (Evaluation & Security Lead).

What a server / network observer sees per session is the trace [(time, k)] - request times and
batch sizes (for XOR: the query array shape (k, ceil(N/8)); for LWE: K = k*span columns).
PIR hides *which* chunks; these attackers test what the trace itself gives away.
"""
from __future__ import annotations

import math
from collections import defaultdict

import numpy as np

from .metrics import CostModel, simulate_session

# --------------------------------------------------------------------------- A. crypto layer


def xor_single_server_attack(N=97, trials=20000, seed=0):
    """Attacker sees ONE server's query vector and guesses the wanted index.
    Heuristic: pick a uniformly random position among the set bits (and, separately, among the unset
    bits).  Information-theoretically the best possible accuracy is 1/N."""
    from pir_core import XorPIRClient
    rng = np.random.default_rng(seed)
    cli = XorPIRClient(N, 16)
    hits_set = hits_unset = 0
    ones = np.zeros(N)
    for t in range(trials):
        i = int(rng.integers(N))
        _, (q0, q1) = cli.query([i])
        bits = np.unpackbits(q1, axis=1, count=N)[0]
        ones += bits
        s = np.flatnonzero(bits)
        u = np.flatnonzero(bits == 0)
        hits_set += int(len(s) and rng.choice(s) == i)
        hits_unset += int(len(u) and rng.choice(u) == i)
    p0 = 1 / N
    ci = 1.96 * math.sqrt(p0 * (1 - p0) / trials)
    return dict(N=N, trials=trials, chance=p0, acc_pick_set_bit=hits_set / trials,
                acc_pick_unset_bit=hits_unset / trials, ci95=ci,
                max_bit_bias=float(np.abs(ones / trials - 0.5).max()))


def xor_colluding_attack(N=97, trials=2000, seed=1):
    """Both servers collude (or both shares are sent to one server): q0 xor q1 = unit vector."""
    from pir_core import XorPIRClient
    rng = np.random.default_rng(seed)
    cli = XorPIRClient(N, 16)
    ok = 0
    for _ in range(trials):
        i = int(rng.integers(N))
        _, (q0, q1) = cli.query([i])
        d = np.unpackbits(q0 ^ q1, axis=1, count=N)[0]
        ok += int(np.argmax(d) == i)
    return dict(N=N, trials=trials, acc=ok / trials)


def lwe_query_distinguisher(N=120, B=24, trials=300, n=128, seed=2):
    """Sanity check (NOT a security proof): the entry of the LWE query at the wanted column should look
    uniform.  We compare the top byte at the target column vs. a non-target column with a chi-square test
    and run a guessing attacker (argmax of top byte).  Uses reduced n so it runs fast; real security
    rests on LWE hardness for n=1024, which this test cannot establish."""
    from pir_core import LwePIR, LweParams
    rng = np.random.default_rng(seed)
    recs = rng.integers(0, 256, (N, B), dtype=np.uint8)
    p = LwePIR(LweParams(n=n))
    p.setup(recs)
    lay = p.server.layout
    target = 7
    tcol, ocol = lay.columns_for(target)[0], lay.columns_for(0)[0]
    _, Q = p.client.query([target] * trials)               # (m, trials) independent queries
    top = (Q >> 24).astype(int)

    def chi2(x):
        c = np.bincount(x, minlength=256)
        e = len(x) / 256
        return float(((c - e) ** 2 / e).sum())             # 255 dof, mean 255, sd ~22.6
    hits = 0
    for j in range(trials):
        hits += int(int(np.argmax(top[:, j])) in lay.columns_for(target))
    return dict(m=lay.cols, trials=trials, chi2_target=chi2(top[tcol]), chi2_other=chi2(top[ocol]),
                chi2_expected=255.0, guess_acc=hits / trials, chance=1 / lay.cols)


# --------------------------------------------------------------------------- B. video-length fingerprinting


def make_catalog(V, rng, zipf_a=0.8):
    L = np.clip(rng.lognormal(math.log(150), 0.7, V), 20, 1500).astype(int)
    pop = 1.0 / np.arange(1, V + 1) ** zipf_a
    rng.shuffle(pop)
    return L, pop / pop.sum()


def random_session_args(L, rng, p_full=0.5, p_seek=0.3, tau=2.0):
    full = rng.random() < p_full
    watch = L * tau if full else float(rng.uniform(0.1, 1.0) * L * tau)
    seeks = []
    if rng.random() < p_seek:
        for _ in range(int(rng.integers(1, 3))):
            seeks.append((float(rng.uniform(0.1, 0.9) * watch), int(rng.integers(0, L))))
    return watch, seeks


def gen_sessions(L_v, n, cfg, rng, bw_choices=(10, 25, 50, 100)):
    out = []
    for _ in range(n):
        watch, seeks = random_session_args(L_v, rng, p_seek=cfg.get("p_seek", 0.3))
        bw = float(rng.choice(bw_choices))
        cost = CostModel(cfg.get("scheme", "xor"), bw, 0.05)
        out.append(simulate_session(L_v, cost, watch_s=watch, seeks=seeks, rng=rng,
                                    **cfg.get("sim", {})))
    return out


def fingerprint_attack(V, cfg, n_train=40, n_test=20, seed=0, features="TR", alpha=0.05):
    """Bayes-style attacker who knows the catalog's lengths and the viewing model: it learns
    P(feature | video) by simulating the client itself, then returns argmax posterior.
    features: 'TR' = (total chunks requested, number of requests); 'T' = total only."""
    rng = np.random.default_rng(seed)
    L, prior = make_catalog(V, rng)
    key = (lambda s: (s.T, s.R)) if features == "TR" else (lambda s: (s.T,))
    table = defaultdict(dict)                    # feature -> {video: count}
    n_v = np.full(V, float(n_train))
    for v in range(V):
        for s in gen_sessions(int(L[v]), n_train, cfg, rng):
            f = key(s)
            table[f][v] = table[f].get(v, 0) + 1
    M = max(1, len(table))
    denom = n_v + alpha * M
    base = prior * alpha / denom
    cache = {}

    def posterior(f):
        if f not in cache:
            post = base.copy()
            for v, c in table.get(f, {}).items():
                post[v] += prior[v] * c / denom[v]
            cache[f] = post / post.sum()
        return cache[f]
    correct = 0
    eff_sets, overheads, total = [], [], 0
    for v in range(V):
        for s in gen_sessions(int(L[v]), n_test, cfg, rng):
            post = posterior(key(s))
            correct += int(np.argmax(post) == v)
            h = -(post[post > 0] * np.log2(post[post > 0])).sum()
            eff_sets.append(2 ** h)
            overheads.append(s.overhead)
            total += 1
    return dict(V=V, top1=correct / total, prior_only=float(prior.max()), uniform_chance=1 / V,
                eff_anonymity_set=float(np.mean(eff_sets)), overhead=float(np.mean(overheads)),
                n_test=total)


# --------------------------------------------------------------------------- C. seek-detection attack


def seek_features(trace, tau=2.0):
    """Gap-ratio feature.  In steady state a buffer-driven client issues the next batch exactly
    k_prev*tau seconds after the previous one, so a flushed buffer (seek) shows up as an early request."""
    t = np.array([a for a, _ in trace])
    k = np.array([b for _, b in trace])
    if len(t) < 5:
        return 1.0
    ratio = np.diff(t) / (k[:-1] * tau)
    steady = np.flatnonzero(np.abs(ratio - 1) < 0.08)
    if len(steady) == 0:
        return 1.0
    return float(ratio[steady[0] + 1:].min()) if steady[0] + 1 < len(ratio) else 1.0


def auc(pos, neg):
    """P(score_pos < score_neg) + 0.5 ties  (lower ratio = more seek-like)."""
    pos, neg = np.asarray(pos), np.asarray(neg)
    gt = (pos[:, None] < neg[None, :]).mean()
    eq = (pos[:, None] == neg[None, :]).mean()
    return float(gt + 0.5 * eq)


def seek_attack(cfg, n=300, L=200, seed=3, thr=0.75, bw=50):
    rng = np.random.default_rng(seed)
    cost = CostModel(cfg.get("scheme", "xor"), bw, 0.05)
    f_seek, f_none, lat = [], [], []
    for i in range(2 * n):
        has = i % 2 == 0
        watch = L * 2.0
        seeks = [(float(rng.uniform(0.2, 0.7) * watch), int(rng.integers(0, L)))] if has else []
        s = simulate_session(L, cost, watch_s=watch, seeks=seeks, rng=rng, **cfg.get("sim", {}))
        (f_seek if has else f_none).append(seek_features(s.trace))
        if has:
            lat.append(s.seek_latency_s)
    f_seek, f_none = np.array(f_seek), np.array(f_none)
    tpr, fpr = float((f_seek < thr).mean()), float((f_none < thr).mean())
    return dict(auc=auc(f_seek, f_none), tpr=tpr, fpr=fpr, bal_acc=(tpr + 1 - fpr) / 2,
                mean_seek_latency_s=float(np.mean(lat)))
