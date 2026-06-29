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
  p  push full report → Telegram (no arm needed)
  g  edit the scheduled auto-report (time / on-off / weekdays / channel)
  m  edit Telegram channels (config only — values stay in .env)
  f  flatten ALL            t  live intraday tick
  c  live Capitol run       b  manual buy        s  sell selected row
  e  rebalance to top-N (sell the rest, redeploy cash)

Run (needs a real terminal):  python3 tui.py

Reuses data/order functions from hermes_report.py, intraday_momentum.py,
capitol_copier.py — places no orders on import.
"""
from __future__ import annotations

import datetime as dt
import re

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
import rebalance_top_n as rb
import config_io

try:
    import telegram_notifier as tg
except ImportError:
    tg = None


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# Key-hint line for the footer. Plain text with spaces so it wraps onto extra
# lines when the terminal is narrow (the built-in Footer clips instead).
KEY_HINTS = (
    "[b]r[/b] refresh   [b]d[/b] dry-run   [b]p[/b] report   [b]g[/b] sched   "
    "[b]m[/b] channels   [b]a[/b] arm/disarm   [b]q[/b] quit"
    "   •   "
    "[b]f[/b] flatten   [b]t[/b] tick   [b]c[/b] capitol   "
    "[b]b[/b] buy   [b]s[/b] sell   [b]e[/b] rebalance"
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
    """Collect (symbol, notional_usd) for a manual buy, with a live 'cash after'
    readout as you type. Returns (sym, amt) or None on cancel."""
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, avail_cash: float = 0.0):
        super().__init__()
        self._avail = max(0.0, _f(avail_cash))

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Manual BUY — symbol + notional USD", id="q")
            yield Input(placeholder="Symbol e.g. NVDA", id="sym")
            yield Label(f"Cash available: [b]${self._avail:,.2f}[/]", id="avail")
            yield Input(placeholder="Notional USD e.g. 1000", id="amt")
            yield Label("", id="after")
            with Horizontal(id="buttons"):
                yield Button("Buy", variant="error", id="ok")
                yield Button("Cancel", variant="primary", id="cancel")

    def on_mount(self) -> None:
        self._update_after()
        self.query_one("#sym", Input).focus()

    def _update_after(self) -> None:
        amt = _amt_of(self.query_one("#amt", Input).value, self._avail)
        after = self._avail - amt
        lbl = self.query_one("#after", Label)
        if after < 0:
            lbl.update(f"buy [b]${amt:,.2f}[/] → cash [b]$0.00[/] "
                       f"[yellow](+${-after:,.2f} on margin/buying power)[/]")
        else:
            lbl.update(f"buy [b]${amt:,.2f}[/] → cash after [b]${after:,.2f}[/]")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "amt":
            self._update_after()

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


class SellModal(ModalScreen[str | None]):
    """Collect a sell amount for ONE position: 'all' (full exit) or a $ amount
    (partial). Live 'cash after' + remaining-position readout. Returns the amount
    string, or None on cancel."""
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, symbol: str, position_value: float, cash: float):
        super().__init__()
        self._sym = symbol
        self._pv = max(0.0, _f(position_value))
        self._cash = max(0.0, _f(cash))

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"SELL {self._sym} — amount", id="q")
            yield Label(f"Position value: [b]${self._pv:,.2f}[/]   "
                        f"Cash now: [b]${self._cash:,.2f}[/]", id="avail")
            yield Input(value="all", id="amt", placeholder="all / $ amount")
            yield Label("", id="after")
            with Horizontal(id="buttons"):
                yield Button("Sell", variant="error", id="ok")
                yield Button("Cancel", variant="primary", id="cancel")

    def on_mount(self) -> None:
        self._update_after()
        self.query_one("#amt", Input).focus()

    def _update_after(self) -> None:
        amt = min(_amt_of(self.query_one("#amt", Input).value, self._pv), self._pv)
        self.query_one("#after", Label).update(
            f"sell [b]${amt:,.2f}[/] → cash after [b]${self._cash + amt:,.2f}[/], "
            f"position left [b]${self._pv - amt:,.2f}[/]")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "amt":
            self._update_after()

    def _accept(self) -> None:
        self.dismiss(self.query_one("#amt", Input).value.strip().lower() or "all")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self._accept()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._accept()

    def action_cancel(self) -> None:
        self.dismiss(None)


def _amt_of(s: str, base: float) -> float:
    """Parse a deploy/withdraw field: 'all'/'max' -> base, else a $ number."""
    s = str(s).strip().lower()
    if s in ("all", "max"):
        return base
    try:
        return max(0.0, float(s.replace(",", "").replace("$", "")))
    except ValueError:
        return 0.0


class RebalanceModal(ModalScreen[dict | None]):
    """Keep top-N, and (pick ONE) deploy idle cash OR withdraw/raise cash. Both
    show a live readout as you type. Returns {'n','deploy','withdraw'} or None."""
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, avail_cash: float = 0.0, invested: float = 0.0):
        super().__init__()
        self._avail = max(0.0, _f(avail_cash))
        self._invested = max(0.0, _f(invested))

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Rebalance — keep the top how many (by P&L %)?\n"
                        "Sells the rest, redeploys proceeds into the kept names.\n"
                        "Then pick ONE: deploy idle cash OR withdraw (raise cash).\n"
                        "Each field: 0 = none, a $ amount, or 'all'.", id="q")
            yield Input(value="20", id="topn", placeholder="keep top N")
            yield Label(f"Available idle cash: [b]${self._avail:,.2f}[/]", id="avail")
            yield Input(value="0", id="deploy", placeholder="DEPLOY idle cash: 0 / amount / all")
            yield Label("", id="leftover")
            yield Input(value="0", id="withdraw", placeholder="WITHDRAW / raise cash: 0 / amount / all")
            yield Label("", id="remaining")
            yield Label("", id="err")
            with Horizontal(id="buttons"):
                yield Button("Build plan", variant="error", id="ok")
                yield Button("Cancel", variant="primary", id="cancel")

    def on_mount(self) -> None:
        self._update_leftover()
        self._update_remaining()
        self.query_one("#topn", Input).focus()

    def _update_leftover(self) -> None:
        amt = _amt_of(self.query_one("#deploy", Input).value, self._avail)
        left = self._avail - amt
        lbl = self.query_one("#leftover", Label)
        if left < 0:
            lbl.update(f"[yellow]deploy ${amt:,.2f} → exceeds cash by ${-left:,.2f} "
                       f"(will cap at ${self._avail:,.2f}; no leverage)[/]")
        else:
            lbl.update(f"deploy [b]${amt:,.2f}[/] → cash leftover [b]${left:,.2f}[/]")

    def _update_remaining(self) -> None:
        amt = _amt_of(self.query_one("#withdraw", Input).value, self._invested)
        rem = self._invested - amt
        lbl = self.query_one("#remaining", Label)
        if rem < 0:
            lbl.update(f"[yellow]withdraw ${amt:,.2f} → exceeds invested ${self._invested:,.2f} "
                       f"(will cap = flatten)[/]")
        else:
            lbl.update(f"withdraw [b]${amt:,.2f}[/] → remaining invested [b]${rem:,.2f}[/]")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "deploy":
            self._update_leftover()
        elif event.input.id == "withdraw":
            self._update_remaining()

    def _accept(self) -> None:
        try:
            n = int(self.query_one("#topn", Input).value.strip())
        except ValueError:
            n = 0
        deploy = self.query_one("#deploy", Input).value.strip().lower() or "0"
        withdraw = self.query_one("#withdraw", Input).value.strip().lower() or "0"
        d_on = deploy not in ("0", "", "0.0")
        w_on = withdraw not in ("0", "", "0.0")
        if d_on and w_on:
            self.query_one("#err", Label).update(
                "[red]pick ONE: deploy OR withdraw, not both[/]")
            return
        self.dismiss({"n": n, "deploy": deploy, "withdraw": withdraw} if n > 0 else None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self._accept()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._accept()

    def action_cancel(self) -> None:
        self.dismiss(None)


def _yn(s: str, default: bool = False) -> bool:
    return str(s).strip().lower() in ("y", "yes", "true", "on", "1") if str(s).strip() else default


_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


class ScheduleModal(ModalScreen[dict | None]):
    """Edit report_schedule (enabled / time_et / weekdays_only / channel).
    Returns the changed dict, or None on cancel."""
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, rs: dict, channels: list[str]):
        super().__init__()
        self._rs = rs or {}
        self._channels = channels or ["home"]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Auto-report schedule", id="q")
            yield Input(value="yes" if self._rs.get("enabled") else "no",
                        id="enabled", placeholder="enabled? yes/no")
            yield Input(value=str(self._rs.get("time_et", "16:00")),
                        id="time", placeholder="time ET (HH:MM, 24h)")
            yield Input(value="yes" if self._rs.get("weekdays_only", True) else "no",
                        id="weekdays", placeholder="weekdays only? yes/no")
            yield Input(value=str(self._rs.get("channel", self._channels[0])),
                        id="channel", placeholder="channel: " + ", ".join(self._channels))
            yield Label("", id="err")
            with Horizontal(id="buttons"):
                yield Button("Save", variant="error", id="ok")
                yield Button("Cancel", variant="primary", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#time", Input).focus()

    def _accept(self) -> None:
        time_v = self.query_one("#time", Input).value.strip()
        chan_v = self.query_one("#channel", Input).value.strip()
        if not _TIME_RE.match(time_v):
            self.query_one("#err", Label).update("[red]time must be HH:MM (24h)[/]")
            return
        if chan_v not in self._channels:
            self.query_one("#err", Label).update(
                f"[red]channel must be one of: {', '.join(self._channels)}[/]")
            return
        self.dismiss({
            "enabled": _yn(self.query_one("#enabled", Input).value),
            "time_et": time_v,
            "weekdays_only": _yn(self.query_one("#weekdays", Input).value, True),
            "channel": chan_v,
        })

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self._accept()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._accept()

    def action_cancel(self) -> None:
        self.dismiss(None)


class ChannelModal(ModalScreen[dict | None]):
    """Manage Telegram channels in config ONLY (never .env). Set the default
    channel and/or add a new channel (name + env-var NAMES). Returns an action
    dict {'kind': 'default'|'add', ...} or None on cancel."""
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, channels: dict, default: str):
        super().__init__()
        self._channels = channels or {}
        self._default = default

    def compose(self) -> ComposeResult:
        names = ", ".join(self._channels) or "(none)"
        with Vertical(id="dialog"):
            yield Label(f"Telegram channels: {names}\n"
                        f"default = {self._default}\n"
                        "Values live in .env — this edits config only.", id="q")
            yield Input(value=self._default, id="default",
                        placeholder="set default channel (existing name)")
            yield Input(id="newname", placeholder="add channel — name (optional)")
            yield Input(id="tokenenv", placeholder="new channel TOKEN env-var name")
            yield Input(id="chatenv", placeholder="new channel CHAT env-var name")
            yield Label("", id="err")
            with Horizontal(id="buttons"):
                yield Button("Save", variant="error", id="ok")
                yield Button("Cancel", variant="primary", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#default", Input).focus()

    def _accept(self) -> None:
        new_name = self.query_one("#newname", Input).value.strip()
        if new_name:
            te = self.query_one("#tokenenv", Input).value.strip()
            ce = self.query_one("#chatenv", Input).value.strip()
            if not (te and ce):
                self.query_one("#err", Label).update(
                    "[red]new channel needs both env-var names[/]")
                return
            self.dismiss({"kind": "add", "name": new_name, "token_env": te, "chat_env": ce})
            return
        dflt = self.query_one("#default", Input).value.strip()
        if dflt and dflt not in self._channels:
            self.query_one("#err", Label).update(
                f"[red]'{dflt}' is not an existing channel[/]")
            return
        self.dismiss({"kind": "default", "name": dflt})

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self._accept()
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ── Main app ─────────────────────────────────────────────────────────────────

class AlpacaTUI(App):
    TITLE = "Alpaca Paper Trader — Cockpit"
    CSS = """
    #summary { height: auto; padding: 0 1; text-style: bold; }
    #armbar  { height: 1; content-align: center middle; }
    #holdings { height: 1fr; text-style: bold; }
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
        Binding("p", "push_report", "Push report"),
        Binding("g", "edit_schedule", "Edit schedule"),
        Binding("m", "edit_channels", "Channels"),
        Binding("a", "arm", "Arm/Disarm"),
        Binding("f", "flatten", "Flatten ALL"),
        Binding("t", "tick", "Live tick"),
        Binding("c", "capitol", "Capitol run"),
        Binding("b", "buy", "Buy"),
        Binding("s", "sell", "Sell row"),
        Binding("e", "rebalance", "Rebalance top-N"),
        Binding("q", "quit", "Quit"),
    ]

    armed = reactive(False)

    def __init__(self):
        super().__init__()
        self._syms: list[str] = []   # holdings symbols in row order (for sell)
        self.market_open: bool | None = None   # None until first clock fetch
        self._next_open: str = "?"             # human-readable next-open time
        self._tg_notify: bool = True           # send Telegram pings on actions
        self._mkt_known: bool | None = None    # last market state (for transitions)
        self._cash: float = 0.0                # idle cash from last refresh
        self._lmv: float = 0.0                 # long market value (invested) from last refresh
        self._mv: dict[str, float] = {}        # per-symbol market value from last refresh

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
        try:
            self._tg_notify = im.load_config().get("tui", {}).get("telegram_notify", True)
        except Exception:
            self._tg_notify = True
        tg_state = "on" if (self._tg_notify and tg is not None) else "off"
        self._log(f"[dim]booted DISARMED — press 'a' to enable live actions "
                  f"(Telegram alerts: {tg_state})[/]")
        self._log(f"[dim]{self._schedule_status()} — 'p' push report, 'g' edit schedule, "
                  f"'m' channels[/]")
        self.refresh_data()
        self.set_interval(8, self.refresh_data)

    # ── logging / arm ────────────────────────────────────────────────────────
    def _log(self, msg: str) -> None:
        self.query_one("#log", RichLog).write(msg)

    def _notify(self, msg: str) -> None:
        """Send a Telegram ping (plain text). No-op if disabled/unconfigured.
        MUST be called from a worker thread — tg.send is a blocking network call."""
        if tg is None or not self._tg_notify:
            return
        try:
            tg.send(msg, parse_mode=None)
        except Exception as e:
            # Never let a notification failure disrupt trading
            self.call_from_thread(self._log, f"[yellow]telegram notify failed: {e}[/]")

    def _mkt_suffix(self) -> str:
        """Clarify in notifications that orders queue when the market is closed."""
        if self.market_open is False:
            return f" — QUEUED to next open ({self._next_open})"
        return ""

    def _schedule_status(self) -> str:
        try:
            rs = im.load_config().get("report_schedule", {}) or {}
        except Exception:
            return "auto-report: ?"
        if rs.get("enabled"):
            scope = "weekdays" if rs.get("weekdays_only", True) else "daily"
            return f"auto-report: ON @ {rs.get('time_et', '16:00')} ET ({scope})"
        return "auto-report: OFF"

    def _channels(self) -> dict:
        try:
            return (im.load_config().get("telegram", {}) or {}).get("channels", {}) or {}
        except Exception:
            return {}

    # ── schedule editor (g) ───────────────────────────────────────────────────
    def action_edit_schedule(self) -> None:
        try:
            cfg = im.load_config()
        except Exception as e:
            self._log(f"[red]config read error: {e}[/]")
            return
        rs = cfg.get("report_schedule", {}) or {}
        names = list((cfg.get("telegram", {}) or {}).get("channels", {}).keys()) or ["home"]
        self.push_screen(ScheduleModal(rs, names),
                         lambda res: self._save_schedule(res) if res else None)

    @work(thread=True, group="config")
    def _save_schedule(self, res: dict) -> None:
        try:
            def mut(cfg):
                cfg.setdefault("report_schedule", {}).update(res)
                return cfg
            new = config_io.update_config(mut)
            rs = new.get("report_schedule", {})
            warn = ""
            if int(rs.get("window_minutes", 20)) < 10:
                warn = "  [yellow]⚠ window_minutes < 10m heartbeat — report may be missed[/]"
            self.call_from_thread(self._log, f"[cyan]{self._schedule_status()}[/]{warn}")
        except Exception as e:
            self.call_from_thread(self._log, f"[red]schedule save error: {e}[/]")

    # ── channel editor (m) — config only, never .env ─────────────────────────-
    def action_edit_channels(self) -> None:
        try:
            cfg = im.load_config()
        except Exception as e:
            self._log(f"[red]config read error: {e}[/]")
            return
        tg_cfg = cfg.get("telegram", {}) or {}
        self.push_screen(
            ChannelModal(tg_cfg.get("channels", {}) or {}, tg_cfg.get("default_channel", "")),
            lambda res: self._save_channels(res) if res else None)

    @work(thread=True, group="config")
    def _save_channels(self, res: dict) -> None:
        try:
            def mut(cfg):
                tgc = cfg.setdefault("telegram", {})
                tgc.setdefault("channels", {})
                if res["kind"] == "add":
                    tgc["channels"][res["name"]] = {
                        "token_env": res["token_env"], "chat_env": res["chat_env"]}
                    return cfg
                if res.get("name"):
                    tgc["default_channel"] = res["name"]
                return cfg
            config_io.update_config(mut)
            if res["kind"] == "add":
                self.call_from_thread(
                    self._log,
                    f"[cyan]channel '{res['name']}' added — set {res['token_env']} / "
                    f"{res['chat_env']} in .env (restart TUI to use)[/]")
            else:
                self.call_from_thread(self._log, f"[cyan]default channel → {res.get('name')}[/]")
        except Exception as e:
            self.call_from_thread(self._log, f"[red]channel save error: {e}[/]")

    def action_arm(self) -> None:
        self.armed = not self.armed
        if self.armed and self.market_open is False:
            self._log(
                "[yellow]⚠ Market CLOSED — any orders you place now will not fill "
                f"immediately. They QUEUE and execute at the next open ({self._next_open}).[/]")

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
        try:
            clk = im.get_clock()
        except Exception:
            clk = None
        self.call_from_thread(self._apply, acct, positions, clk)

    def _market_badge(self, clk: dict | None) -> str:
        """Update market state from the clock and return a status badge string."""
        if clk is None:
            self.market_open = None
            return "[dim] market ? [/]"
        self.market_open = bool(clk.get("is_open"))
        nxt = clk.get("next_open") or ""
        try:
            self._next_open = dt.datetime.fromisoformat(nxt).strftime("%a %m-%d %H:%M ET")
        except ValueError:
            self._next_open = nxt or "?"
        # Notify once on an open↔closed transition (skip the very first reading).
        if self._mkt_known is not None and self._mkt_known != self.market_open:
            note = ("📈 Market OPEN — TUI orders fill live."
                    if self.market_open else
                    f"🌙 Market CLOSED — TUI orders now queue to next open ({self._next_open}).")
            self.run_worker(lambda: self._notify(note), thread=True, group="notify")
        self._mkt_known = self.market_open
        if self.market_open:
            return "[b black on green] MARKET OPEN [/]"
        return f"[b black on yellow] MARKET CLOSED [/][yellow] orders queue → {self._next_open}[/]"

    def _apply(self, acct: dict, positions: list[dict], clk: dict | None = None) -> None:
        equity = _f(acct.get("equity"))
        cash   = _f(acct.get("cash"))
        self._cash = cash
        last   = _f(acct.get("last_equity"), equity)
        rt     = _f(acct.get("regt_buying_power"))
        dtbp   = _f(acct.get("daytrading_buying_power"))
        lmv    = _f(acct.get("long_market_value"))
        self._lmv = lmv
        daypl  = equity - last
        daypct = (daypl / last * 100) if last else 0.0
        lev    = (lmv / equity) if equity else 0.0
        pc = "green" if daypl >= 0 else "red"
        self.query_one("#summary", Static).update(
            f"Equity [b]${equity:,.0f}[/]   Cash ${cash:,.0f}   "
            f"RegT(2x) ${rt:,.0f}   DT(4x) ${dtbp:,.0f}   "
            f"DayP&L [{pc}]{daypl:+,.0f} ({daypct:+.2f}%)[/]   "
            f"Exposure [b]{lev:.2f}x[/]\n"
            f"{self._market_badge(clk)}"
        )

        t = self.query_one("#holdings", DataTable)
        t.clear()
        self._syms = []
        self._mv = {}
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
            self._mv[sym] = _f(p.get("market_value"))
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

    # ── push full report to Telegram (safe — no arm; sends a report, not a trade)
    def action_push_report(self) -> None:
        self._log("[cyan]building report → Telegram… (this can take ~20-30s)[/]")
        self._do_push_report()

    @work(thread=True, exclusive=True, group="report")
    def _do_push_report(self) -> None:
        try:
            res = hr.run_report(
                push=True,
                log=lambda m: self.call_from_thread(self._log, f"[dim]{m}[/]"))
            if res.get("sent"):
                self.call_from_thread(self._log, "[green]✓ report pushed to Telegram[/]")
                self._notify("📑 TUI pushed the full portfolio report")
            else:
                self.call_from_thread(self._log, "[yellow]report built but not sent[/]")
        except Exception as e:
            self.call_from_thread(self._log, f"[red]report error: {e}[/]")

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
            self._notify(f"🛑 TUI FLATTEN — submitted close on {n} positions{self._mkt_suffix()}")
        except Exception as e:
            self.call_from_thread(self._log, f"[red]flatten error: {e}[/]")
            self._notify(f"⚠️ TUI flatten error: {e}")
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
            self._notify(f"⚡ TUI intraday tick complete{self._mkt_suffix()}")
        except Exception as e:
            self.call_from_thread(self._log, f"[red]tick error: {e}[/]")
            self._notify(f"⚠️ TUI tick error: {e}")
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
            self._notify(f"🏛️ TUI Capitol Copier run complete{self._mkt_suffix()}")
        except Exception as e:
            self.call_from_thread(self._log, f"[red]capitol error: {e}[/]")
            self._notify(f"⚠️ TUI capitol error: {e}")
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
        self.push_screen(BuyModal(self._cash), after_modal)

    def action_sell(self) -> None:
        if not self._require_armed():
            return
        t = self.query_one("#holdings", DataTable)
        row = t.cursor_row
        if row is None or row < 0 or row >= len(self._syms):
            self._log("[yellow]no position selected[/]")
            return
        sym = self._syms[row]
        pv = self._mv.get(sym, 0.0)

        def after_modal(amt):
            if not amt:
                return
            if amt in ("all", "max"):
                self.push_screen(
                    ConfirmModal(f"SELL ALL of {sym}?"),
                    lambda ok: self._do_order(sym, "sell") if ok else None,
                )
            else:
                d = min(_amt_of(amt, pv), pv)
                if d <= 0:
                    self._log("[yellow]nothing to sell (amount 0)[/]")
                    return
                self.push_screen(
                    ConfirmModal(f"SELL ${d:,.0f} of {sym}?"),
                    lambda ok: self._do_order(sym, "sell", d) if ok else None,
                )
        self.push_screen(SellModal(sym, pv, self._cash), after_modal)

    @work(thread=True, group="action")
    def _do_order(self, sym: str, side: str, notional: float | None = None) -> None:
        try:
            if side == "buy":
                res = cc.place_market_order(sym, "buy", notional=notional)
            elif notional:                       # partial sell by dollar amount
                res = cc.place_market_order(sym, "sell", notional=notional)
            else:                                # full-position exit
                pos = cc.get_position(sym)
                qty = abs(_f(pos.get("qty"))) if pos else 0.0
                if qty <= 0:
                    self.call_from_thread(self._log, f"[yellow]no {sym} position to sell[/]")
                    return
                res = cc.place_market_order(sym, "sell", qty=qty)
            oid = res.get("id") or res.get("message") or "?"
            self.call_from_thread(self._log, f"[green]{side.upper()} {sym} sent → {str(oid)[:14]}[/]")
            amt = f" ${notional:,.0f}" if notional else ""
            self._notify(f"🟢 TUI {side.upper()} {sym}{amt} — submitted{self._mkt_suffix()}")
        except Exception as e:
            self.call_from_thread(self._log, f"[red]order error: {e}[/]")
            self._notify(f"⚠️ TUI {side} {sym} error: {e}")
        self.call_from_thread(self.refresh_data)

    # ── rebalance to top-N (sell the rest, redeploy cash) ───────────────────--
    def action_rebalance(self) -> None:
        if not self._require_armed():
            return
        self.push_screen(
            RebalanceModal(self._cash, self._lmv),
            lambda res: self._plan_rebalance(res) if res else None,
        )

    @work(thread=True, group="action")
    def _plan_rebalance(self, res: dict) -> None:
        n = res["n"]
        try:
            positions = cc.get_positions()
            longs = [p for p in positions if _f(p.get("qty")) > 0]
            avail = rb.available_cash()
            req_d = res.get("deploy", "0")
            req_w = res.get("withdraw", "0")
            deploy = avail if req_d == "all" else max(0.0, _f(req_d))
            withdraw = (sum(_f(p.get("market_value")) for p in longs)
                        if req_w == "all" else max(0.0, _f(req_w)))
            if deploy > 0 and withdraw > 0:
                self.call_from_thread(self._log, "[red]pick ONE: deploy OR withdraw[/]")
                return
            if deploy > avail:
                self.call_from_thread(
                    self._log,
                    f"[yellow]deploy ${deploy:,.0f} > cash ${avail:,.0f} — capping (no leverage)[/]")
                deploy = avail
            if len(longs) <= n and deploy <= 0 and withdraw <= 0:
                self.call_from_thread(
                    self._log,
                    f"[yellow]only {len(longs)} long positions at top {n}, nothing to do[/]")
                return
            keep, sell, buys, trims, freed, skipped = rb.build_plan(
                positions, n, "plpc", 0.10, deploy_cash=deploy, withdraw_cash=withdraw)
        except Exception as e:
            self.call_from_thread(self._log, f"[red]rebalance plan error: {e}[/]")
            return
        if trims:
            raised = sum(t["notional"] for t in trims)
            summary = (f"Rebalance: SELL {len(sell)} + TRIM {len(trims)} kept "
                       f"to raise ${raised:,.0f} cash?")
        else:
            cash_bit = f" + ${deploy:,.0f} idle cash" if deploy > 0 else ""
            summary = (f"Rebalance: SELL {len(sell)}, deploy "
                       f"${freed + deploy:,.0f} (${freed:,.0f} proceeds{cash_bit}) "
                       f"into top {len(buys)}?")
        self.call_from_thread(self._log, f"[cyan]{summary}[/]")
        self.call_from_thread(
            self.push_screen,
            ConfirmModal(summary),
            lambda ok: self._exec_rebalance(sell, buys, trims) if ok else None,
        )

    @work(thread=True, group="action")
    def _exec_rebalance(self, sell: list, buys: list, trims: list) -> None:
        try:
            rb.execute(sell, buys, trims,
                       log=lambda m: self.call_from_thread(self._log, m))
            # ONE batched notification, not one per order
            tail = (f" + {len(trims)} trims" if trims else
                    (f" + {len(buys)} buys" if buys else ""))
            self._notify(
                f"♻️ TUI rebalance — submitted {len(sell)} sells{tail}"
                f"{self._mkt_suffix()}")
        except Exception as e:
            self.call_from_thread(self._log, f"[red]rebalance error: {e}[/]")
            self._notify(f"⚠️ TUI rebalance error: {e}")
        self.call_from_thread(self.refresh_data)


if __name__ == "__main__":
    AlpacaTUI().run()
