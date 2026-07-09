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
  o  toggle chart: whole portfolio (equity) vs the selected holding
  w  cycle chart timeframe: 1D → 1W → 1M → 3M → 6M → 1Y
  g  edit the scheduled auto-report (time / on-off / weekdays / channel)
  m  edit Telegram channels (config only — values stay in .env)
  f  flatten ALL            t  live intraday tick
  c  live Capitol run       b  manual buy        s  sell selected row
  e  rebalance to top-N (sell the rest, redeploy cash)
  /  search ticker → company bio (needs FMP_API_KEY in .env)
  l  toggle language English ⇄ 繁體中文 (persisted to strategy_config.json)

Run (needs a real terminal):  python3 tui.py

Reuses data/order functions from hermes_report.py, intraday_momentum.py,
capitol_copier.py — places no orders on import.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import zoneinfo

import requests

_ET = zoneinfo.ZoneInfo("America/New_York")

from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button, DataTable, Header, Input, Label, MarkdownViewer, RichLog, Static,
)
from textual_plotext import PlotextPlot

import hermes_report as hr
import intraday_momentum as im
import capitol_copier as cc
import rebalance_top_n as rb
import config_io
import config_fields as cf
import wallets as wl
import compare as cmpw
import i18n
from i18n import t

try:
    import telegram_notifier as tg
except ImportError:
    tg = None


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# ── company bio lookup (Financial Modeling Prep) ───────────────────────────────
# FMP's /profile endpoint returns a prose `description` on the free tier. Key lives
# in .env as FMP_API_KEY (loaded on import via hermes_report/wallets load_dotenv).
FMP_PROFILE_URL = "https://financialmodelingprep.com/stable/profile"


