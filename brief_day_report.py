#!/usr/bin/env python3
"""
brief_day_report.py — brief daily trading activity and P&L report.
Generates a concise report showing today's trades and day P&L for each wallet.
"""

import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import requests

import i18n
import wallets
import hermes_report as hr  # for _split_for_telegram and REPORTS_DIR
import telegram_notifier as tn

ET = hr.ET  # reuse the ET timezone from hermes_report
_HERE = Path(__file__).resolve().parent

# ---- Helper functions (copied from wallet_report.py for independence) ----

def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default

def _m(v: float) -> str:
    return f"${v:,.0f}"

def _s(v: float) -> str:
    return f"{v:+,.0f}"

def _p(v: float) -> str:
    return f"{v:+.2f}%"

def _qty(q: float) -> str:
    """Trim trailing zeros so whole-share fills read '5', fractional '0.37'."""
    return f"{q:g}"

def fetch_today_fills(key: str, secret: str, base: str) -> List[Dict]:
    """Today's (ET) buy+sell fills for one wallet via /account/activities."""
    start_et = dt.datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0)
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

def gather_report_data() -> tuple[List[Dict], Dict[str, List[Dict]]]:
    """Every declared wallet's snapshot (compare.py, per-request creds) plus its today-fills."""
    results: List[Dict] = []
    fills: Dict[str, List[Dict]] = {}
    for info in wallets.list_wallets():
        name = info["name"]
        creds = wallets.resolve(name)
        if creds is None:
            results.append({"name": name, "ok": False,
                            "err": "not configured — set " +
                                   " / ".join(info["missing"]) + " in .env"})
            continue
        key, secret, base = creds
        # We don't have compare.snapshot here, so we'll fetch account directly for equity/day P&L.
        # But to keep it simple and consistent, let's reuse the compare.snapshot if available.
        # However, to avoid extra dependency, we'll do a lightweight fetch.
        # Alternatively, we can import compare and use it.
        try:
            import compare
            r = compare.snapshot(name, key, secret, base, "1D")
        except Exception as e:
            r = {"name": name, "ok": False, "err": f"compare failed: {e}"}
        results.append(r)
        if r.get("ok"):
            fills[name] = fetch_today_fills(key, secret, base)
    return results, fills

# ---- Report building ----

def build_brief_report(results: List[Dict], fills_by_wallet: Dict[str, List[Dict]]) -> str:
    """Build a brief report focused on today's trades and day P&L."""
    now_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    t = i18n.t
    ok = [r for r in results if r.get("ok")]
    broken = [r for r in results if not r.get("ok")]

    L: List[str] = []
    L.append(f"📊 *Brief Day Report*")
    L.append(f"_{now_str}_")
    L.append("")

    if ok:
        tot_eq = sum(r["equity"] for r in ok)
        tot_day = sum(r["day_pl"] for r in ok)
        tot_base = sum(r.get("baseline_usd") or compare.ACCOUNT_BASELINE_USD for r in ok)
        tot_pl = tot_eq - tot_base
        up = sum(1 for r in ok if r["day_pl"] >= 0)
        arrow = "🟢" if tot_day >= 0 else "🔴"
        L.append(f"*Combined*: Equity {_m(tot_eq)} · Day {_s(tot_day)} ({_p(tot_day/(tot_eq-tot_day)*100 if tot_eq!=tot_day else 0.0)})")
        L.append("")

        # Per wallet
        for r in ok:
            name = r["name"]
            equity = r["equity"]
            day_pl = r["day_pl"]
            day_pct = r["day_pct"]
            wallet_arrow = "🟢" if day_pl >= 0 else "🔴"
            L.append(f"*{name}*: {wallet_arrow} Equity {_m(equity)} · Day {_s(day_pl)} ({_p(day_pct)})")
            fills = fills_by_wallet.get(name, [])
            if fills:
                L.append("  Today's Trades:")
                for x in fills[:5]:  # limit to 5 trades per wallet for brevity
                    L.append(f"    {x['time_et']} {x['side']} {x['sym']:<6} {_qty(x['qty']):>8} @{x['price']:,.2f}")
                if len(fills) > 5:
                    L.append(f"    ... and {len(fills)-5} more")
            else:
                L.append("  _No trades today_")
            L.append("")
    else:
        L.append("_No wallets with valid configuration_")
        L.append("")

    if broken:
        L.append("*Errors*")
        for r in broken:
            L.append(f"  {r['name']}: {r.get('err', '?')}")
        L.append("")

    return "\n".join(L).rstrip() + "\n"

def send_brief_report(push: bool = True, channel: str | None = None, log=print) -> Dict:
    """Gather all wallets, build brief report, optionally push."""
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    md_path = hr.REPORTS_DIR / f"brief_day_{stamp}.md"

    log("[1/2] Fetching all wallets…")
    results, fills = gather_report_data()
    n_ok = sum(1 for r in results if r.get("ok"))
    log(f"[2/2] {n_ok}/{len(results)} wallets ok — building report…")

    report = build_brief_report(results, fills)
    md_path.write_text(report, encoding="utf-8")
    log(f"✓ Report: {md_path}")

    sent = False
    if push:
        log("[Telegram] Sending to Telegram…")
        parts = hr._split_for_telegram(report)
        sent_parts = 0
        for i_, part in enumerate(parts, 1):
            if tn.send(part, parse_mode="Markdown", channel=channel):
                sent_parts += 1
            else:
                log(f"[tg] send failed (part {i_}/{len(parts)}) — check channel env vars")
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
    res = send_brief_report(push="--no-push" not in sys.argv)
    if "--no-push" in sys.argv:
        print("\n" + res["report"])
    sys.exit(0 if (res["sent"] or "--no-push" in sys.argv) else 1)