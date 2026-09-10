"""run.py — the single entry point for the engine on the box.

A Claude session never writes code on the box. It writes INPUT files (the raw Robinhood
quote payload, the scan's collected data, options chains, sentiment payloads — whatever
comes through the connector) into one directory together with a `manifest.json`, then
calls this program. This program stages a run directory exactly the way the test suite
and the task prompts stage one, runs the engine step for the slot as subprocesses of the
same interpreter, writes the outputs back into the local clone of the private state repo,
records a run manifest, and commits and pushes. Every refusal leaves a record: a manifest,
an `outcome.json` next to the inputs, and an `aborted: true` row in the coverage file — so
a run that did not happen never looks like a calm hour.

    python run.py --slot midday --desk all --inputs C:\\ai-trading-inputs\\<run_id> \\
                  --state C:\\ai-trading-state [--engine <clone>] [--dry-run] [--no-push]

Exit codes: 0 committed or already done, 1 refused (inputs, window, lock), 2 failed
(an engine step or git). Stdlib only — the box installs nothing but `tzdata`.

Paper mode is inherited whole from the engine. This program passes no `--mode` and never
touches an order tool; it cannot flip the flag the book carries.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

RUNNER_VERSION = "0.1.0"
HERE = Path(__file__).resolve().parent
SLOTS_PATH = HERE / "slots.json"
LOCK_NAME = ".runner.lock"
RUNS_DIR = ".runs"
STATE_IGNORE = (".runs/", ".runner.lock", "__pycache__/")
PM_SLOTS = ("pre-market", "opening-range", "midday", "power-hour")
ALL_SLOTS = PM_SLOTS + ("sentinel", "watch", "health")
COVERAGE_WHAT = ("Proof that each desk was looked at, including on runs that wrote nothing "
                 "else. Written by runner/run.py from pm.py's per-run heartbeat files "
                 "(COVER-01, PM.md section 12d). A row with `aborted: true` is a run the "
                 "runner refused or that failed before the engine could act.")


class Refused(Exception):
    """The inputs, the window or the lock said no. Nothing ran; exit 1."""


class Failed(Exception):
    """An engine step or git said no. Exit 2."""


# ------------------------------------------------------------------ small helpers
def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def json_bytes(obj):
    """Exactly how pm.py serialises its own outputs (json.dump(obj, f, indent=2))."""
    return json.dumps(obj, indent=2).encode("utf-8")


def write_json(path, obj):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "wb") as f:
        f.write(json_bytes(obj))


def utc_now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(ts):
    return ts.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(s):
    """An aware UTC datetime from an ISO string; naive input is taken as UTC."""
    t = dt.datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(dt.timezone.utc)


def _us_eastern_fallback(utc_ts):
    """EDT/EST by the post-2007 rule, for a Windows box with no tz database.

    DST starts the second Sunday of March at 07:00 UTC and ends the first Sunday of
    November at 06:00 UTC. Only used when zoneinfo cannot resolve America/New_York; the
    manifest records which path was taken.
    """
    y = utc_ts.year

    def nth_sunday(month, n):
        d = dt.date(y, month, 1)
        d += dt.timedelta(days=(6 - d.weekday()) % 7)
        return d + dt.timedelta(weeks=n - 1)
    start = dt.datetime.combine(nth_sunday(3, 2), dt.time(7), tzinfo=dt.timezone.utc)
    end = dt.datetime.combine(nth_sunday(11, 1), dt.time(6), tzinfo=dt.timezone.utc)
    hours = -4 if start <= utc_ts < end else -5
    return dt.timezone(dt.timedelta(hours=hours), "ET-fallback")


def to_et(utc_ts, tz_name="America/New_York"):
    """(datetime in ET, source) where source is 'zoneinfo' or 'fallback'."""
    try:
        from zoneinfo import ZoneInfo
        return utc_ts.astimezone(ZoneInfo(tz_name)), "zoneinfo"
    except Exception:
        return utc_ts.astimezone(_us_eastern_fallback(utc_ts)), "fallback"


def load_slots(path=None):
    return load_json(path or SLOTS_PATH)


def slot_setting(slots, slot_cfg, key):
    return slot_cfg.get(key, (slots.get("defaults") or {}).get(key))


def _hhmm(s):
    h, m = str(s).split(":")
    return int(h) * 60 + int(m)


# ------------------------------------------------------------------ (a) the input manifest
def newest_quote_ts(payload):
    """The most recent venue timestamp anywhere in a raw get_equity_quotes payload."""
    rows = payload
    if isinstance(payload, dict):
        rows = ((payload.get("data") or {}).get("results")
                or payload.get("results") or [])
    best = None
    for row in rows or []:
        q = row.get("quote") if isinstance(row, dict) and isinstance(row.get("quote"), dict) else row
        if not isinstance(q, dict):
            continue
        for k in ("venue_last_trade_time", "venue_last_non_reg_trade_time",
                  "venue_bid_time", "venue_ask_time", "updated_at"):
            v = q.get(k)
            if not v:
                continue
            try:
                t = parse_iso(v)
            except ValueError:
                continue
            if best is None or t > best:
                best = t
    return best


def check_window(as_of, now, slot, slot_cfg, slots, session=None):
    """Raise Refused unless `as_of` sits inside the slot window on today's ET date."""
    tz = slots.get("timezone", "America/New_York")
    as_of_et, _ = to_et(as_of, tz)
    now_et, _ = to_et(now, tz)
    if as_of_et.date() != now_et.date():
        raise Refused(f"as_of {iso(as_of)} is dated {as_of_et.date()} ET but today is "
                      f"{now_et.date()} ET — inputs from another day")
    if as_of > now + dt.timedelta(minutes=5):
        raise Refused(f"as_of {iso(as_of)} is in the future (now {iso(now)})")
    lag_min = (now - as_of).total_seconds() / 60.0
    max_lag = float(slot_setting(slots, slot_cfg, "max_input_lag_min") or 25)
    if lag_min > max_lag:
        raise Refused(f"inputs are {lag_min:.0f} minutes old at invocation — over the "
                      f"{max_lag:.0f}-minute limit; fetch fresh quotes and re-run")
    cfg = slot_cfg
    if slot == "watch":
        cfg = (slot_cfg.get("sessions") or {}).get(session) or {}
    minute = as_of_et.hour * 60 + as_of_et.minute
    win = cfg.get("window_et")
    if win:
        lo, hi = _hhmm(win[0]), _hhmm(win[1])
        if not (lo <= minute <= hi):
            raise Refused(f"as_of {as_of_et.strftime('%H:%M')} ET is outside the {slot}"
                          f"{' ' + session if session else ''} window {win[0]}–{win[1]} ET")
        return
    nominal = cfg.get("nominal_et")
    if not nominal:
        return
    half = float(slot_setting(slots, slot_cfg, "window_min") or 90)
    if abs(minute - _hhmm(nominal)) > half:
        raise Refused(f"as_of {as_of_et.strftime('%H:%M')} ET is more than {half:.0f} minutes "
                      f"from the {slot} slot at {nominal} ET")


