"""
config_fields.py — declarative registry of strategy settings editable from the TUI.

This whitelist IS the configuration feature list: the TUI renders exactly these
fields, validates raw input against their bounds, and refuses to touch anything
not listed here. Deliberately EXCLUDED (see TRADING_UPGRADE_REPORT.md):

  - intraday.* (incl. intraday.enabled) — enabling it makes the TUI 't' key
    place 4x-leveraged orders and its EOD flatten would liquidate the entire
    swing book. Hand-edit strategy_config.json only, with eyes open.
  - take_profit_levels / pyramid_levels — nested lists, read-only in v1.
  - telegram / report_schedule — already editable via the TUI 'm' / 'g' keys.

Pure module: no Textual imports, unit-testable. Writes go through
config_io.update_config (atomic) — this module only describes and validates.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sectors import TICKER_SECTOR

KNOWN_SECTORS = sorted(set(TICKER_SECTOR.values()))

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
# A ticker: letter first, then letters/digits/'.'/'-' (BRK.B, BF-B), ≤10 chars.
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

# What the allow-list row reads whenever the anchor fence is OFF. English source
# word (like every label here); the TUI maps it through i18n for zh.
UNRESTRICTED = "unrestricted"

# danger kinds drive the TUI's consequence previews:
#   master       — engine on/off switch (goes live/off at next scheduler tick)
#   prune        — enabling sells every off-sector position next tick
#   max_holdings — lowering below current count cap-tail sells next tick
#   exposure     — >= 1.0 means margin territory
DANGER_NONE = ""


@dataclass(frozen=True)
class Field:
    section: str
    path: str            # dotted path into strategy_config.json
    label: str
    ftype: str           # int | float | bool | time | csv_sectors | csv_tickers | csv_tickers_opt | choice
    lo: float | None = None
    hi: float | None = None
    danger: str = DANGER_NONE
    desc: str = ""
    choices: tuple = ()  # for ftype "choice": the allowed words


FIELDS: list[Field] = [
    # ── MASTER SWITCHES ──────────────────────────────────────────────────────
    Field("MASTER", "swing.enabled", "Swing buyer ON/OFF", "bool",
          danger="master",
          desc="Daily RS-momentum buys + dip-adds. Scheduler picks this up next tick."),
    Field("MASTER", "capitol_copier.exits_on", "Exit engine ON/OFF", "bool",
          danger="master",
          desc="Stops, trailing stops, take-profits and pyramids. Keep this ON — it is "
               "what enforces dynamic_exits.stop_loss_pct."),
    Field("MASTER", "capitol_copier.copy_on", "Disclosure copying ON/OFF", "bool",
          danger="master",
          desc="Daily politician disclosure copy loop. Independent of the exit engine."),

    # ── RISK RAILS ───────────────────────────────────────────────────────────
    Field("RISK", "pool.max_total_exposure_pct", "Max exposure (frac of equity)", "float",
          0.30, 1.00, danger="exposure",
          desc="Hard cap on invested market value. 1.00 = fully invested (margin edge)."),
    Field("RISK", "swing.min_cash_reserve_usd", "Cash reserve floor $", "int", 0, 50_000,
          desc="Swing buyer never spends below this cash cushion."),
    Field("RISK", "pool.max_position_usd", "Per-name cap $", "int", 500, 25_000,
          desc="No single position may exceed this market value via buys/adds."),
    Field("RISK", "pool.min_position_usd", "Min order size $", "int", 50, 5_000,
          desc="Orders smaller than this are skipped."),
    Field("RISK", "risk.max_position_pct", "Per-name cap (frac of equity)", "float",
          0.05, 0.25, danger="exposure",
          desc="Hard concentration limit enforced in place_market_order() for EVERY "
               "engine AND manual buys. Wallets with a code ceiling (High Risk: 25%) "
               "cannot be raised above it here — the ceiling wins."),
    Field("RISK", "risk.max_gross_exposure", "Max gross exposure (x equity)", "float",
          0.50, 2.00, danger="exposure",
          desc="Total long market value across the whole book, all engines. Code "
               "ceiling 2.00, or 1.00 (no borrowing) for High Risk. Stops many "
               "mid-sized positions adding up to the leverage that a per-name cap "
               "alone cannot see."),
    Field("RISK", "risk.gross_by_regime.bear", "Gross cap in a BEAR market", "float",
          0.00, 2.00, danger="exposure",
          desc="Leverage allowed when SPY is below its 200-day average. Deleverages "
               "on the way into a downturn instead of after it."),
    Field("RISK", "risk.gross_by_regime.neutral", "Gross cap, NEUTRAL market", "float",
          0.00, 2.00, danger="exposure",
          desc="SPY above its 200-day but below its 50-day."),
    Field("RISK", "risk.hedge_by_regime.bear", "Hedge size in a BEAR market", "float",
          0.00, 0.50,
          desc="Share of equity held in the inverse ETF when SPY is below its "
               "200-day average. 0 in a bull market — a permanent hedge is a drag."),
    Field("ISR", "isr_alpha.regime_gate", "ISR: no new buys in a bear", "bool",
          desc="Blocks NEW positions when SPY is below its 200-day average. "
               "Trimming and exiting continue regardless."),

    # ── ISR ALPHA ────────────────────────────────────────────────────────────
    # None of these were on the dashboard before, which is why max_turnover_pct
    # sat at 0.80 unnoticed while the engine churned the book all day.
    Field("ISR", "isr_alpha.enabled", "ISR Alpha ON/OFF", "bool", danger="master",
          desc="Catalyst/moat engine. Drives most of this wallet's trading."),
    Field("ISR", "isr_alpha.max_turnover_pct", "Max turnover per run (frac)", "float",
          0.05, 0.80, danger="exposure",
          desc="Share of equity ISR may trade in one rebalance. 0.80 caused the churn."),
    Field("ISR", "isr_alpha.min_drift_pct", "Min drift to act (frac)", "float", 0.05, 1.0,
          desc="Leave a holding alone until it is this far from target. Stops "
               "tiny ranking wobbles causing full round trips."),
    Field("ISR", "isr_alpha.exit_rank", "Exit rank (hysteresis)", "int", 5, 100,
          desc="Enter on max_positions, exit only when a name falls out of this "
               "rank. Prevents sell-and-rebuy at the rank boundary."),
    Field("ISR", "isr_alpha.max_positions", "Max positions", "int", 3, 40,
          desc="How many names ISR targets at once."),
    Field("ISR", "isr_alpha.base_position_usd", "Base position $", "int", 500, 25_000,
          desc="Starting size before score multipliers. Capped by risk.max_position_pct."),

    # ── ANCHOR PLAN (2026-08-30) ─────────────────────────────────────────────
    # Five names, first two hours only, enforced in entry_gate.py on every buy.
    Field("ANCHOR", "anchor.enabled", "Anchor fence ON/OFF", "bool", danger="master",
          desc="Only the anchor names may be bought, only inside the entry window. "
               "Off = every engine's old behaviour returns."),
    # The allow list itself. Peter trades this wallet by hand now (2026-09-22),
    # and the same fence gates his manual buys, so the list has to be editable
    # from here rather than by hand-editing JSON. Fence OFF reads 'unrestricted'.
    Field("ANCHOR", "anchor.universe", "Allowed stocks (allow list)", "csv_tickers",
          desc="While the fence is ON only these names may be bought — by every engine "
               "AND by manual buys from this dashboard. Fence OFF = unrestricted. "
               "Comma or space separated. An empty list is refused: it would block every buy."),
    # Core holds (2026-09-22): positions Peter wants left to compound. The
    # five-up-days rule and the take-profit tiers skip them; caps and the
    # trailing stop do NOT — protection is never optional, trimming is.
    Field("ANCHOR", "anchor.core_holds", "Core holds (kept whole)", "csv_tickers_opt",
          desc="Names the five-up-days rule and the take-profit tiers must leave alone. "
               "Toggle from the holdings table with C. Caps and the trailing stop still "
               "apply. Blank = none."),
    Field("ANCHOR", "anchor.max_entries_per_name_per_day", "Entries per name per day", "int", 1, 5,
          desc="Filled buys allowed per anchor name per session. Counted from Alpaca fills."),
    Field("ANCHOR", "anchor.max_entries_per_day", "Entries per day, all names (0=off)", "int", 0, 20,
          desc="Total filled buys per session across the anchors. 0 disables the total cap."),
    Field("ANCHOR", "anchor.position_pct", "Anchor position size (frac of equity)", "float", 0.05, 0.25,
          danger="exposure",
          desc="anchor_trade.py sizes each name to this share of equity. Cannot exceed risk.max_position_pct."),
    Field("ANCHOR", "anchor.max_stop_pct", "Max initial stop (frac)", "float", 0.01, 0.05,
          desc="anchor_trade.py clamps WB's stop so it is never further than this below entry."),
    Field("ANCHOR", "anchor.consecutive_loss_halt", "Halt after N losses in a row", "int", 1, 5,
          desc="Closed anchor losers in a row today that stop new entries for the session."),
    Field("ANCHOR", "anchor.daily_loss_limit_pct", "Daily loss halt (%)", "float", 1.0, 5.0,
          desc="Equity this far below last close = no new buys today. Rule 4 in SOUL.md."),

    # ── EXIT ENGINE ──────────────────────────────────────────────────────────
    Field("EXITS", "dynamic_exits.stop_loss_pct", "Stop-loss (frac)", "float", 0.02, 0.25,
          desc="Sell all when unrealized loss reaches this (0.08 = -8%)."),
    Field("EXITS", "dynamic_exits.stop_atr_mult", "Broker stop width: ATR multiple", "float", 1.0, 5.0,
          desc="Resting stop width per name = this x its 14-day average daily range, "
               "kept between the floor and ceiling below. Boxed wallets only."),
    Field("EXITS", "dynamic_exits.stop_min_pct", "Broker stop width: floor (frac)", "float", 0.02, 0.20,
          desc="Narrowest resting stop allowed (0.06 = 6% below the high-water mark)."),
    Field("EXITS", "dynamic_exits.stop_max_pct", "Broker stop width: ceiling (frac)", "float", 0.05, 0.30,
          desc="Widest resting stop allowed; also the width used when no bars are available."),
    Field("EXITS", "dynamic_exits.trail_trigger_pct", "Trail trigger (frac)", "float", 0.01, 1.0,
          desc="Trailing stop activates once peak gain reaches this."),
    Field("EXITS", "dynamic_exits.trail_giveback_pct", "Trail giveback (frac)", "float", 0.01, 1.0,
          desc="After trigger, sell if price falls this far off the peak."),
    Field("EXITS", "dynamic_exits.pyramid_add_frac", "Pyramid add size (frac of orig)", "float",
          0.0, 1.0,
          desc="Each pyramid tier adds this fraction of the original position size."),
    Field("EXITS", "dynamic_exits.max_holdings", "Max holdings (exit engine)", "int", 5, 40,
          danger="max_holdings",
          desc="Cap-tail: exceeding this sells the smallest positions next tick."),
    Field("EXITS", "dynamic_exits.prune_off_target", "Prune off-sector holdings", "bool",
          danger="prune",
          desc="DANGER: enabling sells EVERY position outside target_sectors next tick."),

    # ── SWING BUYER ──────────────────────────────────────────────────────────
    Field("SWING", "swing.entry_size_usd", "New entry size $", "int", 500, 10_000,
          desc="Notional per new RS-momentum entry."),
    Field("SWING", "swing.max_new_positions_per_run", "Max new entries / day", "int", 0, 5,
          desc="0 pauses new entries while keeping dip-adds."),
    Field("SWING", "swing.rs_lookback_days", "RS lookback (days)", "int", 5, 60,
          desc="Relative-strength ranking window vs SPY."),
    Field("SWING", "swing.regime_sma_days", "Regime SMA (days)", "int", 20, 200,
          desc="No new buys while SPY closes below this moving average."),
    Field("SWING", "swing.dip_add_usd", "Dip-add size $", "int", 0, 5_000,
          desc="Notional added to a proven winner on a pullback. 0 disables dip-adds."),
    Field("SWING", "swing.dip_min_peak_gain", "Dip: min peak gain (frac)", "float", 0.03, 0.30,
          desc="Position must have been up this much at its peak to qualify."),
    Field("SWING", "swing.dip_trigger_off_peak", "Dip: pullback off peak (frac)", "float",
          0.02, 0.15,
          desc="...and pulled back at least this far off that peak (while above entry)."),
    Field("SWING", "swing.dip_cooldown_days", "Dip cooldown (days)", "int", 1, 30,
          desc="At most one dip-add per name per this many days."),
    Field("SWING", "swing.stop_cooldown_days", "Stop re-buy cooldown (days)", "int", 0, 30,
          desc="Never rebuy a name the exit engine stopped out within this window."),
    Field("SWING", "swing.max_holdings", "Max holdings (swing buyer)", "int", 5, 40,
          desc="Swing buyer opens no new names beyond this count."),

    # ── CAPITOL COPIER ───────────────────────────────────────────────────────
    Field("COPIER", "pool.daily_budget_usd", "Daily copy budget $", "int", 500, 5_000,
          desc="Base budget split by pool weights when copying disclosures."),
    Field("COPIER", "pool.consensus_boost_multiplier", "Consensus boost x", "float", 1.0, 5.0,
          desc="Size multiplier when 2+ pool members buy the same ticker in 14d."),
    Field("COPIER", "capitol_copier.max_disclosure_lag_days", "Max disclosure age (days)", "int",
          3, 45,
          desc="Skip disclosures older than this — stale info has no edge."),
    Field("COPIER", "capitol_copier.sentiment_veto_enabled", "Sentiment veto", "bool",
          desc="If on, a 1/5 bearish LLM sentiment blocks the buy (off = only scales size)."),
    Field("COPIER", "capitol_copier.target_sectors", "Target sectors (csv)", "csv_sectors",
          desc="Whitelist for NEW copy buys. Known: " + ", ".join(KNOWN_SECTORS)),

    # ── PRICE WATCHER (High Risk guardrails, 2026-09-22) ─────────────────────
    Field("WATCHER", "price_watcher.buyback_mode", "Buy-back after a stop-out", "choice",
          choices=("notify", "trade", "off"),
          desc="When a stopped-out name falls 20% below its old entry: notify = Telegram "
               "once a day, no order (default); trade = the old automatic buy, through every "
               "gate; off = nothing."),
    Field("ANCHOR", "anchor.earnings_warn_days", "Earnings warning lead (sessions)", "int", 0, 5,
          desc="Morning report and evening check flag any held name reporting within this "
               "many sessions. An earnings gap jumps straight through a stop. 0 = off."),
    Field("ANCHOR", "anchor.cooling_off_pct", "Cooling-off trigger (% below 20-day high)", "float", 3.0, 15.0,
          desc="Equity this far below its 20-day high pauses NEW buys (sells and stops keep "
               "working). Re-arms only after a new high is made. 0 = off."),
    Field("ANCHOR", "anchor.cooling_off_days", "Cooling-off length (sessions)", "int", 1, 20,
          desc="How many trading days the pause lasts once triggered."),
    Field("ANCHOR", "anchor.max_open_names", "Max names held at once", "int", 1, 15,
          desc="A buy of a NEW name is refused when this many are already held. The allow "
               "list is a menu, not a portfolio. 0 = off."),
    Field("ANCHOR", "anchor.min_cash_pct", "Cash floor (frac of equity)", "float", 0.0, 0.5,
          desc="A buy that would take cash below this share of equity is refused. 0 = off."),

    # ── SCHEDULER ────────────────────────────────────────────────────────────
    Field("SCHED", "trading_schedule.manage_every_minutes", "Exit engine cadence (min)", "int",
          5, 120,
          desc="How often stops/trails/TPs are checked during market hours."),
    Field("SCHED", "trading_schedule.swing_time_et", "Swing buy time (ET)", "time",
          desc="Daily swing-buyer window start, HH:MM 24h ET."),
    Field("SCHED", "trading_schedule.copy_time_et", "Copy loop time (ET)", "time",
          desc="Daily disclosure-copy window start, HH:MM 24h ET."),
    Field("SCHED", "trading_schedule.window_minutes", "Daily window width (min)", "int", 10, 60,
          desc="Width of the swing/copy fire windows."),
    Field("SCHED", "trading_schedule.weekdays_only", "Weekdays only", "bool",
          desc="Skip Saturday/Sunday ticks entirely."),
]

_BY_PATH = {f.path: f for f in FIELDS}


def by_path(path: str) -> Field | None:
    return _BY_PATH.get(path)


# ── dotted-path helpers ──────────────────────────────────────────────────────

def get_path(cfg: dict, path: str):
    node = cfg
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def set_path(cfg: dict, path: str, value) -> dict:
    parts = path.split(".")
    node = cfg
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value
    return cfg


# ── display + validation ─────────────────────────────────────────────────────

def fmt_value(field: Field, value, cfg: dict | None = None,
              compact: bool = False) -> str:
    """Display text for a value.

    `cfg` (the whole wallet strategy) and `compact` matter only for the allow
    list: its meaning depends on a *sibling* setting — with the fence OFF the
    list is irrelevant and reads UNRESTRICTED — and fifteen tickers do not fit
    a 96-column table row, so the row shows a count plus the first few while
    the edit dialog (cfg=None, compact=False) shows the full editable list.
    """
    if field.ftype == "csv_tickers_opt":
        names = ([str(v).upper() for v in value] if isinstance(value, list)
                 else ([str(value).upper()] if value else []))
        if not names:
            return "none" if cfg is not None else ""   # table says none; edit box is blank
        return " ".join(names) if compact else ", ".join(names)
    if field.ftype == "csv_tickers":
        names = ([str(v).upper() for v in value] if isinstance(value, list)
                 else ([str(value).upper()] if value else []))
        if cfg is not None and not get_path(cfg, "anchor.enabled"):
            return UNRESTRICTED
        if not names:
            # Fence ON with nothing allowed = every buy blocked. Say so loudly
            # in the table; in the edit box just leave it empty to type into.
            return "⚠ empty — every buy blocked" if cfg is not None else ""
        if not compact:
            return ", ".join(names)
        shown = names if len(names) <= 6 else names[:5]
        tail = "" if len(names) <= 6 else " …"
        return f"{len(names)} names: {' '.join(shown)}{tail}"
    if value is None:
        return "—"
    if field.ftype == "bool":
        return "ON" if value else "off"
    if field.ftype == "csv_sectors":
        return ",".join(value) if isinstance(value, list) else str(value)
    if field.ftype == "float":
        return f"{float(value):g}"
    return str(value)


def fmt_range(field: Field) -> str:
    if field.ftype == "bool":
        return "on/off"
    if field.ftype == "time":
        return "HH:MM"
    if field.ftype == "csv_sectors":
        return "csv"
    if field.ftype == "csv_tickers":
        return "tickers"
    if field.ftype == "csv_tickers_opt":
        return "tickers or blank"
    if field.ftype == "choice":
        return "/".join(field.choices)
    if field.lo is not None and field.hi is not None:
        return f"{field.lo:g}–{field.hi:g}"
    return ""


def validate(field: Field, raw: str) -> tuple[bool, object]:
    """(True, parsed_value) or (False, error_message). Never raises."""
    raw = str(raw).strip()

    if field.ftype == "bool":
        low = raw.lower()
        if low in ("y", "yes", "true", "on", "1"):
            return True, True
        if low in ("n", "no", "false", "off", "0"):
            return True, False
        return False, "enter on/off (or yes/no, true/false, 1/0)"

    if field.ftype == "time":
        if _TIME_RE.match(raw):
            return True, raw
        return False, "time must be HH:MM (24h)"

    if field.ftype == "choice":
        low = raw.lower()
        if low in field.choices:
            return True, low
        return False, "one of: " + ", ".join(field.choices)

    if field.ftype == "csv_sectors":
        parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
        if not parts:
            return False, "need at least one sector"
        bad = [p for p in parts if p not in KNOWN_SECTORS]
        if bad:
            return False, f"unknown sector(s): {', '.join(bad)} — known: {', '.join(KNOWN_SECTORS)}"
        return True, parts

    if field.ftype in ("csv_tickers", "csv_tickers_opt"):
        parts = [p.strip().upper() for p in re.split(r"[,\s;]+", raw) if p.strip()]
        if not parts:
            if field.ftype == "csv_tickers_opt":
                return True, []
            return False, ("enter at least one ticker — to lift all restrictions, turn "
                           "the Anchor fence OFF instead (the row then reads "
                           f"'{UNRESTRICTED}')")
        bad = [p for p in parts if not _TICKER_RE.match(p)]
        if bad:
            return False, (f"not a valid ticker: {', '.join(bad)} — letters/digits "
                           "only, e.g. AAPL, BRK.B")
        seen: list[str] = []
        for p in parts:            # dedupe, keep the order typed
            if p not in seen:
                seen.append(p)
        return True, seen

    # numeric
    try:
        val = int(raw) if field.ftype == "int" else float(raw)
    except ValueError:
        return False, f"must be a{'n integer' if field.ftype == 'int' else ' number'}"
    if field.lo is not None and val < field.lo:
        return False, f"minimum is {field.lo:g}"
    if field.hi is not None and val > field.hi:
        return False, f"maximum is {field.hi:g}"
    return True, val


def enable_blocked_reason(cfg: dict, field: Field, new_val) -> str | None:
    """A change that must be refused outright — no confirm, no save — or None.

    The fence blocks every buy whose name is not on the list, so turning it ON
    while the list is empty blocks ALL buying, manual buys included. Nobody
    ever means that. The sanctioned way to lift restrictions is fence OFF,
    which the table shows as UNRESTRICTED. (An empty list can't be *saved*
    either — validate() refuses it — so this is the one remaining gap.)
    """
    if field.path == "anchor.enabled" and new_val:
        if not (get_path(cfg, "anchor.universe") or []):
            return ("cannot turn the fence ON with an empty allow list — every buy "
                    "would be blocked; add names to 'Allowed stocks' first")
    # The stop-width floor and ceiling must stay in order, or every width
    # collapses to one number and the ATR sizing means nothing.
    if field.path == "dynamic_exits.stop_min_pct":
        hi = get_path(cfg, "dynamic_exits.stop_max_pct")
        if hi is not None and float(new_val) > float(hi):
            return f"floor {float(new_val):g} is above the ceiling {float(hi):g} — raise the ceiling first"
    if field.path == "dynamic_exits.stop_max_pct":
        lo = get_path(cfg, "dynamic_exits.stop_min_pct")
        if lo is not None and float(new_val) < float(lo):
            return f"ceiling {float(new_val):g} is below the floor {float(lo):g} — lower the floor first"
    return None


def hard_limits_text(limits: dict | None, stops_on: bool, t=None) -> str | None:
    """The strategy screen's header line for a boxed wallet, or None for an
    open one. `limits` is capitol_copier.hard_limits_for(wallet); `t` is the
    translator (i18n.t) — injected so this stays testable without the TUI."""
    if not limits:
        return None
    if t is None:
        import i18n
        t = i18n.t
    return t("cfg.hard",
             pos=f"{float(limits.get('position_pct', 1.0)) * 100:.0f}",
             gross=f"{float(limits.get('gross', 2.0)):.1f}",
             stops=t("state.on") if stops_on else t("state.off"))
