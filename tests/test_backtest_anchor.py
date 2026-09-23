"""backtest_anchor — the pure pieces, on synthetic bars. No network."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backtest_anchor as ba  # noqa: E402


def _bar(d, o, h, l, c):
    return {"t": f"{d}T04:00:00Z", "o": o, "h": h, "l": l, "c": c}


def _flat(n, px=100.0, start=1):
    return [_bar(f"2026-01-{start + k:02d}", px, px, px, px) for k in range(n)]


def test_stop_width_clamps_and_defaults_wide():
    assert ba.stop_width(0.02, 2.5, 0.06, 0.14) == 0.06
    assert ba.stop_width(0.04, 2.5, 0.06, 0.14) == 0.10
    assert ba.stop_width(0.10, 2.5, 0.06, 0.14) == 0.14
    assert ba.stop_width(None, 2.5, 0.06, 0.14) == 0.14


def test_atr_pct_is_mean_true_range_over_close():
    bars = [_bar("2026-01-01", 100, 100, 100, 100)] + [_bar(f"2026-01-{k:02d}", 100, 102, 98, 100) for k in range(2, 17)]
    assert abs(ba.atr_pct(bars, len(bars) - 1, 14) - 0.04) < 1e-9
    assert ba.atr_pct(bars, 5, 14) is None


def test_stop_fill_gap_and_touch():
    assert ba.stop_fill(_bar("d", 95, 96, 94, 95), 98) == 95        # gapped below → open
    assert ba.stop_fill(_bar("d", 100, 101, 97, 99), 98) == 98      # touched → stop price
    assert ba.stop_fill(_bar("d", 100, 101, 99, 100), 98) is None   # untouched


def _pullback_series():
    """20 flat sessions at 100, then a slide to 92 and a bounce day."""
    bars = _flat(20)
    bars += [_bar("2026-02-01", 98, 98, 94, 94), _bar("2026-02-02", 93, 93, 90, 91)]   # low 90
    bars += [_bar("2026-02-03", 91, 93, 91, 92.5)]                                     # bounce: 92.5/90 = +2.8%, up day, 7.5% under the high
    return bars


def test_signal_fires_on_the_bounce_day_only():
    bars = _pullback_series()
    assert ba.signal(bars, len(bars) - 1, 20, 0.05, 0.015) is not None
    assert ba.signal(bars, len(bars) - 2, 20, 0.05, 0.015) is None       # still falling
    assert ba.signal(bars, 19, 20, 0.05, 0.015) is None                  # no pullback yet


def test_signal_needs_the_pullback_depth_and_an_up_day():
    bars = _pullback_series()
    assert ba.signal(bars, len(bars) - 1, 20, 0.10, 0.015) is None       # only 7.5% under the high
    bars[-1] = _bar("2026-02-03", 91, 93, 91, 90.5)                       # down day
    assert ba.signal(bars, len(bars) - 1, 20, 0.05, 0.015) is None


def test_simulate_respects_the_box():
    """Two names signal on the same day; max_names=1 lets only one in, and
    the cash floor caps the size."""
    a = _pullback_series()
    b = [dict(x) for x in a]
    dates = [x["t"][:10] for x in a] + ["2026-02-04", "2026-02-05"]
    a += [_bar("2026-02-04", 93, 95, 93, 95), _bar("2026-02-05", 95, 96, 94, 96)]
    b += [_bar("2026-02-04", 93, 95, 93, 95), _bar("2026-02-05", 95, 96, 94, 96)]
    series = {"AAA": {x["t"][:10]: x for x in a}, "BBB": {x["t"][:10]: x for x in b},
              "QQQ": {d: _bar(d, 1, 1, 1, 1) for d in dates}}
    p = dict(ba.DEFAULTS, max_names=1, size=0.90, name_cap=0.90, cash_floor=0.20, lookback=20, atr_days=14)
    curve, trades = ba.simulate(series, dates, ["AAA", "BBB"], p)
    # one entry only, filled at 2026-02-04's open, sized to leave the 20% cash floor
    assert curve[-1][2] > 0.70 and curve[-1][2] <= 0.81
    assert trades == []                                                   # nothing stopped out yet
    # with the real 25% name cap the same entry is capped at a quarter of equity
    p = dict(ba.DEFAULTS, max_names=1, size=0.90, cash_floor=0.20)
    curve, _ = ba.simulate(series, dates, ["AAA", "BBB"], p)
    assert 0.20 < curve[-1][2] <= 0.26
