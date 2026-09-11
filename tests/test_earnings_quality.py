"""earnings_quality.py — the E22 / E23 features (P-06), each pinned to a hand-built input:
SUE on a synthetic 8-quarter history, EAR on synthetic bars against SPY, the REG residual
on a cross-section built as EAR = 2×SUE + noise, the agreement gate, nulls when the inputs
are short, the features on scanned rows, and the golden unchanged without the map.
"""
import datetime as dt
import json
import math
import random

import pytest

from conftest import FIX, ROOT, ENGINE

import earnings_quality as eq


AS_OF = "2026-09-10"


def _hist(surprises, last_date=dt.date(2026, 8, 26), estimate=1.0, timing=None):
    """n quarters ending on last_date, 91 days apart, oldest first, with the given
    actual − estimate surprises."""
    out = []
    n = len(surprises)
    for i, s in enumerate(surprises):
        d = last_date - dt.timedelta(days=91 * (n - 1 - i))
        out.append({"fiscal_quarter": f"FY{i}", "report_date": d.isoformat(), "timing": timing,
                    "eps_actual": estimate + s, "eps_estimate": estimate,
                    "surprise_pct": s / estimate * 100.0})
    return out


def _bars(daily_returns, start=dt.date(2026, 7, 1), first_close=100.0):
    """Weekday bars, oldest first, from a list of daily simple returns."""
    bars, d, c = [], start, first_close
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    bars.append({"begins_at": d.isoformat(), "open_price": c, "high_price": c, "low_price": c,
                 "close_price": c, "volume": 1000})
    for r in daily_returns:
        d += dt.timedelta(days=1)
        while d.weekday() >= 5:
            d += dt.timedelta(days=1)
        c = c * (1.0 + r)
        bars.append({"begins_at": d.isoformat(), "open_price": c, "high_price": c,
                     "low_price": c, "close_price": c, "volume": 1000})
    return bars


# ------------------------------------------------------------------ SUE
def test_sue_is_the_latest_surprise_over_the_std_of_the_last_eight():
    s = [0.10, -0.05, 0.02, 0.08, -0.01, 0.03, 0.06, 0.12]
    sd = math.sqrt(sum((x - sum(s) / 8) ** 2 for x in s) / 7)
    assert eq.sue(_hist(s), AS_OF) == pytest.approx(0.12 / sd)


def test_sue_uses_only_the_last_eight_of_a_longer_history():
    s = [5.0, 5.0] + [0.10, -0.05, 0.02, 0.08, -0.01, 0.03, 0.06, 0.12]
    assert eq.sue(_hist(s), AS_OF) == pytest.approx(eq.sue(_hist(s[2:]), AS_OF))


def test_sue_is_null_under_four_quarters_or_with_a_zero_std():
    assert eq.sue(_hist([0.1, 0.2, 0.3]), AS_OF) is None
    assert eq.sue(_hist([0.1, 0.1, 0.1, 0.1]), AS_OF) is None
    assert eq.sue(_hist([0.1, 0.2, 0.1, 0.3]), AS_OF) is not None


def test_sue_ignores_quarters_after_as_of_and_unreported_ones():
    h = _hist([0.1, -0.1, 0.2, 0.05, 0.3], last_date=dt.date(2026, 11, 18))
    assert eq.sue(h, AS_OF) == pytest.approx(eq.sue(h[:-1], AS_OF))
    h2 = _hist([0.1, -0.1, 0.2, 0.05]) + [{"report_date": "2026-11-18", "eps_estimate": 1.1,
                                          "eps_actual": None}]
    assert eq.sue(h2, "2026-12-01") == pytest.approx(eq.sue(h2[:-1], AS_OF))
    assert eq.days_since_earnings(h2, "2026-12-01") == eq.sessions_between(dt.date(2026, 8, 26),
                                                                            dt.date(2026, 12, 1))


# ------------------------------------------------------------------ EAR
def test_ear_is_the_three_session_car_against_spy():
    spy_r = [0.001] * 30
    stock_r = list(spy_r)
    spy = _bars(spy_r)
    # returns[i] is bar i+1's return. Event session t = bar 15; the stock's returns on
    # bars 14, 15, 16 (t−1, t, t+1) are +2%, +5%, −1%
    stock_r[13], stock_r[14], stock_r[15] = 0.02, 0.05, -0.01
    stock = _bars(stock_r)
    event_date = stock[15]["begins_at"]
    want = (0.02 - 0.001) + (0.05 - 0.001) + (-0.01 - 0.001)
    assert eq.ear(stock, event_date, spy) == pytest.approx(want)


