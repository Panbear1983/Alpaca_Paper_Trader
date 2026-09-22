"""Step 10 — strategy-tab rows for the guardrails and the hard-limits header. No network."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config_fields as cf  # noqa: E402
import i18n  # noqa: E402

NEW_ROWS = ["dynamic_exits.stop_atr_mult", "dynamic_exits.stop_min_pct", "dynamic_exits.stop_max_pct",
            "anchor.core_holds", "anchor.earnings_warn_days", "anchor.cooling_off_pct",
            "anchor.cooling_off_days", "anchor.max_open_names", "anchor.min_cash_pct",
            "price_watcher.buyback_mode", "dynamic_exits.max_holdings"]


def test_every_guardrail_row_is_registered_with_a_chinese_label():
    for path in NEW_ROWS:
        f = cf.by_path(path)
        assert f is not None, path
        assert path in i18n._FIELD_ZH, path


def test_width_rows_accept_sane_values_and_refuse_nonsense():
    mult, lo, hi = (cf.by_path(p) for p in NEW_ROWS[:3])
    assert cf.validate(mult, "2.5") == (True, 2.5)
    assert cf.validate(lo, "0.06") == (True, 0.06)
    assert cf.validate(hi, "0.14") == (True, 0.14)
    assert cf.validate(mult, "9")[0] is False
    assert cf.validate(lo, "0.5")[0] is False


def test_floor_and_ceiling_stay_in_order():
    cfg = {"dynamic_exits": {"stop_min_pct": 0.06, "stop_max_pct": 0.14}}
    lo, hi = cf.by_path("dynamic_exits.stop_min_pct"), cf.by_path("dynamic_exits.stop_max_pct")
    assert cf.enable_blocked_reason(cfg, lo, 0.10) is None
    assert "above the ceiling" in cf.enable_blocked_reason(cfg, lo, 0.15)
    assert cf.enable_blocked_reason(cfg, hi, 0.20) is None
    assert "below the floor" in cf.enable_blocked_reason(cfg, hi, 0.05)
    # a wallet that never had the keys is not blocked (nothing to compare with)
    assert cf.enable_blocked_reason({}, lo, 0.15) is None


def test_benchmark_wallets_show_a_dash_for_every_guardrail_row():
    for path in NEW_ROWS[:3] + NEW_ROWS[5:9]:
        f = cf.by_path(path)
        assert cf.fmt_value(f, cf.get_path({}, path), {}, compact=True) == "—", path


def test_hard_limits_header():
    t = lambda key, **kw: i18n.t(key, **kw)  # noqa: E731
    line = cf.hard_limits_text({"position_pct": 0.25, "gross": 1.0}, True, t)
    assert "25%" in line and "1.0x" in line and "borrowing none" in line and line.endswith("stops on")
    assert cf.hard_limits_text({"position_pct": 0.25, "gross": 1.0}, False, t).endswith("stops off")
    assert cf.hard_limits_text(None, True, t) is None


def test_header_and_refusal_texts_exist_in_both_languages():
    for key in ("cfg.hard", "cfg.refused"):
        en, zh = i18n._TABLE[key]
        assert en and zh
