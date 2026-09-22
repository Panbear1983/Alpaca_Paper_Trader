"""place_market_order releases the broker guard before a sell — no network.

Everything is faked: the guard module, the position lookup, and the POST.
What must hold: sells on a boxed wallet release first and clamp to what is
available; buys never touch the guard; wallets without guards are untouched.
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import capitol_copier as cc  # noqa: E402


class _Resp:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return dict(self.payload)


def _wire(monkeypatch, *, enabled, qty_available="0.87"):
    calls = {"released": [], "posted": []}
    fake = types.ModuleType("broker_stops")
    fake.enabled = lambda: enabled
    fake.release = lambda sym: calls["released"].append(sym) or True
    monkeypatch.setitem(sys.modules, "broker_stops", fake)

    for gate in ("check_position_cap", "check_gross_cap", "_entry_gate"):
        monkeypatch.setattr(cc, gate, lambda *a, **k: (True, ""))
    monkeypatch.setattr(cc, "get_position", lambda sym: {"symbol": sym, "qty_available": qty_available})

    def post(url, headers=None, json=None, **kw):
        calls["posted"].append(json)
        return _Resp({"id": "o1", "client_order_id": json["client_order_id"]})
    monkeypatch.setattr(cc.requests, "post", post)
    return calls


def test_sell_on_boxed_wallet_releases_then_clamps_to_available(monkeypatch):
    calls = _wire(monkeypatch, enabled=True, qty_available="0.87")
    res = cc.place_market_order("NVDA", "sell", qty=92.87)
    assert res.get("id") == "o1"
    assert calls["released"] == ["NVDA"]
    assert calls["posted"][0]["qty"] == "0.87"      # only what the guard left free


def test_sell_keeps_full_qty_when_everything_is_free(monkeypatch):
    calls = _wire(monkeypatch, enabled=True, qty_available="92.87")
    cc.place_market_order("NVDA", "sell", qty=92.87)
    assert calls["posted"][0]["qty"] == "92.87"


def test_buy_never_touches_the_guard(monkeypatch):
    calls = _wire(monkeypatch, enabled=True)
    cc.place_market_order("NVDA", "buy", notional=500)
    assert calls["released"] == []
    assert calls["posted"][0]["notional"] == "500"


def test_unboxed_wallet_sells_exactly_as_before(monkeypatch):
    calls = _wire(monkeypatch, enabled=False, qty_available="0.87")
    cc.place_market_order("VOO", "sell", qty=10)
    assert calls["released"] == []
    assert calls["posted"][0]["qty"] == "10"        # no clamp, no release


def test_release_failure_does_not_block_the_sell(monkeypatch):
    calls = _wire(monkeypatch, enabled=True)
    sys.modules["broker_stops"].release = lambda sym: (_ for _ in ()).throw(RuntimeError("down"))
    res = cc.place_market_order("NVDA", "sell", qty=1)
    assert res.get("id") == "o1"                    # Alpaca gets to decide, as before


def test_stop_frac_uses_the_guard_width_on_a_boxed_wallet(monkeypatch):
    fake = sys.modules.get("broker_stops") or types.ModuleType("broker_stops")
    fake.enabled = lambda: True
    fake.width_pct = lambda sym: 8.9
    monkeypatch.setitem(sys.modules, "broker_stops", fake)
    assert abs(cc._stop_frac("TSLA", 0.05) - 0.089) < 1e-9
    fake.enabled = lambda: False
    assert cc._stop_frac("TSLA", 0.05) == 0.05


def test_sellable_qty_is_whole_position_only_when_guarded(monkeypatch):
    fake = types.ModuleType("broker_stops")
    monkeypatch.setitem(sys.modules, "broker_stops", fake)
    p = {"qty": "92.87", "qty_available": "0.87"}
    fake.enabled = lambda: True
    assert cc._sellable_qty(p) == 92.87
    fake.enabled = lambda: False
    assert cc._sellable_qty(p) == 0.87