def verify_inputs(inputs_dir, step, slots, slot_cfg, quote_max_age_s=None):
    """Read and verify <inputs>/manifest.json. Returns {as_of, files:{name: sha256}}."""
    inputs_dir = Path(inputs_dir)
    mpath = inputs_dir / "manifest.json"
    if not mpath.exists():
        raise Refused(f"no manifest.json in {inputs_dir}")
    try:
        man = load_json(mpath)
    except ValueError as exc:
        raise Refused(f"manifest.json is not valid JSON: {exc}")
    if not isinstance(man, dict) or "as_of" not in man:
        raise Refused("manifest.json must be an object with `as_of` and `files`")
    try:
        as_of = parse_iso(man["as_of"])
    except (ValueError, TypeError) as exc:
        raise Refused(f"manifest as_of is not an ISO timestamp: {exc}")

    listed = man.get("files") or {}
    if isinstance(listed, list):
        listed = {row["name"]: row for row in listed if isinstance(row, dict) and row.get("name")}
    if not isinstance(listed, dict):
        raise Refused("manifest `files` must be a {name: sha256} map or a list of {name, sha256}")

    hashes = {}
    for name, spec in listed.items():
        want = spec.get("sha256") if isinstance(spec, dict) else spec
        if not isinstance(want, str) or len(want) != 64:
            raise Refused(f"{name}: manifest carries no sha256")
        p = inputs_dir / name
        if not p.is_file():
            raise Refused(f"{name} is listed in the manifest but missing from {inputs_dir}")
        got = sha256_file(p)
        if got.lower() != want.lower():
            raise Refused(f"{name}: sha256 mismatch — manifest {want[:12]}…, file {got[:12]}…")
        hashes[name] = got

    spec = (slots.get("inputs") or {}).get(step) or {}
    for name in spec.get("required") or []:
        if name not in hashes:
            raise Refused(f"{name} is required for the {step} step and is not in the manifest")

    max_age = float(quote_max_age_s or slot_setting(slots, slot_cfg, "quote_max_age_s") or 60)
    for name in slots.get("quote_files") or []:
        if name not in hashes:
            continue
        try:
            payload = load_json(inputs_dir / name)
        except ValueError as exc:
            raise Refused(f"{name} is not valid JSON: {exc}")
        newest = newest_quote_ts(payload)
        if newest is None:
            raise Refused(f"{name} carries no venue timestamps — not a raw quote payload")
        age = (as_of - newest).total_seconds()
        if age > max_age:
            raise Refused(f"{name}: newest quote is {age:.0f}s older than as_of — over the "
                          f"{max_age:.0f}s limit; the payload was not fetched at as_of")
    return {"as_of": as_of, "files": hashes, "manifest": man}