def fetch_company_bio(symbol: str) -> tuple[str, str]:
    """Return (company_name, description) for a symbol via FMP. ('', '') on any
    failure or unknown symbol; ('', '__NO_KEY__') when the key is missing.
    Blocking network call — run in a worker thread."""
    key = os.getenv("FMP_API_KEY", "").strip()
    if not key:
        return ("", "__NO_KEY__")
    try:
        r = requests.get(FMP_PROFILE_URL,
                         params={"symbol": symbol.upper(), "apikey": key},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        r.raise_for_status()
        data = r.json()
        if not data:                       # FMP returns [] for unknown symbols
            return ("", "")
        rec = data[0]
        return (rec.get("companyName") or symbol.upper(),
                (rec.get("description") or "").strip())
    except Exception:
        return ("", "")


def translate_to_zh(text: str) -> str:
    """Company-bio translation EN → 繁體中文 via OpenRouter (haiku tier —
    ~0.1¢ per lookup; the caller caches per symbol so each company is paid
    for once). Returns '' on any failure so the caller keeps English."""
    try:
        import openrouter_analyst as oa
        if not oa.API_KEY:
            return ""
        r = requests.post(
            oa.URL,
            headers={"Authorization": f"Bearer {oa.API_KEY}",
                     "Content-Type": "application/json"},
            json={
                "model": "anthropic/claude-haiku-4.5",
                "messages": [
                    {"role": "system",
                     "content": "Translate the user's company description into "
                                "Traditional Chinese (繁體中文, Taiwan usage). "
                                "Keep company and product names in English. "
                                "Output ONLY the translation."},
                    {"role": "user", "content": text[:4000]},
                ],
                "max_tokens": 1500,
                "temperature": 0.2,
            },
            timeout=30,
        )
        if r.status_code != 200:
            return ""
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return ""


# Legal boilerplate Alpaca appends to asset names — stripped so the chart title
# shows "Robinhood Markets, Inc." instead of "… Class A Common Stock".
_ASSET_NAME_NOISE = (
    " Class A Common Stock", " Class B Common Stock", " Class C Common Stock",
    " Common Stock", " Common Shares", " Ordinary Shares",
    " American Depositary Shares", " Depositary Shares",
)


def fetch_asset_name(symbol: str) -> str:
    """Company/fund full name from Alpaca's /assets endpoint (free, same creds
    as everything else — follows the active wallet). '' on any failure."""
    try:
        r = requests.get(f"{hr.ALPACA_BASE}/assets/{symbol}",
                         headers=hr.HEADERS, timeout=10)
        name = (r.json().get("name") or "").strip()
        for noise in _ASSET_NAME_NOISE:
            if name.endswith(noise):
                name = name[: -len(noise)].rstrip(" ,")
                break
        return name
    except Exception:
        return ""


# Key-hint line for the footer lives in the i18n catalog ("keys.hints") so the
# 'l' language toggle can re-render it. Plain text with spaces so it wraps onto
# extra lines when the terminal is narrow (the built-in Footer clips instead).

_TIMEFRAME_KEYS = ["1D", "1W", "1M", "3M", "6M", "1Y"]


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
                yield Button(t("btn.yes"), variant="error", id="yes")
                yield Button(t("btn.no"), variant="primary", id="no")

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
            yield Label(t("buy.title"), id="q")
            yield Input(placeholder=t("buy.sym_ph"), id="sym")
            yield Label(t("buy.avail", cash=f"{self._avail:,.2f}"), id="avail")
            yield Input(placeholder=t("buy.amt_ph"), id="amt")
            yield Label("", id="after")
            with Horizontal(id="buttons"):
                yield Button(t("btn.buy"), variant="error", id="ok")
                yield Button(t("btn.cancel"), variant="primary", id="cancel")

    def on_mount(self) -> None:
        self._update_after()
        self.query_one("#sym", Input).focus()

    def _update_after(self) -> None:
        amt = _amt_of(self.query_one("#amt", Input).value, self._avail)
        after = self._avail - amt
        lbl = self.query_one("#after", Label)
        if after < 0:
            lbl.update(t("buy.after_margin", amt=f"{amt:,.2f}", over=f"{-after:,.2f}"))
        else:
            lbl.update(t("buy.after", amt=f"{amt:,.2f}", after=f"{after:,.2f}"))

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
            yield Label(t("sell.title", sym=self._sym), id="q")
            yield Label(t("sell.info", pv=f"{self._pv:,.2f}",
                          cash=f"{self._cash:,.2f}"), id="avail")
            yield Input(value="all", id="amt", placeholder=t("sell.amt_ph"))
            yield Label("", id="after")
            with Horizontal(id="buttons"):
                yield Button(t("btn.sell"), variant="error", id="ok")
                yield Button(t("btn.cancel"), variant="primary", id="cancel")

    def on_mount(self) -> None:
        self._update_after()
        self.query_one("#amt", Input).focus()

    def _update_after(self) -> None:
        amt = min(_amt_of(self.query_one("#amt", Input).value, self._pv), self._pv)
        self.query_one("#after", Label).update(
            t("sell.after", amt=f"{amt:,.2f}", after=f"{self._cash + amt:,.2f}",
              left=f"{self._pv - amt:,.2f}"))

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
            yield Label(t("reb.title"), id="q")
            yield Input(value="20", id="topn", placeholder=t("reb.topn_ph"))
            yield Label(t("reb.avail", cash=f"{self._avail:,.2f}"), id="avail")
            yield Input(value="0", id="deploy", placeholder=t("reb.deploy_ph"))
            yield Label("", id="leftover")
            yield Input(value="0", id="withdraw", placeholder=t("reb.withdraw_ph"))
            yield Label("", id="remaining")
            yield Label("", id="err")
            with Horizontal(id="buttons"):
                yield Button(t("btn.build"), variant="error", id="ok")
                yield Button(t("btn.cancel"), variant="primary", id="cancel")

    def on_mount(self) -> None:
        self._update_leftover()
        self._update_remaining()
        self.query_one("#topn", Input).focus()

    def _update_leftover(self) -> None:
        amt = _amt_of(self.query_one("#deploy", Input).value, self._avail)
        left = self._avail - amt
        lbl = self.query_one("#leftover", Label)
        if left < 0:
            lbl.update(t("reb.deploy_over", amt=f"{amt:,.2f}", over=f"{-left:,.2f}",
                         cap=f"{self._avail:,.2f}"))
        else:
            lbl.update(t("reb.deploy_after", amt=f"{amt:,.2f}", left=f"{left:,.2f}"))

    def _update_remaining(self) -> None:
        amt = _amt_of(self.query_one("#withdraw", Input).value, self._invested)
        rem = self._invested - amt
        lbl = self.query_one("#remaining", Label)
        if rem < 0:
            lbl.update(t("reb.withdraw_over", amt=f"{amt:,.2f}",
                         inv=f"{self._invested:,.2f}"))
        else:
            lbl.update(t("reb.withdraw_after", amt=f"{amt:,.2f}", rem=f"{rem:,.2f}"))

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
            self.query_one("#err", Label).update(t("reb.err_both"))
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
            yield Label(t("sched.title"), id="q")
            yield Input(value="yes" if self._rs.get("enabled") else "no",
                        id="enabled", placeholder=t("sched.enabled_ph"))
            yield Input(value=str(self._rs.get("time_et", "16:00")),
                        id="time", placeholder=t("sched.time_ph"))
            yield Input(value="yes" if self._rs.get("weekdays_only", True) else "no",
                        id="weekdays", placeholder=t("sched.weekdays_ph"))
            yield Input(value=str(self._rs.get("channel", self._channels[0])),
                        id="channel",
                        placeholder=t("sched.channel_ph", names=", ".join(self._channels)))
            yield Label("", id="err")
            with Horizontal(id="buttons"):
                yield Button(t("btn.save"), variant="error", id="ok")
                yield Button(t("btn.cancel"), variant="primary", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#time", Input).focus()

    def _accept(self) -> None:
        time_v = self.query_one("#time", Input).value.strip()
        chan_v = self.query_one("#channel", Input).value.strip()
        if not _TIME_RE.match(time_v):
            self.query_one("#err", Label).update(t("sched.err_time"))
            return
        if chan_v not in self._channels:
            self.query_one("#err", Label).update(
                t("sched.err_channel", names=", ".join(self._channels)))
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
            yield Label(t("chan.title", names=names, default=self._default), id="q")
            yield Input(value=self._default, id="default",
                        placeholder=t("chan.default_ph"))
            yield Input(id="newname", placeholder=t("chan.newname_ph"))
            yield Input(id="tokenenv", placeholder=t("chan.token_ph"))
            yield Input(id="chatenv", placeholder=t("chan.chat_ph"))
            yield Label("", id="err")
            with Horizontal(id="buttons"):
                yield Button(t("btn.save"), variant="error", id="ok")
                yield Button(t("btn.cancel"), variant="primary", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#default", Input).focus()

    def _accept(self) -> None:
        new_name = self.query_one("#newname", Input).value.strip()
        if new_name:
            te = self.query_one("#tokenenv", Input).value.strip()
            ce = self.query_one("#chatenv", Input).value.strip()
            if not (te and ce):
                self.query_one("#err", Label).update(t("chan.err_env"))
                return
            self.dismiss({"kind": "add", "name": new_name, "token_env": te, "chat_env": ce})
            return
        dflt = self.query_one("#default", Input).value.strip()
        if dflt and dflt not in self._channels:
            self.query_one("#err", Label).update(
                t("chan.err_missing", name=dflt))
            return
        self.dismiss({"kind": "default", "name": dflt})

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self._accept()
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class EditFieldModal(ModalScreen[tuple | None]):
    """Edit ONE strategy setting. Prefilled Input + description + bounds,
    validated against config_fields. Returns ("ok", parsed_value) or None."""
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, field: cf.Field, current):
        super().__init__()
        self._field = field
        self._current = current

    def compose(self) -> ComposeResult:
        f = self._field
        rng = cf.fmt_range(f)
        label = i18n.field_text(f.path, "label") or f.label
        desc = i18n.field_text(f.path, "desc") or f.desc
        with Vertical(id="dialog"):
            yield Label(f"[b]{label}[/]  ({f.path})\n{desc}", id="q")
            yield Label(t("edit.current", cur=cf.fmt_value(f, self._current))
                        + (t("edit.range", rng=rng) if rng else ""), id="avail")
            yield Input(value=cf.fmt_value(f, self._current), id="val",
                        placeholder=rng or f.ftype)
            yield Label("", id="err")
            with Horizontal(id="buttons"):
                yield Button(t("btn.save"), variant="error", id="ok")
                yield Button(t("btn.cancel"), variant="primary", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#val", Input).focus()

    def _accept(self) -> None:
        ok, parsed = cf.validate(self._field, self.query_one("#val", Input).value)
        if not ok:
            self.query_one("#err", Label).update(f"[red]{parsed}[/]")
            return
        self.dismiss(("ok", parsed))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self._accept()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._accept()

    def action_cancel(self) -> None:
        self.dismiss(None)


class StrategyConfigModal(ModalScreen[None]):
    """Live strategy tunables browser (key 'n'). Renders the config_fields
    whitelist in a DataTable; Enter edits a row; danger fields get a
    consequence-preview ConfirmModal. Saves atomically via config_io — engines
    pick changes up on their next scheduler tick (no restart).

    intraday.* is deliberately NOT editable here: enabling it would make the
    't' key place 4x-leveraged orders and EOD-flatten the whole book."""
    BINDINGS = [
        ("escape", "close", "Close"),
        ("r", "reload", "Reload"),
    ]

    # Keys allowed to pass through while the config table has focus. EVERYTHING
    # else (t/f/c/b/s/e/a/q/…) is swallowed so app-level trading bindings can
    # never fire from inside this modal — the DataTable, unlike the Inputs in
    # the other modals, does not consume plain letter keys by itself.
    _PASS_KEYS = {"up", "down", "pageup", "pagedown", "home", "end",
                  "enter", "escape", "tab", "shift+tab", "r"}

    def __init__(self, syms: list[str]):
        super().__init__()
        self._syms = list(syms)      # holdings snapshot for danger previews
        self._col_keys = None

    def on_key(self, event: events.Key) -> None:
        if event.key not in self._PASS_KEYS:
            event.stop()
            event.prevent_default()

    def compose(self) -> ComposeResult:
        with Vertical(id="cfg_dialog"):
            yield Label("", id="cfg_head")
            yield DataTable(id="cfgtable", cursor_type="row", zebra_stripes=True)
            yield Label(i18n.t("cfg.hint"), id="cfg_hint")

    def on_mount(self) -> None:
        t = self.query_one("#cfgtable", DataTable)
        self._col_keys = t.add_columns(
            i18n.t("cfg.col.section"), i18n.t("cfg.col.setting"),
            i18n.t("cfg.col.value"), i18n.t("cfg.col.range"))
        self._populate()
        t.focus()

    def _sched_status(self) -> str:
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   ".trading_schedule_state.json")) as f:
                st = json.load(f)
        except Exception:
            st = {}
        man = (st.get("last_manage_at") or "never")[:16].replace("T", " ")
        return i18n.t("cfg.head", man=man,
                      swing=st.get("last_swing_date", "never"),
                      copy=st.get("last_copy_date", "never"))

    def _populate(self) -> None:
        cfg = config_io.load_config()
        t = self.query_one("#cfgtable", DataTable)
        t.clear()
        self.query_one("#cfg_head", Label).update(self._sched_status())
        for f in cf.FIELDS:
            val = cf.get_path(cfg, f.path)
            mark = " ⚠" if f.danger else ""
            label = i18n.field_text(f.path, "label") or f.label
            t.add_row(i18n.section_text(f.section), label + mark,
                      cf.fmt_value(f, val), cf.fmt_range(f), key=f.path)

    def action_close(self) -> None:
        self.dismiss(None)

    def action_reload(self) -> None:
        self._populate()

    # keep this modal's table events out of the app's holdings-table handlers
    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        event.stop()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        path = str(event.row_key.value) if event.row_key else ""
        field = cf.by_path(path)
        if not field:
            return
        current = cf.get_path(config_io.load_config(), path)
        self.app.push_screen(
            EditFieldModal(field, current),
            lambda res: self._after_edit(field, current, res),
        )

    def _after_edit(self, field: cf.Field, old, res) -> None:
        if not res:
            return
        _, new_val = res
        if new_val == old:
            return
        prompt = self._danger_prompt(field, new_val)
        if prompt:
            self.app.push_screen(
                ConfirmModal(prompt),
                lambda ok: self._save(field, old, new_val) if ok else None,
            )
        else:
            self._save(field, old, new_val)

    def _danger_prompt(self, field: cf.Field, new_val) -> str | None:
        """Consequence preview for danger fields; None = no confirm needed."""
        if field.danger == "master":
            eng = (i18n.t("danger.eng_swing") if field.path.startswith("swing")
                   else i18n.t("danger.eng_capitol"))
            state = i18n.t("danger.live") if new_val else i18n.t("danger.off")
            return i18n.t("danger.master", eng=eng, state=state)
        if field.danger == "prune":
            if not new_val:
                return None                      # turning OFF is harmless
            cfg = config_io.load_config()
            syms = self._syms
            if not syms:                         # modal opened before first refresh
                try:
                    syms = [p["symbol"] for p in cc.get_positions()]
                except Exception:
                    syms = []
            doomed = [s for s in syms
                      if s not in cc.NON_CC_SYMBOLS
                      and not cc.is_eligible_ticker(s, cfg)]
            return i18n.t("danger.prune", n=len(doomed),
                          names=", ".join(doomed) if doomed
                          else i18n.t("danger.prune_none"))
        if field.danger == "max_holdings":
            n_held = len([s for s in self._syms if s not in cc.NON_CC_SYMBOLS])
            if new_val < n_held:
                return i18n.t("danger.cap", new=new_val, held=n_held,
                              k=n_held - new_val)
            return None
        if field.danger == "exposure" and float(new_val) >= 1.0:
            return i18n.t("danger.exposure")
        return None

    def _save(self, field: cf.Field, old, new_val) -> None:
        try:
            config_io.update_config(lambda c: cf.set_path(c, field.path, new_val))
        except Exception as e:
            self.app._log(i18n.t("log.cfg_save_err", path=field.path, e=e))
            return
        # refresh the row and log the change
        try:
            t = self.query_one("#cfgtable", DataTable)
            t.update_cell(field.path, self._col_keys[2], cf.fmt_value(field, new_val))
        except Exception:
            self._populate()
        self.app._log(i18n.t("log.cfg_changed", path=field.path,
                             old=cf.fmt_value(field, old),
                             new=cf.fmt_value(field, new_val)))


class ManualModal(ModalScreen[None]):
    """In-app manual (key 'h') — renders README.md via Textual's MarkdownViewer,
    which auto-builds a navigable table-of-contents sidebar from the file's own
    headings. Read-only: no config or trading side effects, so it needs no ARM
    gate and no confirm modal."""
    BINDINGS = [("escape", "close", "Close")]

    # Same rationale as StrategyConfigModal: MarkdownViewer's TOC is a Tree-like
    # widget that does NOT consume plain letter keys on its own, so without this
    # guard 't'/'f'/etc. would bubble up to the app's trading bindings while the
    # manual is open. Allow only navigation/scroll keys through.
    _PASS_KEYS = {"up", "down", "left", "right", "home", "end",
                  "pageup", "pagedown", "ctrl+pageup", "ctrl+pagedown",
                  "enter", "escape", "tab", "shift+tab"}

    def compose(self) -> ComposeResult:
        base = os.path.dirname(os.path.abspath(__file__))
        readme = os.path.join(base, f"README.{i18n.get_lang()}.md")
        if not os.path.exists(readme):           # no localized manual → English
            readme = os.path.join(base, "README.md")
        try:
            with open(readme, encoding="utf-8") as f:
                text = f.read()
        except Exception as e:
            text = t("man.unavailable", file=os.path.basename(readme), e=e)
        with Vertical(id="manual_dialog"):
            yield Label(t("man.hint"), id="manual_hint")
            yield MarkdownViewer(text, show_table_of_contents=True, id="manual_viewer")

    def on_key(self, event: events.Key) -> None:
        if event.key not in self._PASS_KEYS:
            event.stop()
            event.prevent_default()

    def action_close(self) -> None:
        self.dismiss(None)


class RenameWalletModal(ModalScreen[str | None]):
    """Prompt for a new display name for a wallet. Returns the new name, or
    None on cancel."""
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, current_name: str):
        super().__init__()
        self._current = current_name

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(t("wren.title", name=self._current), id="q")
            yield Input(value=self._current, id="newname", placeholder=t("wren.ph"))
            yield Label("", id="err")
            with Horizontal(id="buttons"):
                yield Button(t("btn.rename"), variant="error", id="ok")
                yield Button(t("btn.cancel"), variant="primary", id="cancel")

    def on_mount(self) -> None:
        inp = self.query_one("#newname", Input)
        inp.focus()

    def _accept(self) -> None:
        name = self.query_one("#newname", Input).value.strip()
        if not name:
            self.query_one("#err", Label).update(t("wren.err_empty"))
            return
        self.dismiss(name)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self._accept() if event.button.id == "ok" else self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._accept()

    def action_cancel(self) -> None:
        self.dismiss(None)


