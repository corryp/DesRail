#!/usr/bin/env python3
"""Per-train delay distributions, learned against engineered, N=10.

Table 4 reports means, a paired difference and two maxima per rate. It cannot
show that the two supervisors differ mostly in the tail, which is the substance
of reviewer comment 2.14 (engineered looks better on the means) and of 1.5's
request for observed maxima. The unit here is the train, not the replication.

Two facts about the data decide the form, and both are stated on the figure
rather than hidden by it:

  * the distribution is zero-inflated, from 39% of trains at lambda=1.00 to
    0.4% at 3.00, and a kernel density cannot represent an atom at zero, so
    those trains are excluded from the density and their share is printed.
    Folded in with them is every wait below six seconds, at most 1.3% of a
    cell and operationally no wait at all: the shortest recorded is 7 ms, and
    leaving those in stretched the density three decades below anything the
    axis could show, drawing a long needle under every violin;
  * the spread runs from under a minute to over a thousand, so the density is
    estimated on log10(wait) and the axis relabelled, which is also why no
    train with zero wait can appear on it.

Reads the merged cells of extract_train_delays.py, which cover all 10,000
held-out replications per cell. Three numbers per violin come from exact counts
over every train rather than from the plotted sample: the annotated maximum, the
share below six seconds, and the median. The density is a kernel estimate on the
cell's reservoir sample of 40,000 trains, which smooths one real feature worth
knowing about separately: under the engineered set at lambda=3.00 there is a
point mass of 616,073 trains, 6.1% of all of them, at 65.3 minutes, and 99.88%
of trains sit at or below it. Drawn from the exact histogram that atom is a
single-bin needle 0.01 decades wide that scales every other violin to nothing,
so it is reported in the text instead.

    <venv-python> figures/plot_train_delay.py --data <dir> --out fig.png
"""
import os, json, gzip, math, argparse, random
import matplotlib
from scipy.stats import gaussian_kde
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

RATES = ["1.00", "1.50", "2.00", "2.50", "3.00"]
ARMS = [("native", "Learned", "#0072B2"), ("engineered", "Engineered", "#E69F00")]
OFFSET = 0.19
WIDTH = 0.34
# Six seconds, below which a wait is not a delay. See the module docstring.
NEGLIGIBLE_MIN = 0.1


def load(root, mode, rate, metric="wait"):
    """The reservoir sample in minutes, with the exact statistics beside it:
    (sample, n_trains, n_negligible, true_max, exact_median). The sample is for
    the density and the points; every number printed on the figure comes from
    the exact counts, because a 40,000-train sample does not hold the maximum
    (at lambda=3.00 it reaches 847 minutes against a true 1,052)."""
    p = os.path.join(root, "discard", mode, "N10", f"r{rate}", "cell.json.gz")
    with gzip.open(p, "rt") as f:
        d = json.load(f)
    ix = {c: i for i, c in enumerate(d["per_seed_cols"])}
    col = "max_wait" if metric == "wait" else "max_spawn"
    true_max = max(x[ix[col]] for x in d["per_seed"])
    # The exact median off the log10 histogram, to the bin, counting the
    # negligible trains in their place at the bottom.
    lo, w = d["bins"]["lo"], d["bins"]["w"]
    acc, target, med = d["below"][metric], 0.5 * d["seen"][metric], None
    for i, c in enumerate(d["hist"][metric]):
        acc += c
        if acc >= target:
            med = lo + (i + 0.5) * w
            break
    return {"sample": d["reservoir"][metric], "seen": d["seen"][metric],
            "below": d["below"][metric], "max": true_max, "med_log10": med,
            "hist": d["hist"][metric], "lo": lo, "w": w}