# ------------------------------------------------------------------ identity, lock, idempotency
def git(state, *args, check=False):
    """Run git in `state`. Returns CompletedProcess; raises Failed when check and non-zero."""
    exe = os.environ.get("RUNNER_GIT", "git")
    r = subprocess.run([exe, "-C", str(state), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise Failed(f"git {' '.join(args)} failed ({r.returncode}): {r.stderr.strip()[:400]}")
    return r


def engine_identity(engine_root):
    """(sha, branch) of the engine clone, or (None, None) when git cannot say."""
    try:
        sha = git(engine_root, "rev-parse", "HEAD")
        br = git(engine_root, "rev-parse", "--abbrev-ref", "HEAD")
    except OSError:
        return None, None
    if sha.returncode != 0:
        return None, None
    return sha.stdout.strip(), (br.stdout.strip() if br.returncode == 0 else None)


def run_key(slot, step, desk, now_et, session=None):
    """The idempotency key: what `manifests/<date>/<key>.json` is named."""
    if step == "scan":
        return f"{slot}-scan"
    if slot == "sentinel":
        return f"sentinel-{now_et.strftime('%H')}-{desk}"
    if slot == "watch":
        return f"watch-{session}-{desk}"
    if slot == "health":
        return f"health-{now_et.strftime('%H%M')}"      # on demand, never "already done"
    return f"{slot}-{desk}"


def make_run_id(slot, step, now_et, session=None):
    date = now_et.date().isoformat()
    if step == "scan":
        return f"{date}-{slot}-scan"
    if slot == "sentinel":
        return f"{date}-sentinel-{now_et.strftime('%H%M')}"
    if slot == "watch":
        return f"{date}-watch-{session}"
    if slot == "health":
        return f"{date}-health-{now_et.strftime('%H%M')}"
    return f"{date}-{slot}"


def manifest_path(state, date, key):
    return Path(state) / "manifests" / date / f"{key}.json"


def already_done(state, date, key):
    m = load_json(manifest_path(state, date, key))
    return isinstance(m, dict) and m.get("outcome") == "committed"


class Lock:
    """`<state>/.runner.lock`, created O_EXCL, holding pid and start time.

    A lock older than `stale_min` belongs to a run that died without releasing it (the
    box rebooted, the process was killed) and is taken over; a younger one is honoured
    and the run is refused, on the record.
    """

    def __init__(self, state, stale_min=45):
        self.path = Path(state) / LOCK_NAME
        self.stale_min = stale_min
        self.held = False

    def _read(self):
        try:
            info = load_json(self.path, {}) or {}
        except (OSError, ValueError):
            info = {}
        try:
            started = parse_iso(info.get("started"))
            age_min = (utc_now() - started).total_seconds() / 60.0
        except (ValueError, TypeError):
            age_min = None
        return info, age_min

    def acquire(self):
        for attempt in (1, 2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                info, age_min = self._read()
                if attempt == 1 and (age_min is None or age_min > self.stale_min):
                    try:
                        self.path.unlink()      # stale, or unreadable: take it over
                    except OSError:
                        pass
                    continue
                age = f"{age_min:.0f} min ago" if age_min is not None else "age unknown"
                raise Refused(f"another run holds {self.path.name} (pid {info.get('pid')}, "
                              f"started {info.get('started')}, {age})")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "started": iso(utc_now()),
                           "host": platform.node()}, f)
            self.held = True
            return self
        raise Refused("could not take the runner lock")

    def release(self):
        if self.held:
            try:
                self.path.unlink()
            except OSError:
                pass
            self.held = False


# ------------------------------------------------------------------ (d) staging
def desks_config(engine_dir):
    return (load_json(Path(engine_dir) / "desks.json") or {}).get("desks") or {}


def ensure_state_ignore(state):
    gi = Path(state) / ".gitignore"
    have = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    missing = [line for line in STATE_IGNORE if line not in have]
    if missing:
        with open(gi, "a", encoding="utf-8") as f:
            if have and have[-1].strip():
                f.write("\n")
            f.write("\n".join(missing) + "\n")


def stage_run(engine_dir, state, inputs_dir, run_dir, desks, engine_sha, config_path=None,
              only=None):
    """Copy engine modules, the desk books/journals, the shared state files and the inputs.

    Mirrors tests/conftest.py and the task prompts: the run directory holds engine/*.py
    and *.json (pm.py inserts $SCAN_DIR at the head of sys.path, so the copies ARE the
    modules that run), the books under the names desks.json gives, the journals under the
    names pm.py expects, and every input file. `engine_sha` is stamped for config.py.
    """
    engine_dir, state, inputs_dir, run_dir = map(Path, (engine_dir, state, inputs_dir, run_dir))
    run_dir.mkdir(parents=True, exist_ok=True)
    for f in list(engine_dir.glob("*.py")) + list(engine_dir.glob("*.json")):
        shutil.copy2(f, run_dir / f.name)
    staged = {"books": [], "journals": [], "missing_books": [], "inputs": [], "state": []}
    cfg = desks_config(engine_dir)
    for desk in desks:
        d = cfg.get(desk) or {}
        book = d.get("book") or f"paper_book_{desk}.json"
        journal = d.get("journal") or f"pm_journal_current_{desk}.json"
        src = state / "books" / f"{desk}.json"
        if src.exists():
            shutil.copy2(src, run_dir / book)
            staged["books"].append(desk)
        else:
            staged["missing_books"].append(desk)
        jsrc = state / "journals" / f"{desk}.json"
        if jsrc.exists():
            shutil.copy2(jsrc, run_dir / journal)
            staged["journals"].append(desk)
    # Peer books for the house caps and the paper mirror: every desk that exists.
    for desk, d in cfg.items():
        if desk in desks or d.get("inactive"):
            continue
        src = state / "books" / f"{desk}.json"
        if src.exists():
            shutil.copy2(src, run_dir / (d.get("book") or f"paper_book_{desk}.json"))
    for src_name, dst_name in (("scans/latest.json", "scan_results.json"),
                               ("scan-index.json", "index_current.json"),
                               ("scan-history.json", "history_current.json"),
                               ("journals/watch.json", "watch_journal_current.json")):
        src = state / src_name
        if src.exists():
            shutil.copy2(src, run_dir / dst_name)
            staged["state"].append(src_name)
    # S-03: the followed set lives in the archive and is maintained on every scan, so the
    # engine must see the stored copy or every run would start the roster from nothing.
    followed = state / "archive" / "followed.json"
    if followed.exists():
        (run_dir / "archive").mkdir(exist_ok=True)
        shutil.copy2(followed, run_dir / "archive" / "followed.json")
        staged["state"].append("archive/followed.json")
    for f in sorted(inputs_dir.iterdir()):
        if not f.is_file() or f.name in ("manifest.json", "outcome.json"):
            continue
        if only is not None and f.name not in only:
            staged.setdefault("unlisted", []).append(f.name)   # not in the manifest: not staged
            continue
        shutil.copy2(f, run_dir / f.name)       # inputs outrank the staged state copy
        staged["inputs"].append(f.name)
    if config_path and Path(config_path).exists():
        shutil.copy2(config_path, run_dir / "engine-config.json")
        staged["config"] = True
    if engine_sha:
        (run_dir / "engine_sha").write_text(engine_sha + "\n", encoding="utf-8")
    return staged


# ------------------------------------------------------------------ (e) engine steps
class Ctx:
    """Everything a step needs, in one place."""

    def __init__(self, run_dir, python, log, now_override=None):
        self.run_dir = Path(run_dir)
        self.python = python
        self.log = log
        self.now_override = now_override
        self.steps = []          # [{name, argv, exit_code, seconds, stdout, stderr}]

    def run(self, name, script, *args, fatal=True):
        argv = [self.python, str(self.run_dir / script), *[str(a) for a in args]]
        env = dict(os.environ)
        env["SCAN_DIR"] = str(self.run_dir)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        t0 = time.monotonic()
        r = subprocess.run(argv, cwd=str(self.run_dir), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env)
        rec = {"name": name, "argv": argv[1:], "exit_code": r.returncode,
               "seconds": round(time.monotonic() - t0, 2), "stdout": r.stdout, "stderr": r.stderr}
        self.steps.append(rec)
        (self.run_dir / f"{name}.stdout.txt").write_text(r.stdout, encoding="utf-8")
        (self.run_dir / f"{name}.stderr.txt").write_text(r.stderr, encoding="utf-8")
        self.log(f"step {name}: exit {r.returncode} in {rec['seconds']}s")
        if r.returncode != 0 and fatal:
            tail = (r.stderr or r.stdout).strip().splitlines()[-3:]
            raise Failed(f"{name} exited {r.returncode}: " + " | ".join(tail))
        return rec


def _now_args(ctx):
    return ["--now", ctx.now_override] if ctx.now_override else []


def pm_desk(ctx, slot, desk, cfg, state):
    """pm.py for one desk, then the section-8 --check against the stored book.

    Returns a per-desk result: {desk, exit_code, quiet, heartbeat, wrote, check}.
    """
    d = cfg.get(desk) or {}
    book = d.get("book") or "paper_book.json"
    journal = d.get("journal") or "pm_journal_current.json"
    sfx = "" if desk == "swing" else f"-{desk}"
    args = ["--slot", slot, "--desk", desk, "--book", book, "--journal", journal,
            "--quotes", "pm_quotes.json", "--broker", "pm_broker.json"]
    if slot != "sentinel":
        args += ["--scan", "scan_results.json"]
    rec = ctx.run(f"pm-{desk}", "pm.py", *args, *_now_args(ctx), fatal=False)
    out = {"desk": desk, "exit_code": rec["exit_code"], "quiet": None, "wrote": False,
           "check": None, "heartbeat": load_json(ctx.run_dir / f"pm_heartbeat{sfx}.json")}
    if rec["exit_code"] != 0:
        return out
    nxt = ctx.run_dir / f"pm_book_next{sfx}.json"
    if not nxt.exists():
        out["quiet"] = True             # a QUIET sentinel: heartbeat only, by design
        return out
    out["quiet"] = False
    # Section 8: re-read the stored book immediately before writing back.
    stored = Path(state) / "books" / f"{desk}.json"
    probe = ctx.run_dir / f"fresh_probe{sfx}.json"
    shutil.copy2(stored, probe)
    chk = ctx.run(f"check-{desk}", "pm.py", "--desk", desk, "--book", nxt.name,
                  "--check", probe.name, fatal=False)
    out["check"] = chk["exit_code"]
    if chk["exit_code"] != 0:
        out["exit_code"] = chk["exit_code"]
        return out
    out["wrote"] = True
    out["state"] = load_json(ctx.run_dir / f"pm_state{sfx}.json") or {}
    return out


def pm_steps(ctx, slot, desks, cfg, state):
    results = [pm_desk(ctx, slot, desk, cfg, state) for desk in desks]
    # The rolling Trade Desk board is the swing desk's; render it when the swing run acted.
    if (ctx.run_dir / "pm_state.json").exists():
        ctx.run("render-pm", "render_pm.py", fatal=False)
    return results


def merge_technicals(run_dir):
    """The docs' 'merge technicals.json into each candidate in scan_data.json' line."""
    tech = load_json(Path(run_dir) / "technicals.json")
    data = load_json(Path(run_dir) / "scan_data.json")
    if not isinstance(tech, dict) or not isinstance(data, dict):
        return 0
    cands = data.get("candidates") or {}
    n = 0
    rows = cands.items() if isinstance(cands, dict) else ((c.get("symbol"), c) for c in cands)
    for sym, c in rows:
        t = tech.get(str(sym).upper()) if sym else None
        if isinstance(t, dict) and isinstance(c, dict):
            c.update({k: v for k, v in t.items() if k != "bars"})
            n += 1
    write_json(Path(run_dir) / "scan_data.json", data)
    return n


SENTIMENT_ARGS = [("apewisdom.json", "--apewisdom"), ("st_trending.json", "--stocktwits-trending"),
                  ("st_gauges.json", "--stocktwits-gauges"), ("reddit_posts.json", "--reddit"),
                  ("rh_watchlists.json", "--watchlists"), ("quotes_min.json", "--quotes"),
                  ("options_scan.json", "--options-scan"), ("earnings_days.json", "--earnings-days")]


def scan_steps(ctx, slot_cfg, today_utc):
    """SCAN.md section 3 in order. Returns {run_id, record, history_refused, ...}."""
    rd = ctx.run_dir
    out = {"technicals_merged": 0, "sentiment": False, "history_refused": False}
    if (rd / "bars.json").exists():
        args = ["--bars", "bars.json", "--out", "technicals.json"]
        if (rd / "fundamentals.json").exists():
            args += ["--fundamentals", "fundamentals.json"]
        if (rd / "quotes.json").exists():
            args += ["--quotes", "quotes.json"]
        if slot_cfg.get("scan_premarket"):
            args.append("--premarket")
        ctx.run("technicals", "technicals.py", *args)
        out["technicals_merged"] = merge_technicals(rd)
    sargs = []
    for fname, flag in SENTIMENT_ARGS:
        if (rd / fname).exists():
            sargs += [flag, fname]
    if sargs:
        ctx.run("sentiment", "sentiment.py", *sargs, "--out", "sentiment.json",
                "--merge-into", "scan_data.json")
        out["sentiment"] = True
    ctx.run("scanner", "scanner.py")
    res = load_json(rd / "scan_results.json") or {}
    if not res.get("results"):
        raise Failed("scanner.py wrote no ranked rows")
    mirror = ctx.run("paper-mirror", "paper_mirror.py", fatal=False)
    if mirror["exit_code"] == 0 and (rd / "portfolio.json").exists():
        ctx.run("portfolio", "portfolio.py", fatal=False)
    ctx.run("render", "render.py", fatal=False)
    ctx.run("archive-record", "archive.py", "--record")
    idx_args = ["--index", "--out", "index_merged.json"]
    if (rd / "index_current.json").exists():
        idx_args += ["--current", "index_current.json"]
    ctx.run("archive-index", "archive.py", *idx_args)
    ctx.run("history-entry", "archive.py", "--history-entry", "history_entry.json")
    hargs = ["--entry", "history_entry.json", "--today", today_utc, "--out", "history_merged.json"]
    if (rd / "history_current.json").exists():
        hargs += ["--current", "history_current.json"]
    h = ctx.run("history", "history.py", *hargs, fatal=False)
    out["history_refused"] = h["exit_code"] != 0
    recs = sorted(rd.glob("scan-record-*.json"))
    if not recs:
        raise Failed("archive.py --record left no scan-record file")
    out["record"] = recs[-1].name
    out["run_id"] = recs[-1].name[len("scan-record-"):-len(".json")]
    return out


def watch_steps(ctx, session, desks, cfg):
    """watch.py per desk, chaining the single watch journal through the desks."""
    rd = ctx.run_dir
    results = []
    current = "watch_journal_current.json"
    for desk in desks:
        d = cfg.get(desk) or {}
        book = d.get("book") or "paper_book.json"
        if not (rd / book).exists():
            results.append({"desk": desk, "exit_code": None, "missing_book": True})
            continue
        out = f"watch_journal_next-{desk}.json"
        args = ["--session", session, "--desk", desk, "--book", book, "--quotes", "pm_quotes.json",
                "--tradability", "pm_tradability.json", "--journal", current, "--out", out]
        if (rd / "pm_broker.json").exists():
            args += ["--broker", "pm_broker.json"]
        rec = ctx.run(f"watch-{desk}", "watch.py", *args, *_now_args(ctx), fatal=False)
        wrote = (rd / out).exists()
        if wrote:
            shutil.copy2(rd / out, rd / current)
        results.append({"desk": desk, "exit_code": rec["exit_code"], "wrote": wrote,
                        "quiet": "WATCH QUIET" in rec["stdout"]})
    return results


HEALTH_STATUS_RANK = {"pass": 0, "warn": 1, "fail": 2}


def _health_row(name, status, value, threshold, detail):
    return {"name": name, "status": status, "value": value, "threshold": threshold, "detail": detail}


def state_layout_row(state, engine_root):
    """runner/validate_state.py over the state repo, as one health row (read-only).

    Layout, every JSON parsing, no `mirrors` block or banned shape anywhere, revisions
    monotonic, coverage rows well-formed. An error is `fail`; a warning `warn`; a validator
    that cannot run at all is reported as `warn` rather than taking the health slot down.
    """
    try:
        import validate_state
        engine_root = Path(engine_root)
        engine_dir = engine_root / "engine" if (engine_root / "engine" / "desks.json").exists() else engine_root
        res = validate_state.validate(state, engine_dir)
        status = "fail" if res["errors"] else ("warn" if res["warnings"] else "pass")
        detail = "; ".join(res["errors"][:3] + res["warnings"][:2]) or \
            f"{res['summary']['json']} JSON document(s), books " + \
            ", ".join(f"{d} r{r}" for d, r in sorted(res["summary"]["books"].items()))
        return _health_row("state_layout", status,
                           {"errors": len(res["errors"]), "warnings": len(res["warnings"])},
                           "validate_state.py reports no error", detail)
    except Exception as exc:       # the validator must never take the health slot down
        return _health_row("state_layout", "warn", None, "validate_state.py reports no error",
                           f"validate_state could not run: {type(exc).__name__}: {exc}")


def health_steps(ctx, state, engine_root, engine_sha, engine_branch, now, slots, mirror=None,
                 slots_path=None, config_path=None):
    """The health slot: engine/health.py over the state repo, plus the runner's own checks.

    `engine/health.py` (U-01) owns everything that can be read off the state repo — book
    freshness, coverage, unjudged names, the shadow gap, halts and the ladder, the broker
    policy, commit age and push backlog, the mirror, the heartbeat, the dead-man's switch,
    tzdata and the engine branch. The runner adds only what the engine cannot see from a
    directory: whether the lock is free and whether it could identify the engine at all.
    The runner adds its `lock_free`, `engine_sha` and `state_layout` rows (the last from
    runner/validate_state.py).
    The sheet is `{"checks": [{name, status, value, threshold, detail}], "verdict", ...}`
    and is written to health/<date>.json (and .md) by main().
    """
    state = Path(state)
    args = ["--state", str(state), "--engine", str(engine_root), "--out", "health.json",
            "--md", "health.md", "--slots", str(slots_path or SLOTS_PATH)]
    if mirror:
        args += ["--mirror", str(mirror)]
    if config_path:
        args += ["--config", str(config_path)]
    rec = ctx.run("engine-health", "health.py", *args, *_now_args(ctx), fatal=False)
    doc = load_json(ctx.run_dir / "health.json")
    if not isinstance(doc, dict) or not isinstance(doc.get("checks"), list):
        tail = (rec["stderr"] or rec["stdout"]).strip().splitlines()[-2:]
        doc = {"as_of": iso(now), "verdict": "fail", "checks": [
            _health_row("engine_health", "fail", rec["exit_code"], "health.py exits 0/1/2 and writes health.json",
                        f"health.py exited {rec['exit_code']} without a sheet: " + " | ".join(tail))]}
    checks = list(doc["checks"])
    lock = state / LOCK_NAME
    lock_ok = not lock.exists() or (load_json(lock, {}) or {}).get("pid") == os.getpid()
    checks.append(_health_row("lock_free", "pass" if lock_ok else "warn", lock_ok,
                              "no foreign .runner.lock", str(lock) if lock.exists() else "no lock"))
    checks.append(_health_row("engine_sha", "pass" if engine_sha else "fail", engine_sha,
                              "the engine clone answers rev-parse HEAD",
                              f"{engine_sha[:10]}@{engine_branch}" if engine_sha else "engine sha unknown"))
    checks.append(state_layout_row(state, engine_root))
    doc["checks"] = checks
    doc["verdict"] = max((c["status"] for c in checks), key=lambda s: HEALTH_STATUS_RANK.get(s, 2))
    doc.update({"generated": iso(now), "runner_version": RUNNER_VERSION, "engine_sha": engine_sha,
                "python": platform.python_version(), "host": platform.node(),
                "engine_health_exit": rec["exit_code"], "ok": doc["verdict"] != "fail"})
    write_json(ctx.run_dir / "health.json", doc)
    md = ctx.run_dir / "health.md"
    if md.exists():
        extra = "".join(f"| `{c['name']}` | {c['status'].upper()} | {c['detail']} | {c['threshold']} |\n"
                        for c in checks[-3:])
        md.write_text(md.read_text(encoding="utf-8").rstrip("\n") + "\n" + extra, encoding="utf-8")
    return doc


def write_heartbeat(state, ts, slot, desk, run_id, outcome):
    """K-05 — `<state>/health/heartbeat.json`: proof the runner was invoked at all.

    Written on every invocation, any slot, any outcome (refused and already_done included),
    so an independent checker (runner/deadman.py) can tell "the box is running the runner
    and the runner is saying no" from "nothing is running". Never on a dry run.
    """
    try:
        write_json(Path(state) / "health" / "heartbeat.json",
                   {"ts": ts, "slot": slot, "desk": desk, "run_id": run_id, "outcome": outcome,
                    "pid": os.getpid(), "host": platform.node(), "runner_version": RUNNER_VERSION})
        return True
    except OSError:
        return False


# ------------------------------------------------------------------ (f) write-back
def scrub_book(book):
    """The state repo never carries the broker block (mirror.py's rule, applied at the source)."""
    return {k: v for k, v in book.items() if k != "mirrors"}


def write_back_pm(state, run_dir, run_id, results, cfg):
    """Books, journals and book-history for every desk whose run wrote and passed --check."""
    state, run_dir = Path(state), Path(run_dir)
    written = []
    for r in results:
        if not r.get("wrote"):
            continue
        desk = r["desk"]
        sfx = "" if desk == "swing" else f"-{desk}"
        book = load_json(run_dir / f"pm_book_next{sfx}.json")
        write_json(state / "books" / f"{desk}.json", scrub_book(book))
        written.append(f"books/{desk}.json")
        jn = run_dir / f"pm_journal_next{sfx}.json"
        if jn.exists():
            dst = state / "journals" / f"{desk}.json"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(jn, dst)
            written.append(f"journals/{desk}.json")
        st = r.get("state") or {}
        rid = st.get("run_id")
        if rid:
            arch = state / "archive" / "runs" / run_id
            arch.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(run_dir / f"pm_state{sfx}.json", arch / f"pm_state-{rid}.json")
            written.append(f"archive/runs/{run_id}/pm_state-{rid}.json")
            if st.get("changed"):
                write_json(state / "archive" / "book-history" / f"{rid}.json", scrub_book(book))
                written.append(f"archive/book-history/{rid}.json")
    for html in ("trade-desk.html",) + tuple(p.name for p in run_dir.glob("trade-desk-*.html")):
        src = run_dir / html
        if src.exists():
            arch = state / "archive" / "runs" / run_id
            arch.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, arch / html)
            written.append(f"archive/runs/{run_id}/{html}")
    written += write_back_archive(state, run_dir)
    return written