class WalletModal(ModalScreen[str | None]):
    """Pick which Alpaca paper account the TUI reads + acts on (key 'k'), or
    rename a wallet with 'e'. Returns the chosen wallet name to switch to, or
    None. Unconfigured wallets (missing creds in .env) are shown but not
    selectable."""
    BINDINGS = [("escape", "close", "Close"), ("e", "rename", "Rename")]

    # DataTable doesn't consume letter keys — swallow everything except nav +
    # the rename key so 't'/'f'/etc. can't fire from inside the picker.
    _PASS_KEYS = {"up", "down", "pageup", "pagedown", "home", "end",
                  "enter", "escape", "tab", "shift+tab", "e"}

    def compose(self) -> ComposeResult:
        with Vertical(id="wallet_dialog"):
            yield Label(t("wal.title"), id="q")
            yield DataTable(id="wallettable", cursor_type="row", zebra_stripes=True)
            yield Label("", id="wallet_err")

    def on_mount(self) -> None:
        t = self.query_one("#wallettable", DataTable)
        t.add_columns("", i18n.t("wal.col.wallet"), i18n.t("wal.col.status"))
        self._populate()
        t.focus()

    def _populate(self) -> None:
        t = self.query_one("#wallettable", DataTable)
        t.clear()
        for winfo in wl.list_wallets():
            mark = "●" if winfo["is_current"] else " "
            if winfo["configured"]:
                status = (i18n.t("wal.active") if winfo["is_current"]
                          else i18n.t("wal.ready"))
            else:
                status = i18n.t("wal.missing", vars=" / ".join(winfo["missing"]))
            t.add_row(mark, winfo["name"], status, key=winfo["name"])

    def on_key(self, event: events.Key) -> None:
        if event.key not in self._PASS_KEYS:
            event.stop()
            event.prevent_default()

    def _selected_wallet(self) -> str:
        t = self.query_one("#wallettable", DataTable)
        try:
            cell_key = t.coordinate_to_cell_key(t.cursor_coordinate)
            return str(cell_key.row_key.value)
        except Exception:
            return ""

    def action_rename(self) -> None:
        old = self._selected_wallet()
        if not old:
            return
        def after(new):
            if not new:
                return
            ok, msg = wl.rename(old, new)
            err = self.query_one("#wallet_err", Label)
            if ok:
                err.update(f"[cyan]{msg}[/]")
                self._populate()
                self.app._log(f"[cyan]wallet {msg}[/]")
                self.app.refresh_data()      # badge picks up a renamed active wallet
            else:
                err.update(f"[red]{msg}[/]")
        self.app.push_screen(RenameWalletModal(old), after)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        name = str(event.row_key.value) if event.row_key else ""
        if not name:
            return
        if not wl.is_configured(name):
            miss = ", ".join(wl.missing_envs(name))
            self.query_one("#wallet_err", Label).update(
                t("wal.err_not_configured", name=name, miss=miss))
            return
        self.dismiss(name)

    def action_close(self) -> None:
        self.dismiss(None)


