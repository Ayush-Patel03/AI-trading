"""ledger.py — the experiment ledger: every backtest trial, counted, append-only.

WHY A COUNTER
-------------
Every re-run on the same data spends some of the evidence. Bailey & Lopez de Prado (2014,
"The deflated Sharpe ratio") show that with about two years of data, roughly seven
independently tried configurations are enough for the BEST of them to show an in-sample
Sharpe of 1 from pure noise. Harvey, Liu & Zhu (2016, "...and the cross-section of expected
returns") put the bar for a new factor at t >= 3, and on a hold-out, because of how many
have already been tried. Neither adjustment can be made if nobody knows how many trials
there were. That number is the only thing this file exists to keep honest, and it is why
the file is append-only: a trial that was run and then deleted still spent the evidence.

ROW SCHEMA (one JSON object per line)
-------------------------------------
    id                "E10", or auto "X-<yyyymmdd>-<n>"
    hypothesis        one sentence: what this trial claims
    config_diff       dict or string: what differs from the incumbent
    harness_cmd       the argv that produced it
    window            {"start": ..., "end": ...}
    universe          {"name": ..., "n_symbols": int or null}
    n                 observations
    horizons          [5, 10, 20]
    in_sample         dict of metrics (ic.in_sample_metrics)
    out_of_sample     dict or null — null until a hold-out has been measured
    n_trials_to_date  the multiple-testing counter: prior trial rows + 1
    decision          null until a human sets one (--set-decision)
    date              ISO date
    engine_sha        config.engine_sha(), may be null

A decision is itself a row — {"kind": "decision", "ref": <id>, "decision": ..., "date": ...}
— appended, never written back into the trial row. Decision rows do not count as trials.
`trials()` returns the trial rows with the latest decision folded in for reading.

Usage
-----
    python3 ledger.py --path experiments/ledger.jsonl --list
    python3 ledger.py --path experiments/ledger.jsonl --set-decision E10 "promoted to hold-out"
"""
import argparse
import json
import os
import sys
from datetime import date

BASE = os.environ.get("SCAN_DIR") or os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import config

DEFAULT_PATH = os.path.join("experiments", "ledger.jsonl")
TRIAL_KEYS = ("id", "hypothesis", "config_diff", "harness_cmd", "window", "universe", "n",
              "horizons", "in_sample", "out_of_sample", "n_trials_to_date", "decision",
              "date", "engine_sha")


def read(path):
    """Every row in the file, in order. A missing file is an empty ledger."""
    if not path or not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                raise SystemExit(f"REFUSED: {path} line {i} is not JSON. The ledger is "
                                 "append-only; fix the line by hand, do not regenerate it.")
            if isinstance(row, dict):
                rows.append(row)
    return rows


def is_trial(row):
    return row.get("kind", "trial") == "trial"


def count(path):
    """Number of TRIAL rows — the multiple-testing counter as it stands."""
    return sum(1 for r in read(path) if is_trial(r))


def trials(path):
    """Trial rows with the most recent decision row for each id folded in."""
    rows = read(path)
    decisions = {}
    for r in rows:
        if r.get("kind") == "decision" and r.get("ref"):
            decisions[r["ref"]] = r.get("decision")
    out = []
    for r in rows:
        if is_trial(r):
            t = dict(r)
            if t.get("id") in decisions:
                t["decision"] = decisions[t["id"]]
            out.append(t)
    return out


def _append_line(path, row):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=False) + "\n")


def append(path, row):
    """Append one trial. Fills n_trials_to_date, date, engine_sha and an auto id when
    absent; every schema key is present in what gets written. Returns the row."""
    n_prior = count(path)
    out = {k: row.get(k) for k in TRIAL_KEYS}
    out["n_trials_to_date"] = n_prior + 1
    out["date"] = out["date"] or date.today().isoformat()
    if not out["id"]:
        out["id"] = f"X-{out['date'].replace('-', '')}-{out['n_trials_to_date']}"
    if out["engine_sha"] is None:
        out["engine_sha"] = config.engine_sha()
    if out["window"] is None:
        out["window"] = {"start": None, "end": None}
    if out["universe"] is None:
        out["universe"] = {"name": None, "n_symbols": None}
    if out["horizons"] is None:
        out["horizons"] = []
    extra = {k: v for k, v in row.items() if k not in TRIAL_KEYS and k != "kind"}
    out.update(extra)
    _append_line(path, out)
    return out


def set_decision(path, ref, text, when=None):
    """Append a decision row for trial `ref`. Refuses an id the ledger has never seen."""
    ids = {r.get("id") for r in read(path) if is_trial(r)}
    if ref not in ids:
        raise SystemExit(f"REFUSED: no trial {ref!r} in {path}. Decisions reference a trial "
                         "that exists; they never create one.")
    row = {"kind": "decision", "ref": ref, "decision": text,
           "date": when or date.today().isoformat()}
    _append_line(path, row)
    return row


# ---------------------------------------------------------------- CLI
def _metric_summary(t):
    """One short string of the in-sample score metrics, longest horizon first."""
    ins = t.get("in_sample") or {}
    if not isinstance(ins, dict) or not ins:
        return "—"
    keys = sorted(ins, key=lambda k: -(int(k) if str(k).isdigit() else 0))
    for k in keys:
        v = ins[k]
        if isinstance(v, dict) and v.get("ic_mean") is not None:
            t_nw = v.get("ic_tstat_nw")
            return (f"{k}d IC {v['ic_mean']:+.3f}"
                    + (f" t {t_nw:+.2f}" if isinstance(t_nw, (int, float)) else "")
                    + (f" spread {v['spread_pct']:+.2f}%" if isinstance(v.get("spread_pct"), (int, float)) else ""))
    # backfilled rows carry free-form metrics
    parts = [f"{k} {v}" for k, v in list(ins.items())[:3]]
    return "; ".join(parts)


def table(path):
    rows = trials(path)
    if not rows:
        return f"(empty ledger: {path})\n"
    head = f"{'#':>3}  {'id':<26} {'date':<10} {'n':>6}  {'in-sample':<44} decision"
    lines = [head, "-" * len(head)]
    for t in rows:
        n = t.get("n")
        lines.append(f"{t.get('n_trials_to_date', '?'):>3}  {str(t.get('id'))[:26]:<26} "
                     f"{str(t.get('date') or '')[:10]:<10} {str(n if n is not None else '—'):>6}  "
                     f"{_metric_summary(t)[:44]:<44} {t.get('decision') or '(none yet)'}")
    lines.append(f"{len(rows)} trial(s) to date")
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--path", default=DEFAULT_PATH, help=f"default {DEFAULT_PATH} (relative to cwd)")
    ap.add_argument("--list", action="store_true", help="print the trials as a compact table")
    ap.add_argument("--set-decision", nargs=2, metavar=("ID", "TEXT"),
                    help="append a decision row for trial ID (the ledger is never rewritten)")
    a = ap.parse_args(argv)
    if not (a.list or a.set_decision):
        ap.error("nothing to do: pass --list or --set-decision ID TEXT")
    if a.set_decision:
        ref, text = a.set_decision
        row = set_decision(a.path, ref, text)
        print(f"decision -> {a.path}: {row['ref']}: {row['decision']}")
    if a.list:
        sys.stdout.write(table(a.path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