def write_back_scan(state, run_dir, run_id, scan):
    state, run_dir = Path(state), Path(run_dir)
    written = []
    (state / "scans").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(run_dir / "scan_results.json", state / "scans" / "latest.json")
    written.append("scans/latest.json")
    shutil.copyfile(run_dir / scan["record"], state / "scans" / f"{scan['run_id']}.json")
    written.append(f"scans/{scan['run_id']}.json")
    if (run_dir / "index_merged.json").exists():
        shutil.copyfile(run_dir / "index_merged.json", state / "scan-index.json")
        written.append("scan-index.json")
    if (run_dir / "history_merged.json").exists() and not scan.get("history_refused"):
        shutil.copyfile(run_dir / "history_merged.json", state / "scan-history.json")
        written.append("scan-history.json")
    arch = state / "archive" / "runs" / run_id
    arch.mkdir(parents=True, exist_ok=True)
    for name in ("scan-desk.html", "portfolio_state.json", "scan_data.json"):
        if (run_dir / name).exists():
            shutil.copyfile(run_dir / name, arch / name)
            written.append(f"archive/runs/{run_id}/{name}")
    written += write_back_archive(state, run_dir)
    return written


ARCHIVE_SNAPSHOT_DIRS = ("scan_snapshot", "chain_snapshot")