def test_an_after_close_print_shifts_the_event_to_the_next_session():
    spy = _bars([0.0] * 30)
    stock_r = [0.0] * 30
    stock_r[15] = 0.10                          # bar 16 moves: the session AFTER bar 15
    stock = _bars(stock_r)
    report_date = stock[15]["begins_at"]
    # pm: t = bar 16, window 15..17 — includes it. am: t = bar 15, window 14..16 — also does.
    assert eq.ear(stock, report_date, spy, timing="pm") == pytest.approx(0.10)
    assert eq.ear(stock, report_date, spy, timing="am") == pytest.approx(0.10)
    stock_r2 = [0.0] * 30
    stock_r2[16] = 0.10                         # bar 17: outside the am window, inside pm
    assert eq.ear(_bars(stock_r2), report_date, spy, timing="am") == pytest.approx(0.0)
    assert eq.ear(_bars(stock_r2), report_date, spy, timing="pm") == pytest.approx(0.10)


def test_a_report_date_on_a_weekend_maps_to_the_next_session():
    spy = _bars([0.0] * 30)
    stock_r = [0.0] * 30
    stock_r[15] = 0.03
    stock = _bars(stock_r)
    d15 = dt.date.fromisoformat(stock[15]["begins_at"])
    sunday = d15 - dt.timedelta(days=d15.weekday() + 1)          # the Sunday before bar 15
    monday = sunday + dt.timedelta(days=1)
    # the first session on or after that Sunday is that Monday, and the window is the same
    assert eq.ear(stock, sunday.isoformat(), spy) == pytest.approx(eq.ear(stock, monday.isoformat(), spy))
    assert eq.ear(stock, sunday.isoformat(), spy) is not None


def test_ear_is_null_without_spy_or_without_bars_through_t_plus_one():
    stock = _bars([0.01] * 30)
    assert eq.ear(stock, stock[15]["begins_at"], None) is None
    spy = _bars([0.0] * 30)
    assert eq.ear(stock, stock[-1]["begins_at"], spy) is None        # t+1 does not exist
    assert eq.ear(stock, stock[1]["begins_at"], spy) is None         # t−2 does not exist
    assert eq.ear(stock, "2027-01-01", spy) is None                  # after the bars


def test_ear_aligns_by_date_not_position():
    spy = _bars([0.0] * 30)
    stock_r = [0.0] * 30
    stock_r[15] = 0.04
    stock = _bars(stock_r)
    event = stock[15]["begins_at"]
    spy_missing = [b for b in spy if b["begins_at"] != stock[5]["begins_at"]]
    assert eq.ear(stock, event, spy_missing) == pytest.approx(0.04)


# ------------------------------------------------------------------ REG residual
def test_reg_residual_recovers_the_noise_when_ear_is_two_sue_plus_noise():
    rng = random.Random(7)
    cross = []
    noise = []
    for _ in range(40):
        s = rng.gauss(0, 1.5)
        e = rng.gauss(0, 0.01)
        noise.append(e)
        cross.append((s, 0.005 + 2.0 * s + e))
    # the fitted line is a + 2·sue; the residual is the noise, up to sampling error
    for (s, ear_v), e in zip(cross, noise):
        got = eq.reg_residual(ear_v, s, cross)
        assert got == pytest.approx(e, abs=0.006)
    # a name whose gap is far beyond its surprise: a large positive residual
    assert eq.reg_residual(0.20, 1.0, cross) == pytest.approx(0.20 - 0.005 - 2.0, abs=0.01)


def test_reg_residual_is_null_with_thin_or_degenerate_inputs():
    cross = [(1.0, 0.02), (2.0, 0.04), (3.0, 0.06), (4.0, 0.08)]      # 4 < MIN_CROSS_SECTION
    assert eq.reg_residual(0.02, 1.0, cross) is None
    flat = [(1.0, 0.02 * i) for i in range(6)]                       # constant regressor
    assert eq.reg_residual(0.02, 1.0, flat) is None
    ok = [(float(i), 0.02 * i) for i in range(6)]
    assert eq.reg_residual(None, 1.0, ok) is None
    assert eq.reg_residual(0.02, None, ok) is None
    assert eq.reg_residual(0.02, 1.0, ok + [(None, 0.01), (1.0, None)]) == pytest.approx(0.0, abs=1e-12)


