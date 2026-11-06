"""Turn results/bench.csv into figures + markdown tables.  Run from pir_core/."""
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

HERE = os.path.dirname(__file__)
RES = os.path.join(HERE, "..", "results")
df = pd.read_csv(os.path.join(RES, "bench.csv"))
df["label"] = df.scheme + "/" + df.scan
KB, MB = 1024, 1024 * 1024
STYLE = {"xor/naive": ("tab:blue", "--"), "xor/optimized": ("tab:blue", "-"),
         "lwe/optimized": ("tab:red", "-"), "lwe/naive": ("tab:red", "--")}


def lines(ax, d, x, y, **kw):
    for lab, g in d.groupby("label"):
        c, ls = STYLE[lab]
        g = g.sort_values(x)
        ax.plot(g[x], g[y], color=c, ls=ls, marker="o", label=lab, **kw)


# Fig 1: server time vs catalog size
d = df[(df.exp == "scaling") & (df.k == 1)]
fig, ax = plt.subplots(figsize=(5.5, 3.6))
lines(ax, d, "db_mb", "t_server")
ax.set(xscale="log", yscale="log", xlabel="catalog size (MB, 16 KB chunks)", ylabel="server time, 1 chunk (s)")
ax.legend(); ax.grid(alpha=.3); fig.tight_layout(); fig.savefig(os.path.join(RES, "fig_scaling.png"), dpi=150)

# Fig 2: per-chunk cost vs batch size
d = df[df.exp == "batch"]
fig, ax = plt.subplots(figsize=(5.5, 3.6))
lines(ax, d, "k", "ms_per_record")
ax.set(xscale="log", yscale="log", xlabel="batch size k (chunks)", ylabel="compute latency per chunk (ms)")
ax.legend(); ax.grid(alpha=.3); fig.tight_layout(); fig.savefig(os.path.join(RES, "fig_batch.png"), dpi=150)

out = []
# Table: scaling
t = df[(df.exp == "scaling")].copy()
t["server_ms"] = (t.t_server * 1e3).round(1)
t["total_ms"] = (t.t_latency * 1e3).round(1)
t["up_KB"] = (t.bytes_up / KB).round(1)
t["down_KB"] = (t.bytes_down / KB).round(1)
t["hint_MB"] = (t.offline_bytes / MB).round(1)
out.append("### Catalog-size sweep (16 KB chunks)\n" +
           t[["label", "N", "db_mb", "k", "server_ms", "total_ms", "up_KB", "down_KB", "hint_MB"]]
           .to_markdown(index=False))
# Table: batch
t = df[df.exp == "batch"].copy()
t["server_ms"] = (t.t_server * 1e3).round(1)
t["ms/chunk"] = t.ms_per_record.round(2)
out.append("### Batch-size sweep (1024 x 16 KB = 16 MB catalog)\n" +
           t.pivot_table(index="k", columns="label", values="ms/chunk").round(2).to_markdown())
# Table: record size
t = df[df.exp == "recsize"].copy()
t["B_KB"] = t.B // KB
t["total_ms"] = (t.t_latency * 1e3).round(1)
t["up_KB"] = (t.bytes_up / KB).round(0)
t["down_KB"] = (t.bytes_down / KB).round(0)
out.append("### Record-size sweep (16 MB catalog)\n" +
           t[["label", "B_KB", "N", "k", "total_ms", "up_KB", "down_KB", "lwe_rows", "lwe_cols"]]
           .to_markdown(index=False))

# Model: max sustainable video bitrate on a link, NO overlap between compute and transfer (conservative)
rows = []
t = df[(df.exp == "recsize") & (df.k == 8)]
for _, r in t.iterrows():
    for bw_mbps in (10, 50, 100):
        per_chunk = r.t_latency / r.k + (r.bytes_up + r.bytes_down) * 8 / (bw_mbps * 1e6) / r.k
        rows.append(dict(label=r.label, chunk_KB=int(r.B // KB), link_Mbps=bw_mbps,
                         max_video_Mbps=round(r.B * 8 / per_chunk / 1e6, 1)))
m = pd.DataFrame(rows)
out.append("### Model: max sustainable video bitrate (k=8 batches, no compute/transfer overlap)\n" +
           m.pivot_table(index=["label", "chunk_KB"], columns="link_Mbps", values="max_video_Mbps").to_markdown())

# Naive vs optimized speedups
sp = []
for N in (256, 1024, 4096):
    for k in (1, 16):
        a = df[(df.exp == "scaling") & (df.N == N) & (df.k == k)].set_index("label").t_server
        sp.append(dict(N=N, k=k, xor_speedup=round(a["xor/naive"] / a["xor/optimized"], 2)))
out.append("### XOR optimized-vs-naive speedup\n" + pd.DataFrame(sp).to_markdown(index=False))
n = df[df.exp == "naive_lwe"].set_index("k").t_server          # N=256, 16 KB chunks
o = df[(df.exp == "scaling") & (df.N == 256) & (df.scheme == "lwe")].set_index("k").t_server
out.append("### LWE server: BLAS (optimized) vs numpy-integer matmul (naive), 256 x 16 KB\n" +
           pd.DataFrame({"naive_ms": (n.loc[[1, 16]] * 1e3).round(1), "optimized_ms": (o.loc[[1, 16]] * 1e3).round(1),
                         "speedup": (n.loc[[1, 16]] / o.loc[[1, 16]]).round(1)}).to_markdown())
open(os.path.join(RES, "tables.md"), "w").write("\n\n".join(out) + "\n")
print("\n\n".join(out))
