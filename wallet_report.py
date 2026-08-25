#!/usr/bin/env python3
"""
wallet_report.py — multi-wallet Telegram report (leaderboard + per-wallet detail).

Replaces the single-wallet hermes_report push for scheduled/manual reporting now
that several paper wallets run side by side. Layout (phone-first, ≤36-char code
lines): header → 🏆 leaderboard ranked vs the $100k baseline → combined
all-wallets summary → one grounded analyst take over the whole picture → per
wallet: day P&L, top-3 movers, and TODAY'S buys & sells (fills).

Credential model: same as compare.py — every request carries its own headers
(wallets.resolve per wallet), never wallets.apply(), so building a report can
never cross-wire the TUI's active-wallet globals. Read-only endpoints only.

Language: strings resolve through i18n.t at build time. In-process (TUI) the
report follows the cockpit's current language; the CLI sets the language from
strategy_config.json's tui.language first.

Run manually:  venv/bin/python3 wallet_report.py [--no-push]
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import zoneinfo
from pathlib import Path
from typing import Any

import requests

import compare
import i18n
import wallets
import hermes_report as hr          # _split_for_telegram + REPORTS_DIR reuse
import telegram_notifier as tn

ET = zoneinfo.ZoneInfo("America/New_York")
_HERE = Path(__file__).resolve().parent

_NAME_W = 10                        # leaderboard wallet-name column width


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _k(v: float) -> str:
    """$101.8k-style compact money for the 36-char leaderboard."""
    return f"${v / 1000:.1f}k"


def _m(v: float) -> str:
    return f"${v:,.0f}"


def _s(v: float) -> str:
    return f"{v:+,.0f}"


def _p(v: float) -> str:
    return f"{v:+.2f}%"


def _qty(q: float) -> str:
    """Trim trailing zeros so whole-share fills read '5', fractional '0.37'."""
    return f"{q:g}"


# ── data ──────────────────────────────────────────────────────────────────────

def fetch_today_fills(key: str, secret: str, base: str) -> list[dict]:
    """Today's (ET) buy+sell fills for one wallet via /account/activities.
    Per-request headers — never touches active-wallet globals. Returns
    [{time_et, side, sym, qty, price}] oldest-first; [] on any failure (a
    fills hiccup degrades to 'no trades', it never kills the report)."""
    start_et = dt.datetime.now(ET).replace(hour=0, minute=0, second=0,
                                           microsecond=0)
    try:
        r = requests.get(
            f"{base}/account/activities",
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
                     "Accept": "application/json"},
            params={"activity_types": "FILL",
                    "after": start_et.isoformat()},
            timeout=10)
        r.raise_for_status()
        acts = r.json()
        if not isinstance(acts, list):
            return []
    except Exception:
        return []

    out = []
    for a in acts:
        try:
            ts = dt.datetime.fromisoformat(
                str(a.get("transaction_time", "")).replace("Z", "+00:00"))
            out.append({
                "time_et": ts.astimezone(ET).strftime("%H:%M"),
                "side": "S" if str(a.get("side", "")).startswith("sell") else "B",
                "sym": a.get("symbol", "?"),
                "qty": _f(a.get("qty") or a.get("cum_qty")),
                "price": _f(a.get("price")),
                "_ts": ts,
            })
        except Exception:
            continue
    out.sort(key=lambda x: x["_ts"])
    for o in out:
        o.pop("_ts", None)
    return out


def gather_report_data() -> tuple[list[dict], dict[str, list[dict]]]:
    """Every declared wallet's snapshot (compare.py, per-request creds) plus
    its today-fills. Unconfigured/broken wallets come back ok=False."""
    results: list[dict] = []
    fills: dict[str, list[dict]] = {}
    for info in wallets.list_wallets():
        name = info["name"]
        creds = wallets.resolve(name)
        if creds is None:
            results.append({"name": name, "ok": False,
                            "err": "not configured — set " +
                                   " / ".join(info["missing"]) + " in .env"})
            continue
        key, secret, base = creds
        r = compare.snapshot(name, key, secret, base, "1D")
        results.append(r)
        if r.get("ok"):
            fills[name] = fetch_today_fills(key, secret, base)
    return results, fills


def generate_analyst_commentary(results: list[dict], fills_by_wallet: dict[str, list[dict]]) -> str:
    """Generate analyst commentary based on wallet performance and trading activity."""
    ok = [r for r in results if r.get("ok")]
    if not ok:
        return "No valid wallet data available for analysis."
    
    # Overall performance
    tot_eq = sum(r["equity"] for r in ok)
    tot_day = sum(r["day_pl"] for r in ok)
    tot_base = sum(r.get("baseline_usd") or compare.ACCOUNT_BASELINE_USD for r in ok)
    tot_pl = tot_eq - tot_base
    
    # Find best and worst performers
    sorted_by_day = sorted(ok, key=lambda r: r["day_pl"], reverse=True)
    best_wallet = sorted_by_day[0]
    worst_wallet = sorted_by_day[-1]
    
    # Count trading activity
    total_trades = sum(len(fills_by_wallet.get(r["name"], [])) for r in ok)
    active_wallets = [r for r in ok if len(fills_by_wallet.get(r["name"], [])) > 0]
    
    # Build commentary
    commentary_parts = []
    
    # Overall assessment
    if tot_day >= 0:
        commentary_parts.append(f"The portfolio showed resilience today with a {_p(tot_day/(tot_eq-tot_day)*100 if tot_eq!=tot_day else 0.0)} day gain.")
    else:
        commentary_parts.append(f"Today's session was challenging, with the portfolio declining {_p(abs(tot_day/(tot_eq-tot_day)*100) if tot_eq!=tot_day else 0.0)}.")
    
    # Wallet performance insights
    if best_wallet["day_pl"] != worst_wallet["day_pl"]:
        commentary_parts.append(
            f"{best_wallet['name']} led performance ({_s(best_wallet['day_pl'])}), "
            f"while {worst_wallet['name']} lagged ({_s(worst_wallet['day_pl'])})."
        )
    
    # Trading activity insights
    if total_trades == 0:
        commentary_parts.append("No trading activity occurred today — all moves were purely mark-to-market.")
    elif len(active_wallets) == 1:
        commentary_parts.append(
            f"Only {active_wallets[0]['name']} was active today with {total_trades} trades, "
            "suggesting concentrated conviction or rebalancing in a single strategy."
        )
    else:
        commentary_parts.append(
            f"Trading was distributed across {len(active_wallets)} wallets with {total_trades} total fills, "
            "indicating broader market engagement."
        )
    
    # Risk observations
    max_weight = max((abs(r["day_pl"]) for r in ok), default=0)
    if max_weight > abs(tot_day) * 0.5 and len(ok) > 1:
        commentary_parts.append(
            "Performance was driven disproportionately by one wallet, "
            "highlighting concentration risk in the overall strategy."
        )
    
    # Specific observations from fills
    for wallet_name, wallet_fills in fills_by_wallet.items():
        if wallet_fills:
            buys = [f for f in wallet_fills if f["side"] == "B"]
            sells = [f for f in wallet_fills if f["side"] == "S"]
            if buys and sells:
                commentary_parts.append(
                    f"{wallet_name} showed active rotation "
                    f"with {len(buys)} buys and {len(sells)} sells — suggesting tactical repositioning "
                    f"rather than directional bets."
                )
                break  # Just mention one example to keep commentary concise
    
    # Closing thought
    if tot_day < 0 and len([r for r in ok if r["day_pl"] < 0]) == len(ok):
        commentary_parts.append("Despite today's headwinds, the diversified wallet approach helps mitigate single-strategy risk.")
    elif tot_day > 0:
        commentary_parts.append("Positive performance across multiple strategies validates the diversification approach.")
    
    return " ".join(commentary_parts)


# ── build ─────────────────────────────────────────────────────────────────────

def build_wallets_report(results: list[dict],
                         fills_by_wallet: dict[str, list[dict]],
                         analyst: str | None = None) -> str:
    """Markdown report in the current i18n language. Pass `analyst` to place
    the LLM take after the combined summary; omit it to build the body that
    feeds the LLM (same build-twice pattern as hermes_report)."""
    t = i18n.t
    now_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    active = wallets.current()
    ok = [r for r in results if r.get("ok")]
    broken = [r for r in results if not r.get("ok")]
    ranked = sorted(ok, key=lambda r: (r.get("win_ret") is None,
                                       -(r.get("win_ret") or 0.0)))

    L: list[str] = []
    L.append(t("rpt.title"))
    L.append(f"_{now_str}_")
    L.append("")

    # ── leaderboard — ranked vs the $100k paper baseline ───────────────────
    L.append(t("rpt.leaderboard"))
    L.append("```")
    L.append(f"{'#':<3}{'WALLET':<{_NAME_W}} {'EQUITY':>7} {'DAY%':>6} {'TOT%':>6}")
    L.append("─" * 35)
    for rank, r in enumerate(ranked, start=1):
        marker = "●" if r["name"] == active else " "
        wr = r.get("win_ret")
        L.append(f"{rank}{marker} {r['name'][:_NAME_W]:<{_NAME_W}}"
                 f"{_k(r['equity']):>8}"
                 f"{r['day_pct']:>+6.1f}"
                 f"{(wr if wr is not None else 0.0):>+6.1f}")
    L.append("```")

    # ── combined summary — the whole book in three lines ───────────────────
    if ok:
        tot_eq = sum(r["equity"] for r in ok)
        tot_day = sum(r["day_pl"] for r in ok)
        tot_base = sum(r.get("baseline_usd") or compare.ACCOUNT_BASELINE_USD
                       for r in ok)
        tot_pl = tot_eq - tot_base
        up = sum(1 for r in ok if r["day_pl"] >= 0)
        arrow = "🟢" if tot_day >= 0 else "🔴"
        L.append(t("rpt.combined", n=len(ok)))
        L.append(f"*{t('rpt.equity')} {_m(tot_eq)}*")
        L.append(f"{arrow} {t('rpt.day')} {_s(tot_day)}  "
                 f"({_p(tot_day / (tot_eq - tot_day) * 100 if tot_eq != tot_day else 0.0)})")
        L.append(f"{t('rpt.vs_base')} {_s(tot_pl)}  "
                 f"({_p(tot_pl / tot_base * 100 if tot_base else 0.0)})  ·  "
                 + t("rpt.up_down", up=up, down=len(ok) - up))
        L.append("")

    # ── analyst take (one call, whole book) ────────────────────────────────
    if analyst:
        L.append(f"💬 {analyst}")
        L.append("")

    # ── MY ANALYST COMMENTARY (Human stock analyst take) ─────────────────────
    my_commentary = generate_analyst_commentary(ok, fills_by_wallet)
    if my_commentary:
        L.append(f"📈 *Analyst Commentary:*")
        L.append(f"💬 {my_commentary}")
        L.append("")

    # ── per-wallet blocks ──────────────────────────────────────────────────
    for r in ranked:
        name = r["name"]
        arrow = "🟢" if r["day_pl"] >= 0 else "🔴"
        L.append(f"━━ *{name}* ━━")
        L.append(f"{arrow} {t('rpt.day')} {_s(r['day_pl'])} ({_p(r['day_pct'])})"
                 f"  ·  {t('rpt.equity')} {_m(r['equity'])}")

        movers = sorted(r.get("positions", []),
                        key=lambda p: abs(_f(p.get("unrealized_pl"))),
                        reverse=True)[:3]
        if movers:
            L.append("  ".join(
                f"{p.get('symbol', '?')} {_s(_f(p.get('unrealized_pl')))}"
                for p in movers))
        else:
            L.append(f"_{t('rpt.all_cash')}_")

        fills = fills_by_wallet.get(name, [])
        if fills:
            L.append(f"*{t('rpt.trades')}*")
            L.append("```")
            for x in fills:
                L.append(f"{x['time_et']} {x['side']} {x['sym']:<6}"
                         f"{_qty(x['qty']):>8} @{x['price']:,.2f}")
            L.append("```")
        else:
            L.append(f"_{t('rpt.no_trades')}_")
        L.append("")

    for r in broken:
        L.append(t("rpt.err", name=r["name"], err=r.get("err", "?")))
    if broken:
        L.append("")

    return "\n".join(L).rstrip() + "\n"


# ── entrypoint (TUI key 'p', TUI auto-push, scheduler, CLI) ─────────────────

def send_wallets_report(push: bool = True, channel: str | None = None,
                        log=print) -> dict:
    """Gather all wallets, build (analyst-enriched) report, optionally push.
    Returns {report, md_path, sent}. sent=False when every Telegram part
    failed to deliver (tn.send returns False silently when a channel's env
    vars are unset — surface that instead of pretending success)."""
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    md_path = hr.REPORTS_DIR / f"wallets_{stamp}.md"

    log("[1/3] Fetching all wallets…")
    results, fills = gather_report_data()
    n_ok = sum(1 for r in results if r.get("ok"))
    log(f"[2/3] {n_ok}/{len(results)} wallets ok — building report…")

    body = build_wallets_report(results, fills)

    analyst = None
    try:
        import analyst_llm
        summary = analyst_llm.summarize(body, lang=i18n.get_lang())
        if summary and not summary.startswith("[analyst error"):
            analyst = summary
            log(f"[analyst] summary added ({analyst_llm.MODEL})")
        elif summary:
            log(f"[analyst] {summary}")
        else:
            log("[analyst] Hermes CLI not reachable — skipping summary")
    except Exception as e:
        log(f"[analyst] skipped: {e}")

    report = (build_wallets_report(results, fills, analyst=analyst)
              if analyst else body)
    report += "\n" + i18n.t("rpt.footer")
    md_path.write_text(report, encoding="utf-8")
    log(f"✓ Report: {md_path}")

    sent = False
    if push:
        log("[3/3] Sending to Telegram…")
        parts = hr._split_for_telegram(report)
        sent_parts = 0
        for i_, part in enumerate(parts, 1):
            if tn.send(part, parse_mode="Markdown", channel=channel):
                sent_parts += 1
            else:
                log(f"[tg] send failed (part {i_}/{len(parts)}) — "
                    "check channel env vars")
        sent = sent_parts == len(parts) and sent_parts > 0
        log("✓ Done." if sent else "⚠ not (fully) delivered.")
    else:
        log("(push disabled — Telegram skipped)")

    return {"report": report, "md_path": md_path, "sent": sent}


def _cli_lang() -> None:
    """CLI runs outside the TUI — pick up the cockpit's language setting."""
    try:
        with open(_HERE / "strategy_config.json") as f:
            lang = (json.load(f).get("tui", {}) or {}).get("language", "en")
        i18n.set_lang(lang)
    except Exception:
        pass


if __name__ == "__main__":
    _cli_lang()
    res = send_wallets_report(push="--no-push" not in sys.argv)
    if "--no-push" in sys.argv:
        print("\n" + res["report"])
    sys.exit(0 if (res["sent"] or "--no-push" in sys.argv) else 1)