# ------------------------------------------------------------------ agreement
@pytest.mark.parametrize("s,e,want", [
    (1.5, 0.05, 1), (1.0, 0.02, 1), (-1.2, -0.03, -1), (-1.0, -0.02, -1),
    (1.5, -0.05, 0), (0.5, 0.05, 0), (1.5, 0.01, 0), (-1.5, 0.05, 0),
    (None, 0.05, None), (1.5, None, None),
])
def test_agreement_is_same_sign_high_high(s, e, want):
    assert eq.agreement(s, e) == want


# ------------------------------------------------------------------ build / nulls
def test_features_are_null_when_the_inputs_are_absent():
    out = eq.build({"XYZ": []}, {}, AS_OF)
    assert set(out["XYZ"]) == set(eq.FEATURE_KEYS)
    assert all(v is None for v in out["XYZ"].values())
    out = eq.build({"XYZ": _hist([0.1, -0.1, 0.2, 0.05])}, {}, AS_OF)     # no bars at all
    f = out["XYZ"]
    assert f["sue"] is not None and f["days_since_earnings"] is not None
    assert f["ear_3d"] is None and f["reg_residual"] is None and f["earnings_agreement"] is None


def test_build_computes_the_cross_section_over_the_whole_universe():
    rng = random.Random(3)
    hist, bars = {}, {}
    spy_r = [0.0] * 40
    bars["SPY"] = _bars(spy_r)
    event_idx = 20
    event_date = bars["SPY"][event_idx]["begins_at"]
    for i in range(8):
        sym = f"S{i}"
        surprises = [rng.gauss(0, 0.05) for _ in range(7)] + [0.05 * (i - 3)]
        hist[sym] = _hist(surprises, last_date=dt.date.fromisoformat(event_date))
        r = [0.0] * 40
        r[event_idx] = 0.01 * (i - 3)
        bars[sym] = _bars(r)
    out = eq.build(hist, bars, AS_OF)
    assert all(out[s]["ear_3d"] == pytest.approx(0.01 * (i - 3)) for i, s in
               enumerate(f"S{i}" for i in range(8)))
    assert all(out[s]["reg_residual"] is not None for s in out)
    assert all(out[s]["days_since_earnings"] == eq.sessions_between(
        dt.date.fromisoformat(event_date), dt.date.fromisoformat(AS_OF)) for s in out)
    # one name's residual is its ear minus the pooled fit at its sue
    cross = [(out[s]["sue"], out[s]["ear_3d"]) for s in out]
    assert out["S0"]["reg_residual"] == pytest.approx(
        eq.reg_residual(out["S0"]["ear_3d"], out["S0"]["sue"], cross))


# ------------------------------------------------------------------ the mapping
def test_the_connector_shape_maps_onto_the_file_shape():
    rows = [{"symbol": "NVDA", "year": 2026, "quarter": 2,
             "eps": {"estimate": "1.01", "actual": "1.05"},
             "report": {"date": "2026-08-26", "timing": "pm", "verified": True},
             "revenue": {"estimate": 46.0e9, "actual": 46.7e9}},
            {"symbol": "NVDA", "year": 2026, "quarter": 3,
             "eps": {"estimate": "1.20", "actual": None},
             "report": {"date": "2026-11-18", "timing": "pm", "verified": False}},
            {"year": 2025, "quarter": 4, "eps": {"estimate": 1, "actual": 1},
             "report": {"date": None}}]
    got = eq.from_connector(rows)
    assert [q["fiscal_quarter"] for q in got] == ["2026Q2", "2026Q3"]
    q = got[0]
    assert q["report_date"] == "2026-08-26" and q["timing"] == "pm"
    assert q["eps_actual"] == 1.05 and q["eps_estimate"] == 1.01
    assert q["surprise_pct"] == pytest.approx((1.05 - 1.01) / 1.01 * 100)
    assert q["revenue_actual"] == 46.7e9
    assert got[1]["eps_actual"] is None
    # rows already in the file shape pass through, and a supplied surprise_pct is kept
    again = eq.from_connector(got + [{"fiscal_quarter": "2026Q1", "report_date": "2026-05-20",
                                      "eps_actual": 0.9, "eps_estimate": 0.8, "surprise_pct": 99.0}])
    assert [q["fiscal_quarter"] for q in again] == ["2026Q1", "2026Q2", "2026Q3"]
    assert again[0]["surprise_pct"] == 99.0


