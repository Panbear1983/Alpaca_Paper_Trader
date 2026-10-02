"""Per-wallet code ceilings — no network.

High Risk is boxed at 25% per name and 1.0x gross no matter what the strategy
file says; Low Risk keeps the old 1.0 / 2.0 ceilings. Photonic CPO ETF now has
its own Wanna_buffet mandate ceilings, covered by test_wb_integration.py.
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


def test_low_risk_keeps_the_old_ceilings(monkeypatch):
    _use(monkeypatch, "Low Risk", {"max_position_pct": 1.0, "max_gross_exposure": 2.0})
    assert cc._max_position_pct() == 1.0
    assert cc._max_gross_exposure() == 2.0


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


# ── the ceiling binds buys, never sells (2026-09-22 fix) ─────────────────────

def _book(equity=71_000):
    """TSLA 43%, NVDA 29% of equity — the real High Risk book on 2026-09-22."""
    return [{"symbol": "TSLA", "market_value": str(equity * 0.43), "current_price": "340", "qty": "82.5",
             "qty_available": "82.5"},
            {"symbol": "NVDA", "market_value": str(equity * 0.29), "current_price": "220", "qty": "92.3",
             "qty_available": "92.3"}]


def _trim(monkeypatch, wallet, risk):
    _use(monkeypatch, wallet, risk)
    monkeypatch.setattr(cc, "_sellable_qty", lambda p: float(p["qty"]))
    monkeypatch.setattr(cc, "place_market_order", lambda *a, **k: (_ for _ in ()).throw(AssertionError("order sent")))
    acts = []
    n = cc._trim_to_caps(_book(), 71_000, True, acts)
    return n, acts


def test_landing_the_code_ceiling_trims_nothing_by_itself(monkeypatch):
    n, acts = _trim(monkeypatch, "High Risk", {"max_position_pct": 1.0, "max_gross_exposure": 2.0})
    assert n == 0 and acts == []


def test_setting_the_config_to_the_ceiling_is_what_trims(monkeypatch):
    n, acts = _trim(monkeypatch, "High Risk", {"max_position_pct": 0.25, "max_gross_exposure": 1.0})
    assert n == 2
    assert any("TSLA" in a for a in acts) and any("NVDA" in a for a in acts)


def test_benchmark_wallet_trim_behaviour_unchanged(monkeypatch):
    n, acts = _trim(monkeypatch, "Low Risk", {"max_position_pct": 1.0, "max_gross_exposure": 2.0})
    assert n == 0 and acts == []
    n, acts = _trim(monkeypatch, "Low Risk", {"max_position_pct": 0.30, "max_gross_exposure": 2.0})
    assert n == 1 and "TSLA" in acts[0]


def test_the_agents_wallet_has_no_code_ceiling(monkeypatch):
    """Peter, 2026-10-02: no guardrail from outside on the wallet the agent trades
    by itself — its own file is the only limit (here 1.0 / 2.0, the old defaults)."""
    for w in ("Wanna_Buffet Auto Trading", "Photonic CPO ETF"):
        assert cc.hard_limits_for(w) is None
        _use(monkeypatch, w, {"max_position_pct": 1.0, "max_gross_exposure": 2.0})
        assert cc._max_position_pct() == 1.0, w
        assert cc._max_gross_exposure() == 2.0, w
