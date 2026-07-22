#!/usr/bin/env python3
"""
Render the Toowoomba junction before/after train charts as a 2x2 figure:
    rows    = corridors (Brisbane line, Western line)
    columns = before (no junction constraints) | after (5 learned constraints)
Both columns share the t in [0, T_MAX] window so the deadlock (which gridlocks the
whole network at t~8.3) sits directly alongside the free-flowing case.

Line style/colours adapted from DesRail/output/train_chart_plot.ipynb.
"""
import argparse
import os
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CORRIDOR_NM = {0: "Brisbane line", 1: "Western line"}
MODE_TITLE = {"before": "Without junction constraints",
              "after": "With 5 learned constraints"}


def load(mode):
    df = pd.read_csv(os.path.join(SCRIPT_DIR, "out", mode, "train_chart.csv"))
    for c in ("time", "corridor_id", "train", "chainage"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["time", "corridor_id", "train", "chainage"])


def first_sig(df):
    """Per train-id -> signature of its first chart sample (spawn time, corridor,
    entry arc). The same physical train has the same signature across the before/after
    runs, so colouring by signature (not raw id) means a colour ALWAYS denotes the same
    train -- even where deadlock-induced spawner queueing reorders ids late in 'before'.
    """
    sig = {}
    for tid, g in df.sort_values("time").groupby("train"):
        r = g.iloc[0]
        sig[int(tid)] = (round(float(r["time"]), 3), int(r["corridor_id"]), str(r["arc"]))
    return sig


def color_map(signatures, seed=123):
    sigs = sorted(signatures)
    rng = np.random.default_rng(seed)
    base = list(plt.get_cmap("tab20").colors)
    pool = (base * (len(sigs) // len(base) + 1))[:len(sigs)]
    rng.shuffle(pool)
    return dict(zip(sigs, pool))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tmax", type=float, default=10.0)
    p.add_argument("--out", default=os.path.join(SCRIPT_DIR, "toowoomba_before_after.png"))
    args = p.parse_args()

    data = {m: load(m) for m in ("before", "after")}
    corridors = sorted(set(data["before"]["corridor_id"]) | set(data["after"]["corridor_id"]))
    corridors = [int(c) for c in corridors]
    # Colour by first-appearance signature so the same physical train shares a colour
    # across both panels; per-mode train-id -> colour lookup.
    sig = {m: first_sig(data[m]) for m in ("before", "after")}
    all_sigs = set().union(*(set(sig[m].values()) for m in sig))
    sig2c = color_map(all_sigs)
    id2c = {m: {tid: sig2c[s] for tid, s in sig[m].items()} for m in sig}

    nrows, ncols = len(corridors), 2
    fig, axes = plt.subplots(nrows, ncols, figsize=(11, 2.9 * nrows),
                             dpi=300, sharex=True, squeeze=False)

    for j, mode in enumerate(("before", "after")):
        df = data[mode]
        df = df[df["time"] <= args.tmax]
        for i, cid in enumerate(corridors):
            ax = axes[i][j]
            d = df[df["corridor_id"] == cid]
            for tid, g in d.groupby("train"):
                g = g.sort_values("time")
                ax.plot(g["time"], g["chainage"], color=id2c[mode].get(int(tid), "black"),
                        linewidth=1.3, alpha=0.9)
            ax.grid(True, linestyle=":", alpha=0.6)
            ax.set_xlim(0, args.tmax)
            if i == 0:
                ax.set_title(MODE_TITLE[mode], fontsize=12, fontweight="bold")
            if j == 0:
                ax.set_ylabel(f"{CORRIDOR_NM.get(cid, cid)}\nChainage (km)")
            if i == nrows - 1:
                ax.set_xlabel("Simulation clock (hr)")

    # mark the gridlock instant on the 'before' column
    end_before = data["before"]["time"].max()
    if end_before <= args.tmax:
        for i in range(nrows):
            axes[i][0].axvline(end_before, color="red", linestyle="--",
                               linewidth=1.5, alpha=0.7)
        axes[0][0].annotate(f"total\ngridlock\nt={end_before:.1f} h",
                            xy=(end_before, axes[0][0].get_ylim()[1]),
                            xytext=(4, -4), textcoords="offset points",
                            ha="left", va="top", fontsize=8, color="red")

    fig.tight_layout()
    fig.savefig(args.out, bbox_inches="tight")
    print(f"wrote {args.out}")
    print(f"  before gridlocks at t={end_before:.3f}; after runs to t={data['after']['time'].max():.1f}")


if __name__ == "__main__":
    main()