def write_back_archive(state, run_dir):
    """S-01 / S-03: the per-slot snapshots and the followed set, `archive/` to `archive/`.

    The engine writes them under $SCAN_DIR/archive exactly as the state repo lays them
    out, so this is a straight copy of whatever this run produced: a snapshot file is
    named by its slot and a re-run replaces its own.
    """
    state, run_dir = Path(state), Path(run_dir)
    written = []
    for sub in ARCHIVE_SNAPSHOT_DIRS:
        src_dir = run_dir / "archive" / sub
        if not src_dir.is_dir():
            continue
        dst_dir = state / "archive" / sub
        dst_dir.mkdir(parents=True, exist_ok=True)
        for f in sorted(src_dir.glob("*.jsonl.gz")):
            shutil.copyfile(f, dst_dir / f.name)
            written.append(f"archive/{sub}/{f.name}")
    followed = run_dir / "archive" / "followed.json"
    if followed.exists():
        (state / "archive").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(followed, state / "archive" / "followed.json")
        written.append("archive/followed.json")
    return written


def write_back_watch(state, run_dir, results):
    state, run_dir = Path(state), Path(run_dir)
    if not any(r.get("wrote") for r in results):
        return []
    src = run_dir / "watch_journal_current.json"
    dst = state / "journals" / "watch.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return ["journals/watch.json"]


