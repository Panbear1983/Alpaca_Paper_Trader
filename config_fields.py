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
    ftype: str           # int | float | bool | time | csv_sectors
    lo: float | None = None
    hi: float | None = None
    danger: str = DANGER_NONE
    desc: str = ""


FIELDS: list[Field] = [
    # ── MASTER SWITCHES ──────────────────────────────────────────────────────
    Field("MASTER", "swing.enabled", "Swing buyer ON/OFF", "bool",
          danger="master",
          desc="Daily RS-momentum buys + dip-adds. Scheduler picks this up next tick."),
    Field("MASTER", "capitol_copier.autorun_enabled", "Capitol autorun ON/OFF", "bool",
          danger="master",
          desc="Exit engine (20-min stops/trails/TPs/pyramids) + daily disclosure copies."),

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

    # ── EXIT ENGINE ──────────────────────────────────────────────────────────
    Field("EXITS", "dynamic_exits.stop_loss_pct", "Stop-loss (frac)", "float", 0.02, 0.25,
          desc="Sell all when unrealized loss reaches this (0.08 = -8%)."),
    Field("EXITS", "dynamic_exits.trail_trigger_pct", "Trail trigger (frac)", "float", 0.05, 0.50,
          desc="Trailing stop activates once peak gain reaches this."),
    Field("EXITS", "dynamic_exits.trail_giveback_pct", "Trail giveback (frac)", "float", 0.02, 0.25,
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

def fmt_value(field: Field, value) -> str:
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

    if field.ftype == "csv_sectors":
        parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
        if not parts:
            return False, "need at least one sector"
        bad = [p for p in parts if p not in KNOWN_SECTORS]
        if bad:
            return False, f"unknown sector(s): {', '.join(bad)} — known: {', '.join(KNOWN_SECTORS)}"
        return True, parts

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
