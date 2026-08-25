#!/usr/bin/env python3
"""
Generate daily trading report for High Risk wallet:
- 50% ground-truth from wallet_report.py output
- 50% LLM-generated analyst sentence using OpenAI (Codex) primary, Nemotron 120B fallback.
Also incorporates ISR 2026 repo signals and recent log performance.
"""
import os
import sys
import subprocess
import re
import json
from datetime import datetime

# ---------- Configuration ----------
LOG_DIR = '/Users/peter/GitHub/Alpaca_Paper_Trader/logs/daily_reports'
ISR_SIGNAL_FILE = '/Users/peter/GitHub/Alpaca_Paper_Trader/isr2026_signals.json'  # ticker -> score 0-1
WALLET_REPORT_SCRIPT = '/Users/peter/GitHub/Alpaca_Paper_Trader/wallet_report.py'
LOOKBACK_DAYS = 5
# ---------- End Configuration ----------

def get_wallet_report():
    """Run wallet_report.py --no-push and return output."""
    if not os.path.exists(WALLET_REPORT_SCRIPT):
        sys.exit(f"ERROR: {WALLET_REPORT_SCRIPT} not found")
    try:
        result = subprocess.run(
            ['python', WALLET_REPORT_SCRIPT, '--no-push'],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            sys.exit(f"ERROR: wallet_report.py failed: {result.stderr}")
        return result.stdout
    except Exception as e:
        sys.exit(f"ERROR: Failed to run wallet_report.py: {e}")

def parse_high_risk_section(report_text):
    """Extract High Risk section and return dict with day_pl, day_pct, equity, movers_line, trades_exist, full_section."""
    pattern = r'━━ \*High Risk\* ━━\n(.*?)\n(?=━━|\Z)'
    match = re.search(pattern, report_text, re.DOTALL)
    if not match:
        pattern = r'\*High Risk\*.*?\n(.*?)(?:\n━━|\Z)'
        match = re.search(pattern, report_text, re.DOTALL)
    if not match:
        return None
    section = match.group(1).strip()
    lines = section.split('\n')
    day_line = None
    for line in lines:
        if 'Day' in line and ('%' in line or 'Equity' in line):
            day_line = line.strip()
            break
    if not day_line:
        return None
    pnl_match = re.search(r'Day\s+([+-]?\d+(?:\.\d+)?)\s*\(([+-]?\d+(?:\.\d+)?)%\)', day_line)
    if not pnl_match:
        pnl_match = re.search(r'Day\s+([+-]?\d+(?:\.\d+)?)\s*\(([+-]?\d+(?:\.\d+)?)%\)', day_line.replace('🔴','').replace('🟢',''))
    if not pnl_match:
        return None
    day_pl = float(pnl_match.group(1))
    day_pct = float(pnl_match.group(2))
    equity_match = re.search(r'Equity\s+\$([\d,]+(?:\.\d+)?)', day_line)
    equity = float(equity_match.group(1).replace(',', '')) if equity_match else 0.0
    movers_line = None
    for line in lines:
        if any(sym in line for sym in ['GLW','CRDO','AAOI','IPGP','FOTO','EUV','LAZR']):
            movers_line = line.strip()
            break
    trades_exist = False
    if 'No trading activity occurred' in report_text or 'no trades' in report_text.lower():
        trades_exist = False
    else:
        if 'trading activity' in report_text.lower():
            idx = report_text.lower().find('trading activity')
            ahead = report_text.lower()[idx:idx+20]
            if 'no' not in ahead:
                trades_exist = True
    return {
        'day_pl': day_pl,
        'day_pct': day_pct,
        'equity': equity,
        'movers_line': movers_line,
        'trades_exist': trades_exist,
        'full_section': section
    }

def get_recent_stats(log_dir, lookback=LOOKBACK_DAYS):
    """Compute win-rate, avg P&L, profit factor from last lookback log files."""
    if not os.path.isdir(log_dir):
        return {'win_rate': None, 'avg_pnl': None, 'profit_factor': None}
    files = [f for f in os.listdir(log_dir) if f.endswith('.md')]
    files.sort()
    recent = files[-lookback:] if len(files) >= lookback else files
    pnls = []
    for f in recent:
        path = os.path.join(log_dir, f)
        try:
            with open(path, 'r') as fp:
                content = fp.read()
                m = re.search(r'Day’s P&L:\s*([+-]?\$?[\d,]+(?:\.\d+)?)\s*\(([+-]?\d+(?:\.\d+)?)%\)', content)
                if m:
                    amt_str = m.group(1).replace('$','').replace(',', '')
                    pct_str = m.group(2)
                    pnls.append((float(amt_str), float(pct_str)))
        except Exception:
            continue
    if not pnls:
        return {'win_rate': None, 'avg_pnl': None, 'profit_factor': None}
    gains = [p for p,_ in pnls if p > 0]
    losses = [abs(p) for p,_ in pnls if p < 0]
    win_rate = len(gains) / len(pnls) * 100 if pnls else 0
    avg_pnl = sum(p for p,_ in pnls) / len(pnls)
    profit_factor = sum(gains) / sum(losses) if losses else float('inf') if gains else 0.0
    return {
        'win_rate': round(win_rate, 1),
        'avg_pnl': round(avg_pnl, 2),
        'profit_factor': round(profit_factor, 2) if profit_factor != float('inf') else None
    }

def load_isr_signals():
    """Load ISR 2026 signals from JSON file; return dict ticker->score."""
    if not os.path.isfile(ISR_SIGNAL_FILE):
        return {}
    try:
        with open(ISR_SIGNAL_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {}

def get_isr_context(high_risk_data):
    """Generate a short ISR context string based on movers and signals."""
    signals = load_isr_signals()
    movers = high_risk_data.get('movers_line', '')
    if not movers:
        return "ISR 2026: no specific sector signal today."
    tickers = re.findall(r'[A-Z]{2,5}', movers)
    if not tickers:
        return "ISR 2026: no data for today's movers."
    max_ticker = None
    max_score = 0.0
    for t in tickers:
        score = signals.get(t, 0.0)
        if score > max_score:
            max_score = score
            max_ticker = t
    if max_ticker is None:
        return "ISR 2026: no data for today's movers."
    if max_score >= 0.7:
        strength = "strong"
    elif max_score >= 0.4:
        strength = "moderate"
    else:
        strength = "weak"
    return f"ISR 2026 shows {strength} institutional interest in {max_ticker} (score {max_score:.2f})."

def build_prompt(ground_truth, isr_context, recent_stats, yesterday_report):
    """Construct prompt for LLM."""
    prompt = f"""You are a senior equity analyst. Use only the information below to produce a ONE-SENTENCE market outlook for tomorrow.

GROUND TRUTH (facts from today):
{ground_truth}

ISR 2026 REPO SIGNAL:
{isr_context}

RECENT PERFORMANCE (last {LOOKBACK_DAYS} trading days):
Win-rate: {recent_stats['win_rate']}% | Avg P&L: ${recent_stats['avg_pnl']} | Profit-factor: {recent_stats['profit_factor']}

YESTERDAY’S REPORT:
{yesterday_report}

TASK:
Write ONE sentence that explains what the facts suggest for tomorrow’s market direction or sector move, keeping the tone analyst-like and under 20 words.
"""
    return prompt.strip()

def _analyst():
    """Import analyst_llm from the repo root (this script lives in scripts/)."""
    import importlib.util
    from pathlib import Path
    mod_path = Path(__file__).resolve().parent.parent / "analyst_llm.py"
    spec = importlib.util.spec_from_file_location("analyst_llm", mod_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def get_llm_sentence(prompt, recent_stats):
    """One terse outlook sentence via Hermes: Codex Terra, then NVIDIA Nemotron 120B.

    Routed through analyst_llm (see ../analyst_llm.py) so this script needs NO
    API key of its own — Codex is OAuth held by Hermes and the NVIDIA key lives
    in the Hermes profile. Replaces the old direct OPENAI_API_KEY /
    NEMOTRON_API_KEY HTTP calls, which were never configured and so always fell
    through to the deterministic line below.
    """
    try:
        a = _analyst()
        if a.is_configured():
            terse = (
                "You are a terse financial analyst. Answer in plain English, "
                "no markdown, ONE sentence, under 40 words.\n\n" + prompt
            )
            ok, out = a.ask(terse)      # walks analyst_llm.ROUTES in order
            if ok and out:
                return out.strip().split("\n")[0].strip()
    except Exception as e:
        print(f"[LLM] analyst_llm error: {e}", file=sys.stderr)

    # Deterministic fallback when Hermes is unreachable.
    avg_pnl = recent_stats['avg_pnl']
    if avg_pnl is not None and avg_pnl > 0:
        outlook = "suggests a modest upside bias tomorrow"
    else:
        outlook = "suggests caution for tomorrow"
    return f"Based on recent performance and ISR signals, {outlook}."

def main():
    # 1. Get wallet report
    report = get_wallet_report()
    # 2. Parse High Risk
    high_risk = parse_high_risk_section(report)
    if not high_risk:
        print("ERROR: Could not parse High Risk section")
        sys.exit(1)
    # 3. Build ground-truth half
    if high_risk['trades_exist'] and high_risk['movers_line']:
        tickers = re.findall(r'[A-Z]{2,5}', high_risk['movers_line'])
        if tickers:
            traded_desc = f"traded {', '.join(tickers[:3])} based on signal"
        else:
            traded_desc = "executed intraday trades"
    elif not high_risk['trades_exist']:
        traded_desc = "no trades executed"
    else:
        traded_desc = "managed existing positions"
    day_pl = high_risk['day_pl']
    day_pct = high_risk['day_pct']
    pnl_str = f"${abs(day_pl):,.0f}" if day_pl != 0 else "$0"
    pnl_sign = "+" if day_pl >= 0 else "-"
    pct_sign = "+" if day_pct >= 0 else "-"
    ground_lines = [
        f"Today I traded {traded_desc}.",
        f"Day’s P&L: {pnl_sign}{pnl_str} ({pct_sign}{abs(day_pct):.1f}%).",
        f"Signal worked/didn’t because: {'signal captured upside move' if day_pl >= 0 else 'mark-to-market pressure outweighed signal' if high_risk['trades_exist'] else 'no new signals; existing positions moved with market'}",
        f"Stop hit? {'No' if abs(day_pct) < 5.0 else 'Yes'}",
        f"Next session I will {'stay same and wait for clear signals' if abs(day_pct) <= 2.0 else 'review signal thresholds; stay same if edge remains'}"
    ]
    ground_half = ' '.join(ground_lines)
    # 4. Gather context for LLM
    isr_context = get_isr_context(high_risk)
    recent_stats = get_recent_stats(LOG_DIR)
    latest_path = os.path.join(LOG_DIR, 'latest.md')
    yesterday_report = "No prior report."
    if os.path.islink(latest_path) or os.path.exists(latest_path):
        try:
            with open(latest_path, 'r') as f:
                yesterday_report = f.read().strip()
                if not yesterday_report:
                    yesterday_report = "No prior report."
        except Exception:
            yesterday_report = "Unable to read prior report."
    # 5. Build prompt and get LLM sentence
    prompt = build_prompt(ground_half, isr_context, recent_stats, yesterday_report)
    llm_sentence = get_llm_sentence(prompt, recent_stats)
    # 6. Combine
    full_report = f"{ground_half} {llm_sentence}"
    # Trim to ~100 words if needed
    words = full_report.split()
    if len(words) > 100:
        full_report = ' '.join(words[:100]) + '...'
    # 7. Write to log
    os.makedirs(LOG_DIR, exist_ok=True)
    timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    log_file = os.path.join(LOG_DIR, f'{timestamp}.md')
    with open(log_file, 'w') as f:
        f.write(full_report + '\n')
    latest_link = os.path.join(LOG_DIR, 'latest.md')
    if os.path.islink(latest_link) or os.path.exists(latest_link):
        os.remove(latest_link)
    os.symlink(log_file, latest_link)
    # 8. Output for delivery
    print(full_report)

if __name__ == '__main__':
    main()