def archive_logs(state, run_dir, run_id):
    arch = Path(state) / "archive" / "runs" / run_id / "logs"
    arch.mkdir(parents=True, exist_ok=True)
    for f in Path(run_dir).glob("*.std*.txt"):
        shutil.copyfile(f, arch / f.name)


# ------------------------------------------------------------------ coverage
def coverage_row_from_results(results, slot, run_id, engine_sha, ts, step="pm", key=None):
    desks = {}
    exposure = None
    for r in results:
        hb = r.get("heartbeat") or {}
        if hb:
            desks[r["desk"]] = {k: hb.get(k) for k in ("quiet", "positions", "working_orders",
                                                       "decisions", "warnings", "book_revision")}
            # K-03: every desk's heartbeat carries the SAME house-wide summary (it is one
            # combined book); the row keeps one copy, from the last desk that measured it.
            if isinstance(hb.get("exposure"), dict):
                exposure = hb["exposure"]
        else:
            desks[r["desk"]] = {"failed": True, "exit_code": r.get("exit_code")}
        if r.get("exit_code") not in (0, None):
            desks[r["desk"]]["failed"] = True
            desks[r["desk"]]["exit_code"] = r["exit_code"]
        if r.get("check") not in (None, 0):
            desks[r["desk"]]["abandoned"] = "concurrent write (--check exit 2)"
    row = {"ts": ts, "slot": slot, "step": step, "run_id": run_id, "key": key,
           "engine_sha": engine_sha, "engine_source": "runner",
           "runner_version": RUNNER_VERSION, "desks": desks}
    if exposure is not None:
        row["exposure"] = exposure      # additive; absent when no desk measured the house
    return row


def aborted_row(slot, step, desk, run_id, engine_sha, ts, outcome, reason, key=None):
    return {"ts": ts, "slot": slot, "step": step, "desk": desk, "run_id": run_id, "key": key,
            "engine_sha": engine_sha, "engine_source": "runner", "runner_version": RUNNER_VERSION,
            "aborted": True, "outcome": outcome, "reason": reason, "desks": {}}


def merge_coverage(state, row):
    """Append the row under its date; a row with the same idempotency `key` on the same
    date is replaced (a retry supersedes its own aborted row, never another run's); old
    days beyond keep_days are pruned."""
    path = Path(state) / "coverage" / "pm-coverage.json"
    doc = load_json(path) or {}
    doc.setdefault("_what", COVERAGE_WHAT)
    days = doc.setdefault("days", {})
    date = str(row["ts"])[:10]
    runs = days.setdefault(date, {}).setdefault("runs", [])
    if row.get("key"):
        runs[:] = [r for r in runs if r.get("key") != row["key"]]
    runs.append(row)
    runs.sort(key=lambda r: r.get("ts", ""))
    keep = int(doc.get("keep_days") or 10)
    doc["keep_days"] = keep
    for old in sorted(days)[:-keep]:
        del days[old]
    doc["updated"] = row["ts"]
    write_json(path, doc)
    return path


# ------------------------------------------------------------------ (h) git
def commit_and_push(state, message, push=True, log=print):
    git(state, "add", "-A", check=True)
    status = git(state, "status", "--porcelain")
    if not status.stdout.strip():
        log("git: nothing to commit")
        return {"committed": False, "sha": git(state, "rev-parse", "HEAD").stdout.strip() or None,
                "pushed": False, "push_failed": False}
    ident = ["-c", "user.name=ai-trading-runner", "-c", "user.email=runner@ai-trading.local"]
    git(state, *ident, "commit", "-q", "-m", message, check=True)
    sha = git(state, "rev-parse", "HEAD").stdout.strip()
    log(f"git: committed {sha[:10]}")
    out = {"committed": True, "sha": sha, "pushed": False, "push_failed": False}
    if push:
        r = git(state, "push", "-q")
        if r.returncode == 0:
            out["pushed"] = True
            log("git: pushed")
        else:
            out["push_failed"] = True
            out["push_error"] = (r.stderr or r.stdout).strip()[-400:]
            log(f"git: PUSH FAILED — the commit stays local: {out['push_error'][:200]}")
    return out


