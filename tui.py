"""
Alpaca Trading TUI — full-control terminal cockpit
===================================================
A live Textual dashboard over the Alpaca paper account. Monitors the account and
positions in real time and can ACT on them: flatten everything, run a live
strategy tick, or place manual buy/sell orders.

Safety (two independent gates on every account-mutating action):
  1. ARM switch — the app boots DISARMED; mutating keys are inert until you press
     'a' to arm it.
  2. Confirm modal — each armed action shows exactly what it will do and needs an
     explicit Yes.

Keys:
  r  refresh now            a  arm / disarm
  d  dry-run RS ranking     q  quit
  F  flatten ALL            T  live intraday tick
  C  live Capitol run       B  manual buy        S  sell selected row

Run (needs a real terminal):  python3 tui.py

Reuses data/order functions from hermes_report.py, intraday_momentum.py,
capitol_copier.py — places no orders on import.
"""
from __future__ import annotations

import datetime as dt

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button, DataTable, Header, Input, Label, RichLog, Static,
)

import hermes_report as hr
import intraday_momentum as im
import capitol_copier as cc


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# Key-hint line for the footer. Plain text with spaces so it wraps onto extra
# lines when the terminal is narrow (the built-in Footer clips instead).
KEY_HINTS = (
    "[b]r[/b] refresh   [b]d[/b] dry-run   [b]a[/b] arm/disarm   [b]q[/b] quit"
    "   •   "
    "[b]F[/b] flatten   [b]T[/b] tick   [b]C[/b] capitol   "
    "[b]B[/b] buy   [b]S[/b] sell"
)


# ── Modals ───────────────────────────────────────────────────────────────────

