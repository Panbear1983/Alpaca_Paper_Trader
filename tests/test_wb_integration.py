"""The agent's wallet has no outside code ceiling (Peter, 2026-10-02); offline only."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import capitol_copier as cc
import wallets


def test_photonic_mandate_has_non_overridable_code_ceilings(monkeypatch):
    # 2026-10-02, Peter: no guardrail from outside on the wallet the agent trades by itself.
    # Its caps live in its own strategy file (risk block), which it may change.
    assert cc.hard_limits_for('Photonic CPO ETF') is None
    assert cc.hard_limits_for('Wanna_Buffet Auto Trading') is None
    monkeypatch.setattr(wallets, 'current', lambda: 'Photonic CPO ETF')
    monkeypatch.setattr(cc, 'load_config', lambda: {
        'risk': {'max_position_pct': 1.0, 'max_gross_exposure': 4.0,
                 'gross_by_regime': {'bull': 4.0}}})
    monkeypatch.setattr(cc, 'current_regime', lambda: 'bull')
    # Only the old module-wide defaults (1.0 / 2.0) still clamp — the agent's file is the limit.
    assert cc._max_position_pct() == 1.0
    assert cc._max_gross_exposure() == 2.0