class CompareScreen(ModalScreen[None]):
    """Side-by-side wallet performance (key 'W') — one top-down scrollable page:
    a ranked leaderboard, an all-wallets normalized equity overlay, then per
    wallet its own equity curve + a top-down payout breakdown (every position
    sorted by unrealized P&L). Strictly READ-ONLY: data comes from compare.py's
    per-call-credential fetchers, so it never touches the active-wallet globals
    and can't place orders — no ARM gate needed."""
    BINDINGS = [
        ("escape", "close", "Close"),
        ("w", "cycle_window", "Window"),
        ("r", "refetch", "Refresh"),
    ]

    # Same rationale as ManualModal: swallow everything except scroll keys and
    # this screen's own bindings, so 't'/'f'/etc. can't reach the app's trading
    # bindings while the compare page is open.
    _PASS_KEYS = {"up", "down", "pageup", "pagedown", "home", "end",
                  "escape", "tab", "shift+tab", "w", "r"}

    _BAR_W = 22          # max payout bar length (cells)
    _TOP_N = 15          # positions shown per wallet before "… n more"

    def __init__(self):
        super().__init__()
        self._range: str = "1M"        # compare window (w cycles 1D→…→1Y)
        # Wallet sections are composed once, in config order, for every
        # DECLARED wallet (unconfigured ones render as a note, keeping ids
        # stable however .env is set up).
        self._wnames: list[str] = [w["name"] for w in wl.list_wallets()]

    def compose(self) -> ComposeResult:
        with Vertical(id="cmp_dialog"):
            yield Label("", id="cmp_hint")
            with VerticalScroll(id="cmp_scroll"):
                yield Static("[b]LEADERBOARD[/]", classes="cmp-head")
                yield DataTable(id="cmp_table", cursor_type="none",
                                zebra_stripes=True)
                yield Static("", id="cmp_note", classes="cmp-bars")
                yield Static("[b]ALL WALLETS — equity, normalized to 100 at "
                             "window start[/]", classes="cmp-head")
                yield PlotextPlot(id="cmp_overlay", classes="cmp-chart")
                for i, name in enumerate(self._wnames):
                    yield Static(f"[b]{name}[/]", id=f"cmp_head_{i}",
                                 classes="cmp-head")
                    yield PlotextPlot(id=f"cmp_chart_{i}", classes="cmp-chart")
                    yield Static("[dim]loading…[/]", id=f"cmp_bars_{i}",
                                 classes="cmp-bars")

    def on_mount(self) -> None:
        self.query_one("#cmp_table", DataTable).can_focus = False
        # Focus the scroll container so ↑/↓/PgUp/PgDn scroll the page itself.
        self.query_one("#cmp_scroll", VerticalScroll).focus()
        self._set_hint(loading=True)
        self._fetch()

    def _set_hint(self, loading: bool = False) -> None:
        state = "[yellow]fetching all wallets…[/]" if loading else "[dim]read-only[/]"
        self.query_one("#cmp_hint", Label).update(
            f"[b]Wallet comparison[/] — window [b]{self._range}[/]   {state}\n"
            f"[dim]↑/↓ PgUp/PgDn: scroll · w: cycle window · r: refresh · "
            f"Esc: close · trade keys disabled here[/]")

    def on_key(self, event: events.Key) -> None:
        if event.key not in self._PASS_KEYS:
            event.stop()
            event.prevent_default()

    def action_close(self) -> None:
        self.dismiss(None)

    def action_cycle_window(self) -> None:
        idx = _TIMEFRAME_KEYS.index(self._range)
        self._range = _TIMEFRAME_KEYS[(idx + 1) % len(_TIMEFRAME_KEYS)]
        self._set_hint(loading=True)
        self._fetch()

    def action_refetch(self) -> None:
        self._set_hint(loading=True)
        self._fetch()

    @work(thread=True, exclusive=True, group="cmp_fetch")
    def _fetch(self) -> None:
        results = cmpw.gather(self._range)
        self.app.call_from_thread(self._populate, results)

    # ── rendering ────────────────────────────────────────────────────────────

    @staticmethod
    def _date_forms(rng: str) -> tuple[str, str]:
        """(plotext date_form, strftime fmt) for the compare window."""
        if rng == "1D":
            return "H:M", "%H:%M"
        if rng == "1W":
            return "m/d H:M", "%m/%d %H:%M"
        return "m/d", "%m/%d"

    def _hist_labels(self, hist: list[tuple[str, float]], fmt: str
                     ) -> tuple[list[str], list[float]]:
        """Format history timestamps for plotext; skip unparseable points
        (a bad label makes plotext raise and kills the plot)."""
        ds, ys = [], []
        for iso, eq in hist:
            try:
                d = dt.datetime.fromisoformat(iso).astimezone(_ET)
            except Exception:
                continue
            ds.append(d.strftime(fmt))
            ys.append(eq)
        return ds, ys

    def _populate(self, results: list[dict]) -> None:
        self._set_hint(loading=False)
        rng = self._range
        by_name = {r["name"]: r for r in results}

        # 1) Leaderboard — ranked by window return, errors/unconfigured last.
        tbl = self.query_one("#cmp_table", DataTable)
        tbl.clear(columns=True)
        tbl.add_columns("#", "WALLET", "EQUITY", "DAY P&L", "DAY %",
                        f"{rng} RET %", "TOTAL P&L", "CASH", "EXPO", "POS")
        ranked = sorted(
            (r for r in results if r.get("ok")),
            key=lambda r: (r.get("win_ret") is None,
                           -(r.get("win_ret") or 0.0)))
        for rank, r in enumerate(ranked, start=1):
            dc = "green" if r["day_pl"] >= 0 else "red"
            tc = "green" if r["total_pl"] >= 0 else "red"
            wr = r.get("win_ret")
            wr_txt = (Text("n/a", style="dim") if wr is None else
                      Text(f"{wr:+.2f}%", style="green" if wr >= 0 else "red"))
            marker = "●" if r["name"] == wl.current() else ""
            tbl.add_row(
                f"{rank}{marker}", r["name"], f"${r['equity']:,.0f}",
                Text(f"{r['day_pl']:+,.0f}", style=dc),
                Text(f"{r['day_pct']:+.2f}%", style=dc),
                wr_txt,
                Text(f"{r['total_pl']:+,.0f} ({r['total_pct']:+.1f}%)", style=tc),
                f"${r['cash']:,.0f}", f"{r['exposure']:.2f}x", str(r["npos"]),
            )
        broken = [r for r in results if not r.get("ok")]
        for r in broken:
            tbl.add_row("—", r["name"], Text(r.get("err", "error"),
                                             style="yellow"),
                        "", "", "", "", "", "", "")
        note = ("[dim]● = active wallet · TOTAL P&L vs $100k paper baseline · "
                f"{rng} RET % from Alpaca portfolio history[/]")
        self.query_one("#cmp_note", Static).update(note)

        # 2) Overlay — every ok wallet's equity, base-100 at window start.
        date_form, fmt = self._date_forms(rng)
        overlay = self.query_one("#cmp_overlay", PlotextPlot)
        plt = overlay.plt
        plt.clear_figure()
        plotted = 0
        try:
            plt.date_form(date_form)
            for r in ranked:
                ds, ys = self._hist_labels(r.get("hist", []), fmt)
                base = next((y for y in ys if y), 0.0)
                if len(ds) < 2 or not base:
                    continue
                plt.plot(ds, [y / base * 100 for y in ys], label=r["name"])
                plotted += 1
            plt.title(f"ALL WALLETS [{rng}] — normalized equity"
                      if plotted else f"ALL WALLETS [{rng}] — no history")
        except Exception:
            plt.clear_figure()
            plt.title(f"ALL WALLETS [{rng}] — render error")
        overlay.refresh()

        # 3) Per-wallet sections: equity curve + top-down payout bars.
        for i, name in enumerate(self._wnames):
            r = by_name.get(name)
            head = self.query_one(f"#cmp_head_{i}", Static)
            chart = self.query_one(f"#cmp_chart_{i}", PlotextPlot)
            bars = self.query_one(f"#cmp_bars_{i}", Static)
            cplt = chart.plt
            cplt.clear_figure()
            if not r or not r.get("ok"):
                head.update(f"[b]{name}[/]  [yellow]{(r or {}).get('err', 'no data')}[/]")
                cplt.title(f"{name} — unavailable")
                chart.refresh()
                bars.update("")
                continue
            wr = r.get("win_ret")
            wr_txt = "n/a" if wr is None else f"{wr:+.2f}%"
            head.update(
                f"[b]{name}[/]   equity [b]${r['equity']:,.0f}[/] · "
                f"{rng} return [b]{wr_txt}[/] · cash ${r['cash']:,.0f} · "
                f"exposure {r['exposure']:.2f}x · {r['npos']} positions")
            ds, ys = self._hist_labels(r.get("hist", []), fmt)
            try:
                if len(ds) >= 2:
                    cplt.date_form(date_form)
                    cplt.plot(ds, ys)
                    cplt.title(f"{name} [{rng}]   ${ys[-1]:,.0f}  ({wr_txt})")
                else:
                    cplt.title(f"{name} [{rng}] — no history")
            except Exception:
                cplt.clear_figure()
                cplt.title(f"{name} [{rng}] — render error")
            chart.refresh()
            bars.update(self._payout_text(r))

    def _payout_text(self, r: dict) -> str:
        """Top-down payout: every position, biggest unrealized P&L first, with
        a bar scaled to the wallet's own largest |P&L|."""
        rows = sorted(r.get("positions", []),
                      key=lambda p: _f(p.get("unrealized_pl")), reverse=True)
        if not rows:
            return "[dim]no positions — all cash[/]"
        maxabs = max(abs(_f(p.get("unrealized_pl"))) for p in rows) or 1.0
        lmv = r.get("lmv", 0.0)
        lines = []
        for p in rows[:self._TOP_N]:
            pl = _f(p.get("unrealized_pl"))
            plpc = _f(p.get("unrealized_plpc")) * 100
            wt = (_f(p.get("market_value")) / lmv * 100) if lmv else 0.0
            blen = max(1, round(abs(pl) / maxabs * self._BAR_W))
            col = "green" if pl >= 0 else "red"
            lines.append(
                f"[b]{p.get('symbol', '?'):<6}[/] {wt:5.1f}%  "
                f"[{col}]{'█' * blen:<{self._BAR_W}}[/]  "
                f"[{col}]{pl:+9,.0f}  {plpc:+6.1f}%[/]")
        if len(rows) > self._TOP_N:
            lines.append(f"[dim]… {len(rows) - self._TOP_N} more positions[/]")
        return "\n".join(lines)


class CandlePlot(PlotextPlot):
    """PlotextPlot that reports the x-column of a click so the app can map it to
    the nearest candle (terminals don't expose true plot hit-testing)."""

    class Picked(Message):
        def __init__(self, x: int, width: int) -> None:
            self.x = x
            self.width = width
            super().__init__()

    def on_click(self, event: events.Click) -> None:
        self.post_message(self.Picked(event.x, self.size.width))


# ── Main app ─────────────────────────────────────────────────────────────────