# ------------------------------------------------------------------ main
def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--slot", required=True, choices=ALL_SLOTS)
    ap.add_argument("--desk", default="all", help="swing | pullback | momentum | all")
    ap.add_argument("--inputs", required=True, help="directory holding manifest.json and the inputs")
    ap.add_argument("--state", required=True, help="local clone of ai-trading-state")
    ap.add_argument("--engine", default=None,
                    help="the engine clone (holds engine/); default: the clone this file is in")
    ap.add_argument("--step", default="auto", choices=["auto", "scan", "pm"],
                    help="for the four decision slots: which half runs. auto = scan when the "
                         "inputs carry scan_data.json, else pm")
    ap.add_argument("--session", default=None, choices=["pre-open", "after-hours"],
                    help="watch only; default: by the ET clock (before noon = pre-open)")
    ap.add_argument("--config", default=None,
                    help="engine-config.json (identifiers; never committed). Default: "
                         "<engine clone>/../engine-config.json, then <state>/engine-config.json")
    ap.add_argument("--slots", default=None, help="alternative slots.json")
    ap.add_argument("--mirror", default=None,
                    help="health only: the served mirror directory (manifest.json age check)")
    ap.add_argument("--dry-run", action="store_true",
                    help="verify, stage and run the engine, but write nothing back and do not commit")
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--now", default=None,
                    help="ISO clock override for the runner AND the engine (selftest only; "
                         "recorded in the manifest)")
    ap.add_argument("--keep-run-dir", action="store_true",
                    help="leave .runs/<run_id> in place after a committed run")
    return ap.parse_args(argv)


