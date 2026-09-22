"""Per-wallet code ceilings — no network.

High Risk is boxed at 25% per name and 1.0x gross no matter what the strategy
file says; the two benchmark wallets keep the old 1.0 / 2.0 ceilings so their
behaviour is byte-for-byte what it was before 2026-09-22.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import capitol_copier as cc  # noqa: E402
import wallets  # noqa: E402


def _use(monkeypatch, wallet, risk):
    monkeypatch.setattr(wallets, "current", lambda: wallet)
    monkeypatch.setattr(cc, "load_config", lambda: {"risk": risk})
    monkeypatch.setattr(cc, "current_regime", lambda: "unknown")


def test_high_risk_per_name_cap_cannot_exceed_25_pct(monkeypatch):
    _use(monkeypatch, "High Risk", {"max_position_pct": 1.0})
    assert cc._max_position_pct() == 0.25


def test_high_risk_may_be_set_lower_than_the_ceiling(monkeypatch):
    _use(monkeypatch, "High Risk", {"max_position_pct": 0.10})
    assert cc._max_position_pct() == 0.10


def test_high_risk_never_borrows(monkeypatch):
    _use(monkeypatch, "High Risk", {"max_gross_exposure": 2.0})
    assert cc._max_gross_exposure() == 1.0


def test_high_risk_regime_scaling_cannot_lift_above_1x(monkeypatch):
    _use(monkeypatch, "High Risk", {"max_gross_exposure": 2.0,
                                    "gross_by_regime": {"bull": 1.5}})
    monkeypatch.setattr(cc, "current_regime", lambda: "bull")
    assert cc._max_gross_exposure() == 1.0


def test_benchmark_wallets_keep_the_old_ceilings(monkeypatch):
    for w in ("Low Risk", "Photonic CPO ETF"):
        _use(monkeypatch, w, {"max_position_pct": 1.0, "max_gross_exposure": 2.0})
        assert cc._max_position_pct() == 1.0, w
        assert cc._max_gross_exposure() == 2.0, w


def test_benchmark_wallet_regime_scaling_unchanged(monkeypatch):
    _use(monkeypatch, "Low Risk", {"max_gross_exposure": 2.0,
                                   "gross_by_regime": {"bull": 1.5}})
    monkeypatch.setattr(cc, "current_regime", lambda: "bull")
    assert cc._max_gross_exposure() == 1.5


def test_nonsense_config_falls_back_to_the_wallets_ceiling(monkeypatch):
    _use(monkeypatch, "High Risk", {"max_position_pct": "abc"})
    assert cc._max_position_pct() == 0.25
    _use(monkeypatch, "Low Risk", {"max_position_pct": -3})
    assert cc._max_position_pct() == 1.0


def test_hard_limits_for_exposes_rows_for_the_tui():
    assert cc.hard_limits_for("High Risk") == {"position_pct": 0.25, "gross": 1.0}
    assert cc.hard_limits_for("Low Risk") is None
