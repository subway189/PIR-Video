#!/usr/bin/env python3
"""Charts + summary tables from results/playback.csv  ->  results/playback_*.png, playback_summary.md"""
import csv
import statistics
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

R = "results"
COL = {"baseline (non-private)": "#2a78d6", "two-server XOR": "#eb6834", "single-server lattice": "#1baf7a"}  # palette slots 1-3
SHORT = {"baseline (non-private)": "baseline", "two-server XOR": "XOR PIR", "single-server lattice": "lattice PIR"}
INK, MUTED, GRID = "#0b0b0b", "#52514e", "#e4e3df"

rows = [r for r in csv.DictReader(open(f"{R}/playback.csv")) if r["scenario"] == "play"]
rows += list(csv.DictReader(open(f"{R}/seek.csv")))
num = ("batch", "rate_kbps", "startup_latency_s", "first_visit_startup_s", "rebuffer_count", "rebuffer_total_s",
       "rebuffer_ratio", "rebuffers_per_min", "seek_miss_latency_s_mean", "seek_hit_latency_s_max", "wasted_fetches",
       "n_seeks", "seek_hits", "seek1_latency_s", "seek2_latency_s", "seek3_latency_s", "seek2_hit", "chunks_played", "rounds", "down_kib_per_chunk", "up_kib_per_chunk", "server_ms_per_chunk")
for r in rows:
    for k in num:
        r[k] = float(r[k]) if r.get(k) not in (None, "") else float("nan")


def agg(scenario, key, val, fn=statistics.fmean):
    g = defaultdict(list)
    for r in rows:
        if r["scenario"] == scenario:
            g[key(r)].append(r[val])
    return {k: fn(v) for k, v in g.items()}


def style(ax, title, ylabel, xlabel):
    ax.set_title(title, fontsize=10, color=INK, loc="left")
    ax.set_ylabel(ylabel, fontsize=8, color=MUTED)
    ax.set_xlabel(xlabel, fontsize=8, color=MUTED)
    ax.grid(axis="y", color=GRID, lw=.7)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.tick_params(labelsize=8, colors=MUTED)


links = sorted({r["rate_kbps"] for r in rows if r["scenario"] == "play"}, reverse=True)
lab = {l: f"{int(l / 1000)} Mbps" for l in links}
schemes = list(COL)

# ---- Fig 1: headline, B=1 across links
fig, axs = plt.subplots(1, 3, figsize=(12, 3.8))
for ax, (val, title, yl) in zip(axs, (("startup_latency_s", "Startup latency", "seconds"),
                                       ("rebuffer_ratio", "Rebuffer ratio (stall / total)", "fraction"),
                                       ("rebuffers_per_min", "Rebuffer rate", "stalls per minute"))):
    d = agg("play", lambda r: (r["scheme"], r["batch"], r["rate_kbps"]), val)
    for s in schemes:
        ys = [d.get((s, 1.0, l), float("nan")) for l in links]
        ax.plot(range(len(links)), ys, "-o", ms=5, lw=2, color=COL[s], label=SHORT[s], mfc=COL[s], mec="#fcfcfb", mew=1.5)
    ax.set_xticks(range(len(links)), [lab[l] for l in links])
    style(ax, title, yl, "link rate (40 ms latency), batch = 1")
axs[0].legend(fontsize=8, frameon=False)
fig.tight_layout()
fig.savefig(f"{R}/playback_by_link.png", dpi=160)

# ---- Fig 2: batch size effect at each link, startup + rebuffer ratio (PIR schemes only)
fig, axs = plt.subplots(2, len(links), figsize=(12, 5.4), sharex=True)
batches = sorted({r["batch"] for r in rows})
for j, l in enumerate(links):
    for i, (val, yl) in enumerate((("startup_latency_s", "startup (s)"), ("rebuffer_ratio", "rebuffer ratio"))):
        d = agg("play", lambda r: (r["scheme"], r["batch"], r["rate_kbps"]), val)
        ax = axs[i][j]
        for s in schemes[1:]:
            ax.plot(range(len(batches)), [d.get((s, b, l), float("nan")) for b in batches], "-o", ms=5, lw=2, color=COL[s],
                    label=SHORT[s], mec="#fcfcfb", mew=1.5)
        base = d.get((schemes[0], 1.0, l))
        ax.axhline(base, color=COL[schemes[0]], lw=1.5, ls="--", label="baseline" if (i, j) == (0, 0) else None)
        ax.set_xticks(range(len(batches)), [int(b) for b in batches])
        ax.set_ylim(bottom=0 if i == 1 else None)
        if i == 1 and ax.get_ylim()[1] < 0.05:
            ax.set_ylim(0, 0.05)
        style(ax, f"{lab[l]}" if i == 0 else "", yl, "chunks per PIR round" if i == 1 else "")