class AlpacaTUI(App):
    TITLE = "Alpaca Paper Trader — Cockpit"
    CSS = """
    #summary { height: auto; padding: 0 1; text-style: bold; }
    #armbar  { height: 1; content-align: center middle; }
    #holdings { height: 1fr; text-style: bold; }            /* flexes: absorbs bio growth */
    #search { height: auto; }                               /* ticker bio search bar */
    #ticker_search { margin: 0 1; }
    #bio { height: auto; max-height: 8; padding: 0 1; color: $text-muted; overflow-y: auto; }
    #bottom  { height: 50%; layout: vertical; }             /* fixed share — bio growth never pushes the graph */
    #chart { height: 1fr; border: solid $accent; }          /* takes the rest */
    #log { height: 7; border: solid $accent; }              /* fixed 5 visible lines + border */
    #keys {
        dock: bottom;
        height: auto;
        padding: 0 1;
        background: $panel;
        color: $text-muted;
    }
    ConfirmModal, BuyModal, EditFieldModal, StrategyConfigModal, ManualModal, WalletModal, RenameWalletModal, CompareScreen { align: center middle; }
    #cmp_dialog { width: 96%; height: 96%; padding: 1 2; background: $surface; border: thick $accent; }
    #cmp_hint { height: auto; }
    #cmp_scroll { height: 1fr; }
    #cmp_table { height: auto; }
    #cmp_note { height: auto; }
    .cmp-chart { height: 16; border: solid $accent; }
    .cmp-head { height: auto; padding: 1 0 0 0; }
    .cmp-bars { height: auto; padding: 0 1; }
    #wallet_dialog { width: 72; height: auto; padding: 1 2; background: $surface; border: thick $accent; }
    #wallettable { height: auto; }
    #dialog { width: 64; height: auto; padding: 1 2; background: $surface; border: thick $accent; }
    #buttons { height: auto; align-horizontal: center; }
    #buttons Button { margin: 1 2 0 2; }
    #cfg_dialog { width: 96; height: 80%; padding: 1 2; background: $surface; border: thick $accent; }
    #cfgtable { height: 1fr; }
    #cfg_head, #cfg_hint { height: auto; }
    #manual_dialog { width: 96%; height: 90%; padding: 1 2; background: $surface; border: thick $accent; }
    #manual_hint { height: auto; }
    #manual_viewer { height: 1fr; }
    """

    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("d", "dryrun", "Dry-run RS"),
        Binding("p", "push_report", "Push report"),
        Binding("o", "overview", "Portfolio chart"),
        Binding("w", "cycle_timeframe", "Timeframe"),
        Binding("v", "view_toggle", "Positions/Orders"),
        Binding("g", "edit_schedule", "Edit schedule"),
        Binding("m", "edit_channels", "Channels"),
        Binding("n", "edit_strategy", "Strategy cfg"),
        Binding("k", "switch_wallet", "Wallet"),
        Binding("W", "compare_wallets", "Compare wallets"),
        Binding("l", "toggle_language", "語言/Language"),
        Binding("h", "open_manual", "Manual"),
        Binding("a", "arm", "Arm/Disarm"),
        Binding("f", "flatten", "Flatten ALL"),
        Binding("t", "tick", "Live tick"),
        Binding("c", "capitol", "Capitol run"),
        Binding("b", "buy", "Buy"),
        Binding("s", "sell", "Sell row"),
        Binding("e", "rebalance", "Rebalance top-N"),
        Binding("x", "cancel_order", "Cancel order"),
        Binding("X", "cancel_all_orders", "Cancel all"),
        Binding("/", "focus_search", "Bio search"),
        Binding("q", "quit", "Quit"),
    ]

    armed = reactive(False)

    def __init__(self):
        super().__init__()
        # Language must be set BEFORE compose() resolves t() for chrome strings.
        try:
            i18n.set_lang(im.load_config().get("tui", {}).get("language", "en"))
        except Exception:
            pass
        self._acct_cache: dict | None = None   # last acct dict (for language re-render)
        self._clk_cache: dict | None = None    # last clock dict (same)
        self._syms: list[str] = []   # holdings symbols in row order (for sell)
        self.market_open: bool | None = None   # None until first clock fetch
        self._next_open: str = "?"             # human-readable next-open time
        self._tg_notify: bool = True           # send Telegram pings on actions
        self._mkt_known: bool | None = None    # last market state (for transitions)
        self._cash: float = 0.0                # idle cash from last refresh
        self._lmv: float = 0.0                 # long market value (invested) from last refresh
        self._mv: dict[str, float] = {}        # per-symbol market value from last refresh
        self._price: dict[str, float] = {}     # per-symbol current price from last refresh
        self._equity: float = 0.0              # account equity from last refresh
        self._tick: int = 0                    # refresh counter
        self._sel_sym: str = ""                # holding the cursor is on
        self._portfolio_mode: bool = False     # True = chart the whole portfolio (key 'o')
        self._view_mode: str = "positions"     # "positions" or "orders" (key 'v')
        self._open_orders: list[dict] = []     # last fetched open orders
        self._order_ids: list[str] = []        # order IDs in current table row order
        self._order_syms: list[str] = []       # symbols for those orders
        self._positions_cache: list[dict] = [] # last fetched positions (for view repop)
        self._chart_range: str = "1D"          # active timeframe key
        # live per-refresh candle series, keyed by subject ("" portfolio | symbol):
        #   each entry is (epoch_ts, price); candles are built from consecutive samples
        self._series: dict[str, list[tuple[float, float]]] = {}
        self._candles: list[dict] = []         # last-rendered candles (for click hit-testing)
        self._hist_cache: dict[str, list[dict]] = {}  # cached API bars keyed by "range:subject"
        self._names: dict[str, str] = {}       # symbol → company full name (chart title)
        self._bio_cache: dict[str, tuple[str, str]] = {}  # symbol → (name, en_desc)
        self._bio_zh: dict[str, str] = {}      # symbol → 繁體中文 bio translation
        self._bio_sym: str = ""                # symbol currently shown in #bio

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(t("ui.loading"), id="summary")
        yield Static(id="armbar")
        yield DataTable(id="holdings", cursor_type="row", zebra_stripes=True)
        with Vertical(id="search"):
            yield Input(placeholder=t("ui.search_ph"), id="ticker_search")
            yield Static("", id="bio")
        with Vertical(id="bottom"):
            yield CandlePlot(id="chart")
            yield RichLog(id="log", highlight=False, markup=True)
        yield Static(t("keys.hints"), id="keys")

    def _set_table_columns(self) -> None:
        """Set (or reset) DataTable columns for the current view mode."""
        t = self.query_one("#holdings", DataTable)
        t.clear(columns=True)
        if self._view_mode == "positions":
            t.add_columns(i18n.t("col.sym"), i18n.t("col.qty"), i18n.t("col.avg"),
                          i18n.t("col.price"), i18n.t("col.mkt_val"),
                          i18n.t("col.cost"), i18n.t("col.pl_usd"),
                          i18n.t("col.pl_pct"))
        else:
            t.add_columns(i18n.t("col.sym"), i18n.t("col.side"),
                          i18n.t("col.qty_usd"), i18n.t("col.status"),
                          i18n.t("col.submitted"), i18n.t("col.id"))

    def on_mount(self) -> None:
        self._set_table_columns()
        self.watch_armed(self.armed)
        try:
            self._tg_notify = im.load_config().get("tui", {}).get("telegram_notify", True)
        except Exception:
            self._tg_notify = True
        self.title = t("app.title")
        tg_state = t("state.on") if (self._tg_notify and tg is not None) else t("state.off")
        self._log(t("log.booted", state=tg_state))
        self._log(t("log.boot2", status=self._schedule_status()))
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
            self.call_from_thread(self._log, t("log.tg_failed", e=e))

    def _mkt_suffix(self) -> str:
        """Clarify in notifications that orders queue when the market is closed."""
        if self.market_open is False:
            return f" — QUEUED to next open ({self._next_open})"
        return ""

    def _schedule_status(self) -> str:
        try:
            rs = im.load_config().get("report_schedule", {}) or {}
        except Exception:
            return t("rs.unknown")
        if rs.get("enabled"):
            scope = t("rs.weekdays") if rs.get("weekdays_only", True) else t("rs.daily")
            return t("rs.on", time=rs.get("time_et", "16:00"), scope=scope)
        return t("rs.off")

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
            self._log(t("log.cfg_read_err", e=e))
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
                warn = t("log.sched_warn_window")
            self.call_from_thread(self._log, f"[cyan]{self._schedule_status()}[/]{warn}")
        except Exception as e:
            self.call_from_thread(self._log, t("log.sched_save_err", e=e))

    # ── channel editor (m) — config only, never .env ─────────────────────────-
    def action_edit_channels(self) -> None:
        try:
            cfg = im.load_config()
        except Exception as e:
            self._log(t("log.cfg_read_err", e=e))
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
                    t("log.chan_added", name=res["name"], token=res["token_env"],
                      chat=res["chat_env"]))
            else:
                self.call_from_thread(self._log,
                                      t("log.chan_default", name=res.get("name")))
        except Exception as e:
            self.call_from_thread(self._log, t("log.chan_save_err", e=e))

    # ── strategy tunables editor (n) — see config_fields.py for the registry ──
    def action_edit_strategy(self) -> None:
        self.push_screen(StrategyConfigModal(list(self._syms)))

    # ── wallet switcher (k) — swap the active Alpaca paper account ────────────
    def action_switch_wallet(self) -> None:
        def after(name):
            if name:
                self._do_switch_wallet(name)
        self.push_screen(WalletModal(), after)

    def _do_switch_wallet(self, name: str) -> None:
        if name == wl.current():
            self._log(t("log.wallet_same", name=name))
            return
        ok, msg = wl.apply(name)
        if not ok:
            self._log(f"[red]{msg}[/]")
            return
        # Safety: never carry an armed state across accounts (arm on Main →
        # switch → flatten would hit the wrong book).
        self.armed = False
        # Data for the previous account is now stale — drop every cache and the
        # cursor selection, then force a fresh pull for the new account.
        self._hist_cache.clear()
        self._series.clear()
        self._price.clear()
        self._sel_sym = ""
        self._candles = []
        self._log(t("log.wallet_switched", name=name))
        if name != wl.default_name():
            self._log(t("log.wallet_engines", name=wl.default_name()))
        self.refresh_data()

    def action_compare_wallets(self) -> None:
        """Side-by-side performance of every configured wallet (read-only)."""
        self.push_screen(CompareScreen())

    # ── language toggle (l) — English ⇄ 繁體中文, persisted ────────────────────
    def action_toggle_language(self) -> None:
        """Cycle the UI language and persist it. Modals re-read t() every time
        they open, so only the main screen needs an explicit re-render."""
        i18n.set_lang(i18n.next_lang())
        try:
            def mut(cfg):
                cfg.setdefault("tui", {})["language"] = i18n.get_lang()
                return cfg
            config_io.update_config(mut)
        except Exception as e:
            self._log(t("log.cfg_save_err", path="tui.language", e=e))
        self._apply_language()
        self._log(t("log.lang", lang=i18n.lang_label()))

    def _apply_language(self) -> None:
        """Re-render every live main-screen string in the current language,
        from cached account data — no network hit."""
        self.title = t("app.title")
        self.query_one("#keys", Static).update(t("keys.hints"))
        self.query_one("#ticker_search", Input).placeholder = t("ui.search_ph")
        self.watch_armed(self.armed)
        self._set_table_columns()
        if self._acct_cache is not None:
            self._apply(self._acct_cache, self._positions_cache,
                        self._clk_cache, self._open_orders)
        else:
            self._repopulate_table()
            self.refresh_data()
        self._fetch_and_render()          # chart title in the new language
        if self._bio_sym:                 # re-render the bio in the new language
            self._lookup_bio(self._bio_sym)

    # ── in-app manual (h) — renders README.md with a built-in topic index ─────
    def action_open_manual(self) -> None:
        self.push_screen(ManualModal())

    def action_arm(self) -> None:
        self.armed = not self.armed
        if self.armed and self.market_open is False:
            self._log(t("log.arm_closed", next=self._next_open))

    def watch_armed(self, val: bool) -> None:
        bar = self.query_one("#armbar", Static)
        if val:
            bar.update(t("armbar.armed"))
        else:
            bar.update(t("armbar.disarmed"))

    def _require_armed(self) -> bool:
        if not self.armed:
            self._log(t("log.need_arm"))
            return False
        return True

    # ── data refresh ───────────────────────────────────────────────────────--
    def action_refresh(self) -> None:
        self._log(t("log.refreshing"))
        self.refresh_data()

    @work(thread=True, exclusive=True, group="data")
    def refresh_data(self) -> None:
        try:
            acct = hr.fetch_account()
            positions = hr.fetch_positions()
            open_orders = hr.fetch_open_orders()
        except Exception as e:
            self.call_from_thread(self._log, t("log.refresh_err", e=e))
            return
        try:
            clk = im.get_clock()
        except Exception:
            clk = None
        self.call_from_thread(self._apply, acct, positions, clk, open_orders)

        # Refresh chart with latest data after apply — clear cache so bars are fresh
        self._hist_cache.clear()
        self.call_from_thread(self._fetch_and_render)

    # ── ticker bio search ──────────────────────────────────────────────────────
    def action_focus_search(self) -> None:
        self.query_one("#ticker_search", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # App-level handler: only the bio search box lives here (modal Inputs
        # handle their own submits on their own screens).
        if event.input.id != "ticker_search":
            return
        sym = re.sub(r"[^A-Za-z0-9.\-]", "", event.value.strip().upper())
        bio = self.query_one("#bio", Static)
        if not sym:
            self._bio_sym = ""
            bio.update("")
            return
        bio.update(t("bio.looking", sym=sym))
        self._lookup_bio(sym)

    def on_key(self, event: events.Key) -> None:
        # Escape out of the search box back to the holdings table.
        if event.key == "escape":
            inp = self.query_one("#ticker_search", Input)
            if inp.has_focus:
                self.query_one("#holdings", DataTable).focus()
                event.stop()

    @work(thread=True, exclusive=True, group="ticker_bio")
    def _lookup_bio(self, symbol: str) -> None:
        self._bio_sym = symbol
        if symbol in self._bio_cache:               # repeat lookup / 'l' re-render
            name, desc = self._bio_cache[symbol]
        else:
            name, desc = fetch_company_bio(symbol)
            if desc != "__NO_KEY__" and (name or desc):
                self._bio_cache[symbol] = (name, desc)
        if desc == "__NO_KEY__":
            text = t("bio.no_key")
        elif not name and not desc:
            text = t("bio.none", sym=symbol)
        elif not desc:
            text = f"[b]{name}[/]\n" + t("bio.nodesc")
        else:
            shown = desc
            if i18n.get_lang() == "zh_TW":
                zh = self._bio_zh.get(symbol)
                if zh is None:
                    # Show English immediately, then swap in the translation
                    # (one haiku call per symbol; cached after that).
                    self.call_from_thread(
                        self.query_one("#bio", Static).update,
                        f"[b]{name}[/] ({symbol})  "
                        f"[dim]{t('bio.translating')}[/]\n{desc}")
                    zh = translate_to_zh(desc)
                    if zh:
                        self._bio_zh[symbol] = zh
                if zh:
                    shown = zh
                if symbol != self._bio_sym:   # user moved on mid-translation
                    return
            text = f"[b]{name}[/] ({symbol})\n{shown}"
        self.call_from_thread(self.query_one("#bio", Static).update, text)

    # ── candlestick chart: per-refresh candles of the selected holding, or the
    #    whole portfolio (key 'o'). Each candle = the move over one 8s refresh. ──
    def _chart_subject(self) -> str:
        return "__PORTFOLIO__" if self._portfolio_mode else self._sel_sym

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        # Only react to the main holdings table — the strategy-config modal has
        # its own DataTable whose events must not drive the chart (its modal
        # stops them too; this guard is belt-and-suspenders).
        if getattr(event.control, "id", None) != "holdings":
            return
        if self._view_mode == "orders":
            return
        sym = str(event.row_key.value) if event.row_key else ""
        if sym and sym != self._sel_sym:
            self._sel_sym = sym
            if not self._portfolio_mode:
                self._fetch_and_render()        # switch to this symbol's series

    def action_overview(self) -> None:
        """Toggle: chart the whole portfolio (equity) vs the selected holding."""
        self._portfolio_mode = not self._portfolio_mode
        where = (t("chart.pf") if self._portfolio_mode
                 else t("chart.holding", sym=self._sel_sym or "—"))
        self._log(t("log.chart_to", where=where))
        self._fetch_and_render()

    def action_cycle_timeframe(self) -> None:
        """Cycle through 1D → 1W → 1M → 3M → 6M → 1Y → 1D …"""
        idx = _TIMEFRAME_KEYS.index(self._chart_range)
        self._chart_range = _TIMEFRAME_KEYS[(idx + 1) % len(_TIMEFRAME_KEYS)]
        self._hist_cache.clear()                # invalidate cache on range change
        self._log(t("log.chart_tf", rng=self._chart_range))
        self._fetch_and_render()

    def _fetch_and_render(self) -> None:
        """Kick off a background fetch for chart data, then render."""
        self._do_chart_fetch()

    @work(thread=True, exclusive=True, group="chart")
    def _do_chart_fetch(self) -> None:
        """Fetch historical bars from Alpaca API in a background thread."""
        subj = self._chart_subject()
        rng = self._chart_range
        cache_key = f"{rng}:{subj}"
        if cache_key not in self._hist_cache:
            if not subj:
                self.call_from_thread(self._render_candles, [])
                return
            if self._portfolio_mode:
                bars = hr.fetch_portfolio_history_ranged(rng)
            else:
                bars = hr.fetch_bars_ranged(subj, rng)
            self._hist_cache[cache_key] = bars
        # Company full name for the chart title (once per symbol, then cached).
        if subj and subj != "__PORTFOLIO__" and subj not in self._names:
            self._names[subj] = fetch_asset_name(subj)
        self.call_from_thread(self._render_candles, self._hist_cache.get(cache_key, []))

    def _render_candles(self, bars: list[dict] | None = None) -> None:
        plot = self.query_one("#chart", CandlePlot)
        plt = plot.plt
        plt.clear_figure()
        subj = self._chart_subject()
        rng = self._chart_range
        if self._portfolio_mode:
            label = t("chart.pf")
        else:
            label = self._sel_sym or "—"
            name = self._names.get(self._sel_sym, "")
            if name:                        # "HOOD · Robinhood Markets, Inc."
                label = f"{label} · {name}"
        if not self._portfolio_mode and not self._sel_sym:
            plt.title(t("chart.select"))
            self._candles = []
            plot.refresh()
            return
        if bars is None:
            # Fallback: try cache
            cache_key = f"{rng}:{subj}"
            bars = self._hist_cache.get(cache_key, [])
        if not bars:
            plt.title(t("chart.loading", label=label, rng=rng))
            self._candles = []
            plot.refresh()
            return
        # Get the date label format for this timeframe
        _, _, date_fmt = hr.CHART_TIMEFRAMES.get(rng, (5, "5Min", "%H:%M"))
        O = []; H = []; L = []; C = []; ds = []; cand = []
        for b in bars:
            t_raw = b.get("t", "")
            # Parse ISO timestamp. A label that doesn't match plotext's date_form
            # makes plt.candlestick() raise and kills the whole chart, so a bar we
            # can't format is SKIPPED (keeps ds aligned + every label valid)
            # rather than inserting a raw string.
            try:
                t_dt = dt.datetime.fromisoformat(t_raw.replace("Z", "+00:00")).astimezone(_ET)
                tlabel = t_dt.strftime(date_fmt)
            except Exception:
                continue
            try:
                o_val = float(b.get("o", 0)); h_val = float(b.get("h", 0))
                l_val = float(b.get("l", 0)); c_val = float(b.get("c", 0))
            except (TypeError, ValueError):
                continue
            O.append(o_val); H.append(h_val); L.append(l_val); C.append(c_val)
            ds.append(tlabel)
            cand.append({"t": tlabel, "o": o_val, "c": c_val})
        self._candles = cand
        if not ds:                       # every bar was unparseable
            plt.title(t("chart.nodata", label=label, rng=rng))
            plot.refresh()
            return
        # Pick plotext date_form based on range
        if rng == "1D":
            plt.date_form("H:M")
        elif rng == "1W":
            plt.date_form("m/d H:M")
        else:
            plt.date_form("m/d")
        # Never let a plotext quirk crash the worker callback that renders this.
        try:
            plt.candlestick(ds, {"Open": O, "High": H, "Low": L, "Close": C})
            chg = C[-1] - O[0]
            pct = (chg / O[0] * 100) if O[0] else 0.0
            plt.title(f"{label} [{rng}]   ${C[-1]:,.2f}  ({pct:+.2f}%)")
        except Exception as e:
            plt.clear_figure()
            plt.title(t("chart.render_err", label=label, rng=rng))
            self._log(t("log.chart_render_err", rng=rng, e=e))
        plot.refresh()

    def on_candle_plot_picked(self, message: CandlePlot.Picked) -> None:
        """Map a click x-column to the nearest candle and log its price (approx —
        terminals don't expose true plot hit-testing)."""
        if not self._candles:
            return
        n = len(self._candles)
        left, right = 8, 2                       # ~y-axis labels + borders
        span = max(1, message.width - left - right)
        frac = (message.x - left) / span
        idx = min(n - 1, max(0, round(frac * (n - 1))))
        c = self._candles[idx]
        arrow = "[green]▲[/]" if c["c"] >= c["o"] else "[red]▼[/]"
        self._log(t("log.candle", t=c["t"], c=f"{c['c']:,.2f}", arrow=arrow,
                    o=f"{c['o']:,.2f}"))

    def _wallet_badge(self) -> str:
        """Active-wallet chip for the summary line — always visible so manual
        actions (flatten/sell/…) can never be aimed at the wrong account by
        mistake. Default account = blue; any other = magenta (stands out)."""
        name = wl.current()
        style = "white on blue" if name == wl.default_name() else "white on magenta"
        return f"[b {style}] 💼 {name} [/]"

    def _market_badge(self, clk: dict | None) -> str:
        """Update market state from the clock and return a status badge string."""
        if clk is None:
            self.market_open = None
            return t("badge.market_unknown")
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
            return t("badge.market_open")
        return t("badge.market_closed", next=self._next_open)

    def _apply(self, acct: dict, positions: list[dict], clk: dict | None = None,
               open_orders: list[dict] | None = None) -> None:
        self._tick += 1
        self._acct_cache = acct
        self._clk_cache = clk
        self._positions_cache = positions
        self._open_orders = open_orders or []
        equity = _f(acct.get("equity"))
        cash   = _f(acct.get("cash"))
        self._cash = cash
        self._equity = equity
        last   = _f(acct.get("last_equity"), equity)
        rt     = _f(acct.get("regt_buying_power"))
        dtbp   = _f(acct.get("daytrading_buying_power"))
        lmv    = _f(acct.get("long_market_value"))
        self._lmv = lmv
        daypl  = equity - last
        daypct = (daypl / last * 100) if last else 0.0
        totalpl = equity - 100000.0
        totalpct = (totalpl / 100000.0) * 100
        lev    = (lmv / equity) if equity else 0.0
        pc = "green" if daypl >= 0 else "red"
        tpc = "green" if totalpl >= 0 else "red"
        self.query_one("#summary", Static).update(
            t("sum.line", equity=f"{equity:,.0f}", stocks=f"{lmv:,.0f}",
              cash=f"{cash:,.0f}", regt=f"{rt:,.0f}", dt=f"{dtbp:,.0f}",
              pc=pc, daypl=f"{daypl:+,.0f}", daypct=f"{daypct:+.2f}",
              tpc=tpc, totalpl=f"{totalpl:+,.0f}", totalpct=f"{totalpct:+.2f}",
              lev=f"{lev:.2f}")
            + f"\n{self._wallet_badge()}  {self._market_badge(clk)}"
        )

        # Record time-series data for charts
        now_ts = dt.datetime.now().timestamp()
        if acct.get("equity") is not None:
            buf = self._series.setdefault("__PORTFOLIO__", [])
            buf.append((now_ts, equity))
            del buf[:-240]
        for p in positions:
            if p.get("current_price") is not None:
                sym = p.get("symbol", "")
                cur = _f(p.get("current_price"))
                self._price[sym] = cur
                buf = self._series.setdefault(sym, [])
                buf.append((now_ts, cur))
                del buf[:-240]

        self._repopulate_table()
        n_ord = len(self._open_orders)
        ord_tag = t("log.orders_tag", n=n_ord) if n_ord else ""
        self._log(t("log.refreshed", n=len(positions), orders=ord_tag,
                    time=f"{dt.datetime.now():%H:%M:%S}"))

    # ── table population (positions or orders) ────────────────────────────────
    def _repopulate_table(self) -> None:
        """Re-fill the holdings DataTable from cached data (no column reset)."""
        t = self.query_one("#holdings", DataTable)
        t.clear()
        if self._view_mode == "positions":
            self._syms = []
            self._mv = {}
            for p in sorted(self._positions_cache,
                            key=lambda x: _f(x.get("unrealized_pl")), reverse=True):
                sym  = p.get("symbol", "?")
                qty  = _f(p.get("qty"))
                avg  = _f(p.get("avg_entry_price"))
                cur  = _f(p.get("current_price"))
                mv   = _f(p.get("market_value"), qty * cur)     # 總估值 = QTY × PRICE
                cost = _f(p.get("cost_basis"), qty * avg)       # 總成本 = total bought in
                pl   = _f(p.get("unrealized_pl"))
                plpc = _f(p.get("unrealized_plpc")) * 100
                col  = "green" if pl >= 0 else "red"
                t.add_row(
                    sym, f"{qty:g}", f"{avg:.2f}", f"{cur:.2f}",
                    f"{mv:,.0f}", f"{cost:,.0f}",
                    Text(f"{pl:+,.0f}", style=col), Text(f"{plpc:+.1f}%", style=col),
                    key=sym,
                )
                self._syms.append(sym)
                self._mv[sym] = mv
            if self._sel_sym in self._syms:
                try:
                    t.move_cursor(row=self._syms.index(self._sel_sym), animate=False)
                except Exception:
                    pass
        else:
            self._order_ids = []
            self._order_syms = []
            for o in self._open_orders:
                oid       = o.get("id", "?")
                sym       = o.get("symbol", "?")
                side      = (o.get("side") or "?").upper()
                notional  = o.get("notional")
                qty       = o.get("qty")
                status    = o.get("status", "?")
                submitted = (o.get("submitted_at") or "")[:16].replace("T", " ")
                short_id  = str(oid)[:8]
                if notional:
                    qty_str = f"${float(notional):,.0f}"
                elif qty:
                    qty_str = f"{float(qty):g} sh"
                else:
                    qty_str = "?"
                col = "green" if side == "BUY" else "red"
                t.add_row(
                    sym, Text(side, style=col), qty_str,
                    status, submitted, short_id,
                    key=oid,
                )
                self._order_ids.append(oid)
                self._order_syms.append(sym)

    # ── view toggle (v) ───────────────────────────────────────────────────────
    def action_view_toggle(self) -> None:
        self._view_mode = "orders" if self._view_mode == "positions" else "positions"
        self._set_table_columns()
        self._repopulate_table()
        label = t("view.orders") if self._view_mode == "orders" else t("view.positions")
        hint = t("view.orders_hint") if self._view_mode == "orders" else ""
        self._log(t("log.view", label=label, hint=hint))

    # ── cancel order actions (x / X) — require ARM ───────────────────────────
    def action_cancel_order(self) -> None:
        if self._view_mode != "orders":
            self._log(i18n.t("log.orders_first"))
            return
        if not self._require_armed():
            return
        t = self.query_one("#holdings", DataTable)
        row = t.cursor_row
        if row is None or row < 0 or row >= len(self._order_ids):
            self._log(i18n.t("log.no_order_sel"))
            return
        oid = self._order_ids[row]
        sym = self._order_syms[row]
        self.push_screen(
            ConfirmModal(i18n.t("confirm.cancel_order", sym=sym, id=oid[:8])),
            lambda ok: self._do_cancel_order(oid, sym) if ok else None,
        )

    @work(thread=True, group="action")
    def _do_cancel_order(self, order_id: str, sym: str) -> None:
        ok = hr.cancel_order(order_id)
        if ok:
            self.call_from_thread(self._log,
                                  t("log.cancelled", sym=sym, id=order_id[:8]))
        else:
            self.call_from_thread(self._log,
                                  t("log.cancel_failed", id=order_id[:8]))
        self.call_from_thread(self.refresh_data)

    def action_cancel_all_orders(self) -> None:
        if self._view_mode != "orders":
            self._log(i18n.t("log.orders_first"))
            return
        if not self._require_armed():
            return
        n = len(self._open_orders)
        if n == 0:
            self._log(t("log.no_orders"))
            return
        self.push_screen(
            ConfirmModal(t("confirm.cancel_all", n=n)),
            lambda ok: self._do_cancel_all_orders() if ok else None,
        )

    @work(thread=True, group="action")
    def _do_cancel_all_orders(self) -> None:
        count = hr.cancel_all_orders()
        self.call_from_thread(self._log, t("log.cancel_all", n=count))
        self.call_from_thread(self.refresh_data)

    # ── read-only dry-run ──────────────────────────────────────────────────--
    def action_dryrun(self) -> None:
        self._do_dryrun()

    @work(thread=True, group="action")
    def _do_dryrun(self) -> None:
        try:
            cfg = im.load_config()
            ranked, spy = im.rank_universe(cfg)
            top = cfg["intraday"]["top_n"]
            lines = [t("dry.head", spy=f"{spy*100:+.2f}", top=top)]
            for i, (s, rs, px) in enumerate(ranked[:top], 1):
                lines.append(f"  {i}. {s:<5} {rs*100:+.2f}%  ${px:.2f}")
        except Exception as e:
            lines = [t("log.dry_err", e=e)]
        self.call_from_thread(self._log, "\n".join(lines))

    # ── push full report to Telegram (safe — no arm; sends a report, not a trade)
    def action_push_report(self) -> None:
        self._log(t("log.report_building"))
        self._do_push_report()

    @work(thread=True, exclusive=True, group="report")
    def _do_push_report(self) -> None:
        try:
            res = hr.run_report(
                push=True,
                log=lambda m: self.call_from_thread(self._log, f"[dim]{m}[/]"))
            if res.get("sent"):
                self.call_from_thread(self._log, t("log.report_ok"))
                self._notify("📑 TUI pushed the full portfolio report")
            else:
                self.call_from_thread(self._log, t("log.report_not_sent"))
        except Exception as e:
            self.call_from_thread(self._log, t("log.report_err", e=e))

    # ── account-mutating actions (arm + confirm) ───────────────────────────--
    def action_flatten(self) -> None:
        if not self._require_armed():
            return
        self.push_screen(
            ConfirmModal(t("confirm.flatten")),
            lambda ok: self._do_flatten() if ok else None,
        )

    @work(thread=True, group="action")
    def _do_flatten(self) -> None:
        try:
            st = im.load_state()
            n = im.flatten(st, dry_run=False, reason="manual flatten (TUI)")
            im.save_state(st)
            self.call_from_thread(self._log, t("log.flatten_sent", n=n))
            self._notify(f"🛑 TUI FLATTEN — submitted close on {n} positions{self._mkt_suffix()}")
        except Exception as e:
            self.call_from_thread(self._log, t("log.flatten_err", e=e))
            self._notify(f"⚠️ TUI flatten error: {e}")
        self.call_from_thread(self.refresh_data)

    def action_tick(self) -> None:
        if not self._require_armed():
            return
        self.push_screen(
            ConfirmModal(t("confirm.tick")),
            lambda ok: self._do_tick() if ok else None,
        )

    @work(thread=True, group="action")
    def _do_tick(self) -> None:
        try:
            im.run_tick(im.load_config(), dry_run=False)
            self.call_from_thread(self._log, t("log.tick_done"))
            self._notify(f"⚡ TUI intraday tick complete{self._mkt_suffix()}")
        except Exception as e:
            self.call_from_thread(self._log, t("log.tick_err", e=e))
            self._notify(f"⚠️ TUI tick error: {e}")
        self.call_from_thread(self.refresh_data)

    def action_capitol(self) -> None:
        if not self._require_armed():
            return
        self.push_screen(
            ConfirmModal(t("confirm.capitol")),
            lambda ok: self._do_capitol() if ok else None,
        )

    @work(thread=True, group="action")
    def _do_capitol(self) -> None:
        try:
            cc.run(dry_run=False)
            self.call_from_thread(self._log, t("log.capitol_done"))
            self._notify(f"🏛️ TUI Capitol Copier run complete{self._mkt_suffix()}")
        except Exception as e:
            self.call_from_thread(self._log, t("log.capitol_err", e=e))
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
                ConfirmModal(t("confirm.buy", sym=sym, amt=f"{amt:,.0f}")),
                lambda ok: self._do_order(sym, "buy", amt) if ok else None,
            )
        self.push_screen(BuyModal(self._cash), after_modal)

    def action_sell(self) -> None:
        if not self._require_armed():
            return
        t = self.query_one("#holdings", DataTable)
        row = t.cursor_row
        if row is None or row < 0 or row >= len(self._syms):
            self._log(i18n.t("log.no_pos_sel"))
            return
        sym = self._syms[row]
        pv = self._mv.get(sym, 0.0)

        def after_modal(amt):
            if not amt:
                return
            if amt in ("all", "max"):
                self.push_screen(
                    ConfirmModal(i18n.t("confirm.sell_all", sym=sym)),
                    lambda ok: self._do_order(sym, "sell") if ok else None,
                )
            else:
                d = min(_amt_of(amt, pv), pv)
                if d <= 0:
                    self._log(i18n.t("log.nothing_sell"))
                    return
                self.push_screen(
                    ConfirmModal(i18n.t("confirm.sell_amt", amt=f"{d:,.0f}", sym=sym)),
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
                    self.call_from_thread(self._log, t("log.no_pos", sym=sym))
                    return
                res = cc.place_market_order(sym, "sell", qty=qty)
            oid = res.get("id") or res.get("message") or "?"
            self.call_from_thread(
                self._log,
                t("log.order_sent", side=side.upper(), sym=sym, id=str(oid)[:14]))
            amt = f" ${notional:,.0f}" if notional else ""
            self._notify(f"🟢 TUI {side.upper()} {sym}{amt} — submitted{self._mkt_suffix()}")
        except Exception as e:
            self.call_from_thread(self._log, t("log.order_err", e=e))
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
                self.call_from_thread(self._log, t("log.reb_both"))
                return
            if deploy > avail:
                self.call_from_thread(
                    self._log,
                    t("log.reb_cap", amt=f"{deploy:,.0f}", cash=f"{avail:,.0f}"))
                deploy = avail
            if len(longs) <= n and deploy <= 0 and withdraw <= 0:
                self.call_from_thread(
                    self._log,
                    t("log.reb_nothing", n=len(longs), top=n))
                return
            keep, sell, buys, trims, freed, skipped = rb.build_plan(
                positions, n, "plpc", 0.10, deploy_cash=deploy, withdraw_cash=withdraw)
        except Exception as e:
            self.call_from_thread(self._log, t("log.reb_plan_err", e=e))
            return
        if trims:
            raised = sum(t["notional"] for t in trims)
            summary = t("reb.sum_trim", sells=len(sell), trims=len(trims),
                        raised=f"{raised:,.0f}")
        else:
            cash_bit = t("reb.cashbit", amt=f"{deploy:,.0f}") if deploy > 0 else ""
            summary = t("reb.sum_deploy", sells=len(sell),
                        total=f"{freed + deploy:,.0f}", freed=f"{freed:,.0f}",
                        cashbit=cash_bit, buys=len(buys))
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
            self.call_from_thread(self._log, t("log.reb_err", e=e))
            self._notify(f"⚠️ TUI rebalance error: {e}")
        self.call_from_thread(self.refresh_data)


if __name__ == "__main__":
    AlpacaTUI().run()
