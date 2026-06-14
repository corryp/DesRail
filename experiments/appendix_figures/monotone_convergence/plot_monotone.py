"""
Plot within-seed monotone convergence of deadlock detection time.

For each deadlock-finding seed, plots deadlock_time vs iteration number.
Within each seed, deadlock_time should be non-decreasing (monotone convergence).

Usage:
    python plot_monotone.py               # reads logs/ in script dir, shows plot
    python plot_monotone.py --logs <dir>  # specify log directory
    python plot_monotone.py --save <path> # save PDF to given path
"""

import argparse
import csv
import os
import glob

import matplotlib
matplotlib.rcParams.update({
    'font.family': 'serif',
    'font.size': 9,
    'axes.labelsize': 10,
    'axes.titlesize': 10,
    'legend.fontsize': 8,
    'xtick.labelsize': 9,
    'ytick.labelsize': 9,
})
import matplotlib.pyplot as plt
import matplotlib.cm as cm
import numpy as np


def load_seed_log(path):
    return [{'iteration':        int(r['iteration']),
             'deadlock_found':   int(r['deadlock_found']),
             'deadlock_time':    float(r['deadlock_time']),
             'new_constraints':  int(r['new_constraints']),
             'total_constraints': int(r['total_constraints']),
             'trains_completed': int(r['trains_completed'])}
            for r in csv.DictReader(open(path))]


def check_regressions(dl_times, threshold=2.0):
    """Return list of (iter_index, prev, curr) for regressions > threshold."""
    return [(i, dl_times[i-1], dl_times[i])
            for i in range(1, len(dl_times))
            if dl_times[i] < dl_times[i-1] - 1e-6
            and (dl_times[i-1] - dl_times[i]) > threshold]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--logs', default=None)
    parser.add_argument('--save', default=None,
                        help='Output PDF path (default: show interactively)')
    parser.add_argument('--horizon', type=float, default=192.0)
    parser.add_argument('--ymax', type=float, default=60.0,
                        help='Y-axis upper limit (default: 60)')
    args = parser.parse_args()

    logs_dir = args.logs or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'logs')

    files = sorted(glob.glob(os.path.join(logs_dir, 'dlexp_seed_*.csv')))
    if not files:
        print(f'No dlexp_seed_*.csv files found in {logs_dir}')
        return

    dl_seeds, clean_count = [], 0
    for fpath in files:
        seed_num = int(os.path.basename(fpath)
                       .replace('dlexp_seed_', '').replace('.csv', ''))
        rows = load_seed_log(fpath)
        if any(r['deadlock_found'] for r in rows):
            dl_seeds.append((seed_num, rows))
        else:
            clean_count += 1

    print(f'{len(dl_seeds)} deadlock-finding seeds, {clean_count} clean seeds')

    # Console regression report
    print('\nWithin-seed monotone check (regressions > 1h flagged as anomalous):')
    for seed_num, rows in dl_seeds:
        dl_times = [r['deadlock_time'] for r in rows if r['deadlock_found']]
        bad = check_regressions(dl_times, threshold=1.0)
        status = 'OK' if not bad else f'ANOMALY x{len(bad)}'
        tc = rows[-1]['total_constraints']
        print(f'  seed {seed_num}: {len(dl_times):4d} DL iters, '
              f't=[{min(dl_times):.0f}..{max(dl_times):.0f}]h, '
              f'total_c={tc}  [{status}]')

    # === Figure ===
    fig, ax = plt.subplots(figsize=(6.5, 4.0))

    colours = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728']  # tab10 first 4
    small_regression_markers = []   # (x, y) of 1-unit regressions to annotate

    for idx, (seed_num, rows) in enumerate(dl_seeds):
        dl_iters = [r['iteration'] for r in rows if r['deadlock_found']]
        dl_times = [r['deadlock_time'] for r in rows if r['deadlock_found']]
        total_c  = rows[-1]['total_constraints']
        col      = colours[idx % len(colours)]

        ax.plot(dl_iters, dl_times, color=col, linewidth=0.9,
                alpha=0.85, label=f'Seed {seed_num} ({total_c:,} constraints)')

        # Mark small regressions (≤2h) separately
        for i in range(1, len(dl_times)):
            if dl_times[i] < dl_times[i-1] - 1e-6:
                small_regression_markers.append((dl_iters[i], dl_times[i], col))

    # Annotate regression points
    for x, y, col in small_regression_markers:
        ax.plot(x, y, marker='v', markersize=5, color=col,
                markerfacecolor='white', markeredgewidth=1.2, zorder=5)

    # Dummy entry for legend
    if small_regression_markers:
        ax.plot([], [], marker='v', markersize=5, color='grey',
                markerfacecolor='white', markeredgewidth=1.2, linestyle='none',
                label='Boundary regression ($\\leq$1 h)')

    ax.axhline(args.horizon, color='grey', linestyle=':', linewidth=0.8,
               label=f'Horizon ({args.horizon:.0f} h)')

    ax.set_xlabel('Iteration (within seed)')
    ax.set_ylabel('Deadlock detection time (h)')
    ax.set_xlim(left=0)
    ax.set_ylim(bottom=0, top=args.ymax)
    ax.legend(loc='upper right', framealpha=0.9)
    ax.grid(True, alpha=0.25, linewidth=0.5)
    ax.spines[['top', 'right']].set_visible(False)

    plt.tight_layout(pad=0.5)

    if args.save:
        plt.savefig(args.save, bbox_inches='tight')
        print(f'\nSaved to {args.save}')
    else:
        plt.show()


if __name__ == '__main__':
    main()