axs[0][0].legend(fontsize=8, frameon=False)
fig.tight_layout()
fig.savefig(f"{R}/playback_by_batch.png", dpi=160)

# ---- Fig 3: seek latency (cold seeks) per scheme x batch at each seek link
sl = sorted({r["rate_kbps"] for r in rows if r["scenario"] == "seek"}, reverse=True)
fig, axs = plt.subplots(1, len(sl), figsize=(5.6 * len(sl), 3.8), sharey=True)
axs = [axs] if len(sl) == 1 else axs
for r in rows:
    if r["scenario"] == "seek":
        r["cold_latency"] = (r["seek1_latency_s"] + r["seek3_latency_s"]) / 2
num_extra = None
d = agg("seek", lambda r: (r["scheme"], r["batch"], r["rate_kbps"]), "cold_latency")
for ax, l in zip(axs, sl):
    xs, w = range(len(batches)), 0.26
    for k, s in enumerate(schemes):
        pts = [(i, d.get((s, b, l))) for i, b in enumerate(batches) if (s, b, l) in d]
        ax.bar([i + (k - 1) * w for i, _ in pts], [v for _, v in pts], w * 0.92, color=COL[s], label=SHORT[s])
    ax.set_xticks(list(xs), [f"B={int(b)}" for b in batches])
    style(ax, f"Cold-seek latency at {lab[l]}", "seconds to resume", "")
axs[0].legend(fontsize=8, frameon=False)
fig.tight_layout()
fig.savefig(f"{R}/playback_seek.png", dpi=160)

# ---- summary tables (markdown) for the report
out = ["### Playback (no seeks): mean over reps\n", "| Link | Scheme | B | Startup s | First-visit s | Rebuffers | Stall s | Ratio | /min |", "|---|---|---|---|---|---|---|---|---|"]
for l in links:
    for s in schemes:
        for b in (batches if s != schemes[0] else [1.0]):
            sel = [r for r in rows if r["scenario"] == "play" and r["scheme"] == s and r["batch"] == b and r["rate_kbps"] == l]
            if not sel:
                continue
            f = lambda k: statistics.fmean(r[k] for r in sel)
            out.append(f"| {lab[l]} | {SHORT[s]} | {int(b)} | {f('startup_latency_s'):.2f} | {f('first_visit_startup_s'):.1f} | "
                       f"{f('rebuffer_count'):.1f} | {f('rebuffer_total_s'):.2f} | {f('rebuffer_ratio'):.3f} | {f('rebuffers_per_min'):.1f} |")
out += ["", "### Seeks (per session: play 0-3 -> seek to 16 (cold) -> play 4 -> seek +2 -> play 2 -> seek back to 5 (cold)); mean over reps\n",
        "| Link | Scheme | B | Cold seek 0->16 s | Cold seek back ->5 s | Near-forward seek (+2): buffer hit | its latency s | Wasted chunks | Rebuffers |", "|---|---|---|---|---|---|---|---|---|"]
for l in sl:
    for s in schemes:
        for b in (batches if s != schemes[0] else [1.0]):
            sel = [r for r in rows if r["scenario"] == "seek" and r["scheme"] == s and r["batch"] == b and r["rate_kbps"] == l]
            if not sel:
                continue
            f = lambda k: statistics.fmean(r[k] for r in sel)
            out.append(f"| {lab[l]} | {SHORT[s]} | {int(b)} | {f('seek1_latency_s'):.2f} | {f('seek3_latency_s'):.2f} | "
                       f"{f('seek2_hit') * 100:.0f}% | {f('seek2_latency_s'):.2f} | {f('wasted_fetches'):.1f} | {f('rebuffer_count'):.1f} |")
open(f"{R}/playback_summary.md", "w").write("\n".join(out) + "\n")
print("\n".join(out))
