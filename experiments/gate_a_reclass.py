#!/usr/bin/env python3
"""Gate A reclassification: known-mislabelled seeds -> doomed under drain;
legacy-safe seeds -> drain-clear. Writes results incrementally to a file."""
import sys, csv, os
sys.path.insert(0, '.')
from run_experiments import set_csv_option, run_exe
from run_heldout_eval import set_spawn_rate

W = 'work/diag_coreach/N02/r1.00'; I = W + '/input'; O = W + '/output'
OUT = open('gate_a_results.txt', 'w', buffering=1)  # line-buffered


def log(m): OUT.write(m + '\n'); print(m, flush=True)


def add_or_set(path, opt, val):
    rows = list(csv.reader(open(path)))
    for r in rows:
        if r and r[0] == opt: r[1] = str(val); break
    else: rows.append([opt, str(val), ''])
    csv.writer(open(path, 'w', newline='')).writerows(rows)


def drain_run(seed):
    set_csv_option(I + '/runctrl.csv', 'seed', seed)
    run_exe(W)
    dl = list(csv.DictReader(open(O + '/dlexp_log.csv')))[0]
    return int(dl['deadlock_found']), int(dl['drained_clear']), float(dl['deadlock_time'])


set_csv_option(I + '/runctrl.csv', 'sim_len', 1000.0)
add_or_set(I + '/runctrl.csv', 'start_warmdown', 192.0)
set_csv_option(I + '/dlexp_options.csv', 'WARM_START_FILE', '')

MIS = {1.00: [100199], 2.00: [100070, 100322, 101315, 101577, 101623, 101857, 102422, 102964]}
log('=== KNOWN-MISLABELLED seeds under DRAIN (expect dl=1) ===')
allok = True
for rate, seeds in MIS.items():
    set_spawn_rate(I + '/open_loop_spawners.csv', rate)
    for s in seeds:
        d, c, t = drain_run(s)
        if d != 1: allok = False
        log(f'  r{rate:.2f} {s}: dl={d} clear={c} @t={t:.1f}  {"OK" if d==1 else "FAIL"}')
log(f'  ==> all reclassified doomed: {allok}')

log('=== SAFE CONTROL: 15 legacy-safe seeds/rate under DRAIN (expect dl=0, clear=1) ===')
for rate in [1.00, 2.00]:
    set_spawn_rate(I + '/open_loop_spawners.csv', rate)
    picked = 0; s = 100000; anomalies = []
    while picked < 15 and s < 100000 + 800:
        d, c, t = drain_run(s)
        if d == 0 and c == 1: picked += 1
        elif d == 0 and c == 0: anomalies.append(s)
        s += 1
    log(f'  r{rate:.2f}: {picked} drain-clear-safe confirmed; anomalies(dl=0,clear=0)={anomalies}')

add_or_set(I + '/runctrl.csv', 'start_warmdown', 0)
set_csv_option(I + '/runctrl.csv', 'sim_len', 192.0)
log('DONE')
OUT.close()
