#!/usr/bin/env python3
"""How many trains are on the corridor, as a share of time, learned against
engineered at N=10, lambda=3.00.

Comment 6.4 asks for the consequences of over-conservatism to be quantified.
The mean occupancy answers it with one number per supervisor, and Table 8's
delays answer it per train, but neither shows how much of the time the corridor
is carrying far more than its average, which is the question an operator asks.

The unit here is time, not the train and not the replication: for each integer k
the bar is the fraction of the stats window spent with exactly k trains on the
network, pooled over every replication that drained. It comes from the same
exact sweep that produces the mean (extract_train_delays.occupancy), so the
distribution's own mean reproduces the reported mean to four decimals rather
than approximating it, and the hourly profile's smoothing is avoided entirely.

    <venv-python> figures/plot_occupancy.py --data <dir> --out fig.pdf
"""
import os, json, gzip, argparse
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

ARMS = [("native", "Learned", "#0072B2"), ("engineered", "Engineered", "#E69F00")]
RATE = "3.00"


def load(root, mode, rate):
    p = os.path.join(root, "discard", mode, "N10", f"r{rate}", "cell.json.gz")
    with gzip.open(p, "rt") as f:
        d = json.load(f)
    occ = d["occ_time"]
    total = sum(occ)
    ix = {c: i for i, c in enumerate(d["per_seed_cols"])}
    peaks = [r[ix["peak_on_network"]] for r in d["per_seed"]]
    return {"share": [v / total for v in occ],
            "mean": sum(k * v for k, v in enumerate(occ)) / total,
            "peak_max": max(peaks), "peak_mean": sum(peaks) / len(peaks),
            "reps": len(peaks)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--rate", default=RATE)
    a = ap.parse_args()

    cells = {m: load(a.data, m, a.rate) for m, _, _ in ARMS}
    hi = max(k for c in cells.values() for k, v in enumerate(c["share"]) if v > 0)

    plt.rcParams.update({"font.family": "serif", "font.size": 9,
                         "axes.linewidth": 0.6})
    fig, ax = plt.subplots(figsize=(6.5, 3.2))
    handles = []
    for mode, label, colour in ARMS:
        c = cells[mode]
        y = [100.0 * v for v in c["share"][:hi + 2]]
        x = list(range(len(y)))
        # Step outline with a light fill: two overlapping histograms read better
        # as outlines than as interleaved bars at 27 levels.
        ax.step(x, y, where="mid", color=colour, lw=1.4, zorder=3)
        ax.fill_between(x, y, step="mid", color=colour, alpha=0.22, zorder=2)
        ax.axvline(c["mean"], color=colour, lw=0.9, ls=(0, (4, 2)), zorder=4)
        handles.append(Line2D([0], [0], marker="s", linestyle="",
                              markerfacecolor=colour, markeredgecolor=colour,
                              markersize=6, alpha=0.8,
                              label=f"{label} (mean {c['mean']:.1f})"))
    top = max(max(100.0 * v for v in c["share"]) for c in cells.values())
    for mode, label, colour in ARMS:
        c = cells[mode]
        # Beside the line, not above it: centred over the line the label sat on
        # the line itself and read as part of it.
        ax.text(c["mean"] + 0.2, top * 1.04, f"{c['mean']:.1f}", ha="left",
                va="bottom", fontsize=6.6, color=colour)
    ax.set_ylim(0, top * 1.14)
    ax.set_xlim(-0.6, hi + 1.0)
    ax.set_xticks(range(0, hi + 2, 2))
    ax.set_xlabel("trains on the network")
    ax.set_ylabel("share of time (\\%)" if plt.rcParams["text.usetex"]
                  else "share of time (%)")
    ax.grid(axis="y", color="#d8d8d8", lw=0.5, zorder=0)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    # Upper left: both distributions are near zero below about six trains, while
    # the upper right holds the learned arm's saturation level.
    ax.legend(handles=handles, loc="upper left", frameon=False, fontsize=8,
              handletextpad=0.5)
    fig.tight_layout()
    fig.savefig(a.out, dpi=220)
    print(f"wrote {a.out}")
    for mode, label, _ in ARMS:
        c = cells[mode]
        print(f"  {label:11s} reps={c['reps']} mean={c['mean']:.3f} "
              f"peak mean={c['peak_mean']:.2f} max={c['peak_max']}")


if __name__ == "__main__":
    main()
