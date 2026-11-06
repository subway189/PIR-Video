#!/usr/bin/env python3
"""Charts from results/*.csv  ->  results/*.png"""
import csv, os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

def load(p):
    with open(p) as f:
        return [{k: float(v) if v.replace('.','',1).replace('-','',1).isdigit() else v for k, v in r.items()} for r in csv.DictReader(f)]

R = "results"
x256, x64, lat = load(f"{R}/two_server_R256KiB.csv"), load(f"{R}/two_server_R64KiB.csv"), load(f"{R}/lattice_R64KiB.csv")
C = {"x1": "#1f77b4", "x8": "#6baed6", "l1": "#d62728", "l4": "#fc9272"}

# 1. server time vs catalog size
fig, ax = plt.subplots(1, 2, figsize=(11, 4.2))
for rows, lab, st in ((x256, "XOR R=256 KiB", "-"), (x64, "XOR R=64 KiB", "--")):
    for b, c in ((1, C["x1"]), (8, C["x8"])):
        pts = [(r["db_mib"], r["server_ms"]) for r in rows if r["batch"] == b]
        ax[0].plot(*zip(*pts), st, marker="o", ms=3, color=c, label=f"{lab}, B={b}")
for b, c in ((1, C["l1"]), (4, C["l4"])):
    pts = [(r["db_mib"], r["server_ms"]) for r in lat if r["batch"] == b]
    ax[0].plot(*zip(*pts), "-", marker="s", ms=3, color=c, label=f"lattice R=64 KiB, B={b}")
ax[0].set(xscale="log", yscale="log", xlabel="catalog size (MiB)", ylabel="server time per batch (ms)", title="Server compute vs catalog size")
ax[0].legend(fontsize=7); ax[0].grid(alpha=.3, which="both")
for rows, lab, c in ((x64, "XOR (2 servers)", C["x1"]),):
    pts = [(r["db_mib"], r["server_ms_per_record"]) for r in rows if r["batch"] == 1]
    ax[1].plot(*zip(*pts), marker="o", ms=3, color=c, label=lab)
pts = [(r["db_mib"], r["server_ms_per_record"]) for r in lat if r["batch"] == 1]
ax[1].plot(*zip(*pts), marker="s", ms=3, color=C["l1"], label="lattice (1 server)")
ax[1].axhline(1000, color="gray", ls=":"); ax[1].text(1.2, 1100, "1 s chunk = real-time budget (1 core)", fontsize=7, color="gray")
ax[1].set(xscale="log", yscale="log", xlabel="catalog size (MiB)", ylabel="ms per 64 KiB record, B=1", title="Per-record cost, R = 64 KiB")
ax[1].legend(fontsize=8); ax[1].grid(alpha=.3, which="both")
fig.tight_layout(); fig.savefig(f"{R}/server_time_vs_catalog.png", dpi=150)

# 2. real-time budget: max catalog per core at a 1 s chunk, linear extrapolation from largest point
def cap(rows, b=1):
    r = [r for r in rows if r["batch"] == b][-1]
    return r["db_mib"] / r["server_ms_per_record"] * 1000 / 1024  # GiB servable in 1000 ms
print("catalog servable in 1 s per core (GiB, one chunk/session, B=1):")
print("  XOR R=256KiB", round(cap(x256), 1), "| XOR R=64KiB", round(cap(x64), 1), "| lattice R=64KiB", round(cap(lat), 2))