class ConfirmModal(ModalScreen[bool]):
    """Yes/No confirmation. Returns True only on explicit Yes."""
    BINDINGS = [("y", "yes", "Yes"), ("n", "no", "No"), ("escape", "no", "No")]

    def __init__(self, prompt: str):
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.prompt, id="q")
            with Horizontal(id="buttons"):
                yield Button("Yes", variant="error", id="yes")
                yield Button("No", variant="primary", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class BuyModal(ModalScreen[tuple | None]):
    """Collect (symbol, notional_usd) for a manual buy. Returns None on cancel."""
    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Manual BUY — symbol + notional USD", id="q")
            yield Input(placeholder="Symbol e.g. NVDA", id="sym")
            yield Input(placeholder="Notional USD e.g. 1000", id="amt")
            with Horizontal(id="buttons"):
                yield Button("Buy", variant="error", id="ok")
                yield Button("Cancel", variant="primary", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#sym", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            sym = self.query_one("#sym", Input).value.strip().upper()
            amt = _f(self.query_one("#amt", Input).value)
            if sym and amt > 0:
                self.dismiss((sym, amt))
                return
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ── Main app ─────────────────────────────────────────────────────────────────

class AlpacaTUI(App):
    TITLE = "Alpaca Paper Trader — Cockpit"
    CSS = """
    #summary { height: auto; padding: 0 1; }
    #armbar  { height: 1; content-align: center middle; }
    #holdings { height: 1fr; }
    #log { height: 12; border: solid $accent; }
    #keys {
        dock: bottom;
        height: auto;
        padding: 0 1;
        background: $panel;
        color: $text-muted;
    }
    ConfirmModal, BuyModal { align: center middle; }
    #dialog { width: 64; height: auto; padding: 1 2; background: $surface; border: thick $accent; }
    #buttons { height: auto; align-horizontal: center; }
    #buttons Button { margin: 1 2 0 2; }
    """

    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("d", "dryrun", "Dry-run RS"),
        Binding("a", "arm", "Arm/Disarm"),
        Binding("F", "flatten", "Flatten ALL"),
        Binding("T", "tick", "Live tick"),
        Binding("C", "capitol", "Capitol run"),
        Binding("B", "buy", "Buy"),
        Binding("S", "sell", "Sell row"),
        Binding("q", "quit", "Quit"),
    ]

    armed = reactive(False)

    def __init__(self):
        super().__init__()
        self._syms: list[str] = []   # holdings symbols in row order (for sell)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("loading…", id="summary")
        yield Static(id="armbar")
        yield DataTable(id="holdings", cursor_type="row", zebra_stripes=True)
        yield RichLog(id="log", highlight=False, markup=True)
        yield Static(KEY_HINTS, id="keys")

    def on_mount(self) -> None:
        t = self.query_one("#holdings", DataTable)
        t.add_columns("SYM", "QTY", "AVG", "PRICE", "P&L $", "P&L %")
        self.watch_armed(self.armed)
        self._log("[dim]booted DISARMED — press 'a' to enable live actions[/]")
        self.refresh_data()
        self.set_interval(8, self.refresh_data)

    # ── logging / arm ────────────────────────────────────────────────────────
    def _log(self, msg: str) -> None:
        self.query_one("#log", RichLog).write(msg)

    def action_arm(self) -> None:
        self.armed = not self.armed

    def watch_armed(self, val: bool) -> None:
        bar = self.query_one("#armbar", Static)
        if val:
            bar.update("[b white on red]  ARMED — live orders ENABLED  [/]")
        else:
            bar.update("[b black on green]  DISARMED — safe (press 'a' to arm)  [/]")

    def _require_armed(self) -> bool:
        if not self.armed:
            self._log("[yellow]DISARMED — press 'a' first[/]")
            return False
        return True

    # ── data refresh ───────────────────────────────────────────────────────--
    def action_refresh(self) -> None:
        self._log("[dim]refreshing…[/]")
        self.refresh_data()

    @work(thread=True, exclusive=True, group="data")
    def refresh_data(self) -> None:
        try:
            acct = hr.fetch_account()
            positions = hr.fetch_positions()
        except Exception as e:
            self.call_from_thread(self._log, f"[red]refresh error: {e}[/]")
            return
        self.call_from_thread(self._apply, acct, positions)

    def _apply(self, acct: dict, positions: list[dict]) -> None:
        equity = _f(acct.get("equity"))
        cash   = _f(acct.get("cash"))
        last   = _f(acct.get("last_equity"), equity)
        rt     = _f(acct.get("regt_buying_power"))
        dtbp   = _f(acct.get("daytrading_buying_power"))
        lmv    = _f(acct.get("long_market_value"))
        daypl  = equity - last
        daypct = (daypl / last * 100) if last else 0.0
        lev    = (lmv / equity) if equity else 0.0
        pc = "green" if daypl >= 0 else "red"
        self.query_one("#summary", Static).update(
            f"Equity [b]${equity:,.0f}[/]   Cash ${cash:,.0f}   "
            f"RegT(2x) ${rt:,.0f}   DT(4x) ${dtbp:,.0f}   "
            f"DayP&L [{pc}]{daypl:+,.0f} ({daypct:+.2f}%)[/]   "
            f"Exposure [b]{lev:.2f}x[/]"
        )

        t = self.query_one("#holdings", DataTable)
        t.clear()
        self._syms = []
        for p in sorted(positions, key=lambda x: _f(x.get("unrealized_pl")), reverse=True):
            sym  = p.get("symbol", "?")
            qty  = _f(p.get("qty"))
            avg  = _f(p.get("avg_entry_price"))
            cur  = _f(p.get("current_price"))
            pl   = _f(p.get("unrealized_pl"))
            plpc = _f(p.get("unrealized_plpc")) * 100
            col  = "green" if pl >= 0 else "red"
            t.add_row(
                sym, f"{qty:g}", f"{avg:.2f}", f"{cur:.2f}",
                Text(f"{pl:+,.0f}", style=col), Text(f"{plpc:+.1f}%", style=col),
                key=sym,
            )
            self._syms.append(sym)
        self._log(f"[dim]refreshed {len(positions)} positions @ {dt.datetime.now():%H:%M:%S}[/]")

    # ── read-only dry-run ──────────────────────────────────────────────────--
    def action_dryrun(self) -> None:
        self._do_dryrun()

    @work(thread=True, group="action")
    def _do_dryrun(self) -> None:
        try:
            cfg = im.load_config()
            ranked, spy = im.rank_universe(cfg)
            top = cfg["intraday"]["top_n"]
            lines = [f"[cyan]RS ranking — SPY {spy*100:+.2f}%  (top {top} = LONG):[/]"]
            for i, (s, rs, px) in enumerate(ranked[:top], 1):
                lines.append(f"  {i}. {s:<5} {rs*100:+.2f}%  ${px:.2f}")
        except Exception as e:
            lines = [f"[red]dry-run error: {e}[/]"]
        self.call_from_thread(self._log, "\n".join(lines))

    # ── account-mutating actions (arm + confirm) ───────────────────────────--
    def action_flatten(self) -> None:
        if not self._require_armed():
            return
        self.push_screen(
            ConfirmModal("Flatten ALL positions to cash?"),
            lambda ok: self._do_flatten() if ok else None,
        )

    @work(thread=True, group="action")
    def _do_flatten(self) -> None:
        try:
            st = im.load_state()
            n = im.flatten(st, dry_run=False, reason="manual flatten (TUI)")
            im.save_state(st)
            self.call_from_thread(self._log, f"[red]FLATTEN sent — {n} positions[/]")
        except Exception as e:
            self.call_from_thread(self._log, f"[red]flatten error: {e}[/]")
        self.call_from_thread(self.refresh_data)

    def action_tick(self) -> None:
        if not self._require_armed():
            return
        self.push_screen(
            ConfirmModal("Run a LIVE intraday tick now?"),
            lambda ok: self._do_tick() if ok else None,
        )

    @work(thread=True, group="action")
    def _do_tick(self) -> None:
        try:
            im.run_tick(im.load_config(), dry_run=False)
            self.call_from_thread(self._log, "[cyan]intraday tick complete[/]")
        except Exception as e:
            self.call_from_thread(self._log, f"[red]tick error: {e}[/]")
        self.call_from_thread(self.refresh_data)

    def action_capitol(self) -> None:
        if not self._require_armed():
            return
        self.push_screen(
            ConfirmModal("Run a LIVE Capitol Copier cycle now?"),
            lambda ok: self._do_capitol() if ok else None,
        )

    @work(thread=True, group="action")
    def _do_capitol(self) -> None:
        try:
            cc.run(dry_run=False)
            self.call_from_thread(self._log, "[cyan]Capitol run complete[/]")
        except Exception as e:
            self.call_from_thread(self._log, f"[red]capitol error: {e}[/]")
        self.call_from_thread(self.refresh_data)

    def action_buy(self) -> None:
        if not self._require_armed():
            return
        def after_modal(res):
            if not res:
                return
            sym, amt = res
            self.push_screen(
                ConfirmModal(f"BUY {sym}  ${amt:,.0f}?"),
                lambda ok: self._do_order(sym, "buy", amt) if ok else None,
            )
        self.push_screen(BuyModal(), after_modal)

    def action_sell(self) -> None:
        if not self._require_armed():
            return
        t = self.query_one("#holdings", DataTable)
        row = t.cursor_row
        if row is None or row < 0 or row >= len(self._syms):
            self._log("[yellow]no position selected[/]")
            return
        sym = self._syms[row]
        self.push_screen(
            ConfirmModal(f"SELL ALL of {sym}?"),
            lambda ok: self._do_order(sym, "sell") if ok else None,
        )

    @work(thread=True, group="action")
    def _do_order(self, sym: str, side: str, notional: float | None = None) -> None:
        try:
            if side == "buy":
                res = cc.place_market_order(sym, "buy", notional=notional)
            else:
                pos = cc.get_position(sym)
                qty = abs(_f(pos.get("qty"))) if pos else 0.0
                if qty <= 0:
                    self.call_from_thread(self._log, f"[yellow]no {sym} position to sell[/]")
                    return
                res = cc.place_market_order(sym, "sell", qty=qty)
            oid = res.get("id") or res.get("message") or "?"
            self.call_from_thread(self._log, f"[green]{side.upper()} {sym} sent → {str(oid)[:14]}[/]")
        except Exception as e:
            self.call_from_thread(self._log, f"[red]order error: {e}[/]")
        self.call_from_thread(self.refresh_data)


if __name__ == "__main__":
    AlpacaTUI().run()