def linear_density(c, ymax, nbins):
    """Counts per linear bin over [0, ymax], from the exact log10 histogram, so
    the density is over every train rather than over the 40,000 sampled.

    Each log bin's count is spread over the linear bins its interval covers, in
    proportion to the overlap, rather than dropped at its centre. Centre-dropping
    leaves a comb above about 40 minutes, where a log bin is wider than a linear
    one and consecutive log bins land in the same linear bin or skip one.

    Trains below six seconds carry no log bin of their own and go into the first
    linear bin, which is where they belong once the axis starts at zero."""
    lw = ymax / nbins
    dens = [0.0] * nbins
    dens[0] += c["below"]
    over = 0
    for i, n in enumerate(c["hist"]):
        if not n:
            continue
        a = 10.0 ** (c["lo"] + i * c["w"])
        b = 10.0 ** (c["lo"] + (i + 1) * c["w"])
        if a >= ymax:
            over += n
            continue
        if b > ymax:                      # straddles the truncation
            over += n * (b - ymax) / (b - a)
            n *= (ymax - a) / (b - a)
            b = ymax
        j0, j1 = int(a / lw), min(nbins - 1, int(b / lw))
        if j0 == j1:
            dens[j0] += n
            continue
        for j in range(j0, j1 + 1):
            olap = min(b, (j + 1) * lw) - max(a, j * lw)
            if olap > 0:
                dens[j] += n * olap / (b - a)
    return dens, over