def test_bars_map_accepts_the_payload_the_results_array_and_a_map():
    payload = {"data": {"results": [{"symbol": "spy", "bars": [1]}, {"symbol": "A", "bars": []}]}}
    assert eq.bars_map_of(payload) == {"SPY": [1], "A": []}
    assert eq.bars_map_of(payload["data"]["results"]) == {"SPY": [1], "A": []}
    assert eq.bars_map_of({"a": [2]}) == {"A": [2]}


# ------------------------------------------------------------------ the CLI and the scanner
@pytest.fixture
def scanner(run_dir):
    import importlib
    import scanner as s
    importlib.reload(s)
    return s


@pytest.fixture
def scan_data():
    return json.loads((FIX / "scan_data.json").read_text(encoding="utf-8"))


def test_the_golden_is_unchanged_without_the_map(scanner, scan_data):
    got = json.loads(json.dumps(scanner.scan(scan_data), sort_keys=True))
    want = json.loads((FIX / "scan_results_golden.json").read_text(encoding="utf-8"))
    assert got == want
    assert "earnings_quality" not in got["meta"]


def test_the_features_ride_on_the_row_and_are_null_for_uncovered_names(scanner, scan_data):
    m = {"NVDA": {"sue": 1.7, "ear_3d": 0.04, "reg_residual": 0.01, "earnings_agreement": 1,
                  "days_since_earnings": 11}}
    base = scanner.scan(json.loads(json.dumps(scan_data)))
    out = scanner.scan(json.loads(json.dumps(scan_data)), earnings_quality=m)
    by = {r["ticker"]: r for r in out["results"]}
    assert by["NVDA"]["features"]["sue"] == 1.7 and by["NVDA"]["features"]["earnings_agreement"] == 1
    assert all(by["MU"]["features"][k] is None for k in scanner.EARNINGS_QUALITY_KEYS)
    assert out["meta"]["earnings_quality"]["symbols_with_data"] == ["NVDA"]
    # scores, verdicts and every non-feature field are untouched
    for r0, r in zip(base["results"], out["results"]):
        assert {k: v for k, v in r.items() if k != "features"} == {k: v for k, v in r0.items() if k != "features"}


def test_the_technicals_features_survive_the_merge(scanner, scan_data):
    scan_data["candidates"]["NVDA"]["features"] = {"ret_12_7": 0.3}
    out = scanner.scan(scan_data, earnings_quality={"NVDA": {"sue": 2.0}})
    f = next(r for r in out["results"] if r["ticker"] == "NVDA")["features"]
    assert f["ret_12_7"] == 0.3 and f["sue"] == 2.0 and f["ear_3d"] is None


def test_the_cli_writes_the_map_the_scanner_reads(run_dir, monkeypatch, capsys):
    hist = {"XYZ": _hist([0.1, -0.05, 0.02, 0.08, -0.01, 0.03, 0.06, 0.12],
                         last_date=dt.date(2026, 8, 26), timing="pm")}
    (run_dir / "earnings_history.json").write_text(json.dumps(hist), encoding="utf-8")
    spy = _bars([0.0] * 50, start=dt.date(2026, 7, 1))
    stock = _bars([0.0] * 50, start=dt.date(2026, 7, 1))
    (run_dir / "bars.json").write_text(json.dumps(
        {"data": {"results": [{"symbol": "SPY", "bars": spy}, {"symbol": "XYZ", "bars": stock}]}}),
        encoding="utf-8")
    import importlib
    importlib.reload(eq)
    rc = eq.main(["--history", str(run_dir / "earnings_history.json"),
                  "--bars", str(run_dir / "bars.json"), "--as-of", AS_OF,
                  "--out", "earnings_quality.json"])
    assert rc == 0
    got = json.loads((run_dir / "earnings_quality.json").read_text(encoding="utf-8"))
    assert set(got["XYZ"]) == set(eq.FEATURE_KEYS)
    assert got["XYZ"]["sue"] is not None and got["XYZ"]["ear_3d"] == pytest.approx(0.0)
    assert got["XYZ"]["reg_residual"] is None        # one name is not a cross-section
    assert "1 symbol(s)" in capsys.readouterr().out


def test_the_manifest_and_ci_allowlist_carry_the_module():
    assert "earnings_quality.py" in (ENGINE / "MANIFEST.txt").read_text(encoding="utf-8").split()
    assert '"earnings_quality"' in (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