def main(argv=None):
    a = parse_args(argv)
    lines = []

    def log(msg):
        line = f"[{utc_now().strftime('%H:%M:%S')}] {msg}"
        lines.append(line)
        print(line, flush=True)

    now = parse_iso(a.now) if a.now else utc_now()
    slots = load_slots(a.slots)
    tz = slots.get("timezone", "America/New_York")
    now_et, tz_source = to_et(now, tz)
    slot_cfg = (slots.get("slots") or {}).get(a.slot) or {}
    engine_root = Path(a.engine).resolve() if a.engine else HERE.parent
    engine_dir = engine_root / "engine" if (engine_root / "engine" / "pm.py").exists() else engine_root
    state = Path(a.state).resolve()
    inputs = Path(a.inputs).resolve()
    known_desks = slots.get("desks") or []
    desks = list(slot_cfg.get("desks", known_desks)) if a.desk == "all" else [a.desk]
    if a.slot == "health":
        desks = []
    for d in desks:
        if d not in known_desks:
            print(f"unknown desk {d!r} — one of {known_desks} or all", file=sys.stderr)
            return 1

    # Which step. One invocation, one step.
    step = a.slot if a.slot in ("sentinel", "watch", "health") else a.step
    if step == "auto":
        probe = load_json(inputs / "manifest.json", {}) if (inputs / "manifest.json").exists() else {}
        listed = probe.get("files") if isinstance(probe, dict) else None
        if isinstance(listed, list):
            listed = {r.get("name") for r in listed if isinstance(r, dict)}
        step = "scan" if "scan_data.json" in set(listed or []) else "pm"
    session = a.session
    if a.slot == "watch" and not session:
        session = "pre-open" if now_et.hour < 12 else "after-hours"

    date = now_et.date().isoformat()
    key = run_key(a.slot, step, a.desk, now_et, session)
    run_id = make_run_id(a.slot, step, now_et, session)
    engine_sha, engine_branch = engine_identity(engine_root)
    manifest = {
        "run_id": run_id, "slot": a.slot, "step": step, "desk": a.desk, "desks": desks,
        "session": session, "runner_version": RUNNER_VERSION, "engine_sha": engine_sha,
        "engine_branch": engine_branch, "engine_dir": str(engine_dir),
        "python_version": platform.python_version(), "host": platform.node(),
        "tz_source": tz_source, "clock_override": a.now, "dry_run": a.dry_run,
        "input_hashes": {}, "started": iso(now), "finished": None, "outcome": None,
        "reason": None, "engine_exit_code": None, "written": [], "git": None,
    }
    log(f"runner {RUNNER_VERSION} slot={a.slot} step={step} desk={a.desk} run_id={run_id} "
        f"engine={engine_sha[:10] if engine_sha else '?'}@{engine_branch or '?'} "
        f"python={platform.python_version()}")
    if tz_source != "zoneinfo":
        log("WARNING: no tz database — ET computed by the built-in DST rule; install tzdata "
            "so the engine's own scan-freshness and macro gates work")

    def stamp():
        return iso(now if a.now else utc_now())

    def finish(outcome, reason=None, code=0, commit_msg=None):
        """Write the run manifest, commit if asked, write outcome.json, print one line."""
        manifest["finished"] = stamp()
        manifest["outcome"] = outcome
        manifest["reason"] = reason
        manifest["log"] = lines[-80:]
        # K-05: the heartbeat, before anything else can fail — any slot, any outcome.
        if not a.dry_run and (state / ".git").exists():
            manifest["heartbeat"] = write_heartbeat(state, manifest["finished"], a.slot, a.desk,
                                                    run_id, outcome)
        if not a.dry_run and outcome != "already_done":
            write_json(manifest_path(state, date, key), manifest)
            if commit_msg:
                try:
                    manifest["git"] = commit_and_push(state, commit_msg, push=not a.no_push, log=log)
                except Failed as exc:
                    log(f"git: {exc}")
                    manifest["git"] = {"committed": False, "error": str(exc)}
                    if outcome == "committed":
                        outcome, reason, code = "failed", f"git: {exc}", 2
                        manifest["outcome"], manifest["reason"] = outcome, reason
                        write_json(manifest_path(state, date, key), manifest)
                if (manifest["git"] or {}).get("push_failed"):
                    # The on-disk manifest carries the push failure; the next run's commit
                    # sweeps it in. A dirty tree here is the documented signal.
                    manifest["push_failed"] = True
                    reason = reason or "push failed; the commit is local"
                    manifest["reason"] = reason
                    write_json(manifest_path(state, date, key), manifest)
        try:
            summary = {k: manifest.get(k) for k in
                       ("run_id", "slot", "step", "desk", "outcome", "reason", "engine_sha",
                        "engine_exit_code", "written", "git", "started", "finished",
                        "desk_results", "scan", "health", "push_failed")}
            summary["exit_code"] = code
            summary["manifest"] = str(manifest_path(state, date, key))
            write_json(inputs / "outcome.json", summary)
        except OSError:
            pass
        print(f"RUNNER {outcome.upper()} {run_id} {a.slot}/{step}/{a.desk}"
              + (f" — {reason}" if reason else "") + f" (exit {code})", flush=True)
        return code

    def record_abort(outcome, reason):
        if a.dry_run:
            return
        try:
            merge_coverage(state, aborted_row(a.slot, step, a.desk, run_id, engine_sha,
                                              stamp(), outcome, reason, key=key))
        except (OSError, ValueError) as exc:
            log(f"could not write the aborted coverage row: {exc}")

    # (b) idempotency — before the lock, before anything touches the tree.
    if not a.dry_run and already_done(state, date, key):
        return finish("already_done", f"manifests/{date}/{key}.json is already committed", 0)
    if not (state / ".git").exists():
        log(f"REFUSED: {state} is not a git repository")
        return finish("refused", f"{state} is not a git repository", 1)

    # (c) the lock. A refusal here is recorded but NOT committed: the run that holds the
    # lock owns the tree and the git index right now, and a second `git commit` racing it
    # would either fail on index.lock or sweep up that run's half-written outputs. The row
    # and the manifest land in the holder's commit, or the next run's.
    lock = Lock(state, stale_min=int(slot_setting(slots, slot_cfg, "lock_stale_min") or 45))
    try:
        lock.acquire()
    except Refused as exc:
        log(f"REFUSED: {exc}")
        record_abort("refused", str(exc))
        return finish("refused", str(exc), 1)

    run_dir = state / RUNS_DIR / run_id
    try:
        if not a.dry_run:
            ensure_state_ignore(state)
        # (a) the input manifest, then the slot window
        ver = verify_inputs(inputs, step, slots, slot_cfg)
        check_window(ver["as_of"], now, a.slot, slot_cfg, slots, session)
        manifest["input_hashes"] = ver["files"]
        manifest["as_of"] = iso(ver["as_of"])
        log(f"inputs verified: {len(ver['files'])} file(s), as_of {manifest['as_of']}")

        # (d) stage
        if run_dir.exists():
            shutil.rmtree(run_dir, ignore_errors=True)
        config_path = a.config or next((p for p in (engine_root.parent / "engine-config.json",
                                                    state / "engine-config.json") if p.exists()), None)
        cfg = desks_config(engine_dir)
        staged = stage_run(engine_dir, state, inputs, run_dir, desks, engine_sha, config_path,
                           only=set(ver["files"]))
        if staged.get("unlisted"):
            log(f"NOT staged (not in the manifest): {staged['unlisted']}")
        manifest["staged"] = staged
        log(f"staged {RUNS_DIR}/{run_dir.name}: books {staged['books']}, inputs {staged['inputs']}"
            + (f", MISSING books {staged['missing_books']}" if staged["missing_books"] else ""))
        if step in ("pm", "sentinel", "watch") and staged["missing_books"]:
            raise Refused(f"no stored book for desk(s) {staged['missing_books']} in "
                          f"{state / 'books'} — PM.md section 9: nothing else is safe")

        # (e) the engine, (f) the write-back
        ctx = Ctx(run_dir, sys.executable, log, now_override=a.now)
        written = []
        failed_desks = []
        if step in ("pm", "sentinel"):
            results = pm_steps(ctx, a.slot if step == "pm" else "sentinel", desks, cfg, state)
            failed_desks = [r for r in results if r["exit_code"] != 0]
            manifest["engine_exit_code"] = max((r["exit_code"] for r in results), default=0)
            manifest["desk_results"] = [{k: v for k, v in r.items() if k != "state"} for r in results]
            if not a.dry_run:
                written += write_back_pm(state, run_dir, run_id, results, cfg)
                merge_coverage(state, coverage_row_from_results(results, a.slot, run_id, engine_sha,
                                                                stamp(), step, key=key))
                written.append("coverage/pm-coverage.json")
        elif step == "scan":
            scan = scan_steps(ctx, slot_cfg, now.strftime("%Y-%m-%d"))
            manifest["engine_exit_code"] = 0
            manifest["scan"] = scan
            if not a.dry_run:
                written += write_back_scan(state, run_dir, run_id, scan)
        elif step == "watch":
            results = watch_steps(ctx, session, desks, cfg)
            manifest["desk_results"] = results
            failed_desks = [r for r in results if r.get("exit_code") not in (0, None)]
            manifest["engine_exit_code"] = max((r.get("exit_code") or 0 for r in results), default=0)
            if not a.dry_run:
                written += write_back_watch(state, run_dir, results)
                row = coverage_row_from_results([], a.slot, run_id, engine_sha, stamp(), step, key=key)
                row["session"] = session
                row["desks"] = {r["desk"]: {"quiet": r.get("quiet"), "wrote": r.get("wrote"),
                                            "exit_code": r.get("exit_code"), "read_only": True}
                                for r in results}
                merge_coverage(state, row)
                written.append("coverage/pm-coverage.json")
        elif step == "health":
            doc = health_steps(ctx, state, engine_root, engine_sha, engine_branch, now, slots,
                               mirror=a.mirror, slots_path=a.slots, config_path=config_path)
            manifest["engine_exit_code"] = HEALTH_STATUS_RANK.get(doc["verdict"], 2)
            manifest["health"] = {c["name"]: c["status"] for c in doc["checks"]}
            manifest["health_verdict"] = doc["verdict"]
            if not a.dry_run:
                write_json(state / "health" / f"{date}.json", doc)
                written.append(f"health/{date}.json")
                md = run_dir / "health.md"
                if md.exists():
                    shutil.copyfile(md, state / "health" / f"{date}.md")
                    written.append(f"health/{date}.md")
            flagged = [f"{c['name']} ({c['status']})" for c in doc["checks"] if c["status"] != "pass"]
            log(f"health: {doc['verdict'].upper()}" + (" — " + ", ".join(flagged) if flagged else ""))
        manifest["steps"] = [{k: v for k, v in s.items() if k not in ("stdout", "stderr")}
                             for s in ctx.steps]
        manifest["written"] = written
        if failed_desks:
            names = ", ".join(f"{r['desk']} (exit {r.get('exit_code')})" for r in failed_desks)
            raise Failed(f"engine failed for {names}; the other desks were written back")
        if a.dry_run:
            log("dry run: nothing written back, nothing committed")
            return finish("dry_run", None, 0)
        archive_logs(state, run_dir, run_id)
        code = finish("committed", None, 0,
                      commit_msg=f"{a.slot} {a.desk} {date} {run_id} engine {engine_sha or 'unknown'}")
        if code == 0 and not a.keep_run_dir:
            shutil.rmtree(run_dir, ignore_errors=True)
        return code
    except Refused as exc:
        log(f"REFUSED: {exc}")
        record_abort("refused", str(exc))
        return finish("refused", str(exc), 1, commit_msg=f"{a.slot} {a.desk} {date} {run_id} REFUSED")
    except Failed as exc:
        log(f"FAILED: {exc}")
        if step not in ("pm", "sentinel") or "desk_results" not in manifest:
            record_abort("failed", str(exc))     # pm/sentinel rows were merged above
        if not a.dry_run and run_dir.exists():
            archive_logs(state, run_dir, run_id)
        return finish("failed", str(exc), 2, commit_msg=f"{a.slot} {a.desk} {date} {run_id} FAILED")
    except Exception as exc:          # a runner bug is still a failed run, on the record
        log(f"FAILED (runner): {type(exc).__name__}: {exc}")
        record_abort("failed", f"runner error: {type(exc).__name__}: {exc}")
        return finish("failed", f"runner error: {type(exc).__name__}: {exc}", 2,
                      commit_msg=f"{a.slot} {a.desk} {date} {run_id} FAILED")
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