def draw_linear(ax, c, pos, colour, a, rnd):
    """One violin on a linear axis truncated at a.ymax, with the share of trains
    above the truncation printed over it. The density is exact (every train);
    the points are the reservoir sample, which is all they are for."""
    dens, over = linear_density(c, a.ymax, a.nbins)
    lw = a.ymax / a.nbins
    grid = [(j + 0.5) * lw for j in range(a.nbins)]
    scale = (WIDTH / 2.0) / max(dens)
    # No edge line here, unlike the log variant. Most of the axis holds a
    # density too small to draw, and an outline turns that into a full-height
    # hairline on every violin, which reads as a spread the data does not have.
    ax.fill_betweenx(grid, [pos - d * scale for d in dens],
                     [pos + d * scale for d in dens],
                     facecolor=colour, alpha=0.6, linewidth=0, zorder=3)
    if a.points:
        samp = [x for x in rnd.sample(c["sample"], min(a.points, len(c["sample"])))
                if x < a.ymax]
        ax.scatter([pos + rnd.uniform(-0.06, 0.06) for _ in samp], samp,
                   s=0.6, color=colour, alpha=0.18, linewidths=0, zorder=2)
    med = 10.0 ** c["med_log10"]
    if med < a.ymax:
        ax.plot([pos - WIDTH / 2.6, pos + WIDTH / 2.6], [med, med],
                color="#1a1a1a", lw=1.2, zorder=4, solid_capstyle="butt")
    pct = 100.0 * over / c["seen"]
    lbl = ("0" if over == 0 else "<0.1%" if pct < 0.1 else
           f"{pct:.1f}%" if pct < 1.0 else f"{pct:.0f}%")
    ax.text(pos, a.ymax * 1.015, lbl, ha="center", va="bottom", fontsize=6.4,
            color=colour)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--metric", default="wait", choices=["wait", "spawn"])
    ap.add_argument("--points", type=int, default=1500,
                    help="trains drawn as points per violin; 0 for none")
    ap.add_argument("--scale", default="log", choices=["log", "linear"],
                    help="linear truncates the axis at --ymax and prints the "
                         "share of trains above it")
    ap.add_argument("--ymax", type=float, default=100.0,
                    help="minutes; the truncation point for --scale linear")
    ap.add_argument("--nbins", type=int, default=200,
                    help="linear density bins over [0, ymax]")
    a = ap.parse_args()
    random.seed(7)

    plt.rcParams.update({"font.family": "serif", "font.size": 9,
                         "axes.linewidth": 0.6})
    fig, ax = plt.subplots(figsize=(6.5, 3.4))
    legend_handles = []
    for ai, (mode, label, colour) in enumerate(ARMS):
        sign = -1 if ai == 0 else 1
        for xi, rate in enumerate(RATES):
            c = load(a.data, mode, rate, a.metric)
            v, n_seen, n_below = c["sample"], c["seen"], c["below"]
            true_max, med = c["max"], c["med_log10"]
            pos = xi + sign * OFFSET
            zero_pct = 100.0 * n_below / n_seen
            if a.scale == "linear":
                draw_linear(ax, c, pos, colour, a, random)
                continue
            nz = [x for x in v if x >= NEGLIGIBLE_MIN]
            lg = [math.log10(x) for x in nz]
            # The density is drawn only over the range the data actually covers.
            # matplotlib's violinplot evaluates its kernel well past both
            # extremes, which drew a tail below the shortest wait ever recorded
            # and invited the reader to believe in values that never occurred.
            kde = gaussian_kde(lg)
            lo, hi = min(lg), max(lg)
            grid = [lo + (hi - lo) * i / 256.0 for i in range(257)]
            dens = kde(grid)
            scale = (WIDTH / 2.0) / max(dens)
            ax.fill_betweenx(grid, [pos - d * scale for d in dens],
                             [pos + d * scale for d in dens],
                             facecolor=colour, alpha=0.55, edgecolor=colour,
                             linewidth=0.8, zorder=3)
            if a.points:
                samp = random.sample(lg, min(a.points, len(lg)))
                ax.scatter([pos + random.uniform(-0.06, 0.06) for _ in samp], samp,
                           s=0.6, color=colour, alpha=0.18, linewidths=0, zorder=2)
            lg.sort()
            ax.plot([pos - WIDTH / 2.6, pos + WIDTH / 2.6], [med, med],
                    color="#1a1a1a", lw=1.2, zorder=4, solid_capstyle="butt")
            ax.text(pos, math.log10(true_max) + 0.07, f"{true_max:,.0f}",
                    ha="center", va="bottom", fontsize=6.2, color=colour)
            # A share under 1% still gets a decimal: the learned cell at
            # lambda=3.00 is 0.46%, and "0%" would read as none at all.
            lbl = f"{zero_pct:.0f}%" if zero_pct >= 1.0 else f"{zero_pct:.1f}%"
            ax.text(pos, -0.92, lbl, ha="center", va="bottom",
                    fontsize=6.2, color="#6b6b6b")
        legend_handles.append(Line2D([0], [0], marker="s", linestyle="",
                                     markerfacecolor=colour, markeredgecolor=colour,
                                     markersize=6, alpha=0.75, label=label))

    if a.scale == "linear":
        ax.set_ylim(0, a.ymax * 1.12)
        ax.set_yticks([t for t in ax.get_yticks() if 0 <= t <= a.ymax])
        ax.set_xlim(-0.78, len(RATES) - 0.28)
        ax.text(-0.74, a.ymax * 1.015, f"over {a.ymax:.0f}:", ha="left",
                va="bottom", fontsize=6.4, color="#6b6b6b")
    else:
        ax.set_yticks([-1, 0, 1, 2, 3])
        ax.set_yticklabels(["0.1", "1", "10", "100", "1,000"])
        ax.set_ylim(-1.05, 3.35)
    ax.set_xticks(range(len(RATES)))
    ax.set_xticklabels(RATES)
    ax.set_xlabel(r"spawn rate $\lambda$ (trains/hr)")
    word = "wait" if a.metric == "wait" else "spawn delay"
    tag = "log scale" if a.scale == "log" else f"truncated at {a.ymax:.0f}"
    ax.set_ylabel(f"per-train {word} (minutes, {tag})")
    if a.scale == "log":
        ax.text(-0.52, -0.92, "under 6\u2009s:", ha="right", va="bottom",
                fontsize=6.2, color="#6b6b6b")
    ax.grid(axis="y", color="#d8d8d8", lw=0.5, zorder=0)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    # On the linear axis the top row carries the over-truncation shares, so the
    # legend drops below it rather than sitting on top of them.
    ax.legend(handles=legend_handles, loc="upper left", frameon=False,
              fontsize=8, handletextpad=0.5,
              bbox_to_anchor=(0.0, 0.94) if a.scale == "linear" else None)
    fig.tight_layout()
    fig.savefig(a.out, dpi=220)
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
